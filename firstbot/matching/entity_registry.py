from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Entity:
    canonical_id: str
    sport: str | None
    league: str | None
    official_name: str
    aliases: tuple[str, ...]
    platform_ids: dict[str, tuple[str, ...]]


class EntityRegistry:
    def __init__(self, entities: tuple[Entity, ...] | None = None) -> None:
        self.entities = entities or _load_default_entities()

    def resolve(
        self,
        value: str | None,
        sport: str | None = None,
        league: str | None = None,
        participants: tuple[str, ...] = (),
        platform: str | None = None,
    ) -> str | None:
        normalized = _norm(value or "")
        if not normalized:
            return None
        scoped = [
            entity
            for entity in self.entities
            if _scope_matches(entity.sport, sport) and _scope_matches(entity.league, league)
        ]
        for entity in scoped:
            if platform and normalized in {_norm(item) for item in entity.platform_ids.get(platform, ())}:
                return entity.canonical_id
        for entity in scoped:
            if normalized == _norm(entity.canonical_id):
                return entity.canonical_id
        for entity in scoped:
            if normalized == _norm(entity.official_name):
                return entity.canonical_id
        for entity in scoped:
            if normalized in {_norm(alias) for alias in entity.aliases}:
                return entity.canonical_id
        if participants:
            for participant in participants:
                if normalized == _norm(participant) or normalized in _norm(participant).split():
                    return participant
        return f"unknown:{normalized.replace(' ', '_')}"

    def aliases_for(self, canonical_id: str) -> tuple[str, ...]:
        for entity in self.entities:
            if entity.canonical_id == canonical_id:
                return (entity.official_name, *entity.aliases)
        return (canonical_id,)


def _scope_matches(entity_value: str | None, wanted: str | None) -> bool:
    return entity_value is None or wanted is None or _norm(entity_value) == _norm(wanted)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _load_default_entities() -> tuple[Entity, ...]:
    path = Path(__file__).with_name("data") / "aliases.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raw = []
    entities: list[Entity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        platform_ids = {
            str(key): tuple(str(value) for value in values)
            for key, values in (item.get("platform_ids") or {}).items()
            if isinstance(values, list)
        }
        entities.append(
            Entity(
                canonical_id=str(item["canonical_id"]),
                sport=item.get("sport"),
                league=item.get("league"),
                official_name=str(item.get("official_name") or item["canonical_id"]),
                aliases=tuple(str(alias) for alias in item.get("aliases", [])),
                platform_ids=platform_ids,
            )
        )
    return tuple(entities)


def normalize_text(value: Any) -> str:
    return _norm(value or "")
