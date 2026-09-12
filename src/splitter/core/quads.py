"""Quad model / class / scene catalog, bundled with the app as static data.

VelociDrone resolves ``model_id`` → name through its local SQLite database
(``models`` table: ``model_id, name, prefab_name, component_group_id,
active``) and the same file carries the ``sceneries`` table. Splitter ships a
snapshot of those tables as ``splitter/data/catalog.json`` (the same file
Marshal bundles) so the "Track…" dialog can offer the real quad list grouped
by class, and so scene ids from the online track lists get their in-game
names. Regenerate after a game update with::

    splitter extract-catalog /path/to/settings.db

On Windows the game keeps the file at
``%USERPROFILE%\\AppData\\LocalLow\\<publisher>\\VelociDrone\\settings.db``.

A "quad class" is the game's ``component_group_id``; ``CLASS_NAMES`` carries
the official class names from velocidrone.com.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BUNDLED_CATALOG = Path(__file__).resolve().parent.parent / "data" / "catalog.json"

# Official VelociDrone class names for component_group_id (velocidrone.com, 1.16).
CLASS_NAMES: dict[int, str] = {
    1: "Racing",
    2: "Mega",
    3: "Micro",
    4: "Toothpick",
    5: "Combat",
    6: "Freestyle",
    7: "Street League",
    8: "Freedom Spec",
    9: "TBS Spec",
    10: "MultiGP PRO Spec",
}

# Fallback scene names when no catalog is bundled (velocidrone-db docs).
_SEED_SCENES: dict[int, str] = {
    3: "Hangar",
    7: "Industrial Wasteland",
    8: "Football Stadium",
    12: "Countryside",
    14: "Karting Track",
    15: "Subway",
    16: "Empty Scene Day",
    18: "NEC Birmingham",
    22: "Coastal",
    24: "City",
    30: "Bando",
    55: "Night Factory 2",
    103: "Alpine Lake",
    104: "Roman City",
    105: "Night Factory 3",
}


@dataclass(frozen=True)
class QuadModel:
    model_id: int
    name: str
    component_group_id: int
    active: bool = True

    @property
    def class_name(self) -> str:
        return class_name(self.component_group_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "class_id": self.component_group_id,
            "class_name": self.class_name,
        }


@dataclass(frozen=True)
class GameCatalog:
    models: list[QuadModel] = field(default_factory=list)
    scenes: dict[int, str] = field(default_factory=dict)  # flyable, enabled scenes only
    disabled_scenes: list[int] = field(default_factory=list)
    generated_at: str = ""
    source: str = ""

    # ── lookups ──────────────────────────────────────────────────────

    def model(self, model_id: int) -> QuadModel | None:
        return next((m for m in self.models if m.model_id == model_id), None)

    def model_by_name(self, name: str) -> QuadModel | None:
        """Exact, case-insensitive name match (the game's ``quadType`` string)."""
        key = name.strip().lower()
        if not key:
            return None
        return next((m for m in self.models if m.name.strip().lower() == key), None)

    def classes(self) -> list[dict[str, Any]]:
        """Active models grouped by class, for the quad picker; classes in id order."""
        groups: dict[int, list[QuadModel]] = {}
        for m in self.models:
            if m.active:
                groups.setdefault(m.component_group_id, []).append(m)
        return [
            {
                "class_id": gid,
                "class_name": class_name(gid),
                "models": [m.to_dict() for m in sorted(members, key=lambda m: m.name.lower())],
            }
            for gid, members in sorted(groups.items())
        ]


class CatalogFileError(ValueError):
    """The file is not a usable VelociDrone settings database / catalog."""


def class_name(group_id: int) -> str:
    return CLASS_NAMES.get(group_id, f"Class {group_id}" if group_id else "")


# ── reading the game's settings.db ───────────────────────────────────


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def read_game_db(path: Path) -> GameCatalog:
    """Read models and scenes from a copy of the game's ``settings.db``."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - platform-specific messages
        raise CatalogFileError(f"cannot open database: {exc}") from exc
    try:
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        except sqlite3.DatabaseError as exc:
            raise CatalogFileError("not a SQLite database") from exc
        if "models" not in tables:
            raise CatalogFileError("no 'models' table — is this the game's settings.db?")
        cols = _columns(conn, "models")
        if not {"model_id", "name"} <= cols:
            raise CatalogFileError("'models' table is missing model_id/name")
        group_col = "component_group_id" if "component_group_id" in cols else "0"
        active_col = "active" if "active" in cols else "'true'"
        models = [
            QuadModel(int(r[0]), str(r[1]), int(r[2] or 0), _truthy(r[3]))
            for r in conn.execute(
                f"SELECT model_id, name, {group_col}, {active_col} FROM models ORDER BY name"
            )
        ]
        scenes: dict[int, str] = {}
        disabled: list[int] = []
        if "sceneries" in tables:
            scols = _columns(conn, "sceneries")
            if {"id", "name"} <= scols:
                title_col = "title" if "title" in scols else "name"
                type_col = "type" if "type" in scols else "'track'"
                enabled_col = "enabled" if "enabled" in scols else "'true'"
                query = f"SELECT id, name, {title_col}, {type_col}, {enabled_col} FROM sceneries"
                for sid, name, title, kind, enabled in conn.execute(query):
                    if str(kind) != "track":
                        continue
                    if not _truthy(enabled):
                        disabled.append(int(sid))
                        continue
                    scenes[int(sid)] = str(title or name)
        return GameCatalog(
            models=models,
            scenes=scenes,
            disabled_scenes=sorted(disabled),
            generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            source=path.name,
        )
    finally:
        conn.close()


# ── bundled JSON ─────────────────────────────────────────────────────


def catalog_to_json(catalog: GameCatalog) -> str:
    payload: dict[str, Any] = {
        "generated_at": catalog.generated_at,
        "source": catalog.source,
        "models": [
            {
                "model_id": m.model_id,
                "name": m.name,
                "component_group_id": m.component_group_id,
                "active": m.active,
            }
            for m in catalog.models
        ],
        "scenes": {str(k): v for k, v in sorted(catalog.scenes.items())},
        "disabled_scenes": list(catalog.disabled_scenes),
    }
    return json.dumps(payload, indent=1, ensure_ascii=False) + "\n"


def catalog_from_json(text: str) -> GameCatalog:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogFileError(f"catalog.json is not valid JSON: {exc}") from exc
    return GameCatalog(
        models=[
            QuadModel(
                int(m["model_id"]),
                str(m["name"]),
                int(m.get("component_group_id", 0)),
                bool(m.get("active", True)),
            )
            for m in data.get("models", [])
        ],
        scenes={int(k): str(v) for k, v in data.get("scenes", {}).items()},
        disabled_scenes=[int(x) for x in data.get("disabled_scenes", [])],
        generated_at=str(data.get("generated_at", "")),
        source=str(data.get("source", "")),
    )


def load_bundled_catalog(path: Path = BUNDLED_CATALOG) -> GameCatalog:
    """The catalog shipped with the package; empty (seed scenes only) if missing."""
    if not path.is_file():
        return GameCatalog(scenes=dict(_SEED_SCENES))
    return catalog_from_json(path.read_text(encoding="utf-8"))


def write_bundled_catalog(catalog: GameCatalog, path: Path = BUNDLED_CATALOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(catalog_to_json(catalog), encoding="utf-8")


# ── process-wide registry ────────────────────────────────────────────

_catalog: GameCatalog | None = None


def catalog() -> GameCatalog:
    """The loaded catalog (bundled file on first use)."""
    global _catalog
    if _catalog is None:
        _catalog = load_bundled_catalog()
    return _catalog


def use_catalog(cat: GameCatalog) -> None:
    global _catalog
    _catalog = cat


def scene_name(scene_id: int) -> str:
    """Display name for a scene id, including retired ones seen in old data."""
    return catalog().scenes.get(scene_id) or _SEED_SCENES.get(scene_id) or f"Scene {scene_id}"


def model_name(model_id: int) -> str:
    m = catalog().model(model_id) if model_id else None
    return m.name if m else ""
