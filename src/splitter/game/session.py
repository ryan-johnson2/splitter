"""What we know about the current track / quad / mode, and where it came from."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from splitter.util import utcnow

SOURCE_GAME = "game"  # the game's `session` event (multiplayer room created here)
SOURCE_STICKY = "sticky"  # reused from the previous run
SOURCE_MANUAL = "manual"  # entered on the tablet


@dataclass
class SessionState:
    track_name: str = ""
    scenery: str = ""
    quad_type: str = ""
    quad_size: str = ""
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

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["updated_at"] = self.updated_at.replace(microsecond=0).isoformat() + "Z"
        d["known"] = self.known
        return d
