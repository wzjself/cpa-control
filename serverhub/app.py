from __future__ import annotations

import json
import os
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

from flask import Flask, jsonify, render_template, request

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

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, template_folder="templates", static_folder="static")
stop_event = threading.Event()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


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


def cpa_summary(target: dict[str, Any]) -> dict[str, Any]:
    db = cpa_db_path(target['id'])
    summary = {
        'id': target['id'], 'name': target['name'], 'base_url': target['base_url'], 'provider': target['provider'],
        'total': 0, 'invalid_401': 0, 'quota_limited': 0, 'disabled': 0, 'healthy': 0,
        'used_ratio': 0, 'remaining_ratio': 0, 'accounts': [], 'last_run': None,
    }
    if not db.exists():
        return summary
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = [dict(r) for r in cur.execute("SELECT * FROM auth_accounts ORDER BY updated_at DESC, name").fetchall()]
    if rows:
        summary['total'] = len(rows)
        summary['invalid_401'] = sum(1 for r in rows if r.get('is_invalid_401'))
        summary['quota_limited'] = sum(1 for r in rows if r.get('is_quota_limited'))
        summary['disabled'] = sum(1 for r in rows if r.get('disabled'))
        summary['healthy'] = sum(1 for r in rows if not r.get('is_invalid_401') and not r.get('is_quota_limited'))
        ratios = [r.get('quota_remaining_ratio') for r in rows if r.get('quota_remaining_ratio') is not None]
        if not ratios:
            ratios = [r.get('usage_remaining_ratio') for r in rows if r.get('usage_remaining_ratio') is not None]
        if ratios:
            remaining = max(0.0, min(1.0, sum(ratios) / len(ratios)))
            summary['remaining_ratio'] = round(remaining * 100, 2)
            summary['used_ratio'] = round((1 - remaining) * 100, 2)
        summary['accounts'] = [{
            'name': r.get('name'),
            'email': r.get('email'),
            'disabled': bool(r.get('disabled')),
            'invalid_401': bool(r.get('is_invalid_401')),
            'quota_limited': bool(r.get('is_quota_limited')),
            'remaining_ratio': round(((r.get('quota_remaining_ratio') if r.get('quota_remaining_ratio') is not None else r.get('usage_remaining_ratio')) or 0) * 100, 2),
            'status': r.get('status') or 'unknown',
        } for r in rows[:200]]
    run = cur.execute("SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if run:
        summary['last_run'] = dict(run)
    conn.close()
    return summary


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/overview')
def api_overview():
    range_key = request.args.get('range', '24h')
    hours_map = {'3h': 3, '24h': 24, 'all': None}
    history_hours = hours_map.get(range_key, 24)
    return jsonify({
        'server': get_metric_summary(history_hours),
        'clirelay': get_clirelay_summary(),
        'cpas': [cpa_summary(t) for t in load_cpas()],
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
