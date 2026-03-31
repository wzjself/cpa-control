from __future__ import annotations

import json
import os
import urllib.parse
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import io
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, render_template, request, Response
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "serverhub.db"
CPA_WARDEN_DIR = Path("/root/cpa-warden")
CLIRELAY_DB = Path("/opt/clirelay/data/usage.db")
CLIRELAY_BASE = os.environ.get("CLIRELAY_BASE", "http://127.0.0.1:8317")
CLIRELAY_MGMT_KEY = os.environ.get("CLIRELAY_MGMT_KEY", "wzjself")
SAMPLE_INTERVAL = 60
RETENTION_DAYS = 30
PORT = int(os.environ.get("SERVERHUB_PORT", "8321"))
HOST = os.environ.get("SERVERHUB_HOST", "0.0.0.0")
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, template_folder="templates", static_folder="static")
stop_event = threading.Event()
quota_probe_cache: dict[str, dict[str, Any]] = {}
QUOTA_CACHE_TTL_SECONDS = 60
clirelay_summary_cache: dict[str, Any] = {'data': None, 'cached_at_ts': 0.0}
CLIRELAY_CACHE_TTL_SECONDS = 1.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


def mgmt_headers(token: str, include_json: bool = False) -> dict[str, str]:
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json, text/plain, */*',
    }
    if include_json:
        headers['Content-Type'] = 'application/json'
    return headers


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metric_samples (
            ts TEXT PRIMARY KEY,
            cpu_percent REAL,
            mem_percent REAL,
            disk_percent REAL,
            net_rx_mb REAL,
            net_tx_mb REAL,
            load_1 REAL,
            load_5 REAL,
            load_15 REAL
        );

        CREATE TABLE IF NOT EXISTS cpa_targets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            token TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'codex',
            sort_order INTEGER NOT NULL DEFAULT 0,
            expanded INTEGER NOT NULL DEFAULT 0,
            keepalive_enabled INTEGER NOT NULL DEFAULT 0,
            keepalive_days INTEGER NOT NULL DEFAULT 15,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS credential_store (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            uploaded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT,
            last_target_id TEXT,
            uploaded_to_cpa INTEGER NOT NULL DEFAULT 0,
            upload_status_text TEXT NOT NULL DEFAULT '',
            upload_error_detail TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    cpa_cols = [r[1] for r in conn.execute("PRAGMA table_info(cpa_targets)").fetchall()]
    if 'sort_order' not in cpa_cols: conn.execute("ALTER TABLE cpa_targets ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    if 'expanded' not in cpa_cols: conn.execute("ALTER TABLE cpa_targets ADD COLUMN expanded INTEGER NOT NULL DEFAULT 0")
    if 'keepalive_enabled' not in cpa_cols: conn.execute("ALTER TABLE cpa_targets ADD COLUMN keepalive_enabled INTEGER NOT NULL DEFAULT 0")
    if 'keepalive_days' not in cpa_cols: conn.execute(f"ALTER TABLE cpa_targets ADD COLUMN keepalive_days INTEGER NOT NULL DEFAULT {KEEPALIVE_DEFAULT_DAYS}")
    conn.execute("UPDATE cpa_targets SET sort_order = rowid WHERE sort_order = 0 OR sort_order IS NULL")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(credential_store)").fetchall()]
    if 'uploaded_to_cpa' not in cols: conn.execute("ALTER TABLE credential_store ADD COLUMN uploaded_to_cpa INTEGER NOT NULL DEFAULT 0")
    if 'upload_status_text' not in cols: conn.execute("ALTER TABLE credential_store ADD COLUMN upload_status_text TEXT NOT NULL DEFAULT ''")
    if 'upload_error_detail' not in cols: conn.execute("ALTER TABLE credential_store ADD COLUMN upload_error_detail TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def cleanup_old_metrics() -> None:
    cutoff = (utc_now() - timedelta(days=RETENTION_DAYS)).isoformat()
    conn = get_conn()
    conn.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()


def read_proc_stat() -> tuple[list[int], int]:
    with open('/proc/stat', 'r', encoding='utf-8') as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + vals[4]
    total = sum(vals)
    return vals, total - idle


def cpu_percent_sample(interval: float = 0.15) -> float:
    _, busy1 = read_proc_stat()
    with open('/proc/stat', 'r', encoding='utf-8') as f:
        parts1 = [int(x) for x in f.readline().split()[1:]]
    total1 = sum(parts1)
    idle1 = parts1[3] + parts1[4]
    time.sleep(interval)
    with open('/proc/stat', 'r', encoding='utf-8') as f:
        parts2 = [int(x) for x in f.readline().split()[1:]]
    total2 = sum(parts2)
    idle2 = parts2[3] + parts2[4]
    dt = max(total2 - total1, 1)
    didle = idle2 - idle1
    return round(max(0.0, min(100.0, 100.0 * (dt - didle) / dt)), 2)


def mem_percent() -> float:
    info = {}
    with open('/proc/meminfo', 'r', encoding='utf-8') as f:
        for line in f:
            key, val = line.split(':', 1)
            info[key] = int(val.strip().split()[0])
    total = info.get('MemTotal', 1)
    available = info.get('MemAvailable', 0)
    used = total - available
    return round(used * 100 / total, 2)


def disk_percent(path: str = '/') -> float:
    usage = shutil.disk_usage(path)
    return round(usage.used * 100 / max(usage.total, 1), 2)


def net_mb() -> tuple[float, float]:
    with open('/proc/net/dev', 'r', encoding='utf-8') as f:
        lines = f.readlines()[2:]
    rx = tx = 0
    for line in lines:
        iface, data = line.split(':', 1)
        iface = iface.strip()
        if iface == 'lo':
            continue
        parts = data.split()
        rx += int(parts[0])
        tx += int(parts[8])
    return round(rx / 1024 / 1024, 2), round(tx / 1024 / 1024, 2)


def top_processes(limit: int = 5) -> list[dict[str, Any]]:
    cmd = "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 6"
    out = subprocess.check_output(['sh', '-lc', cmd], text=True)
    rows = []
    for line in out.strip().splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append({
                'pid': int(parts[0]),
                'name': parts[1],
                'cpu': float(parts[2]),
                'mem': float(parts[3]),
            })
    return rows[:limit]


def compute_health(sample: dict[str, Any], clirelay: dict[str, Any] | None = None) -> int:
    relay_health = (clirelay or {}).get('health_score')
    if relay_health is not None:
        return int(relay_health)
    system = (clirelay or {}).get('system') or {}
    cpu = float(system.get('system_cpu_pct', sample.get('cpu_percent', 0.0)) or 0.0)
    mem = float(system.get('system_mem_pct', sample.get('mem_percent', 0.0)) or 0.0)
    disk = float(system.get('disk_pct', sample.get('disk_percent', 0.0)) or 0.0)
    raw = 100 - (cpu * 0.35 + mem * 0.35 + disk * 0.30)
    return int(max(0, min(100, round(raw))))


def collect_sample() -> dict[str, Any]:
    rx_mb, tx_mb = net_mb()
    load1, load5, load15 = os.getloadavg()
    return {
        'ts': now_iso(),
        'cpu_percent': cpu_percent_sample(),
        'mem_percent': mem_percent(),
        'disk_percent': disk_percent('/'),
        'net_rx_mb': rx_mb,
        'net_tx_mb': tx_mb,
        'load_1': round(load1, 2),
        'load_5': round(load5, 2),
        'load_15': round(load15, 2),
    }


def store_sample(sample: dict[str, Any]) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO metric_samples
        (ts, cpu_percent, mem_percent, disk_percent, net_rx_mb, net_tx_mb, load_1, load_5, load_15)
        VALUES (:ts, :cpu_percent, :mem_percent, :disk_percent, :net_rx_mb, :net_tx_mb, :load_1, :load_5, :load_15)
        """,
        sample,
    )
    conn.commit()
    conn.close()


def sampler_loop() -> None:
    while not stop_event.is_set():
        try:
            store_sample(collect_sample())
            cleanup_old_metrics()
        except Exception as exc:
            print('sampler error:', exc, flush=True)
        stop_event.wait(SAMPLE_INTERVAL)


def get_metric_history(hours: int | None = 24) -> list[dict[str, Any]]:
    conn = get_conn()
    if hours is None:
        rows = [dict(r) for r in conn.execute("SELECT * FROM metric_samples ORDER BY ts").fetchall()]
    else:
        since = (utc_now() - timedelta(hours=hours)).isoformat()
        rows = [dict(r) for r in conn.execute("SELECT * FROM metric_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()]
    conn.close()
    return rows


def get_metric_summary(history_hours: int | None = 24, clirelay: dict[str, Any] | None = None) -> dict[str, Any]:
    history = get_metric_history(history_hours)
    latest = history[-1] if history else collect_sample()
    totals = {'rx_mb': 0.0, 'tx_mb': 0.0}
    if history:
        totals['rx_mb'] = round(history[-1]['net_rx_mb'] - history[0]['net_rx_mb'], 2)
        totals['tx_mb'] = round(history[-1]['net_tx_mb'] - history[0]['net_tx_mb'], 2)
    current_rx_kbps = 0.0
    current_tx_kbps = 0.0
    if len(history) >= 2:
        prev, cur = history[-2], history[-1]
        prev_ts = datetime.fromisoformat(prev['ts']).timestamp()
        cur_ts = datetime.fromisoformat(cur['ts']).timestamp()
        seconds = max(cur_ts - prev_ts, 1)
        current_rx_kbps = round(max(0.0, (float(cur['net_rx_mb']) - float(prev['net_rx_mb'])) * 1024 / seconds), 2)
        current_tx_kbps = round(max(0.0, (float(cur['net_tx_mb']) - float(prev['net_tx_mb'])) * 1024 / seconds), 2)
    return {
        'latest': latest,
        'health': compute_health(latest, clirelay),
        'top_processes': top_processes(),
        'traffic_24h': totals,
        'current_net_kbps': {'rx': current_rx_kbps, 'tx': current_tx_kbps},
        'history': history,
    }


def get_clirelay_summary(force: bool = False) -> dict[str, Any]:
    now_ts = time.time()
    cached = clirelay_summary_cache.get('data')
    cached_at_ts = float(clirelay_summary_cache.get('cached_at_ts') or 0)
    if (not force) and cached is not None and (now_ts - cached_at_ts) < CLIRELAY_CACHE_TTL_SECONDS:
        return dict(cached)

    try:
        import requests  # type: ignore
        headers = {'x-management-key': CLIRELAY_MGMT_KEY}
        dash = requests.get(f'{CLIRELAY_BASE}/v0/management/dashboard-summary?days=7', headers=headers, timeout=3)
        chart = requests.get(f'{CLIRELAY_BASE}/v0/management/usage/chart-data?days=7', headers=headers, timeout=3)
        system = requests.get(f'{CLIRELAY_BASE}/v0/management/system-stats', headers=headers, timeout=3)
        auth_files = requests.get(f'{CLIRELAY_BASE}/v0/management/auth-files', headers=headers, timeout=3)
        if dash.ok and chart.ok and system.ok:
            dash_j = dash.json()
            chart_j = chart.json()
            sys_j = system.json()
            auth_j = auth_files.json() if auth_files.ok else {'files': []}
            kpi = dash_j.get('kpi', {})
            active = sys_j.get('active_concurrency', []) or []
            rpm_now = sum(int(x.get('rpm', 0) or 0) for x in active)
            tpm_now = sum(int(x.get('tpm', 0) or 0) for x in active)
            sys_cpu = float(sys_j.get('system_cpu_pct', 0) or 0)
            sys_mem = float(sys_j.get('system_mem_pct', 0) or 0)
            sys_disk = float(sys_j.get('disk_pct', 0) or 0)
            health_score = int(max(0, min(100, round(100 - (sys_cpu * 0.35 + sys_mem * 0.35 + sys_disk * 0.30)))))
            result = {
                'available': True,
                'source': 'management-api',
                'request_count': int(kpi.get('total_requests', 0) or 0),
                'success_rate': round(kpi.get('success_rate', 0), 2),
                'input_tokens': int(kpi.get('input_tokens', 0) or 0),
                'output_tokens': int(kpi.get('output_tokens', 0) or 0),
                'cached_tokens': int(kpi.get('cached_tokens', 0) or 0),
                'total_tokens': int(kpi.get('total_tokens', 0) or 0),
                'rpm': rpm_now,
                'tpm': tpm_now,
                'health_score': health_score,
                'top_api_keys': chart_j.get('apikey_distribution', []),
                'recent': [
                    {'minute': x.get('date', ''), 'requests': x.get('requests', 0), 'tokens': (x.get('input_tokens', 0) + x.get('output_tokens', 0))}
                    for x in chart_j.get('daily_series', [])
                ],
                'system': sys_j,
                'counts': dash_j.get('counts', {}),
                'auth_files': auth_j.get('files', []),
                'models': chart_j.get('model_distribution', []),
            }
            clirelay_summary_cache['data'] = dict(result)
            clirelay_summary_cache['cached_at_ts'] = now_ts
            return result
    except Exception:
        pass

    if not CLIRELAY_DB.exists():
        result = {'available': False, 'request_count': 0, 'total_tokens': 0, 'rpm': 0, 'tpm': 0}
        clirelay_summary_cache['data'] = dict(result)
        clirelay_summary_cache['cached_at_ts'] = now_ts
        return result
    conn = sqlite3.connect(CLIRELAY_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    total = dict(cur.execute(
        """
        SELECT COUNT(*) AS request_count,
               SUM(CASE WHEN failed = 0 THEN 1 ELSE 0 END) AS success_count,
               SUM(COALESCE(input_tokens,0)) AS input_tokens,
               SUM(COALESCE(output_tokens,0)) AS output_tokens,
               SUM(COALESCE(cached_tokens,0)) AS cached_tokens,
               SUM(COALESCE(total_tokens,0)) AS total_tokens
        FROM request_logs
        """
    ).fetchone())
    rpm = dict(cur.execute(
        "SELECT COUNT(*) AS rpm, SUM(COALESCE(total_tokens,0)) AS tpm FROM request_logs WHERE timestamp >= datetime('now','-1 minute')"
    ).fetchone())
    by_key = [dict(r) for r in cur.execute(
        """
        SELECT COALESCE(api_key_name,'未命名') AS api_key_name,
               COUNT(*) AS requests,
               SUM(COALESCE(total_tokens,0)) AS total_tokens,
               SUM(CASE WHEN failed = 0 THEN 1 ELSE 0 END) AS success_count
        FROM request_logs
        GROUP BY api_key_name
        ORDER BY requests DESC
        LIMIT 10
        """
    ).fetchall()]
    recent = [dict(r) for r in cur.execute(
        """
        SELECT substr(timestamp,1,16) AS minute,
               COUNT(*) AS requests,
               SUM(COALESCE(total_tokens,0)) AS tokens
        FROM request_logs
        WHERE timestamp >= datetime('now','-24 hours')
        GROUP BY substr(timestamp,1,16)
        ORDER BY minute
        """
    ).fetchall()]
    conn.close()
    request_count = total.get('request_count') or 0
    success_count = total.get('success_count') or 0
    result = {
        'available': True,
        'source': 'usage-db',
        'request_count': request_count,
        'success_rate': round((success_count / request_count * 100.0), 2) if request_count else 0,
        'input_tokens': total.get('input_tokens') or 0,
        'output_tokens': total.get('output_tokens') or 0,
        'cached_tokens': total.get('cached_tokens') or 0,
        'total_tokens': total.get('total_tokens') or 0,
        'rpm': rpm.get('rpm') or 0,
        'tpm': rpm.get('tpm') or 0,
        'top_api_keys': by_key,
        'recent': recent,
    }
    clirelay_summary_cache['data'] = dict(result)
    clirelay_summary_cache['cached_at_ts'] = now_ts
    return result


def get_cpa_usage_stats(target: dict[str, Any]) -> dict[str, Any]:
    if not CLIRELAY_DB.exists():
        return {'request_count': 0, 'total_tokens': 0, 'matched_by': ''}
    conn = sqlite3.connect(CLIRELAY_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    candidates = [str(target.get('name') or '').strip()]
    request_count = 0
    total_tokens = 0
    matched_by = ''
    for field in ('channel_name', 'api_key_name'):
        for value in candidates:
            if not value:
                continue
            row = cur.execute(
                f"SELECT COUNT(*) AS request_count, COALESCE(SUM(total_tokens), 0) AS total_tokens FROM request_logs WHERE {field} = ?",
                (value,),
            ).fetchone()
            if row and ((row['request_count'] or 0) > 0 or (row['total_tokens'] or 0) > 0):
                request_count = int(row['request_count'] or 0)
                total_tokens = int(row['total_tokens'] or 0)
                matched_by = f'{field}:{value}'
                conn.close()
                return {'request_count': request_count, 'total_tokens': total_tokens, 'matched_by': matched_by}
    conn.close()
    return {'request_count': 0, 'total_tokens': 0, 'matched_by': ''}


def load_cpas() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cpa_targets ORDER BY sort_order ASC, created_at DESC").fetchall()]
    conn.close()
    return rows


def normalize_credential_name(value: str) -> str:
    raw = str(value or '').strip().lower()
    for suffix in ('.json', '.txt'):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break
    return raw


def list_credentials() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM credential_store WHERE archived = 0 ORDER BY uploaded_at DESC").fetchall()]
    conn.close()
    return rows


def credential_dedupe_key(item: dict[str, Any]) -> str:
    return normalize_credential_name(str(item.get('name') or item.get('filename') or ''))


def cleanup_duplicate_credentials(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own_conn = False
    if conn is None:
        conn = get_conn()
        own_conn = True
    rows = [dict(r) for r in conn.execute("SELECT * FROM credential_store WHERE archived = 0 ORDER BY uploaded_at DESC, updated_at DESC, id DESC").fetchall()]
    keep_seen = set()
    removed_ids = []
    now = now_iso()
    for row in rows:
        key = credential_dedupe_key(row)
        if not key:
            continue
        if key in keep_seen:
            conn.execute('UPDATE credential_store SET archived = 1, updated_at = ? WHERE id = ?', (now, row['id']))
            removed_ids.append(row['id'])
        else:
            keep_seen.add(key)
    if own_conn:
        conn.commit()
        conn.close()
    return {'removed': len(removed_ids), 'removed_ids': removed_ids}


def build_credential_cpa_presence(cpas: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    presence: dict[str, list[dict[str, Any]]] = {}
    for cpa in cpas:
        snapshot = load_cpa_snapshot(str(cpa.get('id') or '')) or {}
        accounts = snapshot.get('accounts') or []
        for acc in accounts:
            key = normalize_credential_name(acc.get('email') or acc.get('name') or '')
            if not key:
                continue
            if acc.get('invalid_401'):
                status_text = '401失效'
                good = False
            elif acc.get('quota_limited') or str(acc.get('status') or '').lower() == 'active':
                status_text = '可用 / 限额'
                good = True
            else:
                status_text = '异常'
                good = False
            presence.setdefault(key, []).append({
                'cpa_id': cpa.get('id'),
                'cpa_name': cpa.get('name'),
                'status_text': status_text,
                'good': good,
                'snapshot_saved_at': snapshot.get('snapshot_saved_at'),
            })
    return presence


def cpa_config_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}.json'


def cpa_db_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}.sqlite3'


def cpa_invalid_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}_401.json'


def cpa_quota_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}_quota.json'


def cpa_log_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}.log'


def cpa_snapshot_path(cpa_id: str) -> Path:
    return DATA_DIR / f'cpa_{cpa_id}_snapshot.json'


def load_cpa_snapshot(cpa_id: str) -> dict[str, Any] | None:
    path = cpa_snapshot_path(cpa_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_cpa_snapshot(cpa_id: str, summary: dict[str, Any]) -> None:
    payload = dict(summary)
    payload['snapshot_saved_at'] = now_iso()
    cpa_snapshot_path(cpa_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def write_cpa_config(target: dict[str, Any]) -> None:
    conf = {
        'base_url': target['base_url'],
        'token': target['token'],
        'target_type': target.get('provider', 'codex'),
        'provider': '',
        'probe_workers': 50,
        'action_workers': 20,
        'timeout': 15,
        'retries': 2,
        'delete_retries': 1,
        'quota_action': 'none',
        'invalid_401_action': 'none',
        'delete_401': False,
        'auto_reenable': False,
        'reenable_scope': 'signal',
        'db_path': str(cpa_db_path(target['id'])),
        'invalid_output': str(cpa_invalid_path(target['id'])),
        'quota_output': str(cpa_quota_path(target['id'])),
        'log_file': str(cpa_log_path(target['id'])),
        'debug': False,
    }
    cpa_config_path(target['id']).write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding='utf-8')


def scan_cpa(target: dict[str, Any]) -> dict[str, Any]:
    write_cpa_config(target)
    cmd = [
        '/root/.local/bin/uv', 'run', 'python', 'cpa_warden.py',
        '--config', str(cpa_config_path(target['id'])), '--mode', 'scan'
    ]
    proc = subprocess.run(cmd, cwd=str(CPA_WARDEN_DIR), capture_output=True, text=True, timeout=300)
    return {'returncode': proc.returncode, 'stdout': proc.stdout[-4000:], 'stderr': proc.stderr[-4000:]}


def native_refresh_cpa(target: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    refreshed_at = now_iso()
    warden_map = load_cpa_warden_accounts(target)
    t0 = time.time()
    raw_files = fetch_cpa_auth_files(target)
    t1 = time.time()
    hydrated = hydrate_live_quota(target, raw_files)
    t2 = time.time()
    live_accounts = [classify_cpa_file(raw) for raw in hydrated]
    accounts = merge_cpa_accounts(live_accounts, warden_map, include_warden_only=False)
    for acc in accounts:
        acc['refreshed_at'] = refreshed_at
    summary = {
        'id': target['id'], 'name': target['name'], 'base_url': target['base_url'], 'provider': target['provider'],
        'sort_order': int(target.get('sort_order') or 0),
        'expanded': bool(target.get('expanded')),
        'total': 0, 'invalid_401': 0, 'quota_limited': 0, 'abnormal': 0, 'disabled': 0, 'healthy': 0,
        'used_ratio': None, 'remaining_ratio': None, 'accounts': [],
        'last_run': {
            'source': 'native-management-auth-files+warden-db',
            'count': len(accounts),
            'live_count': len(live_accounts),
            'warden_count': len(warden_map),
        },
    }
    accounts.sort(key=lambda r: (
        0 if r.get('invalid_401') else 1 if r.get('quota_limited') else 2 if r.get('disabled') else 3,
        float(r.get('remaining_ratio') or 0),
        str(r.get('email') or r.get('name') or ''),
    ))
    usage_stats = get_cpa_usage_stats(target)
    summary['request_count'] = usage_stats['request_count']
    summary['total_tokens'] = usage_stats['total_tokens']
    summary['usage_matched_by'] = usage_stats['matched_by']
    if accounts:
        summary['total'] = len(accounts)
        summary['invalid_401'] = sum(1 for r in accounts if r.get('invalid_401'))
        summary['quota_limited'] = sum(1 for r in accounts if r.get('quota_limited'))
        summary['abnormal'] = sum(1 for r in accounts if (not r.get('invalid_401')) and (not r.get('quota_limited')) and str(r.get('status') or '').lower() in {'error', 'exception', 'abnormal', 'unknown'})
        summary['disabled'] = sum(1 for r in accounts if r.get('disabled'))
        summary['healthy'] = sum(1 for r in accounts if not r.get('invalid_401') and not r.get('quota_limited') and not r.get('disabled') and str(r.get('status') or '').lower() not in {'error', 'exception', 'abnormal', 'unknown'})
        remaining_values = [float(r.get('remaining_ratio')) for r in accounts if r.get('remaining_ratio') is not None]
        if remaining_values:
            summary['remaining_ratio'] = round(sum(remaining_values) / len(remaining_values), 2)
            summary['used_ratio'] = round(100 - summary['remaining_ratio'], 2)
        summary['accounts'] = accounts[:200]
    save_cpa_snapshot(target['id'], summary)
    return {
        'summary': summary,
        'metrics': {
            'fetch_auth_files_ms': int((t1 - t0) * 1000),
            'hydrate_usage_ms': int((t2 - t1) * 1000),
            'total_ms': int((time.time() - started) * 1000),
            'files': len(raw_files),
            'accounts': len(accounts),
        }
    }


def fetch_cpa_auth_files(target: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{target['base_url'].rstrip('/')}/v0/management/auth-files"
    r = requests.get(url, headers=mgmt_headers(target['token']), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get('files', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])


def extract_remaining_ratio(rate_limit: dict[str, Any] | None) -> float | None:
    if not isinstance(rate_limit, dict):
        return None
    window = rate_limit.get('primary_window') if isinstance(rate_limit.get('primary_window'), dict) else rate_limit
    used_percent = window.get('used_percent') if isinstance(window, dict) else None
    try:
        if used_percent is None:
            return None
        return max(0.0, min(1.0, 1.0 - (float(used_percent) / 100.0)))
    except Exception:
        return None


def probe_cpa_quota(target: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    auth_index = str(item.get('auth_index') or '').strip()
    id_token = item.get('id_token') or {}
    account_id = str(id_token.get('chatgpt_account_id') or item.get('chatgpt_account_id') or '').strip()
    cache_key = f"{target.get('id')}::{item.get('name')}::{auth_index}::{account_id}"
    now_ts = time.time()
    cached = quota_probe_cache.get(cache_key)
    if cached and (now_ts - float(cached.get('cached_at_ts') or 0) < QUOTA_CACHE_TTL_SECONDS):
        return dict(cached)
    if not auth_index or not account_id:
        return None
    payload = {
        'authIndex': auth_index,
        'method': 'GET',
        'url': WHAM_USAGE_URL,
        'header': {
            'Authorization': 'Bearer $TOKEN$',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0',
            'Chatgpt-Account-Id': account_id,
        },
    }
    url = f"{target['base_url'].rstrip('/')}/v0/management/api-call"
    r = requests.post(url, headers=mgmt_headers(target['token'], include_json=True), json=payload, timeout=25)
    r.raise_for_status()
    outer = r.json()
    if not isinstance(outer, dict) or outer.get('status_code') != 200:
        return None
    body = outer.get('body')
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None
    rate_limit = body.get('rate_limit') if isinstance(body.get('rate_limit'), dict) else None
    remaining = extract_remaining_ratio(rate_limit)
    result = {
        'remaining_ratio': round(remaining * 100, 2) if remaining is not None else None,
        'quota_limited': bool(rate_limit.get('limit_reached')) if isinstance(rate_limit, dict) else False,
        'plan_type': str(body.get('plan_type') or item.get('plan_type') or ((item.get('id_token') or {}).get('plan_type')) or 'unknown').lower(),
        'quota_signal_source': 'wham-usage',
        'quota_checked_at': now_iso(),
        'cached_at_ts': now_ts,
    }
    quota_probe_cache[cache_key] = dict(result)
    return result


def hydrate_live_quota(target: dict[str, Any], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    need = []
    for idx, item in enumerate(files):
        if item.get('quota_remaining_ratio') is None and item.get('usage_remaining_ratio') is None:
            if item.get('auth_index') and ((item.get('id_token') or {}).get('chatgpt_account_id') or item.get('chatgpt_account_id')):
                need.append((idx, item))
    if not need:
        return files
    max_workers = min(4, len(need))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(probe_cpa_quota, target, item): idx for idx, item in need}
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            if not info:
                continue
            if info.get('remaining_ratio') is not None:
                files[idx]['usage_remaining_ratio'] = round(float(info['remaining_ratio']) / 100.0, 4)
            if info.get('quota_limited') is not None:
                files[idx]['usage_limit_reached'] = bool(info['quota_limited'])
            if info.get('plan_type'):
                files[idx]['plan_type'] = info['plan_type']
            if info.get('quota_signal_source'):
                files[idx]['quota_signal_source'] = info['quota_signal_source']
            if info.get('quota_checked_at'):
                files[idx]['quota_checked_at'] = info['quota_checked_at']
    return files


def classify_cpa_file(item: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(item.get('status') or '').lower()
    msg = str(item.get('status_message') or '')
    plan_type = str(((item.get('id_token') or {}).get('plan_type')) or item.get('plan_type') or 'unknown').lower()
    error_type = extract_status_message_error_type(msg)
    api_status_code = item.get('api_status_code')
    invalid_401 = str(api_status_code or '') == '401' or error_type in {'401', 'unauthorized', 'invalid_api_key', 'token_expired'}
    quota_limited = (not invalid_401) and (bool(item.get('usage_limit_reached')) or error_type in {'usage_limit_reached', 'rate_limit', 'quota'})
    remaining_ratio = None
    ratio_value = item.get('quota_remaining_ratio') if item.get('quota_remaining_ratio') is not None else item.get('usage_remaining_ratio')
    if ratio_value is not None:
        try:
            remaining_ratio = round(float(ratio_value) * 100, 2)
        except Exception:
            remaining_ratio = None
    refresh_time = item.get('quota_checked_at') or item.get('last_refresh') or item.get('modtime') or item.get('created_at')
    return {
        'name': item.get('name') or item.get('id'),
        'email': item.get('email') or item.get('account') or item.get('label') or item.get('name'),
        'disabled': bool(item.get('disabled')),
        'invalid_401': invalid_401,
        'quota_limited': quota_limited,
        'remaining_ratio': remaining_ratio,
        'status': '401' if invalid_401 else 'limit' if quota_limited else (raw_status or plan_type or 'unknown'),
        'status_message': msg,
        'plan_type': plan_type,
        'api_status_code': api_status_code,
        'usage_limit_reached': item.get('usage_limit_reached'),
        'quota_signal_source': item.get('quota_signal_source'),
        'quota_checked_at': refresh_time,
        'source': 'management-auth-files',
    }


def extract_status_message_error_type(msg: Any) -> str:
    if not msg:
        return ''
    if isinstance(msg, dict):
        err = msg.get('error') if isinstance(msg.get('error'), dict) else msg
        return str(err.get('type') or '').strip().lower() if isinstance(err, dict) else ''
    try:
        parsed = json.loads(str(msg))
    except Exception:
        lowered = str(msg).lower()
        if 'usage_limit_reached' in lowered:
            return 'usage_limit_reached'
        if 'unauthorized' in lowered or 'invalid_api_key' in lowered or 'token_expired' in lowered or ' 401' in lowered:
            return '401'
        return ''
    return extract_status_message_error_type(parsed)


def find_external_warden_db(target: dict[str, Any]) -> Path | None:
    data_dir = CPA_WARDEN_DIR / 'data'
    if not data_dir.exists():
        return None
    target_base = str(target.get('base_url') or '').rstrip('/')
    target_token = str(target.get('token') or '')
    for cfg in sorted(data_dir.glob('*_config.json')):
        try:
            conf = json.loads(cfg.read_text(encoding='utf-8'))
        except Exception:
            continue
        if str(conf.get('base_url') or '').rstrip('/') != target_base:
            continue
        if target_token and str(conf.get('token') or '') != target_token:
            continue
        db_path = Path(str(conf.get('db_path') or '')).expanduser()
        if not db_path.is_absolute():
            db_path = (CPA_WARDEN_DIR / db_path).resolve()
        if db_path.exists():
            return db_path
    return None


def load_cpa_warden_accounts(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    db = find_external_warden_db(target) or cpa_db_path(target['id'])
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = [dict(r) for r in cur.execute("SELECT * FROM auth_accounts ORDER BY updated_at DESC, name").fetchall()]
    conn.close()
    out = {}
    for r in rows:
        key = str((r.get('name') or '')).lower()
        api_status_code = r.get('api_status_code')
        status_message = r.get('status_message') or ''
        error_type = extract_status_message_error_type(status_message)
        invalid_401 = (str(api_status_code or '') == '401') or bool(r.get('unavailable')) or error_type in {'401', 'unauthorized', 'invalid_api_key', 'token_expired'}
        quota_limited = (not invalid_401) and (bool(r.get('is_quota_limited')) or bool(r.get('usage_limit_reached')) or error_type in {'usage_limit_reached', 'rate_limit', 'quota'})
        ratio_value = r.get('quota_remaining_ratio') if r.get('quota_remaining_ratio') is not None else r.get('usage_remaining_ratio')
        remaining_ratio = round(float(ratio_value) * 100, 2) if ratio_value is not None else None
        out[key] = {
            'name': r.get('name'),
            'email': r.get('email'),
            'disabled': bool(r.get('disabled')),
            'invalid_401': invalid_401,
            'quota_limited': quota_limited,
            'remaining_ratio': remaining_ratio,
            'status': r.get('status') or 'unknown',
            'status_message': status_message,
            'plan_type': (r.get('usage_plan_type') or r.get('id_token_plan_type') or 'unknown'),
            'api_status_code': api_status_code,
            'usage_allowed': r.get('usage_allowed'),
            'usage_limit_reached': r.get('usage_limit_reached'),
            'quota_signal_source': r.get('quota_signal_source'),
            'quota_checked_at': r.get('auth_last_refresh') or r.get('last_probed_at') or r.get('updated_at') or r.get('auth_modtime') or r.get('auth_updated_at'),
            'source': 'warden-db',
        }
    return out


def merge_cpa_accounts(auth_accounts: list[dict[str, Any]], warden_map: dict[str, dict[str, Any]], include_warden_only: bool = True) -> list[dict[str, Any]]:
    merged = []
    seen = set()

    def account_keys(item: dict[str, Any]) -> list[str]:
        keys = []
        name = normalize_credential_name(str(item.get('name') or ''))
        email = str(item.get('email') or '').strip().lower()
        if name:
            keys.append(f'name:{name}')
        if email:
            keys.append(f'email:{email}')
        return keys

    normalized_warden_map: dict[str, dict[str, Any]] = {}
    for w in warden_map.values():
        for k in account_keys(w):
            normalized_warden_map[k] = w

    for acc in auth_accounts:
        acc_keys = account_keys(acc)
        w = next((normalized_warden_map.get(k) for k in acc_keys if normalized_warden_map.get(k)), {})

        # 实时接口优先；warden 只补充缺失字段，不覆盖实时状态
        invalid_401 = bool(acc.get('invalid_401'))
        quota_limited = bool(acc.get('quota_limited')) and not invalid_401
        plan_type = str(acc.get('plan_type') or w.get('plan_type') or 'unknown').lower()
        remaining_ratio = acc.get('remaining_ratio')
        if remaining_ratio is None and w.get('remaining_ratio') is not None:
            remaining_ratio = w.get('remaining_ratio')
        if quota_limited or invalid_401:
            remaining_ratio = 0.0
        status = '401' if invalid_401 else 'limit' if quota_limited else (acc.get('status') or w.get('status') or (plan_type if plan_type != 'unknown' else 'unknown'))
        status_message = acc.get('status_message') or w.get('status_message') or ''
        quota_checked_at = acc.get('quota_checked_at') or w.get('quota_checked_at')

        merged.append({
            'name': acc.get('name') or w.get('name'),
            'email': acc.get('email') or w.get('email'),
            'disabled': bool(acc.get('disabled')),
            'invalid_401': invalid_401,
            'quota_limited': quota_limited,
            'remaining_ratio': round(float(remaining_ratio), 2) if remaining_ratio is not None else None,
            'status': status,
            'status_message': status_message,
            'plan_type': plan_type,
            'quota_checked_at': quota_checked_at,
            'source': 'merged',
        })
        seen.update(acc_keys)

    if not include_warden_only:
        return merged

    for w in warden_map.values():
        w_keys = account_keys(w)
        if any(k in seen for k in w_keys):
            continue
        invalid_401 = bool(w.get('invalid_401'))
        quota_limited = bool(w.get('quota_limited')) and not invalid_401
        plan_type = str(w.get('plan_type') or 'unknown').lower()
        remaining_ratio = w.get('remaining_ratio')
        if quota_limited or invalid_401:
            remaining_ratio = 0.0
        merged.append({
            'name': w.get('name'),
            'email': w.get('email'),
            'disabled': bool(w.get('disabled')),
            'invalid_401': invalid_401,
            'quota_limited': quota_limited,
            'remaining_ratio': round(float(remaining_ratio), 2) if remaining_ratio is not None else None,
            'status': '401' if invalid_401 else 'limit' if quota_limited else plan_type if plan_type != 'unknown' else (w.get('status') or 'unknown'),
            'status_message': w.get('status_message') or '',
            'plan_type': plan_type,
            'quota_checked_at': w.get('quota_checked_at'),
            'source': 'warden-db-only',
        })
    return merged


def cpa_summary(target: dict[str, Any], live: bool = False, prefer_snapshot: bool = True) -> dict[str, Any]:
    if (not live) and prefer_snapshot:
        snapshot = load_cpa_snapshot(target['id'])
        if snapshot:
            snapshot['id'] = target['id']
            snapshot['name'] = target['name']
            snapshot['base_url'] = target['base_url']
            snapshot['provider'] = target['provider']
            snapshot['sort_order'] = int(target.get('sort_order') or 0)
            snapshot['expanded'] = bool(target.get('expanded'))
            accounts = snapshot.get('accounts') or []
            snapshot['total'] = len(accounts)
            snapshot['invalid_401'] = sum(1 for r in accounts if r.get('invalid_401'))
            snapshot['quota_limited'] = sum(1 for r in accounts if r.get('quota_limited'))
            snapshot['abnormal'] = sum(1 for r in accounts if (not r.get('invalid_401')) and (not r.get('quota_limited')) and str(r.get('status') or '').lower() in {'error', 'exception', 'abnormal', 'unknown'})
            snapshot['disabled'] = sum(1 for r in accounts if r.get('disabled'))
            snapshot['healthy'] = sum(1 for r in accounts if not r.get('invalid_401') and not r.get('quota_limited') and not r.get('disabled') and str(r.get('status') or '').lower() not in {'error', 'exception', 'abnormal', 'unknown'})
            usage_stats = get_cpa_usage_stats(target)
            snapshot['request_count'] = usage_stats['request_count']
            snapshot['total_tokens'] = usage_stats['total_tokens']
            snapshot['usage_matched_by'] = usage_stats['matched_by']
            return snapshot

    summary = {
        'id': target['id'], 'name': target['name'], 'base_url': target['base_url'], 'provider': target['provider'],
        'sort_order': int(target.get('sort_order') or 0),
        'expanded': bool(target.get('expanded')),
        'total': 0, 'invalid_401': 0, 'quota_limited': 0, 'abnormal': 0, 'disabled': 0, 'healthy': 0,
        'used_ratio': None, 'remaining_ratio': None, 'accounts': [], 'last_run': None,
    }
    accounts: list[dict[str, Any]] = []
    warden_map = load_cpa_warden_accounts(target)
    if live:
        try:
            raw_files = fetch_cpa_auth_files(target)
            hydrated = hydrate_live_quota(target, raw_files)
            live_accounts = [classify_cpa_file(raw) for raw in hydrated]
            accounts = merge_cpa_accounts(live_accounts, warden_map, include_warden_only=False)
            summary['last_run'] = {
                'source': 'management-auth-files+warden-db',
                'count': len(accounts),
                'live_count': len(live_accounts),
                'warden_count': len(warden_map),
            }
        except Exception:
            pass
    if not accounts:
        db = cpa_db_path(target['id'])
        if db.exists():
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = [dict(r) for r in cur.execute("SELECT * FROM auth_accounts ORDER BY updated_at DESC, name").fetchall()]
            db_accounts = [{
                'name': r.get('name'),
                'email': r.get('email'),
                'disabled': bool(r.get('disabled')),
                'invalid_401': bool(r.get('is_invalid_401')) or str(r.get('api_status_code') or '') == '401',
                'quota_limited': bool(r.get('is_quota_limited')) or bool(r.get('usage_limit_reached')),
                'remaining_ratio': round(((r.get('quota_remaining_ratio') if r.get('quota_remaining_ratio') is not None else r.get('usage_remaining_ratio')) or 0) * 100, 2) if ((r.get('quota_remaining_ratio') is not None) or (r.get('usage_remaining_ratio') is not None)) else None,
                'status': r.get('status') or 'unknown',
                'status_message': r.get('status_message') or '',
                'plan_type': str(r.get('usage_plan_type') or r.get('id_token_plan_type') or 'unknown').lower(),
                'source': 'warden-db',
            } for r in rows]
            accounts = db_accounts
            run = cur.execute("SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT 1").fetchone()
            if run:
                summary['last_run'] = dict(run)
            conn.close()
        elif warden_map:
            accounts = merge_cpa_accounts([], warden_map)
            summary['last_run'] = {'source': 'warden-db-only', 'count': len(accounts)}

    accounts.sort(key=lambda r: (
        0 if r.get('invalid_401') else 1 if r.get('quota_limited') else 2 if r.get('disabled') else 3,
        float(r.get('remaining_ratio') or 0),
        str(r.get('email') or r.get('name') or ''),
    ))

    usage_stats = get_cpa_usage_stats(target)
    summary['request_count'] = usage_stats['request_count']
    summary['total_tokens'] = usage_stats['total_tokens']
    summary['usage_matched_by'] = usage_stats['matched_by']
    if accounts:
        summary['total'] = len(accounts)
        summary['invalid_401'] = sum(1 for r in accounts if r.get('invalid_401'))
        summary['quota_limited'] = sum(1 for r in accounts if r.get('quota_limited'))
        summary['abnormal'] = sum(1 for r in accounts if (not r.get('invalid_401')) and (not r.get('quota_limited')) and str(r.get('status') or '').lower() in {'error', 'exception', 'abnormal', 'unknown'})
        summary['disabled'] = sum(1 for r in accounts if r.get('disabled'))
        summary['healthy'] = sum(1 for r in accounts if not r.get('invalid_401') and not r.get('quota_limited') and not r.get('disabled') and str(r.get('status') or '').lower() not in {'error', 'exception', 'abnormal', 'unknown'})
        remaining_values = [float(r.get('remaining_ratio')) for r in accounts if r.get('remaining_ratio') is not None]
        if remaining_values:
            summary['remaining_ratio'] = round(sum(remaining_values) / len(remaining_values), 2)
            summary['used_ratio'] = round(100 - summary['remaining_ratio'], 2)
        summary['accounts'] = accounts[:200]

    if live and accounts:
        save_cpa_snapshot(target['id'], summary)
    return summary

def upload_cpa_auth_file(target: dict[str, Any], file_name: str, content: str) -> dict[str, Any]:
    base = target['base_url'].rstrip('/')
    upload_url = f"{base}/v0/management/auth-files"
    data_bytes = (content or '').encode('utf-8')
    try:
        multipart_resp = requests.post(
            upload_url,
            headers=mgmt_headers(target['token']),
            files={'file': (file_name, data_bytes, 'application/json')},
            timeout=30,
        )
        if multipart_resp.status_code in (200, 201):
            return {'ok': True, 'status_code': multipart_resp.status_code, 'text': multipart_resp.text[:300], 'mode': 'multipart'}
        if multipart_resp.status_code not in (404, 405, 415):
            return {'ok': False, 'status_code': multipart_resp.status_code, 'text': multipart_resp.text[:300], 'mode': 'multipart'}
    except Exception as exc:
        return {'ok': False, 'status_code': None, 'text': str(exc), 'mode': 'multipart'}

    raw_url = f"{upload_url}?name={urllib.parse.quote(file_name, safe='')}"
    try:
        raw_resp = requests.post(
            raw_url,
            headers={**mgmt_headers(target['token']), 'Content-Type': 'application/json'},
            data=content or '',
            timeout=30,
        )
        return {'ok': raw_resp.status_code in (200, 201), 'status_code': raw_resp.status_code, 'text': raw_resp.text[:300], 'mode': 'raw-json'}
    except Exception as exc:
        return {'ok': False, 'status_code': None, 'text': str(exc), 'mode': 'raw-json'}


def delete_cpa_auth_file(target: dict[str, Any], name: str) -> dict[str, Any]:
    url = f"{target['base_url'].rstrip('/')}/v0/management/auth-files?name={urllib.parse.quote(name, safe='')}"
    r = requests.delete(url, headers=mgmt_headers(target['token']), timeout=30)
    return {'ok': r.ok, 'status_code': r.status_code, 'text': r.text[:500]}


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/overview')
def api_overview():
    range_key = request.args.get('range', '24h')
    include_cpas = request.args.get('include_cpas', '1') != '0'
    include_clirelay = request.args.get('include_clirelay', '1') != '0'
    hours_map = {'3h': 3, '24h': 24, '7d': 24 * 7, '30d': 24 * 30, 'all': None}
    history_hours = hours_map.get(range_key, 24)
    payload = {
        'server': get_metric_summary(history_hours),
        'range': range_key,
    }
    if include_clirelay:
        payload['clirelay'] = get_clirelay_summary()
    if include_cpas:
        payload['cpas'] = [cpa_summary(t, live=False) for t in load_cpas()]
    return jsonify(payload)


@app.get('/api/server-status')
def api_server_status():
    range_key = request.args.get('range', '24h')
    hours_map = {'3h': 3, '24h': 24, '7d': 24 * 7, '30d': 24 * 30, 'all': None}
    history_hours = hours_map.get(range_key, 24)
    return jsonify({
        'server': get_metric_summary(history_hours),
        'clirelay': get_clirelay_summary(),
        'range': range_key,
    })


@app.post('/api/cpas')
def api_add_cpa():
    data = request.get_json(force=True) or {}
    cpa_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    max_sort = conn.execute('SELECT COALESCE(MAX(sort_order), 0) FROM cpa_targets').fetchone()[0] or 0
    row = {
        'id': cpa_id,
        'name': (data.get('name') or '未命名CPA').strip(),
        'base_url': (data.get('base_url') or '').strip().rstrip('/'),
        'token': (data.get('token') or '').strip(),
        'provider': (data.get('provider') or 'codex').strip() or 'codex',
        'sort_order': int(max_sort) + 1,
        'expanded': 0,
        'created_at': now_iso(),
        'updated_at': now_iso(),
    }
    if not row['base_url'] or not row['token']:
        return jsonify({'error': '缺少 base_url 或 token'}), 400
    conn.execute(
        'INSERT INTO cpa_targets (id, name, base_url, token, provider, sort_order, expanded, created_at, updated_at) VALUES (:id,:name,:base_url,:token,:provider,:sort_order,:expanded,:created_at,:updated_at)',
        row,
    )
    conn.commit()
    conn.close()
    write_cpa_config(row)
    return jsonify(row), 201


@app.patch('/api/cpas/<cpa_id>')
def api_update_cpa(cpa_id: str):
    data = request.get_json(force=True) or {}
    fields = []
    params: list[Any] = []
    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            return jsonify({'error': '名称不能为空'}), 400
        fields.append('name = ?')
        params.append(name)
    if 'expanded' in data:
        fields.append('expanded = ?')
        params.append(1 if bool(data.get('expanded')) else 0)
    if not fields:
        return jsonify({'error': '没有可更新字段'}), 400
    fields.append('updated_at = ?')
    params.append(now_iso())
    params.append(cpa_id)
    conn = get_conn()
    conn.execute(f"UPDATE cpa_targets SET {', '.join(fields)} WHERE id = ?", tuple(params))
    conn.commit()
    row = conn.execute('SELECT * FROM cpa_targets WHERE id = ?', (cpa_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'CPA 不存在'}), 404
    target = dict(row)
    return jsonify({'ok': True, 'cpa': cpa_summary(target, live=False)})


@app.post('/api/cpas/reorder')
def api_reorder_cpas():
    data = request.get_json(force=True) or {}
    ids = data.get('ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': '缺少排序 ids'}), 400
    conn = get_conn()
    for idx, cpa_id in enumerate(ids, start=1):
        conn.execute('UPDATE cpa_targets SET sort_order = ?, updated_at = ? WHERE id = ?', (idx, now_iso(), str(cpa_id)))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'cpas': [cpa_summary(t, live=False) for t in load_cpas()]})


@app.delete('/api/cpas/<cpa_id>')
def api_delete_cpa(cpa_id: str):
    conn = get_conn()
    conn.execute('DELETE FROM cpa_targets WHERE id = ?', (cpa_id,))
    conn.commit()
    conn.close()
    for p in [cpa_config_path(cpa_id), cpa_db_path(cpa_id), cpa_invalid_path(cpa_id), cpa_quota_path(cpa_id), cpa_log_path(cpa_id)]:
        if p.exists():
            p.unlink()
    return jsonify({'ok': True})


@app.post('/api/cpas/scan')
def api_scan_cpas():
    results = []
    for target in load_cpas():
        started = time.time()
        try:
            native = native_refresh_cpa(target)
            results.append({
                'id': target['id'],
                'name': target['name'],
                'mode': 'native-refresh',
                'metrics': native.get('metrics', {}),
                'elapsed_ms': int((time.time() - started) * 1000),
            })
        except Exception as exc:
            results.append({
                'id': target['id'],
                'name': target['name'],
                'error': str(exc),
                'elapsed_ms': int((time.time() - started) * 1000),
            })
    return jsonify({'results': results, 'cpas': [cpa_summary(t, live=False) for t in load_cpas()]})



@app.get('/api/credentials')
def api_list_credentials():
    dedupe = cleanup_duplicate_credentials()
    cpas = load_cpas()
    cpa_map = {c['id']: c for c in cpas}
    presence_map = build_credential_cpa_presence(cpas)
    credentials = []
    for item in list_credentials():
        item = dict(item)
        last_target = cpa_map.get(item.get('last_target_id'))
        item['last_target_name'] = last_target.get('name') if last_target else None
        raw = normalize_credential_name(item.get('name') or item.get('filename') or '')
        item['present_in_cpas'] = presence_map.get(raw, [])
        credentials.append(item)
    return jsonify({'credentials': credentials, 'cpas': cpas, 'dedupe_removed': dedupe.get('removed', 0)})


@app.post('/api/credentials/import')
def api_import_credentials():
    data = request.get_json(force=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': '没有可导入的凭证'}), 400
    conn = get_conn()
    saved = []
    skipped = []
    now = now_iso()
    dedupe = cleanup_duplicate_credentials(conn)
    existing_rows = [dict(r) for r in conn.execute("SELECT name, filename FROM credential_store WHERE archived = 0").fetchall()]
    existing_keys = {credential_dedupe_key(r) for r in existing_rows if credential_dedupe_key(r)}
    for item in items:
        content = str(item.get('content') or '').strip()
        if not content:
            continue
        cred_id = uuid.uuid4().hex[:16]
        filename = str(item.get('filename') or item.get('name') or f'{cred_id}.json').strip()
        name = str(item.get('name') or filename).strip()
        dedupe_key = credential_dedupe_key({'name': name, 'filename': filename})
        if dedupe_key and dedupe_key in existing_keys:
            skipped.append({'name': name, 'filename': filename, 'reason': 'duplicate_name'})
            continue
        row = {
            'id': cred_id,
            'name': name,
            'filename': filename,
            'content': content,
            'note': str(item.get('note') or ''),
            'tags': str(item.get('tags') or ''),
            'uploaded_at': now,
            'updated_at': now,
            'last_used_at': None,
            'last_target_id': None,
            'uploaded_to_cpa': 0,
            'upload_status_text': '',
            'upload_error_detail': '',
            'archived': 0,
        }
        conn.execute('INSERT INTO credential_store (id,name,filename,content,note,tags,uploaded_at,updated_at,last_used_at,last_target_id,uploaded_to_cpa,upload_status_text,upload_error_detail,archived) VALUES (:id,:name,:filename,:content,:note,:tags,:uploaded_at,:updated_at,:last_used_at,:last_target_id,:uploaded_to_cpa,:upload_status_text,:upload_error_detail,:archived)', row)
        existing_keys.add(dedupe_key)
        saved.append({'id': cred_id, 'name': name, 'filename': filename})
    conn.commit()
    conn.close()
    return jsonify({'saved': saved, 'skipped': skipped, 'dedupe_removed': dedupe.get('removed', 0), 'credentials': list_credentials()})


@app.delete('/api/credentials/<cred_id>')
def api_delete_credential(cred_id: str):
    conn = get_conn()
    conn.execute('UPDATE credential_store SET archived = 1, updated_at = ? WHERE id = ?', (now_iso(), cred_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'credentials': list_credentials()})


@app.post('/api/credentials/deploy')
def api_deploy_credentials():
    data = request.get_json(force=True) or {}
    target_id = str(data.get('target_id') or '').strip()
    credential_ids = data.get('credential_ids') or []
    target = next((x for x in load_cpas() if x['id'] == target_id), None)
    if not target:
        return jsonify({'error': '目标 CPA 不存在'}), 404
    if not credential_ids:
        return jsonify({'error': '没有选择凭证'}), 400
    conn = get_conn()
    qmarks = ','.join('?' for _ in credential_ids)
    rows = [dict(r) for r in conn.execute(f'SELECT * FROM credential_store WHERE archived = 0 AND id IN ({qmarks})', tuple(credential_ids)).fetchall()]
    results = []
    now = now_iso()
    for row in rows:
        name = row.get('filename') or row.get('name') or f"{row['id']}.json"
        result = upload_cpa_auth_file(target, name, row.get('content') or '')
        ok = bool(result.get('ok'))
        results.append({'id': row['id'], 'name': row['name'], 'filename': name, 'ok': ok, 'status_code': result.get('status_code'), 'text': result.get('text'), 'mode': result.get('mode')})
        if ok:
            conn.execute('UPDATE credential_store SET last_used_at = ?, last_target_id = ?, uploaded_to_cpa = 1, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE id = ?', (now, target_id, '可用 / 限额', '', now, row['id']))
    conn.commit()
    conn.close()
    return jsonify({'results': results, 'credentials': list_credentials(), 'cpa': cpa_summary(target)})


@app.get('/api/cpas/<cpa_id>')
def api_get_cpa(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    return jsonify({'cpa': cpa_summary(target, live=False)})


@app.post('/api/cpas/<cpa_id>/refresh')
def api_refresh_cpa(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    started = time.time()
    try:
        native = native_refresh_cpa(target)
    except Exception as exc:
        return jsonify({'error': str(exc), 'cpa': cpa_summary(target, live=False), 'elapsed_ms': int((time.time() - started) * 1000)}), 500
    return jsonify({'ok': True, 'scan': {'mode': 'native-refresh', 'metrics': native.get('metrics', {})}, 'cpa': native.get('summary') or cpa_summary(target, live=False), 'elapsed_ms': int((time.time() - started) * 1000)})


@app.delete('/api/cpas/<cpa_id>/auth-files/<path:file_name>')
def api_delete_cpa_auth_file(cpa_id: str, file_name: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    result = delete_cpa_auth_file(target, file_name)
    if result.get('ok'):
        conn = get_conn()
        conn.execute('UPDATE credential_store SET uploaded_to_cpa = 0, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE archived = 0 AND last_target_id = ? AND filename = ?', ('', '', now_iso(), cpa_id, urllib.parse.unquote(file_name)))
        conn.commit()
        conn.close()
    code = 200 if result.get('ok') else 500
    return jsonify({'result': result, 'cpa': cpa_summary(target), 'credentials': list_credentials()}), code


def match_account_by_kind(acc: dict[str, Any], kind: str) -> bool:
    kind = str(kind or '').strip().lower()
    status = str(acc.get('status') or '').lower()
    if kind == '401':
        return bool(acc.get('invalid_401'))
    if kind == 'abnormal':
        return (not acc.get('invalid_401')) and (not acc.get('quota_limited')) and status in {'error', 'exception', 'abnormal', 'unknown'}
    return False


@app.post('/api/cpas/<cpa_id>/delete-401')
def api_delete_cpa_401(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    summary = cpa_summary(target)
    deleted = []
    failed = []
    for acc in summary.get('accounts', []):
        if acc.get('invalid_401'):
            res = delete_cpa_auth_file(target, acc.get('name') or acc.get('email') or '')
            if res.get('ok'):
                deleted.append(acc.get('name') or acc.get('email'))
            else:
                failed.append({'account': acc.get('name') or acc.get('email'), 'result': res})
    return jsonify({'deleted': deleted, 'failed': failed, 'cpa': cpa_summary(target)})


@app.get('/api/cpas/<cpa_id>/export/<kind>')
def api_export_cpa_accounts(cpa_id: str, kind: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    if kind not in {'401', 'abnormal'}:
        return jsonify({'error': '仅支持导出 abnormal 或 401'}), 400
    summary = cpa_summary(target)
    matched_accounts = [acc for acc in summary.get('accounts', []) if match_account_by_kind(acc, kind)]
    matched_names = {
        normalize_credential_name(acc.get('name') or acc.get('email') or '')
        for acc in matched_accounts
        if normalize_credential_name(acc.get('name') or acc.get('email') or '')
    }
    credentials = []
    for item in list_credentials():
        raw = normalize_credential_name(item.get('name') or item.get('filename') or '')
        if raw in matched_names:
            credentials.append(item)

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            'exported_at': now_iso(),
            'cpa_id': target['id'],
            'cpa_name': target['name'],
            'kind': kind,
            'count': len(credentials),
            'matched_accounts': len(matched_accounts),
            'files': [],
        }
        used_names: set[str] = set()
        for idx, item in enumerate(credentials, start=1):
            base_name = str(item.get('filename') or item.get('name') or f'credential_{idx}.json').strip() or f'credential_{idx}.json'
            safe_name = base_name.replace('..', '_').replace('/', '_')
            if safe_name in used_names:
                stem, dot, ext = safe_name.partition('.')
                safe_name = f"{stem}_{idx}{dot}{ext}" if dot else f"{safe_name}_{idx}"
            used_names.add(safe_name)
            zf.writestr(safe_name, item.get('content') or '')
            manifest['files'].append({
                'id': item.get('id'),
                'name': item.get('name'),
                'filename': safe_name,
            })
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    mem.seek(0)
    filename = f"{target['name']}_{kind}_credentials_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.zip"
    return Response(
        mem.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"},
    )


@app.post('/api/cpas/<cpa_id>/delete/<kind>')
def api_delete_cpa_accounts_by_kind(cpa_id: str, kind: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    if kind not in {'401', 'abnormal'}:
        return jsonify({'error': '仅支持删除 abnormal 或 401'}), 400
    summary = cpa_summary(target)
    deleted = []
    failed = []
    for acc in summary.get('accounts', []):
        if not match_account_by_kind(acc, kind):
            continue
        res = delete_cpa_auth_file(target, acc.get('name') or acc.get('email') or '')
        if res.get('ok'):
            deleted.append(acc.get('name') or acc.get('email'))
        else:
            failed.append({'account': acc.get('name') or acc.get('email'), 'result': res})
    return jsonify({'kind': kind, 'deleted': deleted, 'failed': failed, 'cpa': cpa_summary(target), 'credentials': list_credentials()})


@app.post('/api/credentials/sync-upload-status')
def api_sync_credential_upload_status():
    data = request.get_json(silent=True) or {}
    target_id = str(data.get('target_id') or '').strip()
    cpas = load_cpas()
    target = next((x for x in cpas if x['id'] == target_id), None)
    if not target:
        return jsonify({'error': '请先选择目标 CPA'}), 400
    presence_map = build_credential_cpa_presence(cpas)
    account_map = {}
    for key, items in presence_map.items():
        hit = next((x for x in items if str(x.get('cpa_id')) == target_id), None)
        if hit:
            account_map[key] = {'good': bool(hit.get('good')), 'status_text': str(hit.get('status_text') or ''), 'detail': ''}
    matched = 0
    conn = get_conn()
    creds = [dict(r) for r in conn.execute("SELECT * FROM credential_store WHERE archived = 0 ORDER BY uploaded_at DESC").fetchall()]
    now = now_iso()
    for row in creds:
        raw = normalize_credential_name(row.get('name') or row.get('filename') or '')
        hit = account_map.get(raw)
        if hit:
            matched += 1
            conn.execute('UPDATE credential_store SET uploaded_to_cpa = 1, last_target_id = ?, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE id = ?', (target_id, hit['status_text'], hit['detail'], now, row['id']))
        else:
            conn.execute('UPDATE credential_store SET uploaded_to_cpa = 0, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE id = ?', ('', '', now, row['id']))
    conn.commit()
    conn.close()
    credentials = []
    cpa_map = {c['id']: c for c in cpas}
    for item in list_credentials():
        item = dict(item)
        last_target = cpa_map.get(item.get('last_target_id'))
        item['last_target_name'] = last_target.get('name') if last_target else None
        raw = normalize_credential_name(item.get('name') or item.get('filename') or '')
        item['present_in_cpas'] = presence_map.get(raw, [])
        credentials.append(item)
    return jsonify({'ok': True, 'matched': matched, 'credentials': credentials, 'cpas': cpas, 'mode': 'snapshot-cache'})

@app.get('/api/history')
def api_history():
    hours = int(request.args.get('hours', '24'))
    return jsonify({'history': get_metric_history(hours)})


def start_threads() -> None:
    th = threading.Thread(target=sampler_loop, daemon=True)
    th.start()


def handle_sigterm(*_: Any) -> None:
    stop_event.set()


if __name__ == '__main__':
    init_db()
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    store_sample(collect_sample())
    start_threads()
    app.run(host=HOST, port=PORT, debug=False)
