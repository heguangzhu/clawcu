from __future__ import annotations

import json
import time
from typing import Any, Iterable

from clawcu.a2a.card import AgentCard

_PEERS_KEY = "a2a:registry:peers"
_PEER_KEY_PREFIX = "a2a:registry:peer:"
_DEFAULT_TTL_SECONDS = 30


def _redis_from_url(redis_url: str):
    try:
        import redis
    except Exception as exc:  # pragma: no cover - exercised when optional deps missing
        raise RuntimeError("Redis registry store requires the redis package") from exc
    return redis.Redis.from_url(redis_url, decode_responses=True)


def peer_key(name: str) -> str:
    return f"{_PEER_KEY_PREFIX}{name}"


def _card_payload(card: Any) -> dict[str, Any]:
    if hasattr(card, "to_dict"):
        payload = card.to_dict()
        return dict(payload)
    name = getattr(card, "name", "")
    description = getattr(card, "description", "") or "A2A agent"
    endpoint = ""
    interfaces = getattr(card, "supported_interfaces", None) or []
    if interfaces:
        endpoint = getattr(interfaces[0], "url", "") or ""
    skills: list[str] = []
    for skill in getattr(card, "skills", None) or []:
        tags = getattr(skill, "tags", None) or []
        skills.extend(str(tag) for tag in tags if str(tag).strip())
    return {
        "name": str(name),
        "role": str(description),
        "skills": skills or ["chat"],
        "endpoint": str(endpoint),
        "protocol": ["a2a/v0.1"],
    }


def publish_card(redis_url: str, card: Any, *, ttl_s: int = _DEFAULT_TTL_SECONDS) -> None:
    """Publish/refresh one AgentCard snapshot in Redis."""
    client = _redis_from_url(redis_url)
    payload = _card_payload(card)
    agent_name = str(payload.get("name") or "").strip()
    if not agent_name:
        raise ValueError("registry card name is required")
    payload["last_seen"] = time.time()
    pipe = client.pipeline()
    pipe.sadd(_PEERS_KEY, agent_name)
    pipe.set(peer_key(agent_name), json.dumps(payload), ex=max(1, int(ttl_s)))
    pipe.execute()


def list_cards(redis_url: str) -> list[AgentCard]:
    """Return live AgentCards from Redis, pruning stale set members."""
    client = _redis_from_url(redis_url)
    names = sorted(str(name) for name in (client.smembers(_PEERS_KEY) or []))
    cards: list[AgentCard] = []
    stale: list[str] = []
    for name in names:
        raw = client.get(peer_key(name))
        if not raw:
            stale.append(name)
            continue
        try:
            data = json.loads(raw)
            cards.append(AgentCard.from_dict(data))
        except Exception:
            stale.append(name)
    if stale:
        client.srem(_PEERS_KEY, *stale)
    return cards


def make_redis_cards_provider(redis_url: str):
    def provider() -> Iterable[AgentCard]:
        return list_cards(redis_url)

    return provider
