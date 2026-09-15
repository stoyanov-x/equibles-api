"""Entry point: ``python -m equibles_api``."""

from __future__ import annotations

import logging
import sys

from .app import serve
from .config import ConfigError, Settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # Fail loudly at boot: a service that starts with no database password
        # would answer /healthz and fail every real request.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
