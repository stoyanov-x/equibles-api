# equibles-api

Read-only HTTP access to the [Equibles](https://github.com/daniel3303/Equibles) Postgres
database, for the Meridian stack.

## Why this exists

The Meridian Foundry could only generate candidates over ~50 symbols, because its
universes are hand-written tuples of legs. That was never a data limit: the Equibles
database already holds **~9,700 tickers × ~1,700 split/dividend-adjusted daily bars**.
Nothing read it.

The first version of this was a host script that ran a `COPY` through the database
container on a cron and dropped a CSV into the Meridian data volume. That worked, but
it reached into *another application's* volume, which is the wrong layer: Coolify can
recreate that volume, and a `chown` can silently break it.

## Why a service, and why a separate repo

- **Why HTTP:** `meridian-core` is deliberately stdlib-only (no dependencies,
  `mypy --strict`), so it cannot speak the Postgres wire protocol. It already fetches
  data over HTTP with `urllib` from OpenBB and the Equibles MCP, so an HTTP API is the
  consistent interface. Direct SQL would force a driver dependency into a project that
  has carefully avoided one.
- **Why not inside the Equibles fork:** upstream has ~97 projects in `Equibles.sln`
  and edits it on nearly every feature PR. Adding a project there means a recurring
  merge conflict in the weekly upstream sync, plus `docker-compose.yml` and
  `Directory.Packages.props` (central package management forbids versions in
  `.csproj`, so any new package forces an edit). A separate repo has **zero**
  fork-sync risk by construction.

## How it reaches the database

No port is exposed. The Equibles `db` container sits on that app's own bridge network
(named for its app uuid) and already answers to the DNS alias `db`, so this service
simply joins that network as an external one and connects to `Host=db;Port=5432`.

```
equibles-api ──(network lisfl00u818sk9dbws7psox1)──> db:5432   [read-only role]
     ^
     └──(network coolify, alias "equibles-api")───── meridian-core
```

Credentials come from a **read-only role**, `meridian_ro`, not the `postgres`
superuser. The password lives in `/etc/meridian/equibles-ro.env` (mode 0600) on the
host. Verified behaviour: `SELECT` works, `CREATE TABLE` is refused.

### Deploying on Coolify (two things verified the hard way)

1. **Declaring external networks works.** Coolify rewrites the compose before
deploying and adds its own service-level `networks:` key, but it **merges** rather
than replaces. The generated file was:

   ```yaml
           networks:
               equibles: {  }
               coolify:
                   aliases:
                       - equibles-api
               <app-uuid>: null
   ```

   Note that Coolify does **not** put compose apps on the `coolify` network by
default (only `dockerfile`/`image` apps get that), which is why the alias above has
to be declared explicitly for meridian-core to reach this service.

2. **Never remap a secret with `${VAR:?}` in the compose file.** Compose
interpolates during the **build** step, and Coolify passes only *build-time*
variables to that step — so a runtime-only secret fails the build before any image
exists. Marking the password build-time to work around it would bake it into the
image metadata. The service reads `EQUIBLES_RO_PASSWORD` directly instead, because
Coolify injects application env vars into every service.

Coolify also sets the routing port from the service's `expose:` (8080 here). The
legacy `ports_exposes` field shows `3000` and is ignored.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | 302 to `/docs`. A browser landing on the root should not meet a 401 and conclude the service is broken. |
| `GET /docs` | Swagger UI. Unauthenticated. Use **Authorize** to paste the key (`persistAuthorization` is on). |
| `GET /openapi.json` | The OpenAPI 3.1 document that page renders. Unauthenticated. |
| `GET /healthz` | `{"ok": bool}`. Unauthenticated, so it works when a key is set. Always 200 when the process is alive; `ok` reports the database. |
| `GET /v1/coverage` | Row/symbol/date counts and the span. Cheap pre-flight before asking for a panel. |
| `GET /v1/panel.csv` | The price panel: `Date,ListedTicker,AdjustedClose,Volume`, liquidity-ranked. `Volume` is required by the consumer's capacity gate, so a panel without it cannot promote anything. |
| `GET /v1/holdings/summary` | 13F counters, including CUSIP coverage and processed data sets — "holdings are low" is unactionable without knowing which of those is the constraint. |

The document is written by hand (there is no framework to introspect), so its real
risk is drift. `router.API_PATHS` is the single list of served paths, and tests
assert the document covers **exactly** that list in both directions: nothing
documented that 404s, nothing served that is undocumented.

### Panel parameters

| Param | Default | Range |
|---|---|---|
| `min_bars` | `1000` | 1–100000 |
| `limit` | `1500` | 1–`EQUIBLES_API_MAX_ROWS` |
| `lookback_days` | `2200` | 1–20000 |
| `since` | — | ISO date; overrides `lookback_days` |

```bash
curl -sS "http://equibles-api:8080/v1/panel.csv?min_bars=1000&limit=1500" -o prices.csv
```

**Read the `X-Panel-Since` header**, and note the panel calendar is a *union*: the
newest date usually belongs to a handful of early reporters. On the live data the
final date carried 12 of 1,500 symbols, so a cross-sectional rule that ranks the last
row is really a 12-name strategy. Pick the last date with adequate coverage instead.

A `200` means the stream **started**, not that it finished — a failure mid-export
closes the connection without a new status. Treat a truncated panel as a failure.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `EQUIBLES_DB_PASSWORD` | — | **Required.** Boot fails without it. Also accepts `EQUIBLES_RO_PASSWORD` (the name Coolify sets). |
| `EQUIBLES_DB_HOST` | `db` | The container alias on the Equibles network. |
| `EQUIBLES_DB_PORT` | `5432` | |
| `EQUIBLES_DB_NAME` | `equibles` | |
| `EQUIBLES_DB_USER` | `meridian_ro` | Never defaults to `postgres`. |
| `EQUIBLES_API_KEY` | unset | When set, all `/v1/*` routes need `Authorization: Bearer <key>` or `X-API-Key`. |
| `EQUIBLES_API_ALLOWED_ORIGINS` | unset | Comma-separated browser origins allowed to read responses. **Unset means no CORS headers at all** — opening the service to browsers should be a deliberate act. |
| `EQUIBLES_API_HOST` / `_PORT` | `0.0.0.0` / `8080` | |
| `EQUIBLES_API_MAX_ROWS` | `5000` | Ceiling on `limit`; stops an unbounded export. |
| `EQUIBLES_API_STATEMENT_TIMEOUT_MS` | `600000` | Applied per connection. |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
bash ci-local.sh
```

Integration check against the live database (build the image, join the network):

```bash
docker build -t equibles-api .
docker run --rm -e PGPASSWORD="$EQUIBLES_RO_PASSWORD" --network lisfl00u818sk9dbws7psox1 \
  equibles-api python -c "import psycopg,os;print(psycopg.connect(host='db',user='meridian_ro',dbname='equibles').execute('select count(*) from \"ListedDailyStockPrice\"').fetchone())"
```

## Security notes

- The credential is read-only, so the blast radius of a bug here is disclosure, not
  modification.
- SQL is **always** parameterised; values are never formatted into a statement. The
  one non-obvious exception is `statement_timeout`, which uses `set_config()` because
  `SET` does not accept bind parameters.
- Request headers are never logged (they can carry the key), and logged paths are
  truncated.
- The API key is optional because the service is only reachable on the app networks.
  Set it if that assumption ever stops holding.

### CORS (for the dashboard health tile)

`EQUIBLES_API_ALLOWED_ORIGINS` is empty by default, so no CORS headers are sent and
a browser cannot read anything. Set it to the dashboard origin to enable the health
probe:

```
EQUIBLES_API_ALLOWED_ORIGINS=https://dash.example
```

The preflight is deliberately incomplete: it advertises `GET, HEAD` and **no**
`Access-Control-Allow-Headers`. That is the mechanism, not an oversight — a browser
preflight for an `Authorization` header fails, so `/v1/*` is unreachable from
JavaScript. A key inlined in a browser bundle is not a secret, so the tile probes
`/healthz` (unauthenticated by design) and `/v1/*` stays server-to-server.
