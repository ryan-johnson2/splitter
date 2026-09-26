"""The run document: one run as one JSON object.

The export/import format, the upload payload for sync, and the shape another
install can hand-carry (``docs/sync-design.md``). Everything about the run is
in it except local row ids and ``is_best`` (a fact about the receiving
database, not the run). Columns are taken from the ORM so a new column on
``races``, ``laps`` or ``gate_times`` is in the document without touching this
module; the receiver ignores keys it does not know.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.sql import FromClause

from splitter.db.models import GateTime, Lap, Race, TelemetryBlob

DOC_VERSION = 1

# Never in the document: local identity and local verdicts.
_RACE_SKIP = frozenset({"id", "is_best", "reference_race_id", "received_at", "origin"})
_CHILD_SKIP = frozenset({"id", "race_id"})
# Emitted as parsed JSON at the top level rather than as a string column.
_RACE_JSON = {"crashes": "crashes", "fingerprint": "fingerprint"}


class DocumentError(ValueError):
    """A document this build cannot import; the message says why."""


@dataclass
class RunDoc:
    uuid: str
    node_id: str
    seq: int
    race: dict[str, Any]  # column → value, ready for ``Race(**race)``
    laps: list[dict[str, Any]]
    gate_times: list[dict[str, Any]]
    telemetry: TelemetryBlob | None
    splitter_version: str = ""
    doc_version: int = DOC_VERSION
    crashes: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: dict[str, Any] | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DocumentError(f"bad timestamp {value!r}")
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError as e:
        raise DocumentError(f"bad timestamp {value!r}") from e


def _row_dict(table: FromClause, row: Any, skip: frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in table.columns:
        if column.name in skip:
            continue
        value = getattr(row, column.name)
        out[column.name] = _iso(value) if isinstance(column.type, DateTime) else value
    return out


def _row_from_dict(table: FromClause, data: dict[str, Any], skip: frozenset[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DocumentError("row is not an object")
    out: dict[str, Any] = {}
    for column in table.columns:
        if column.name in skip or column.name not in data:
            continue
        value = data[column.name]
        if isinstance(column.type, DateTime):
            value = _parse_iso(value)
        out[column.name] = value
    return out


def build(race: Race, blob: TelemetryBlob | None, splitter_version: str) -> dict[str, Any]:
    """The document for a race (its ``laps`` and ``gate_times`` loaded)."""
    race_d = _row_dict(Race.__table__, race, _RACE_SKIP)
    for column in _RACE_JSON:
        race_d.pop(column, None)
    doc: dict[str, Any] = {
        "doc_version": DOC_VERSION,
        "splitter_version": splitter_version,
        "uuid": race.uuid,
        "node_id": race.node_id,
        "seq": race.seq,
        "race": race_d,
        "laps": [_row_dict(Lap.__table__, lap, _CHILD_SKIP) for lap in race.laps],
        "gate_times": [_row_dict(GateTime.__table__, g, _CHILD_SKIP) for g in race.gate_times],
        "crashes": json.loads(race.crashes) if race.crashes else [],
        "fingerprint": json.loads(race.fingerprint) if race.fingerprint else None,
        "telemetry": None,
    }
    if blob is not None and blob.samples > 0:
        doc["telemetry"] = {
            "hz": blob.hz,
            "samples": blob.samples,
            "columns": json.loads(blob.columns),
            "encoding": blob.encoding,
            "data": base64.b64encode(blob.data).decode("ascii"),
        }
    return doc


def parse(doc: Any) -> RunDoc:
    """Validate a document and turn it into rows-in-waiting.

    Raises :class:`DocumentError` for anything this build cannot take: a newer
    document version, a missing identity, children that disagree with the
    race, or a telemetry blob that does not decode.
    """
    from splitter.core.telemetry import decode_columns

    if not isinstance(doc, dict):
        raise DocumentError("document is not an object")
    version = doc.get("doc_version")
    if not isinstance(version, int) or version < 1:
        raise DocumentError("missing doc_version")
    if version > DOC_VERSION:
        raise DocumentError(f"document version {version} is newer than this build ({DOC_VERSION})")
    uuid = doc.get("uuid")
    if not isinstance(uuid, str) or not (8 <= len(uuid) <= 64) or not uuid.isalnum():
        raise DocumentError("missing or malformed uuid")
    race_in = doc.get("race")
    if not isinstance(race_in, dict):
        raise DocumentError("missing race")
    race = _row_from_dict(Race.__table__, race_in, _RACE_SKIP | frozenset(_RACE_JSON))
    if race.get("started_at") is None:
        raise DocumentError("race has no started_at")
    if race.get("status") not in ("finished", "aborted"):
        raise DocumentError(f"race status {race.get('status')!r} cannot be imported")
    race["uuid"] = uuid
    race["node_id"] = str(doc.get("node_id") or "")
    seq = doc.get("seq") or 0
    if not isinstance(seq, int):
        raise DocumentError("seq is not an integer")
    laps_in, gates_in = doc.get("laps") or [], doc.get("gate_times") or []
    if not isinstance(laps_in, list) or not isinstance(gates_in, list):
        raise DocumentError("laps and gate_times must be lists")
    laps = [_row_from_dict(Lap.__table__, d, _CHILD_SKIP) for d in laps_in]
    gates = [_row_from_dict(GateTime.__table__, d, _CHILD_SKIP) for d in gates_in]
    for d, what in ((laps, "lap"), (gates, "gate_time")):
        for row in d:
            if "cumulative_ms" not in row:
                raise DocumentError(f"{what} without cumulative_ms")
    if len({g.get("seq") for g in gates}) != len(gates):
        raise DocumentError("gate_times have duplicate seq values")
    if race["status"] == "finished" and not gates:
        raise DocumentError("a finished race must have gate crossings")
    crashes = doc.get("crashes") or []
    fingerprint = doc.get("fingerprint")
    if not isinstance(crashes, list) or not (fingerprint is None or isinstance(fingerprint, dict)):
        raise DocumentError("crashes must be a list and fingerprint an object")
    race["crashes"] = json.dumps(crashes) if crashes else ""
    race["crash_count"] = len(crashes)
    race["fingerprint"] = json.dumps(fingerprint) if fingerprint else ""

    blob: TelemetryBlob | None = None
    tele = doc.get("telemetry")
    if tele:
        if not isinstance(tele, dict):
            raise DocumentError("telemetry is not an object")
        try:
            data = base64.b64decode(tele["data"], validate=True)
            samples = int(tele["samples"])
            decoded = decode_columns(data, samples, str(tele.get("encoding", "")))
        except (KeyError, ValueError, TypeError) as e:
            raise DocumentError(f"telemetry blob: {e}") from e
        blob = TelemetryBlob(
            hz=float(tele.get("hz") or 0),
            samples=len(decoded),
            columns=json.dumps(list(tele.get("columns") or [])),
            encoding=str(tele["encoding"]),
            data=data,
        )
        race["telemetry_samples"] = len(decoded)
    else:
        race["telemetry_samples"] = 0
    return RunDoc(
        uuid=uuid,
        node_id=race["node_id"],
        seq=seq,
        race=race,
        laps=laps,
        gate_times=gates,
        telemetry=blob,
        splitter_version=str(doc.get("splitter_version") or ""),
        doc_version=version,
        crashes=crashes,
        fingerprint=fingerprint,
    )
