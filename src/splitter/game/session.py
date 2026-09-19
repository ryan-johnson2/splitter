"""What we know about the current track / quad / mode, and where it came from."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from splitter.util import utcnow

SOURCE_GAME = "game"  # the game's `session` event (multiplayer room created here)
SOURCE_STICKY = "sticky"  # reused from the previous run
SOURCE_MANUAL = "manual"  # entered on the tablet
SOURCE_MATCHED = "matched"  # a run's gates matched a known track (core/trackcheck)


@dataclass
class SessionState:
    track_name: str = ""
    scenery: str = ""
    # Online identity from the track picker (0 / "" when the name was typed
    # or came from the game's session event, which carries names only).
    track_id: int = 0
    scene_id: int = 0
    track_source: str = ""  # official | community | ""
    quad_type: str = ""
    quad_size: str = ""
    # From the quad picker / catalog match (0 when unknown).
    quad_model_id: int = 0
    quad_class_id: int = 0
    race_mode: str = ""
    race_format: str = ""
    race_laps: int = 0
    player_name: str = ""
    session_name: str = ""
    source: str = ""
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def known(self) -> bool:
        return bool(self.track_name)

    @property
    def identified(self) -> bool:
        """Has an online track id — the requirement for PB tracking."""
        return self.track_id > 0

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["updated_at"] = self.updated_at.replace(microsecond=0).isoformat() + "Z"
        d["known"] = self.known
        d["identified"] = self.identified
        return d
