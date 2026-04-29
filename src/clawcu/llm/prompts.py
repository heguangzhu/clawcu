"""Prompt templates for LLM-driven provider config rendering.

Each template is a plain Python string with ``{{placeholder}}``
substitutions (no Jinja2 dependency).  The renderer fills them via
``str.replace`` before sending to the LLM.
"""
from __future__ import annotations


OPENCLAW_RENDER = """You are an expert OpenClaw configuration generator.

Given the canonical provider data below, produce the exact JSON that
OpenClaw expects under its three runtime files.

Target service: openclaw
Target version hint: {{version_hint}}

Canonical provider:
- name: {{name}}
- api_style: {{api_style}}
- base_url: {{base_url}}
- auth_type: {{auth_type}}
- api_key_env_var: {{api_key_env_var}}
- default_model_id: {{default_model_id}}
- fallback_model_ids: {{fallback_model_ids}}
- models:
{{models_yaml}}

Rules:
1. ``models_json`` → content for ``agents/<agent>/agent/models.json``.
   Place the provider under ``providers.<name>`` with ``api``, ``apiKey``
   (literal string, not env-ref), ``baseUrl``, ``headers``, and ``models``.
   Every model MUST include ``id``, ``name``, ``contextWindow`` (int),
   ``maxTokens`` (int), ``input`` (list), ``reasoning`` (bool), ``cost``
   (dict with cacheRead/cacheWrite/input/output numbers).
2. ``auth_profiles_json`` → content for ``agents/<agent>/agent/auth-profiles.json``.
   Include ``version: 1``, ``profiles.<name>:default`` with ``type: api_key``,
   ``provider``, and ``key`` (literal). Include ``lastGood`` mapping.
3. ``openclaw_json`` → content for the root ``openclaw.json`` under
   ``models.providers.<name>``.  Do NOT put ``apiKey`` here; only
   ``api``, ``baseUrl``, ``headers``, ``models``.

Respond with **only** a single JSON object in this exact shape:

```json
{
  "models_json": { ... },
  "auth_profiles_json": { ... },
  "openclaw_json": { ... }
}
```

No prose, no markdown outside the code block, no trailing commentary.
"""


HERMES_RENDER = """You are an expert Hermes configuration generator.

Given the canonical provider data below, produce the exact YAML that
Hermes expects in ``config.yaml`` and the env key/value for ``.env``.

Target service: hermes
Target version hint: {{version_hint}}

Canonical provider:
- name: {{name}}
- api_style: {{api_style}}
- base_url: {{base_url}}
- auth_type: {{auth_type}}
- api_key_env_var: {{api_key_env_var}}
- api_key: <redacted>
- default_model_id: {{default_model_id}}
- fallback_model_ids: {{fallback_model_ids}}

Rules:
1. ``config_yaml`` → Hermes ``config.yaml`` content. Preserve any
   sibling keys that may already exist (e.g. ``plugins``, ``server``).
   Set ``model.provider`` to the provider name, ``model.default`` to
   the default model id.  If ``base_url`` is present, set
   ``model.base_url``.  If fallback_model_ids has entries, set
   ``fallback_model`` with ``provider`` and ``model``.
2. ``env_key`` → the env variable name to write.
3. ``env_value`` → the api_key value to write.
4. ``needs_auth_json`` → true only when auth_type is oauth and an
   oauth_blob is present; false otherwise.

Respond with **only** a single JSON object in this exact shape:

```json
{
  "config_yaml": "... YAML string ...",
  "env_key": "OPENAI_API_KEY",
  "env_value": "sk-...",
  "needs_auth_json": false
}
```

No prose, no markdown outside the code block.
"""


OPENCLAW_DISCOVER = """You are an expert OpenClaw config parser.

Given the raw ``openclaw.json`` and ``models.json`` text below, extract
the semantic provider information and return a JSON object with these
fields:

- ``name``: provider name (the key under ``models.providers``)
- ``api_style``: value of ``api`` field (default "openai")
- ``base_url``: value of ``baseUrl`` (null if empty/missing)
- ``api_key``: literal apiKey value, or fall back to the first profile's
  ``key`` or ``apiKey`` in auth-profiles
- ``models``: list of objects with ``id``, ``name``,
  ``context_window`` (int or null), ``max_tokens`` (int or null)
- ``default_model_id``: the first model's ``id``

Respond with **only** the JSON object, no prose.
"""


HERMES_DISCOVER = """You are an expert Hermes config parser.

Given the raw ``config.yaml`` and ``.env`` text below, extract the
semantic provider information and return a JSON object with these
fields:

- ``name``: value of ``model.provider``
- ``api_style``: always "openai" for Hermes
- ``base_url``: value of ``model.base_url`` (null if missing)
- ``api_key``: the API key from the env file (prefer the key matching
  the provider's known env var, else fall back to any ``*_API_KEY``)
- ``models``: list with one object: ``{"id": <model.default>}``
- ``default_model_id``: value of ``model.default``
- ``oauth_blob``: if ``auth.json`` is present and the provider uses
  OAuth, include its raw text here; otherwise null

Respond with **only** the JSON object, no prose.
"""


def fill(template: str, **kwargs: str) -> str:
    """Naïve ``{{key}}`` substitution — no external templating lib."""
    text = template
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
