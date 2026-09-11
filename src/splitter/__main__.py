"""Run the Splitter app: ``python -m splitter``."""

from __future__ import annotations

import uvicorn

from splitter.app import create_app
from splitter.config import Config


def main() -> None:
    cfg = Config()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
