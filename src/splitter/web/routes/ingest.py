"""The web side of sync: accept runs pushed by nodes, serve reference bundles.

Every route needs ``Authorization: Bearer <ingest_token>``; with no token set
on the Settings page receiving is off and everything here is 403.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from splitter.core.rundoc import DOC_VERSION
from splitter.db import repos
from splitter.util import utcnow
from splitter.version import __version__

router = APIRouter(prefix="/api")

MAX_BODY = 8 * 1024 * 1024


def _require_token(request: Request) -> None:
    token = request.app.state.settings.get("ingest_token").strip()
    if not token:
        raise HTTPException(403, "receiving runs is not enabled on this Splitter")
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), token):
        raise HTTPException(401, "bad ingest token")


@router.get("/ingest/ping")
async def ingest_ping(request: Request) -> dict[str, Any]:
    _require_token(request)
    return {
        "ok": True,
        "version": __version__,
        "doc_version": DOC_VERSION,
        "name": request.app.state.settings.get("node_name"),
    }


@router.put("/ingest/runs/{uuid}")
async def ingest_run(request: Request, uuid: str) -> JSONResponse:
    """Idempotent: the same run twice is ``exists``. The response carries the
    reference bundle for the run's PB key so the node's cache stays fresh."""
    _require_token(request)
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY:
        raise HTTPException(413, f"document larger than {MAX_BODY // (1024 * 1024)} MB")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, f"document larger than {MAX_BODY // (1024 * 1024)} MB")
    try:
        import json

        doc = json.loads(body)
    except ValueError as e:
        raise HTTPException(400, f"body is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("uuid") != uuid:
        raise HTTPException(400, "document uuid does not match the URL")
    version = doc.get("doc_version")
    if isinstance(version, int) and version > DOC_VERSION:
        return JSONResponse(
            {
                "detail": f"document version {version} is newer than this web ({DOC_VERSION})",
                "supported": DOC_VERSION,
            },
            status_code=409,
        )
    # One ingest at a time: idempotency is "uuid already here", which two
    # concurrent inserts of the same run would both get wrong.
    state = request.app.state
    if not hasattr(state, "ingest_lock"):
        state.ingest_lock = asyncio.Lock()
    async with state.ingest_lock, state.session_factory() as db:
        if await repos.is_tombstoned(db, uuid):
            raise HTTPException(410, "this run was deleted here")
        result = await repos.import_run(db, doc, origin="ingest")
        if result.status == "rejected":
            raise HTTPException(422, result.error)
        assert result.key is not None
        if result.status == "created":
            await repos.recalculate_best(db, result.key)
        await repos.note_node(
            db,
            node_id=str(doc.get("node_id") or ""),
            name=request.headers.get("x-splitter-node", ""),
            seq=int(doc.get("seq") or 0),
            imu_seen=bool((doc.get("telemetry") or {}).get("samples")),
        )
        bundle = await repos.reference_bundle(db, result.key)
    await request.app.state.controller.refresh_reference()
    return JSONResponse({"status": result.status, "race_id": result.race_id, "reference": bundle})


@router.get("/reference")
async def reference(
    request: Request, track_id: int, quad_model_id: int = 0, race_laps: int = 0
) -> dict[str, Any]:
    _require_token(request)
    key = repos.PBKey(track_id, quad_model_id, race_laps)
    async with request.app.state.session_factory() as db:
        return await repos.reference_bundle(db, key)


def now_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat() + "Z"
