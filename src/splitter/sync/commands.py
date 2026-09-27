"""The three things a live page may tell a node to do: set the session (track
and quad), abort the run, pause or resume capture.

One implementation for both ways in: the node's own ``/api/...`` routes and
the commands a web sends down the relay (``sync/relay.py``). Whatever the
transport, the controller is the only thing that changes state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from splitter.db import repos
from splitter.game.catalog import SOURCES

if TYPE_CHECKING:
    from splitter.game.bridge import GameBridge
    from splitter.game.controller import RaceController


class CommandError(Exception):
    """A refused command; ``status`` is the HTTP code the node's route would give."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class SessionIn(BaseModel):
    track_name: str
    scenery: str = ""
    quad_type: str = ""
    quad_size: str = ""
    race_laps: int = 0
    race_mode: str = ""
    # From the track picker; 0 / "" when the name was typed by hand.
    track_id: int = 0
    scene_id: int = 0
    track_source: str = ""
    # From the quad picker (catalog model id); 0 = unknown quad.
    quad_model_id: int = 0
    quad_class_id: int = 0
    # Re-attribute this run too (the "did you change tracks?" prompt).
    apply_to_race_id: int = 0


class CaptureIn(BaseModel):
    enabled: bool


class AbortIn(BaseModel):
    in_game: bool = True  # also send the game's abortrace command when connected


async def set_session(
    controller: RaceController, session_factory: Any, body: SessionIn
) -> dict[str, Any]:
    if not body.track_name.strip():
        raise CommandError(400, "track_name is required")
    if body.track_id <= 0:
        # PBs are keyed by online track id, so a typed name is not enough.
        raise CommandError(400, "pick the track from the search so it has an online id")
    await controller.set_manual_session(
        body.track_name,
        body.scenery,
        body.quad_type,
        body.quad_size,
        body.race_laps,
        body.race_mode,
        track_id=body.track_id,
        scene_id=body.scene_id,
        track_source=body.track_source if body.track_source in SOURCES else "",
        quad_model_id=max(0, body.quad_model_id),
        quad_class_id=max(0, body.quad_class_id),
    )
    applied = False
    if body.apply_to_race_id > 0:
        s = controller.session
        async with session_factory() as db:
            race = await repos.update_race(
                db,
                body.apply_to_race_id,
                track_id=s.track_id,
                track_name=s.track_name,
                scenery=s.scenery,
                scene_id=s.scene_id,
                track_source=s.track_source,
            )
        applied = race is not None
        await controller.refresh_reference()
    return dict(controller.session.to_dict()) | {"applied": applied}


async def set_capture(controller: RaceController, body: CaptureIn) -> dict[str, Any]:
    """Pause or resume recording; paused keeps the game link but ignores races."""
    await controller.set_capture(body.enabled)
    return {"enabled": controller.capture}


async def abort(controller: RaceController, bridge: GameBridge, body: AbortIn) -> dict[str, Any]:
    """Manual abort: the timer got stuck (game closed mid-run) or the pilot gives up."""
    sent = False
    if body.in_game and controller.race_active:
        sent = await bridge.abort_race()
    race_id = await controller.abort_race("manual")
    return {"aborted": race_id is not None, "race_id": race_id, "in_game": sent}


COMMANDS = ("session", "capture", "abort")


async def run(
    kind: str,
    data: dict[str, Any],
    controller: RaceController,
    bridge: GameBridge,
    session_factory: Any,
) -> dict[str, Any]:
    """Dispatch one relayed command by name. ``CommandError`` for anything refused."""
    try:
        if kind == "session":
            return await set_session(controller, session_factory, SessionIn(**data))
        if kind == "capture":
            return await set_capture(controller, CaptureIn(**data))
        if kind == "abort":
            return await abort(controller, bridge, AbortIn(**data))
    except ValidationError as e:
        raise CommandError(422, str(e.errors()[0].get("msg", "invalid"))[:200]) from e
    raise CommandError(400, f"unknown command {kind!r}")
