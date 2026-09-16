"""Tests for the OpenAPI document and the docs page.

The document is hand-written, so the value of these tests is drift detection: they
fail if a route is added without documentation, or documented without a route.
"""

from __future__ import annotations

import json

import pytest

from equibles_api import openapi
from equibles_api.config import Settings
from equibles_api.router import API_PATHS, DEFAULT_LIMIT, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_BARS
from tests.test_router import FakeDB, body, call


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_MAX_ROWS": "5000"})


def spec(settings: Settings) -> dict[str, object]:
    return openapi.document(settings)


# -- drift ----------------------------------------------------------------------


def test_documented_paths_are_exactly_the_served_paths(settings: Settings) -> None:
    """The whole point: a documented endpoint that 404s is worse than no docs."""
    documented = set(spec(settings)["paths"])  # type: ignore[arg-type]
    assert documented == set(API_PATHS)


@pytest.mark.parametrize("path", API_PATHS)
def test_every_documented_path_actually_routes(settings: Settings, path: str) -> None:
    db = FakeDB(row={"rows": 1})
    resp = call(db, settings, path=path)
    assert resp.status != 404, f"{path} is documented but not served"
    assert resp.status != 401, f"{path} is documented but needs a key that the docs do not mention"


def test_no_undocumented_v1_route_exists(settings: Settings) -> None:
    """Guards the other direction: a new /v1/* route must be added to API_PATHS."""
    documented = set(spec(settings)["paths"])  # type: ignore[arg-type]
    for candidate in ("/v1/prices", "/v1/foo", "/v2/coverage"):
        assert candidate not in documented
        assert call(FakeDB(), settings, path=candidate).status == 404


def test_documented_defaults_match_the_router(settings: Settings) -> None:
    """Defaults are stated in three places (router, docs, README); pin the first two."""
    panel = spec(settings)["paths"]["/v1/panel.csv"]["get"]  # type: ignore[index]
    by_name = {p["name"]: p for p in panel["parameters"]}
    assert by_name["min_bars"]["schema"]["default"] == DEFAULT_MIN_BARS
    assert by_name["limit"]["schema"]["default"] == DEFAULT_LIMIT
    assert by_name["lookback_days"]["schema"]["default"] == DEFAULT_LOOKBACK_DAYS


def test_limit_maximum_reflects_the_configured_ceiling() -> None:
    """max_rows is the real ceiling, so the spec must not promise more than it gives."""
    tight = Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_MAX_ROWS": "77"})
    panel = openapi.document(tight)["paths"]["/v1/panel.csv"]["get"]
    limit = next(p for p in panel["parameters"] if p["name"] == "limit")
    assert limit["schema"]["maximum"] == 77


# -- shape ----------------------------------------------------------------------


def test_document_is_valid_openapi_shape(settings: Settings) -> None:
    doc = spec(settings)
    assert str(doc["openapi"]).startswith("3.1")
    info = doc["info"]
    assert info["title"] == "equibles-api"  # type: ignore[index]
    assert info["version"]  # type: ignore[index]
    assert doc["components"]["securitySchemes"]  # type: ignore[index]


def test_healthz_is_documented_as_public(settings: Settings) -> None:
    """Healthchecks must not need a key, and the document should say so."""
    healthz = spec(settings)["paths"]["/healthz"]["get"]  # type: ignore[index]
    assert healthz["security"] == []


def test_no_servers_block_so_swagger_targets_its_own_origin(settings: Settings) -> None:
    """Docs are served from the API's own origin, so a hardcoded URL would be wrong
    on any domain but one."""
    assert "servers" not in spec(settings)


def test_spec_serialises(settings: Settings) -> None:
    assert json.loads(json.dumps(spec(settings)))["info"]["title"] == "equibles-api"


# -- routes ---------------------------------------------------------------------


def test_openapi_json_is_served_and_parses(settings: Settings) -> None:
    resp = call(FakeDB(), settings, path="/openapi.json")
    assert resp.status == 200
    assert resp.content_type == "application/json"
    assert json.loads(body(resp))["openapi"].startswith("3.1")


def test_docs_page_serves_swagger_ui(settings: Settings) -> None:
    resp = call(FakeDB(), settings, path="/docs")
    assert resp.status == 200
    assert resp.content_type.startswith("text/html")
    html = body(resp).decode()
    assert "swagger-ui" in html
    assert openapi.SWAGGER_UI_VERSION in html


def test_swagger_assets_are_version_pinned_not_floating() -> None:
    """A major-only CDN tag would let executing JavaScript change without a commit."""
    assert openapi.SWAGGER_UI_VERSION.count(".") >= 2
    assert "swagger-ui-dist@" in openapi.DOCS_HTML


def test_docs_and_spec_need_no_key() -> None:
    keyed = Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_KEY": "k"})
    for path in ("/docs", "/openapi.json", "/healthz"):
        assert call(FakeDB(), keyed, path=path).status == 200, path


def test_root_redirects_to_the_docs(settings: Settings) -> None:
    resp = call(FakeDB(), settings, path="/")
    assert resp.status == 302
    assert resp.headers["Location"] == "/docs"


def test_v1_routes_still_need_the_key(settings: Settings) -> None:
    keyed = Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_KEY": "k"})
    assert call(FakeDB(), keyed, path="/v1/coverage").status == 401
