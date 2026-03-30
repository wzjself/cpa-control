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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, render_template, request
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
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


def compute_health(sample: dict[str, Any]) -> int:
    score = 100.0
    score -= max(0, sample['cpu_percent'] - 65) * 0.6
    score -= max(0, sample['mem_percent'] - 70) * 0.7
    score -= max(0, sample['disk_percent'] - 75) * 0.8
    score -= max(0, sample['load_1'] - os.cpu_count()) * 5
    return int(max(0, min(100, round(score))))


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


def get_metric_summary(history_hours: int | None = 24) -> dict[str, Any]:
    history = get_metric_history(history_hours)
    latest = history[-1] if history else collect_sample()
    totals = {'rx_mb': 0.0, 'tx_mb': 0.0}
    if history:
        totals['rx_mb'] = round(history[-1]['net_rx_mb'] - history[0]['net_rx_mb'], 2)
        totals['tx_mb'] = round(history[-1]['net_tx_mb'] - history[0]['net_tx_mb'], 2)
    return {
        'latest': latest,
        'health': compute_health(latest),
        'top_processes': top_processes(),
        'traffic_24h': totals,
        'history': history,
    }


def get_clirelay_summary() -> dict[str, Any]:
    try:
        import requests  # type: ignore
        headers = {'x-management-key': CLIRELAY_MGMT_KEY}
        dash = requests.get(f'{CLIRELAY_BASE}/v0/management/dashboard-summary?days=7', headers=headers, timeout=20)
        chart = requests.get(f'{CLIRELAY_BASE}/v0/management/usage/chart-data?days=7', headers=headers, timeout=20)
        system = requests.get(f'{CLIRELAY_BASE}/v0/management/system-stats', headers=headers, timeout=20)
        auth_files = requests.get(f'{CLIRELAY_BASE}/v0/management/auth-files', headers=headers, timeout=20)
        if dash.ok and chart.ok and system.ok:
            dash_j = dash.json()
            chart_j = chart.json()
            sys_j = system.json()
            auth_j = auth_files.json() if auth_files.ok else {'files': []}
            kpi = dash_j.get('kpi', {})
            return {
                'available': True,
                'source': 'management-api',
                'request_count': kpi.get('total_requests', 0),
                'success_rate': round(kpi.get('success_rate', 0), 2),
                'input_tokens': kpi.get('input_tokens', 0),
                'output_tokens': kpi.get('output_tokens', 0),
                'cached_tokens': kpi.get('cached_tokens', 0),
                'total_tokens': kpi.get('total_tokens', 0),
                'rpm': sys_j.get('total_rpm', 0),
                'tpm': sys_j.get('total_tpm', 0),
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
    except Exception:
        pass

    if not CLIRELAY_DB.exists():
        return {'available': False}
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
    return {
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


def load_cpas() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cpa_targets ORDER BY created_at DESC").fetchall()]
    conn.close()
    return rows


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
    return {
        'remaining_ratio': round(remaining * 100, 2) if remaining is not None else None,
        'quota_limited': bool(rate_limit.get('limit_reached')) if isinstance(rate_limit, dict) else False,
        'plan_type': str(body.get('plan_type') or item.get('plan_type') or ((item.get('id_token') or {}).get('plan_type')) or 'unknown').lower(),
        'quota_signal_source': 'wham-usage',
    }


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
            'source': 'warden-db',
        }
    return out


def merge_cpa_accounts(auth_accounts: list[dict[str, Any]], warden_map: dict[str, dict[str, Any]], include_warden_only: bool = True) -> list[dict[str, Any]]:
    merged = []
    seen = set()
    for acc in auth_accounts:
        key = str((acc.get('name') or '')).lower()
        w = warden_map.get(key, {})
        invalid_401 = bool(w.get('invalid_401')) or bool(acc.get('invalid_401'))
        quota_limited = bool(acc.get('quota_limited')) or (bool(w.get('quota_limited')) and not invalid_401)
        plan_type = str(acc.get('plan_type') or w.get('plan_type') or 'unknown').lower()
        remaining_ratio = acc.get('remaining_ratio')
        if w.get('remaining_ratio') is not None:
            remaining_ratio = w.get('remaining_ratio')
        if quota_limited:
            remaining_ratio = 0.0
        elif invalid_401:
            remaining_ratio = 0.0
        status = '401' if invalid_401 else 'limit' if quota_limited else (acc.get('status') or w.get('status') or (plan_type if plan_type != 'unknown' else 'unknown'))
        status_message = acc.get('status_message') or w.get('status_message') or ''
        merged.append({
            'name': acc.get('name') or w.get('name'),
            'email': acc.get('email') or w.get('email'),
            'disabled': bool(acc.get('disabled')) or bool(w.get('disabled')),
            'invalid_401': invalid_401,
            'quota_limited': quota_limited,
            'remaining_ratio': round(float(remaining_ratio), 2) if remaining_ratio is not None else None,
            'status': status,
            'status_message': status_message,
            'plan_type': plan_type,
            'source': 'merged',
        })
        seen.add(key)
    if not include_warden_only:
        return merged

    for key, w in warden_map.items():
        if key in seen:
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
            'source': 'warden-db-only',
        })
    return merged


def cpa_summary(target: dict[str, Any]) -> dict[str, Any]:
    summary = {
        'id': target['id'], 'name': target['name'], 'base_url': target['base_url'], 'provider': target['provider'],
        'total': 0, 'invalid_401': 0, 'quota_limited': 0, 'disabled': 0, 'healthy': 0,
        'used_ratio': None, 'remaining_ratio': None, 'accounts': [], 'last_run': None,
    }
    accounts: list[dict[str, Any]] = []
    warden_map = load_cpa_warden_accounts(target)
    try:
        files = hydrate_live_quota(target, fetch_cpa_auth_files(target))
        live_accounts = [classify_cpa_file(item) for item in files]
        accounts = merge_cpa_accounts(live_accounts, warden_map, include_warden_only=False)
        summary['last_run'] = {
            'source': 'management-auth-files+warden-db',
            'count': len(accounts),
            'live_count': len(live_accounts),
            'warden_count': len(warden_map),
        }
    except Exception:
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

    if accounts:
        summary['total'] = len(accounts)
        summary['invalid_401'] = sum(1 for r in accounts if r.get('invalid_401'))
        summary['quota_limited'] = sum(1 for r in accounts if r.get('quota_limited'))
        summary['disabled'] = sum(1 for r in accounts if r.get('disabled'))
        summary['healthy'] = sum(1 for r in accounts if not r.get('invalid_401') and not r.get('quota_limited') and not r.get('disabled'))
        remaining_values = [float(r.get('remaining_ratio')) for r in accounts if r.get('remaining_ratio') is not None]
        if remaining_values:
            summary['remaining_ratio'] = round(sum(remaining_values) / len(remaining_values), 2)
            summary['used_ratio'] = round(100 - summary['remaining_ratio'], 2)
        summary['accounts'] = accounts[:200]
    return summary

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
        payload['cpas'] = [cpa_summary(t) for t in load_cpas()]
    return jsonify(payload)


@app.get('/api/server-status')
def api_server_status():
    range_key = request.args.get('range', '24h')
    hours_map = {'3h': 3, '24h': 24, '7d': 24 * 7, '30d': 24 * 30, 'all': None}
    history_hours = hours_map.get(range_key, 24)
    return jsonify({
        'server': get_metric_summary(history_hours),
        'range': range_key,
    })


@app.post('/api/cpas')
def api_add_cpa():
    data = request.get_json(force=True) or {}
    cpa_id = uuid.uuid4().hex[:12]
    row = {
        'id': cpa_id,
        'name': (data.get('name') or '未命名CPA').strip(),
        'base_url': (data.get('base_url') or '').strip().rstrip('/'),
        'token': (data.get('token') or '').strip(),
        'provider': (data.get('provider') or 'codex').strip() or 'codex',
        'created_at': now_iso(),
        'updated_at': now_iso(),
    }
    if not row['base_url'] or not row['token']:
        return jsonify({'error': '缺少 base_url 或 token'}), 400
    conn = get_conn()
    conn.execute(
        'INSERT INTO cpa_targets (id, name, base_url, token, provider, created_at, updated_at) VALUES (:id,:name,:base_url,:token,:provider,:created_at,:updated_at)',
        row,
    )
    conn.commit()
    conn.close()
    write_cpa_config(row)
    return jsonify(row), 201


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
        result = scan_cpa(target)
        results.append({'id': target['id'], 'name': target['name'], **result})
    return jsonify({'results': results, 'cpas': [cpa_summary(t) for t in load_cpas()]})


@app.get('/api/cpas/<cpa_id>')
def api_get_cpa(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    return jsonify({'cpa': cpa_summary(target)})


@app.post('/api/cpas/<cpa_id>/refresh')
def api_refresh_cpa(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    try:
        scan_result = scan_cpa(target)
    except Exception as exc:
        return jsonify({'error': str(exc), 'cpa': cpa_summary(target)}), 500
    return jsonify({'ok': True, 'scan': scan_result, 'cpa': cpa_summary(target)})


@app.delete('/api/cpas/<cpa_id>/auth-files/<path:file_name>')
def api_delete_cpa_auth_file(cpa_id: str, file_name: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    result = delete_cpa_auth_file(target, file_name)
    code = 200 if result.get('ok') else 500
    return jsonify({'result': result, 'cpa': cpa_summary(target)}), code


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
