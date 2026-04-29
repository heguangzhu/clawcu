from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from clawcu.core.provider_models import CanonicalModel, CanonicalProvider
from clawcu.llm import prompts, render_hermes, render_openclaw
from clawcu.llm.renderer import (
    LLMNotAvailableError,
    LLMParseError,
    LLMRendererError,
    _call_claude,
    _canonical_to_kwargs,
    _claude_path,
    _extract_json_block,
)


# ──────────────────────────────────────────────────────────
# _claude_path availability
# ──────────────────────────────────────────────────────────

def test_claude_path_raises_when_not_found() -> None:
    with patch("clawcu.llm.renderer.shutil.which", return_value=None):
        with pytest.raises(LLMNotAvailableError, match="claude"):
            _claude_path()


# ──────────────────────────────────────────────────────────
# _call_claude
# ──────────────────────────────────────────────────────────

def test_call_claude_success() -> None:
    fake_result = MagicMock()
    fake_result.stdout = '{"ok": true}\n'
    fake_result.stderr = ""
    fake_result.returncode = 0
    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            text = _call_claude("hello", "be concise")

    assert text == '{"ok": true}\n'
    mock_run.assert_called_once()
    args = mock_run.call_args.args[0]
    assert args[0] == "/usr/bin/claude"
    assert "-p" in args
    assert "hello" in args
    assert "--system-prompt" in args
    assert "be concise" in args
    assert "--no-session-persistence" in args


def test_call_claude_raises_on_nonzero_exit() -> None:
    from subprocess import CalledProcessError

    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch(
            "subprocess.run",
            side_effect=CalledProcessError(1, ["claude"], stderr="auth failed"),
        ):
            with pytest.raises(LLMRendererError, match="auth failed"):
                _call_claude("hello", "be concise")


# ──────────────────────────────────────────────────────────
# _extract_json_block
# ──────────────────────────────────────────────────────────

EXTRACT_CASES = [
    ('{"a": 1}', '{"a": 1}'),
    ("```json\n{\"a\": 1}\n```", '{"a": 1}'),
    ("Some text\n```json\n{\"a\": 1}\n```\nMore text", '{"a": 1}'),
    ("```\n{\"a\": 1}\n```", '{"a": 1}'),
    ("  \n  ", ""),
]


@pytest.mark.parametrize("raw,expected", EXTRACT_CASES)
def test_extract_json_block(raw: str, expected: str) -> None:
    assert _extract_json_block(raw) == expected


# ──────────────────────────────────────────────────────────
# _canonical_to_kwargs
# ──────────────────────────────────────────────────────────

def test_canonical_to_kwargs_basic() -> None:
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai-responses",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        api_key="sk-test",
        api_key_env_var="OPENAI_API_KEY",
        default_model_id="gpt-5",
        fallback_model_ids=["gpt-4.1"],
        models=[
            CanonicalModel(id="gpt-5", name="GPT-5", context_window=128000, max_tokens=4096),
            CanonicalModel(id="gpt-4.1", name="GPT-4.1"),
        ],
    )
    kwargs = _canonical_to_kwargs(canonical)
    assert kwargs["name"] == "openai"
    assert kwargs["api_style"] == "openai-responses"
    assert kwargs["base_url"] == "https://api.openai.com/v1"
    assert kwargs["auth_type"] == "api_key"
    assert kwargs["api_key_env_var"] == "OPENAI_API_KEY"
    assert kwargs["default_model_id"] == "gpt-5"
    assert kwargs["fallback_model_ids"] == "gpt-4.1"
    assert "  - id: gpt-5" in kwargs["models_yaml"]
    assert "    context_window: 128000" in kwargs["models_yaml"]
    assert "    max_tokens: 4096" in kwargs["models_yaml"]
    assert "  - id: gpt-4.1" in kwargs["models_yaml"]


def test_canonical_to_kwargs_empty_models() -> None:
    canonical = CanonicalProvider(
        name="x",
        api_style="openai",
        auth_type="api_key",
        models=[],
    )
    kwargs = _canonical_to_kwargs(canonical)
    assert kwargs["models_yaml"] == "  (none)\n"


# ──────────────────────────────────────────────────────────
# prompts.fill
# ──────────────────────────────────────────────────────────

def test_prompts_fill_replaces_placeholders() -> None:
    template = "Hello {{name}}, welcome to {{place}}."
    assert prompts.fill(template, name="Alice", place="Wonderland") == "Hello Alice, welcome to Wonderland."


def test_prompts_fill_leaves_unknown_placeholders() -> None:
    template = "Hello {{name}}, {{missing}}."
    assert prompts.fill(template, name="Alice") == "Hello Alice, {{missing}}."


# ──────────────────────────────────────────────────────────
# render_openclaw (mocked subprocess)
# ──────────────────────────────────────────────────────────

def test_render_openclaw_success() -> None:
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai-responses",
        auth_type="api_key",
        models=[CanonicalModel(id="gpt-5", name="GPT-5")],
    )

    fake_result = MagicMock()
    fake_result.stdout = '```json\n{"models_json": {}, "auth_profiles_json": {}, "openclaw_json": {}}\n```'
    fake_result.stderr = ""
    fake_result.returncode = 0

    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = render_openclaw(canonical, version_hint="2026.4.1")

    assert "models_json" in result
    assert "auth_profiles_json" in result
    assert "openclaw_json" in result
    args = mock_run.call_args.args[0]
    assert "-p" in args
    assert "openai" in mock_run.call_args.kwargs or "openai" in str(args)


def test_render_openclaw_missing_key_raises_parse_error() -> None:
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai-responses",
        auth_type="api_key",
        models=[],
    )

    fake_result = MagicMock()
    fake_result.stdout = '```json\n{"models_json": {}}\n```'
    fake_result.stderr = ""
    fake_result.returncode = 0

    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", return_value=fake_result):
            with pytest.raises(LLMParseError, match="auth_profiles_json"):
                render_openclaw(canonical)


def test_render_openclaw_invalid_json_raises_parse_error() -> None:
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai-responses",
        auth_type="api_key",
        models=[],
    )

    fake_result = MagicMock()
    fake_result.stdout = "not json"
    fake_result.stderr = ""
    fake_result.returncode = 0

    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", return_value=fake_result):
            with pytest.raises(LLMParseError, match="invalid JSON"):
                render_openclaw(canonical)


# ──────────────────────────────────────────────────────────
# render_hermes (mocked subprocess)
# ──────────────────────────────────────────────────────────

def test_render_hermes_success() -> None:
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai",
        auth_type="api_key",
        models=[CanonicalModel(id="gpt-5", name="GPT-5")],
    )

    fake_result = MagicMock()
    fake_result.stdout = (
        '```json\n'
        '{"config_yaml": "model:\\n  provider: openai", "env_key": "OPENAI_API_KEY", "env_value": "sk-test", "needs_auth_json": false}'
        '\n```'
    )
    fake_result.stderr = ""
    fake_result.returncode = 0

    with patch("clawcu.llm.renderer.shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", return_value=fake_result):
            result = render_hermes(canonical, version_hint="2026.4.1")

    assert result["config_yaml"] == "model:\n  provider: openai"
    assert result["env_key"] == "OPENAI_API_KEY"
    assert result["env_value"] == "sk-test"
    assert result["needs_auth_json"] is False


# ──────────────────────────────────────────────────────────
# Adapter dry-run + use_ai paths
# ──────────────────────────────────────────────────────────

def test_openclaw_adapter_write_canonical_ai_dry_run(temp_clawcu_home, tmp_path) -> None:
    from tests.support import make_service
    from clawcu.models import InstanceRecord

    service, _, _, store = make_service(temp_clawcu_home)
    target_datadir = tmp_path / "oc-target"
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(target_datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    adapter = service.adapters["openclaw"]
    canonical = CanonicalProvider(
        name="openai",
        api_style="openai-responses",
        auth_type="api_key",
        models=[CanonicalModel(id="gpt-5", name="GPT-5")],
    )
    record = store.load_record("writer")
    result = adapter.write_canonical(
        service, canonical, record, agent="chat", dry_run=True, use_ai=True,
    )
    assert result["ai"] == "planned"
    assert result["provider"] == "openai"
    assert result["agent"] == "chat"
    assert result["persist"] == "no"
    # No files should be written in dry_run
    assert not target_datadir.exists()


def test_hermes_adapter_write_canonical_ai_dry_run(temp_clawcu_home, tmp_path) -> None:
    from tests.support import make_service
    from clawcu.models import InstanceRecord

    service, _, _, store = make_service(temp_clawcu_home)
    target_datadir = tmp_path / "hm-target"
    store.save_record(
        InstanceRecord(
            service="hermes",
            name="scribe",
            version="2026.4.8",
            upstream_ref="v2026.4.8",
            image_tag="clawcu/hermes-agent:2026.4.8",
            container_name="clawcu-hermes-scribe",
            datadir=str(target_datadir),
            port=8642,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    adapter = service.adapters["hermes"]
    adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    canonical = CanonicalProvider(
        name="openrouter",
        api_style="openai",
        auth_type="api_key",
        models=[CanonicalModel(id="anthropic/claude-sonnet-4.5", name="Claude Sonnet 4.5")],
    )
    record = store.load_record("scribe")
    result = adapter.write_canonical(
        service, canonical, record, agent="main", dry_run=True, use_ai=True,
    )
    assert result["ai"] == "planned"
    assert result["provider"] == "openrouter"
    assert result["agent"] == "main"
    # No files should be written in dry_run
    assert not target_datadir.exists()


# ──────────────────────────────────────────────────────────
# Service plan_apply_provider with use_ai
# ──────────────────────────────────────────────────────────

def test_service_plan_apply_provider_with_ai_dry_run(temp_clawcu_home, tmp_path) -> None:
    from tests.support import make_service, write_provider_source, write_root_provider_source
    from clawcu.models import InstanceRecord

    service, _, _, store = make_service(temp_clawcu_home)
    source_root = tmp_path / "source"
    target_datadir = tmp_path / "oc-target"
    write_root_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    service.collect_providers(path=str(source_root))
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(target_datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )

    plan = service.plan_apply_provider("openai", "writer", "chat", use_ai=True)
    assert plan["provider"] == "openai"
    assert plan["ai"] == "planned"
    assert not target_datadir.exists()
