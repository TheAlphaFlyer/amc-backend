# AMC Backend — Agent Guide

## Overview

Django backend for the ASEAN Motor Club: API (uvicorn), arq worker, Discord bot. Runs inside a NixOS container on `asean-mt-server`. Uses **uv2nix** for Nix packaging — the Nix build reads `uv.lock` to construct the Python environment.

## Structure

```
amc-backend/
├── src/
│   ├── amc/                    # Main Django app (models, commands, tests)
│   │   ├── test_*.py           # Tests (pytest + pytest-django)
│   │   └── ...
│   ├── amc_backend/            # Django project config (settings, urls, asgi, worker)
│   ├── amc_cogs/               # Discord cog modules
│   ├── amc_finance/            # Finance subsystem
│   ├── necesse/                # Necesse game integration
│   ├── manage.py               # Django management entry point
│   └── static/                 # Collected static files
├── packages/scripts/           # uv workspace member — utility scripts
├── flake.nix                   # Nix flake (uv2nix, NixOS modules, checks)
├── pyproject.toml              # Project config, dependencies, entry points
└── uv.lock                     # Locked dependencies (source of truth for Nix)
```

## Dependencies

This project uses **uv** for Python dependency management and **uv2nix** to translate the lock file into Nix packages.

### Adding or updating dependencies

1. Edit `pyproject.toml` — add/change entries in `[project].dependencies` or `[dependency-groups]`.
2. Run **`uv lock`** to regenerate `uv.lock`. This is **required** — the Nix flake reads `uv.lock` directly via `uv2nix.lib.workspace.loadWorkspace`, so the lock file must be in sync.
3. Commit both `pyproject.toml` and `uv.lock`.

```bash
# Add a dependency
uv add some-package

# Update all dependencies
uv lock --upgrade

# Or after manually editing pyproject.toml
uv lock
```

**Do not skip `uv lock`** — Nix builds will fail or use stale dependencies if `uv.lock` is out of date.

### Workspace

The project defines a uv workspace with one member:

- `packages/scripts` — standalone utility scripts (e.g. `dummy_server`, `ingest_logs`)

## Entry Points

| Command       | Module                    | Description                     |
|---------------|---------------------------|---------------------------------|
| `amc-manage`  | `manage:main`             | Django management commands      |

Runtime entry points (defined in NixOS module, not pyproject.toml):
- **uvicorn** — `amc_backend.asgi:application` (API server)
- **arq** — `amc_backend.worker.WorkerSettings` (job queue + Discord bot)

## Testing

Tests are located in `src/amc/test_*.py` and use **pytest** with `pytest-django` and `pytest-asyncio`. They require a **PostgreSQL** database (with PostGIS) and **Redis**.

### Option 1: Nix flake check (recommended, no local DB needed)

The flake's `checks.pytest` spins up a temporary PostgreSQL + PostGIS and Redis in a sandbox, runs migrations, then executes pytest:

```bash
nix flake check .#pytest
```

This is the cleanest way to run tests — it guarantees a consistent environment with no leftover state.

Available flake checks:
| Check             | Description                              |
|-------------------|------------------------------------------|
| `checks.pytest`   | Full test suite with temp Postgres+Redis |
| `checks.ruff`     | Linting via ruff                         |
| `checks.pyrefly`  | Type checking via pyrefly                |
| `checks.django-check` | `manage.py check` validation         |

### Option 2: Local pytest via devShell (recommended for development)

The `.envrc` configures **direnv** with `layout_postgres()`, which creates a local PostgreSQL instance under `.direnv/postgres/`. This is the preferred way to run tests during development.

```bash
# Use direnv exec to leverage the cached nix devShell (much faster than nix develop)
direnv exec . bash -c '
  export PGHOST="$PWD/.direnv/postgres"
  export DJANGO_SETTINGS_MODULE=amc_backend.settings
  export REDIS_PORT=6379

  # Start postgres if not running
  pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" start

  # Run migrations
  python src/manage.py migrate

  # Run tests
  python -m pytest src/amc/test_criminals.py -v --tb=short
'
```

Key details:
- PostgreSQL listens on a **Unix socket** at `.direnv/postgres/.s.PGSQL.5432` — set `PGHOST` to the directory path, not `localhost`
- The `amc` database is created automatically by `layout_postgres()` on first init
- Redis is **not required** for most tests — set `REDIS_PORT=6379` anyway (tests that need it mock the connection)
- Start postgres with `pg_ctl -D "$PGDATA" start` and stop with `pg_ctl -D "$PGDATA" stop`

### Option 3: Local pytest (external database)

If you have a separate PostgreSQL and Redis running:

```bash
export DJANGO_SETTINGS_MODULE=amc_backend.settings
export PGHOST=localhost PGPORT=5432 PGUSER=youruser DB_NAME=amc
export REDIS_PORT=6379
# Set GEOS_LIBRARY_PATH and GDAL_LIBRARY_PATH if using PostGIS

# Run migrations first
python src/manage.py migrate

# Run tests
python -m pytest src/ --tb=short -q
```

### Django test runner

The `pyproject.toml` also defines a taskipy task for Django's built-in test runner:

```bash
uv run task test
```

This uses `django-admin test` and expects `DJANGO_SETTINGS_MODULE` to be set.

## Building

```bash
# Python package (includes uvicorn, arq, django-admin)
nix build .#default

# Static files (collectstatic)
nix build .#staticRoot
```

## Development Shell

The devShell is managed by **direnv** via `.envrc`. It activates automatically when you `cd` into the directory. To run commands inside the devShell:

```bash
# Preferred: use direnv exec (uses cached environment, fast)
direnv exec . <command>

# Or drop into a shell with the environment loaded
direnv exec . bash
```

Do not use `nix develop` directly — it re-evaluates the flake each time and is slow. `direnv exec` uses the cached devShell environment.

This provides: `uv`, PostgreSQL with PostGIS, Redis, `gettext`, `ruff`, `pyrefly`, `pre-commit`, and the editable Python environment.

The shell sets `UV_NO_SYNC=1` and `UV_PYTHON` to prevent uv from managing Python downloads. The `PYTHONPATH` is unset in the shell hook — the editable virtualenv handles imports via `REPO_ROOT`.

## Deployment

Deployed to the `amc-backend` NixOS container on `asean-mt-server`:

```bash
direnv exec . deploy root@asean-mt-server
```

From the monorepo root. The deploy script uses `--override-input amc-backend ./amc-backend` to use the local checkout.
