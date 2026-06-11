from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_NAME = "Tdarr Dashboard"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("TDARR_DB_PATH", "/data/database.db")
DB_IMMUTABLE = os.getenv("TDARR_DB_IMMUTABLE", "").strip().lower() in {"1", "true", "yes", "on"}
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "20"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "45"))
RECENT_TRANSCODE_SAMPLE = int(os.getenv("RECENT_TRANSCODE_SAMPLE", "100"))
TDARR_UI_URL = os.getenv("TDARR_UI_URL", "http://localhost:8265")
EFFICIENT_CODECS = {"hevc", "av1", "vp9"}

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class SnapshotCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.expires_at = 0.0
        self.payload: dict[str, Any] | None = None


cache = SnapshotCache()


def current_asset_version() -> str:
    asset_paths = [
        BASE_DIR / "static" / "style.css",
        BASE_DIR / "static" / "app.js",
        BASE_DIR / "static" / "favicon.png",
        BASE_DIR / "static" / "main-screen-logo.png",
    ]
    mtimes = [path.stat().st_mtime for path in asset_paths if path.exists()]
    return str(int(max(mtimes))) if mtimes else "1"


def build_db_uri() -> str:
    if DB_PATH.startswith("file:"):
        db_uri = DB_PATH
        if DB_IMMUTABLE and "immutable=" not in db_uri:
            separator = "&" if "?" in db_uri else "?"
            db_uri = f"{db_uri}{separator}immutable=1"
    else:
        db_uri = f"file:{DB_PATH}?mode=ro"
        if DB_IMMUTABLE:
            db_uri = f"{db_uri}&immutable=1"

    return db_uri


def resolve_db_filesystem_path() -> Path:
    if DB_PATH.startswith("file:"):
        parsed = urlsplit(DB_PATH)
        return Path(unquote(parsed.path))
    return Path(DB_PATH)


def connect_db() -> sqlite3.Connection:
    db_uri = build_db_uri()
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def union_duration_ms(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    start, end = ordered[0]
    for current_start, current_end in ordered[1:]:
        if current_start <= end:
            end = max(end, current_end)
        else:
            total += end - start
            start, end = current_start, current_end
    total += end - start
    return total


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return numerator / denominator


def format_eta_label(hours: float | None) -> str:
    if hours is None:
        return "Unavailable"
    total_minutes = int(round(hours * 60))
    days, remainder = divmod(total_minutes, 1440)
    hours_part, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours_part:
        parts.append(f"{hours_part}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def fetch_statistics(cur: sqlite3.Cursor) -> dict[str, Any]:
    row = cur.execute(
        "SELECT json_data FROM statisticsjsondb WHERE id = 'statistics'"
    ).fetchone()
    data = parse_json(row["json_data"]) if row else {}
    return {
        "totalFileCount": int(data.get("totalFileCount", 0)),
        "totalTranscodeCount": int(data.get("totalTranscodeCount", 0)),
        "totalHealthCheckCount": int(data.get("totalHealthCheckCount", 0)),
        "savedGiB": round(float(data.get("sizeDiff", 0.0)), 2),
        "tdarrScore": data.get("tdarrScore"),
        "healthCheckScore": data.get("healthCheckScore"),
        "dbLoadStatus": data.get("DBLoadStatus"),
        "dbQueue": int(data.get("DBQueue", 0)),
    }


def fetch_global_settings(cur: sqlite3.Cursor) -> dict[str, Any]:
    row = cur.execute(
        "SELECT json_data FROM settingsglobaljsondb WHERE id = 'globalsettings'"
    ).fetchone()
    data = parse_json(row["json_data"]) if row else {}
    return {
        "stagedFileLimit": int(data.get("stagedFileLimit", 0)),
        "healthcheckWorkerLimit": int(data.get("healthcheckWorkerLimit", 0)),
        "transcodeWorkerLimit": int(data.get("transcodeWorkerLimit", 0)),
        "prioritiseHealthChecks": bool(data.get("prioritiseHealthChecks", False)),
        "prioritiseTranscodes": bool(data.get("prioritiseTranscodes", False)),
        "pauseAllNodes": bool(data.get("pauseAllNodes", False)),
        "nodePriority": bool(data.get("nodePriority", False)),
    }


def fetch_library_map(cur: sqlite3.Cursor) -> dict[str, dict[str, Any]]:
    libraries: dict[str, dict[str, Any]] = {}
    for row in cur.execute("SELECT id, json_data FROM librarysettingsjsondb ORDER BY id"):
        data = parse_json(row["json_data"])
        libraries[row["id"]] = {
            "id": row["id"],
            "name": data.get("name", row["id"]),
            "folder": data.get("folder", ""),
            "cache": data.get("cache", ""),
            "container": data.get("container", ""),
        }
    return libraries


def fetch_queue_summary(
    cur: sqlite3.Cursor, libraries: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    transcode_rows = cur.execute(
        """
        SELECT
            db,
            COALESCE(json_extract(json_data, '$.video_codec_name'), '') AS codec,
            COALESCE(json_extract(json_data, '$.file_size'), 0) AS file_size_mib,
            COALESCE(json_extract(json_data, '$.duration'), 0) AS duration_seconds
        FROM filejsondb
        WHERE transcode_decision_maker = 'Queued'
        """
    ).fetchall()

    health_totals = cur.execute(
        """
        SELECT
            COUNT(*) AS count,
            COALESCE(SUM(COALESCE(json_extract(json_data, '$.file_size'), 0)), 0) AS file_size_mib,
            COALESCE(SUM(COALESCE(json_extract(json_data, '$.duration'), 0)), 0) AS duration_seconds
        FROM filejsondb
        WHERE health_check = 'Queued'
        """
    ).fetchone()

    by_library: dict[str, dict[str, Any]] = {}
    codec_counter: Counter[str] = Counter()
    codec_sizes: defaultdict[str, float] = defaultdict(float)
    total_transcode_mib = 0.0
    total_transcode_hours = 0.0
    likely_compressible_mib = 0.0
    likely_compressible_count = 0
    remux_cleanup_mib = 0.0
    remux_cleanup_count = 0

    for row in transcode_rows:
        library_id = row["db"]
        codec = (row["codec"] or "unknown").lower()
        size_mib = float(row["file_size_mib"] or 0.0)
        duration_hours = float(row["duration_seconds"] or 0.0) / 3600.0
        library_name = libraries.get(library_id, {}).get("name", library_id)

        total_transcode_mib += size_mib
        total_transcode_hours += duration_hours
        codec_counter[codec] += 1
        codec_sizes[codec] += size_mib

        entry = by_library.setdefault(
            library_id,
            {
                "id": library_id,
                "name": library_name,
                "count": 0,
                "inputGiB": 0.0,
                "mediaHours": 0.0,
            },
        )
        entry["count"] += 1
        entry["inputGiB"] += size_mib / 1024.0
        entry["mediaHours"] += duration_hours

        if codec in EFFICIENT_CODECS:
            remux_cleanup_count += 1
            remux_cleanup_mib += size_mib
        else:
            likely_compressible_count += 1
            likely_compressible_mib += size_mib

    for entry in by_library.values():
        entry["inputGiB"] = round(entry["inputGiB"], 2)
        entry["mediaHours"] = round(entry["mediaHours"], 1)

    by_codec = [
        {
            "codec": codec,
            "count": count,
            "inputGiB": round(codec_sizes[codec] / 1024.0, 2),
            "likelyCompressible": codec not in EFFICIENT_CODECS,
        }
        for codec, count in codec_counter.most_common()
    ]

    total_health_mib = float(health_totals["file_size_mib"] or 0.0) if health_totals else 0.0
    total_health_hours = (
        float(health_totals["duration_seconds"] or 0.0) / 3600.0 if health_totals else 0.0
    )
    total_health_count = int(health_totals["count"] or 0) if health_totals else 0

    return {
        "transcodes": {
            "count": len(transcode_rows),
            "inputGiB": round(total_transcode_mib / 1024.0, 2),
            "mediaHours": round(total_transcode_hours, 1),
            "likelyCompressibleCount": likely_compressible_count,
            "likelyCompressibleGiB": round(likely_compressible_mib / 1024.0, 2),
            "remuxCleanupCount": remux_cleanup_count,
            "remuxCleanupGiB": round(remux_cleanup_mib / 1024.0, 2),
        },
        "healthChecks": {
            "count": total_health_count,
            "inputGiB": round(total_health_mib / 1024.0, 2),
            "mediaHours": round(total_health_hours, 1),
        },
        "byLibrary": sorted(
            by_library.values(), key=lambda item: item["count"], reverse=True
        ),
        "byCodec": by_codec,
    }


def fetch_staging(cur: sqlite3.Cursor) -> dict[str, Any]:
    status_rows = cur.execute(
        """
        SELECT
            COALESCE(json_extract(json_data, '$.status'), '') AS status,
            COALESCE(json_extract(json_data, '$.handling'), '') AS handling,
            COUNT(*) AS count
        FROM stagedjsondb
        GROUP BY 1, 2
        ORDER BY count DESC
        """
    ).fetchall()
    grouped = [
        {
            "status": row["status"] or "unknown",
            "handling": row["handling"] or "",
            "count": int(row["count"]),
        }
        for row in status_rows
    ]

    counts = defaultdict(int)
    for item in grouped:
        counts[item["status"]] += item["count"]

    return {
        "total": sum(item["count"] for item in grouped),
        "processing": counts["processing"],
        "queued": counts["queued"],
        "copyFailed": counts["copyFailed"],
        "accepted": counts["accepted"],
        "byStatus": grouped,
    }


def fetch_open_jobs(
    cur: sqlite3.Cursor, file_ids: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    query = "SELECT json_data FROM jobsjsondb"
    params: tuple[Any, ...] = ()
    if file_ids:
        placeholders = ",".join("?" for _ in file_ids)
        query += f" WHERE json_extract(json_data, '$.job.fileId') IN ({placeholders})"
        params = tuple(file_ids)
    query += " ORDER BY timestamp DESC"
    for row in cur.execute(query, params):
        data = parse_json(row["json_data"])
        if data.get("status"):
            continue
        job = data.get("job") or {}
        file_id = job.get("fileId")
        if file_id:
            jobs[file_id] = data
    return jobs


def fetch_processing(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    staged_rows = cur.execute(
        """
        SELECT json_data
        FROM stagedjsondb
        WHERE json_extract(json_data, '$.status') = 'processing'
        ORDER BY COALESCE(json_extract(json_data, '$.start'), timestamp)
        """
    ).fetchall()
    file_ids = [
        (parse_json(row["json_data"]).get("job") or {}).get("fileId", "")
        for row in staged_rows
    ]
    open_jobs = fetch_open_jobs(cur, [file_id for file_id in file_ids if file_id])

    items: list[dict[str, Any]] = []
    for row in staged_rows:
        data = parse_json(row["json_data"])
        job = data.get("job") or {}
        open_job = open_jobs.get(job.get("fileId", ""), {})
        node_name = (
            (open_job.get("nodeNames") or [None])[0]
            or data.get("nodeName")
            or data.get("nodeID")
            or "Unknown"
        )
        start_ms = int(data.get("start") or open_job.get("start") or 0)
        age_minutes = round((now_ms - start_ms) / 60000.0, 1) if start_ms else None
        items.append(
            {
                "file": data.get("_id", ""),
                "workerType": data.get("workerType", ""),
                "jobType": job.get("type", ""),
                "nodeName": node_name,
                "ageMinutes": age_minutes,
                "startedAt": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
                if start_ms
                else None,
            }
        )
    return items


def fetch_recent_transcodes(cur: sqlite3.Cursor, sample_size: int) -> list[dict[str, Any]]:
    return [
        parse_json(row["json_data"])
        for row in cur.execute(
            """
            SELECT json_data
            FROM jobsjsondb
            WHERE json_extract(json_data, '$.status') = 'Transcode success'
              AND json_extract(json_data, '$.job.type') = 'transcode'
              AND CAST(json_extract(json_data, '$.end') AS INTEGER)
                > CAST(json_extract(json_data, '$.start') AS INTEGER)
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (sample_size,),
        )
    ]


def fetch_file_durations(cur: sqlite3.Cursor, file_paths: list[str]) -> dict[str, float]:
    if not file_paths:
        return {}
    placeholders = ",".join("?" for _ in file_paths)
    durations: dict[str, float] = {}
    query = (
        f"SELECT id, COALESCE(json_extract(json_data, '$.duration'), 0) AS duration_seconds "
        f"FROM filejsondb WHERE id IN ({placeholders})"
    )
    for row in cur.execute(query, file_paths):
        durations[row["id"]] = float(row["duration_seconds"] or 0.0)
    return durations


def build_performance(
    queue: dict[str, Any],
    recent_transcodes: list[dict[str, Any]],
    file_durations: dict[str, float],
    current_saved_gib: float,
) -> dict[str, Any]:
    if not recent_transcodes:
        return {
            "sampleSize": 0,
            "throughputGiBPerHour": None,
            "savingsRatio": None,
            "etaHours": None,
            "etaLabel": "Unavailable",
            "etaBySizeHours": None,
            "etaByMediaHours": None,
            "completionTime": None,
            "projectedSavingsGiB": None,
            "projectedTotalSavedGiB": current_saved_gib,
            "recentActiveHours": None,
            "nodeThroughput": [],
            "recentCompletions": [],
            "lastCompletedAt": None,
        }

    intervals = [(int(row["start"]), int(row["end"])) for row in recent_transcodes]
    active_ms = union_duration_ms(intervals)
    active_hours = active_ms / 3600000.0 if active_ms else 0.0

    total_input_gib = sum(float(row.get("fileSizeStartGB") or 0.0) for row in recent_transcodes)
    total_saved_gib = sum(float(row.get("fileSizeDiffGB") or 0.0) for row in recent_transcodes)
    total_media_hours = sum(
        file_durations.get(row.get("file", ""), 0.0) / 3600.0 for row in recent_transcodes
    )

    throughput_gib_per_hour = safe_ratio(total_input_gib, active_hours)
    media_hours_per_hour = safe_ratio(total_media_hours, active_hours)
    savings_ratio = safe_ratio(total_saved_gib, total_input_gib)

    queue_input_gib = float(queue["transcodes"]["inputGiB"])
    queue_media_hours = float(queue["transcodes"]["mediaHours"])
    eta_by_size = (
        queue_input_gib / throughput_gib_per_hour if throughput_gib_per_hour else None
    )
    eta_by_media = (
        queue_media_hours / media_hours_per_hour if media_hours_per_hour else None
    )

    eta_candidates = [value for value in [eta_by_size, eta_by_media] if value]
    eta_hours = sum(eta_candidates) / len(eta_candidates) if eta_candidates else None
    completion_time = (
        (datetime.now(timezone.utc) + timedelta(hours=eta_hours)).isoformat()
        if eta_hours is not None
        else None
    )

    likely_input_gib = float(queue["transcodes"]["likelyCompressibleGiB"])
    projected_savings_gib = (
        round(likely_input_gib * savings_ratio, 2) if savings_ratio is not None else None
    )

    per_node_jobs: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in recent_transcodes:
        node_name = ((row.get("nodeNames") or ["Unknown"])[0]) or "Unknown"
        per_node_jobs[node_name].append(row)

    node_throughput: list[dict[str, Any]] = []
    for node_name, rows in per_node_jobs.items():
        node_intervals = [(int(row["start"]), int(row["end"])) for row in rows]
        node_active_hours = union_duration_ms(node_intervals) / 3600000.0
        node_input = sum(float(row.get("fileSizeStartGB") or 0.0) for row in rows)
        node_saved = sum(float(row.get("fileSizeDiffGB") or 0.0) for row in rows)
        node_throughput.append(
            {
                "nodeName": node_name,
                "jobs": len(rows),
                "inputGiB": round(node_input, 2),
                "throughputGiBPerHour": round(node_input / node_active_hours, 2)
                if node_active_hours
                else None,
                "savingsRatio": round(node_saved / node_input, 3) if node_input else None,
            }
        )

    recent_completions = [
        {
            "file": row.get("file", ""),
            "nodeName": ((row.get("nodeNames") or ["Unknown"])[0]) or "Unknown",
            "durationMinutes": round(float(row.get("duration") or 0.0) / 60000.0, 1),
            "inputGiB": round(float(row.get("fileSizeStartGB") or 0.0), 2),
            "savedGiB": round(float(row.get("fileSizeDiffGB") or 0.0), 2),
            "endedAt": datetime.fromtimestamp(int(row["end"]) / 1000, tz=timezone.utc).isoformat(),
        }
        for row in recent_transcodes[:10]
    ]

    return {
        "sampleSize": len(recent_transcodes),
        "throughputGiBPerHour": round(throughput_gib_per_hour, 2)
        if throughput_gib_per_hour
        else None,
        "mediaHoursPerHour": round(media_hours_per_hour, 2)
        if media_hours_per_hour
        else None,
        "savingsRatio": round(savings_ratio, 3) if savings_ratio is not None else None,
        "etaHours": round(eta_hours, 2) if eta_hours is not None else None,
        "etaLabel": format_eta_label(eta_hours),
        "etaBySizeHours": round(eta_by_size, 2) if eta_by_size is not None else None,
        "etaByMediaHours": round(eta_by_media, 2) if eta_by_media is not None else None,
        "completionTime": completion_time,
        "projectedSavingsGiB": projected_savings_gib,
        "projectedTotalSavedGiB": round(
            current_saved_gib + (projected_savings_gib or 0.0), 2
        ),
        "recentActiveHours": round(active_hours, 2),
        "nodeThroughput": sorted(
            node_throughput,
            key=lambda item: item["throughputGiBPerHour"] or 0.0,
            reverse=True,
        ),
        "recentCompletions": recent_completions,
        "lastCompletedAt": recent_completions[0]["endedAt"] if recent_completions else None,
    }


def fetch_nodes(
    cur: sqlite3.Cursor, processing: list[dict[str, Any]]
) -> dict[str, Any]:
    active_by_node: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item in processing:
        active_by_node[item["nodeName"]][item["workerType"]] += 1

    items: list[dict[str, Any]] = []
    configured_transcode = 0
    configured_health = 0

    for row in cur.execute("SELECT id, json_data FROM nodejsondb ORDER BY id"):
        data = parse_json(row["json_data"])
        worker_limits = data.get("workerLimits") or {}
        configured_transcode += int(worker_limits.get("transcodecpu", 0)) + int(
            worker_limits.get("transcodegpu", 0)
        )
        configured_health += int(worker_limits.get("healthcheckcpu", 0)) + int(
            worker_limits.get("healthcheckgpu", 0)
        )
        counters = active_by_node.get(row["id"], Counter())
        items.append(
            {
                "name": row["id"],
                "priority": int(data.get("priority", 0)),
                "paused": bool(data.get("nodePaused", False)),
                "scheduleEnabled": bool(data.get("scheduleEnabled", False)),
                "configured": {
                    "transcodecpu": int(worker_limits.get("transcodecpu", 0)),
                    "transcodegpu": int(worker_limits.get("transcodegpu", 0)),
                    "healthcheckcpu": int(worker_limits.get("healthcheckcpu", 0)),
                    "healthcheckgpu": int(worker_limits.get("healthcheckgpu", 0)),
                },
                "active": {
                    "transcode": counters.get("transcodecpu", 0)
                    + counters.get("transcodegpu", 0),
                    "healthcheck": counters.get("healthcheckcpu", 0)
                    + counters.get("healthcheckgpu", 0),
                },
                "gpuSelect": data.get("gpuSelect"),
            }
        )

    active_transcodes = sum(item["active"]["transcode"] for item in items)
    active_health = sum(item["active"]["healthcheck"] for item in items)

    return {
        "configuredTranscodeWorkers": configured_transcode,
        "configuredHealthcheckWorkers": configured_health,
        "activeTranscodeWorkers": active_transcodes,
        "activeHealthcheckWorkers": active_health,
        "items": items,
    }


def build_notes(
    queue: dict[str, Any], staging: dict[str, Any], performance: dict[str, Any]
) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    if staging["copyFailed"]:
        notes.append(
            {
                "level": "warning",
                "message": f"{staging['copyFailed']} staged item(s) are currently in copyFailed.",
            }
        )
    if queue["transcodes"]["remuxCleanupCount"]:
        notes.append(
            {
                "level": "info",
                "message": (
                    f"{queue['transcodes']['remuxCleanupCount']} queued file(s) are already "
                    "in efficient codecs (hevc/av1/vp9), so the savings projection excludes them."
                ),
            }
        )
    if performance["sampleSize"] < 25:
        notes.append(
            {
                "level": "warning",
                "message": "ETA confidence is low because there are not many recent successful transcodes to sample.",
            }
        )
    return notes


def build_snapshot() -> dict[str, Any]:
    with connect_db() as conn:
        cur = conn.cursor()
        libraries = fetch_library_map(cur)
        stats = fetch_statistics(cur)
        settings = fetch_global_settings(cur)
        queue = fetch_queue_summary(cur, libraries)
        staging = fetch_staging(cur)
        processing = fetch_processing(cur)
        recent_transcodes = fetch_recent_transcodes(cur, RECENT_TRANSCODE_SAMPLE)
        file_durations = fetch_file_durations(
            cur, [row.get("file", "") for row in recent_transcodes]
        )
        performance = build_performance(
            queue, recent_transcodes, file_durations, stats["savedGiB"]
        )
        nodes = fetch_nodes(cur, processing)

    notes = build_notes(queue, staging, performance)
    return {
        "appName": APP_NAME,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "refreshSeconds": REFRESH_SECONDS,
        "stats": {
            "totalFileCount": stats["totalFileCount"],
            "totalTranscodeCount": stats["totalTranscodeCount"],
            "totalHealthCheckCount": stats["totalHealthCheckCount"],
            "savedGiB": stats["savedGiB"],
            "tdarrScore": stats["tdarrScore"],
            "healthCheckScore": stats["healthCheckScore"],
            "dbLoadStatus": stats["dbLoadStatus"],
            "dbQueue": stats["dbQueue"],
        },
        "queue": {
            "transcodes": {"count": queue["transcodes"]["count"]},
            "healthChecks": {"count": queue["healthChecks"]["count"]},
        },
        "staging": staging,
        "nodes": {
            "configuredTranscodeWorkers": nodes["configuredTranscodeWorkers"],
            "configuredHealthcheckWorkers": nodes["configuredHealthcheckWorkers"],
            "activeTranscodeWorkers": nodes["activeTranscodeWorkers"],
            "activeHealthcheckWorkers": nodes["activeHealthcheckWorkers"],
        },
        "notes": notes,
    }


def get_snapshot() -> dict[str, Any]:
    now = time.time()
    with cache.lock:
        if cache.payload is not None and now < cache.expires_at:
            return cache.payload
        snapshot = build_snapshot()
        cache.payload = snapshot
        cache.expires_at = time.time() + CACHE_TTL_SECONDS
        return cache.payload


@app.get("/")
def index(request: Request) -> Any:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": APP_NAME,
            "refresh_seconds": REFRESH_SECONDS,
            "asset_version": current_asset_version(),
            "tdarr_ui_url": TDARR_UI_URL,
        },
    )


@app.get("/api/dashboard")
@app.get("/api/summary")
def api_summary() -> JSONResponse:
    return JSONResponse(get_snapshot())


@app.get("/health")
def health() -> JSONResponse:
    db_exists = resolve_db_filesystem_path().exists()
    payload = {
        "status": "ok" if db_exists else "degraded",
        "dbExists": db_exists,
    }
    return JSONResponse(payload, status_code=200 if db_exists else 503)
