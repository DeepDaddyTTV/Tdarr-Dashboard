# Tdarr Dashboard

`Tdarr Dashboard` is a small FastAPI app that reads your Tdarr SQLite database and turns it into a clean, live dashboard for queue pressure, completed transcodes, health checks, space savings, and worker activity.

It is designed to be:

- lightweight
- read-only against the Tdarr database
- easy to run with `docker compose`
- safe to publish, with generic defaults and no environment-specific paths baked into the repo

## What It Shows

- Queue count
- Processed transcodes
- Error count
- Space saved
- Workflow telemetry
- Tdarr and health scores
- Total files, total transcodes, health checks, DB queue, DB load, and last sync time
- A direct `Open Tdarr` button back to your Tdarr UI

## Image

Pull the official image from GHCR:

```bash
docker pull ghcr.io/deepdaddyttv/tdarr-dashboard:latest
```

## Quick Start

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and set `TDARR_DATABASE_FILE` to the full path of your Tdarr `database.db` file.

3. Start the dashboard:

```bash
docker compose up -d
```

4. Open:

```text
http://localhost:8270
```

## Docker Compose

The repo includes a generic `compose.yml` that uses the official image and only requires a path to your Tdarr database file.

Typical Tdarr database path examples:

- `/path/to/Tdarr/DB2/SQL/database.db`
- `/opt/tdarr/server/Tdarr/DB2/SQL/database.db`
- `/srv/tdarr/Tdarr/DB2/SQL/database.db`

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TDARR_DATABASE_FILE` | required in `.env` | Host path to Tdarr `database.db` |
| `TDARR_DASHBOARD_PORT` | `8270` | Host port for the dashboard |
| `TDARR_DB_PATH` | `/data/database.db` | In-container path to the mounted SQLite file |
| `TDARR_UI_URL` | `http://localhost:8265` | URL opened by the `Open Tdarr` button |
| `TDARR_DB_IMMUTABLE` | `true` | Opens the database in immutable mode when possible |
| `REFRESH_SECONDS` | `20` | Frontend refresh interval |
| `CACHE_TTL_SECONDS` | `45` | Server-side snapshot cache time |
| `RECENT_TRANSCODE_SAMPLE` | `100` | Number of recent successful transcodes used for scoring |

## Local Development

Run it without Docker:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TDARR_DB_PATH=/path/to/database.db
uvicorn app.main:app --host 0.0.0.0 --port 8270
```

## Health Endpoint

The container exposes a lightweight health check endpoint:

```text
/health
```

It reports whether the mounted database file is reachable, without exposing internal filesystem paths.

## Theme Support

The dashboard includes both dark mode and light mode, with a built-in theme toggle that remembers your preference in the browser.

## Publishing

This repository includes a GitHub Actions workflow that publishes:

- `ghcr.io/deepdaddyttv/tdarr-dashboard:latest` on pushes to `main`
- version tags for `v*` git tags

## Notes

- The dashboard reads Tdarr data only. It does not write back to Tdarr.
- Mount the database file read-only.
- If your Tdarr UI is on a different host or port, set `TDARR_UI_URL` in `.env`.
