"""The HTTP adapter: a thin skin over :mod:`equibles_api.router`."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .db import Database
from .router import QuerySource, route

logger = logging.getLogger("equibles_api")

#: Truncate logged paths so a hostile request line cannot fill the log.
MAX_LOGGED_PATH = 200


def make_handler(
    settings: Settings, db: QuerySource
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one configuration and data source."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "equibles-api"

        # HTTP/1.0 keeps streaming simple: the body ends at connection close, so a
        # multi-megabyte CSV needs no Content-Length and no chunked encoding.
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:
            self._serve("GET")

        def do_HEAD(self) -> None:
            self._serve("HEAD")

        def do_OPTIONS(self) -> None:
            # Preflight. The router answers only for allowed origins, and only with
            # GET/HEAD, so a browser can probe /healthz and nothing more.
            self._serve("OPTIONS")

        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            response = route(
                method=method,
                path=parts.path,
                query=parse_qs(parts.query),
                headers={k.lower(): v for k, v in self.headers.items()},
                db=db,
                settings=settings,
            )
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if method == "HEAD":
                return
            try:
                for chunk in response.body:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # A consumer that hung up mid-export is normal when a timeout fires.
                logger.warning("client disconnected mid-stream")

        def log_message(self, format: str, *args: object) -> None:
            # Log the path only: the query string is caller-controlled noise, and
            # request headers may carry the API key, so never touch them here.
            logger.info("%s %s", self.command, urlsplit(self.path).path[:MAX_LOGGED_PATH])

    return Handler


def serve(settings: Settings, db: QuerySource | None = None) -> None:
    """Run until interrupted. ``db`` is injectable for tests."""
    server = ThreadingHTTPServer(
        (settings.host, settings.port),
        make_handler(settings, db if db is not None else Database(settings)),
    )
    logger.info("listening on %s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
