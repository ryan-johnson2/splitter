"""The reference bundle: what a node needs from the web for one PB key.

Returned with every ingest acknowledgement and by ``GET /api/reference``:
the PB (for live splits) and the track's gate geometry (for the track-change
check). Pure: building takes rows, parsing gives back core types.
"""

from __future__ import annotations

from typing import Any

from splitter.core.geometry import GatePosition
from splitter.core.splits import Reference, build_reference
from splitter.db.models import Race
from splitter.util import utcnow

BUNDLE_VERSION = 1


def key_str(track_id: int, quad_model_id: int, race_laps: int) -> str:
    return f"{track_id}:{quad_model_id}:{race_laps}"


def build(
    track_id: int,
    quad_model_id: int,
    race_laps: int,
    best: Race | None,
    geometry: tuple[int | None, dict[int, GatePosition]] | None,
) -> dict[str, Any]:
    reference: dict[str, Any] | None = None
    if best is not None and best.total_time_ms:
        reference = {
            "race_uuid": best.uuid,
            "race_id": best.id,
            "total_ms": best.total_time_ms,
            "crossings": [[g.seq, g.cumulative_ms, g.gate_ms] for g in best.gate_times],
            "laps": [[lap.lap, lap.lap_ms] for lap in best.laps],
            "gates_per_lap": best.gates_per_lap,
        }
    geom: dict[str, Any] | None = None
    if geometry is not None and (geometry[0] or geometry[1]):
        count, positions = geometry
        geom = {
            "gates_per_lap": count,
            "positions": [
                [p.k, round(p.x, 3), round(p.y, 3), round(p.z, 3), p.samples]
                for p in positions.values()
            ],
        }
    return {
        "version": BUNDLE_VERSION,
        "key": {"track_id": track_id, "quad_model_id": quad_model_id, "race_laps": race_laps},
        "reference": reference,
        "geometry": geom,
        "generated_at": utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def to_reference(bundle: dict[str, Any]) -> Reference | None:
    ref = bundle.get("reference")
    if not ref or not ref.get("total_ms"):
        return None
    return build_reference(
        race_id=0,
        total_ms=int(ref["total_ms"]),
        crossings=[(int(c[0]), int(c[1]), int(c[2])) for c in ref.get("crossings", [])],
        laps=[(int(lap), int(ms)) for lap, ms in ref.get("laps", [])],
        gates_per_lap=ref.get("gates_per_lap"),
        race_uuid=str(ref.get("race_uuid") or ""),
        remote=True,
    )


def to_geometry(bundle: dict[str, Any]) -> tuple[int | None, dict[int, GatePosition]] | None:
    geom = bundle.get("geometry")
    if not geom:
        return None
    positions = {
        int(p[0]): GatePosition(int(p[0]), float(p[1]), float(p[2]), float(p[3]), int(p[4]))
        for p in geom.get("positions", [])
    }
    return geom.get("gates_per_lap"), positions
