from __future__ import annotations

import json
import os
import urllib.parse
import sqlite3
import subprocess
import uuid
import io
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, render_template, request, Response
import requests

BASE_DIR = Path(__file__).resolve().parent
REQUESTS_SESSION = requests.Session()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "cpa-control.db"
CPA_WARDEN_DIR = Path("/root/cpa-warden")
PORT = int(os.environ.get("SERVERHUB_PORT", "8321"))
HOST = os.environ.get("SERVERHUB_HOST", "0.0.0.0")
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, template_folder="templates", static_folder="static")
quota_probe_cache: dict[str, dict[str, Any]] = {}
refresh_progress_store: dict[str, dict[str, Any]] = {}
QUOTA_CACHE_TTL_SECONDS = 60
MAX_BULK_WORKERS = 40


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
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
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

        CREATE INDEX IF NOT EXISTS idx_cpa_targets_sort_order ON cpa_targets(sort_order);
        CREATE INDEX IF NOT EXISTS idx_credential_store_archived_uploaded_at ON credential_store(archived, uploaded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_credential_store_last_target_id ON credential_store(last_target_id);
        CREATE INDEX IF NOT EXISTS idx_credential_store_name ON credential_store(name);
        CREATE INDEX IF NOT EXISTS idx_credential_store_filename ON credential_store(filename);
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


def list_credentials(include_content: bool = True) -> list[dict[str, Any]]:
    conn = get_conn()
    columns = '*'
    if not include_content:
        columns = 'id,name,filename,note,tags,uploaded_at,updated_at,last_used_at,last_target_id,uploaded_to_cpa,upload_status_text,upload_error_detail,archived'
    rows = [dict(r) for r in conn.execute(f"SELECT {columns} FROM credential_store WHERE archived = 0 ORDER BY uploaded_at DESC").fetchall()]
    conn.close()
    return rows


def serialize_credentials_for_list(cpas: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    cpas = cpas if cpas is not None else load_cpas()
    cpa_map = {c['id']: c for c in cpas}
    presence_map = build_credential_cpa_presence(cpas)
    credentials = []
    for item in list_credentials(include_content=False):
        item = dict(item)
        last_target = cpa_map.get(item.get('last_target_id'))
        item['last_target_name'] = last_target.get('name') if last_target else None
        raw = normalize_credential_name(item.get('name') or item.get('filename') or '')
        item['present_in_cpas'] = presence_map.get(raw, [])
        credentials.append(item)
    return credentials


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


def native_refresh_cpa(target: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.time()
    refreshed_at = now_iso()
    if progress is not None:
        progress['stage'] = '读取远端凭证列表'
        progress['percent'] = 1
    warden_map = load_cpa_warden_accounts(target)
    t0 = time.time()
    raw_files = fetch_cpa_auth_files(target)
    t1 = time.time()
    if progress is not None:
        progress['total'] = len(raw_files)
        progress['scanned'] = 0
        progress['stage'] = '正在扫描远端凭证'
        progress['percent'] = 3 if raw_files else 100
    hydrated = hydrate_live_quota(target, raw_files, progress=progress)
    t2 = time.time()
    if progress is not None:
        progress['stage'] = '正在汇总刷新结果'
        progress['percent'] = 95 if raw_files else 100
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
        summary['accounts'] = accounts
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


def extract_quota_reset_at(rate_limit: dict[str, Any] | None) -> str | None:
    if not isinstance(rate_limit, dict):
        return None
    window = rate_limit.get('primary_window') if isinstance(rate_limit.get('primary_window'), dict) else rate_limit
    if not isinstance(window, dict):
        return None
    for key in ('reset_at', 'resets_at', 'next_refresh_at', 'refresh_at', 'reset_time', 'next_reset_at'):
        value = window.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        try:
            if value is None or value == '':
                continue
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            pass
    for key in ('reset_ts', 'reset_at_ts', 'next_refresh_ts', 'refresh_ts'):
        value = window.get(key)
        try:
            if value is None or value == '':
                continue
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            continue
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
    r = requests.post(url, headers=mgmt_headers(target['token'], include_json=True), json=payload, timeout=12)
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
        'quota_reset_at': extract_quota_reset_at(rate_limit),
        'cached_at_ts': now_ts,
    }
    quota_probe_cache[cache_key] = dict(result)
    return result


def hydrate_live_quota(target: dict[str, Any], files: list[dict[str, Any]], progress: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    need = []
    for idx, item in enumerate(files):
        if item.get('quota_remaining_ratio') is None and item.get('usage_remaining_ratio') is None:
            if item.get('auth_index') and ((item.get('id_token') or {}).get('chatgpt_account_id') or item.get('chatgpt_account_id')):
                need.append((idx, item))
    if progress is not None:
        progress['total'] = len(files)
        progress['scanned'] = 0
        progress['probe_total'] = len(need)
        progress['probe_done'] = 0
        progress['stage'] = '正在扫描远端凭证'
    if not need:
        if progress is not None:
            progress['scanned'] = len(files)
            progress['percent'] = 100
        return files
    max_workers = min(MAX_BULK_WORKERS, len(need))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(probe_cpa_quota, target, item): idx for idx, item in need}
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            if not info:
                if progress is not None:
                    progress['probe_done'] = int(progress.get('probe_done') or 0) + 1
                    progress['scanned'] = min(len(files), int(round((progress['probe_done'] / max(progress['probe_total'], 1)) * len(files))))
                    progress['percent'] = int(round((progress['scanned'] / max(len(files), 1)) * 100))
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
            if info.get('quota_reset_at'):
                files[idx]['quota_reset_at'] = info['quota_reset_at']
            if progress is not None:
                progress['probe_done'] = int(progress.get('probe_done') or 0) + 1
                progress['scanned'] = min(len(files), int(round((progress['probe_done'] / max(progress['probe_total'], 1)) * len(files))))
                progress['percent'] = int(round((progress['scanned'] / max(len(files), 1)) * 100))
    if progress is not None:
        progress['scanned'] = len(files)
        progress['percent'] = 100
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
        'quota_reset_at': item.get('quota_reset_at') or item.get('next_refresh_at') or item.get('reset_at'),
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
        quota_reset_at = acc.get('quota_reset_at') or w.get('quota_reset_at')

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
            'quota_reset_at': quota_reset_at,
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
            'quota_reset_at': w.get('quota_reset_at'),
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
        summary['accounts'] = accounts

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


def fetch_cpa_auth_file_content(target: dict[str, Any], name: str) -> dict[str, Any]:
    base = target['base_url'].rstrip('/')
    raw_url = f"{base}/v0/management/auth-files?name={urllib.parse.quote(name, safe='')}"
    try:
        r = requests.get(raw_url, headers=mgmt_headers(target['token']), timeout=30)
        content_type = str(r.headers.get('content-type') or '').lower()
        body_text = r.text or ''
        payload = None
        if 'application/json' in content_type:
            try:
                payload = r.json()
            except Exception:
                payload = None
        if isinstance(payload, dict):
            if isinstance(payload.get('content'), str):
                body_text = payload.get('content') or ''
            elif isinstance(payload.get('file'), dict) and isinstance(payload['file'].get('content'), str):
                body_text = payload['file'].get('content') or ''
            elif isinstance(payload.get('data'), dict) and isinstance(payload['data'].get('content'), str):
                body_text = payload['data'].get('content') or ''
        return {'ok': r.ok, 'status_code': r.status_code, 'text': body_text, 'content_type': content_type}
    except Exception as exc:
        return {'ok': False, 'status_code': None, 'text': str(exc), 'content_type': ''}


def save_credentials_to_store(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own_conn = False
    if conn is None:
        conn = get_conn()
        own_conn = True
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
    if own_conn:
        conn.commit()
        conn.close()
    return {'saved': saved, 'skipped': skipped, 'dedupe_removed': int((dedupe or {}).get('removed') or 0)}


def delete_cpa_auth_file(target: dict[str, Any], name: str) -> dict[str, Any]:
    url = f"{target['base_url'].rstrip('/')}/v0/management/auth-files?name={urllib.parse.quote(name, safe='')}"
    r = requests.delete(url, headers=mgmt_headers(target['token']), timeout=30)
    return {'ok': r.ok, 'status_code': r.status_code, 'text': r.text[:500]}


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/cpas/overview')
def api_cpas_overview():
    return jsonify({'cpas': [cpa_summary(t, live=False) for t in load_cpas()]})


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
    targets = load_cpas()
    def worker(target: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        try:
            native = native_refresh_cpa(target)
            return {
                'id': target['id'],
                'name': target['name'],
                'mode': 'native-refresh',
                'metrics': native.get('metrics', {}),
                'elapsed_ms': int((time.time() - started) * 1000),
            }
        except Exception as exc:
            return {
                'id': target['id'],
                'name': target['name'],
                'error': str(exc),
                'elapsed_ms': int((time.time() - started) * 1000),
            }
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, targets))
    return jsonify({'results': results, 'cpas': [cpa_summary(t, live=False) for t in load_cpas()]})



@app.get('/api/credentials')
def api_list_credentials():
    dedupe = cleanup_duplicate_credentials()
    cpas = load_cpas()
    credentials = serialize_credentials_for_list(cpas)
    return jsonify({'credentials': credentials, 'cpas': cpas, 'dedupe_removed': int((dedupe or {}).get('removed') or 0)})


@app.post('/api/credentials/import')
def api_import_credentials():
    data = request.get_json(force=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': '没有可导入的凭证'}), 400
    result = save_credentials_to_store(items)
    cpas = load_cpas()
    return jsonify({'saved': result.get('saved', []), 'skipped': result.get('skipped', []), 'dedupe_removed': result.get('dedupe_removed', 0), 'credentials': serialize_credentials_for_list(cpas), 'cpas': cpas})


@app.post('/api/credentials/import-bulk/start')
def api_import_credentials_bulk_start():
    data = request.get_json(force=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list) or not items:
        return jsonify({'error': '没有可导入的凭证'}), 400
    task_id = uuid.uuid4().hex[:16]
    progress = {'task_id': task_id, 'type': 'import-bulk', 'stage': '已创建本地上传仓库任务', 'total': len(items), 'scanned': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'percent': 0, 'done': False, 'ok': False, 'error': None, 'elapsed_ms': 0}
    refresh_progress_store[task_id] = progress
    def runner():
        started = time.time()
        try:
            normalized = []
            for item in items:
                content = str((item or {}).get('content') or '').strip()
                if not content:
                    progress['scanned'] = int(progress.get('scanned') or 0) + 1
                    progress['failed'] = int(progress.get('failed') or 0) + 1
                    total = max(int(progress.get('total') or 0), 1)
                    progress['percent'] = round((int(progress.get('scanned') or 0) / total) * 100)
                    continue
                normalized.append({
                    'name': str((item or {}).get('name') or (item or {}).get('filename') or '').strip(),
                    'filename': str((item or {}).get('filename') or (item or {}).get('name') or '').strip(),
                    'content': content,
                })
                progress['scanned'] = int(progress.get('scanned') or 0) + 1
                total = max(int(progress.get('total') or 0), 1)
                progress['percent'] = round((int(progress.get('scanned') or 0) / total) * 100)
            progress['stage'] = '正在写入仓库'
            result = save_credentials_to_store(normalized)
            progress['success'] = len(result.get('saved', []))
            progress['skipped'] = len(result.get('skipped', []))
            progress['failed'] = int(progress.get('failed') or 0)
            progress['done'] = True
            progress['ok'] = True
            progress['percent'] = 100
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            cpas = load_cpas()
            progress['result'] = {'saved': result.get('saved', []), 'skipped': result.get('skipped', []), 'dedupe_removed': result.get('dedupe_removed', 0), 'credentials': serialize_credentials_for_list(cpas), 'cpas': cpas}
        except Exception as exc:
            progress['done'] = True
            progress['ok'] = False
            progress['error'] = str(exc)
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'error': str(exc)}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@app.delete('/api/credentials/<cred_id>')
def api_delete_credential(cred_id: str):
    conn = get_conn()
    conn.execute('UPDATE credential_store SET archived = 1, updated_at = ? WHERE id = ?', (now_iso(), cred_id))
    conn.commit()
    conn.close()
    cpas = load_cpas()
    return jsonify({'ok': True, 'credentials': serialize_credentials_for_list(cpas), 'cpas': cpas})


def _deploy_credentials_to_target(target: dict[str, Any], credential_ids: list[str], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    if not credential_ids:
        return {'error': '没有选择凭证', 'status': 400}
    conn = get_conn()
    qmarks = ','.join('?' for _ in credential_ids)
    rows = [dict(r) for r in conn.execute(f'SELECT * FROM credential_store WHERE archived = 0 AND id IN ({qmarks})', tuple(credential_ids)).fetchall()]
    now = now_iso()
    if progress is not None:
        progress['stage'] = '正在上传到 CPA'
        progress['total'] = len(rows)
        progress['scanned'] = 0
        progress['success'] = 0
        progress['failed'] = 0
        progress['skipped'] = 0
        progress['percent'] = 0
    def worker(row: dict[str, Any]) -> dict[str, Any]:
        name = row.get('filename') or row.get('name') or f"{row['id']}.json"
        result = upload_cpa_auth_file(target, name, row.get('content') or '')
        ok = bool(result.get('ok'))
        return {
            'id': row['id'],
            'name': row['name'],
            'filename': name,
            'ok': ok,
            'status_code': result.get('status_code'),
            'text': result.get('text'),
            'mode': result.get('mode'),
        }
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_BULK_WORKERS, len(rows) or 1))) as pool:
        fut_map = {pool.submit(worker, row): row for row in rows}
        for fut in as_completed(fut_map):
            item = fut.result()
            results.append(item)
            if progress is not None:
                progress['scanned'] = int(progress.get('scanned') or 0) + 1
                if item.get('ok'):
                    progress['success'] = int(progress.get('success') or 0) + 1
                else:
                    progress['failed'] = int(progress.get('failed') or 0) + 1
                total = max(int(progress.get('total') or 0), 1)
                progress['percent'] = round((int(progress.get('scanned') or 0) / total) * 100)
    for item in results:
        if item.get('ok'):
            conn.execute('UPDATE credential_store SET last_used_at = ?, last_target_id = ?, uploaded_to_cpa = 1, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE id = ?', (now, target['id'], '可用 / 限额', '', now, item['id']))
    conn.commit()
    conn.close()
    cpas = load_cpas()
    return {'results': results, 'credentials': serialize_credentials_for_list(cpas), 'cpas': cpas, 'cpa': cpa_summary(target)}


@app.post('/api/credentials/deploy')
def api_deploy_credentials():
    data = request.get_json(force=True) or {}
    target_id = str(data.get('target_id') or '').strip()
    credential_ids = data.get('credential_ids') or []
    target = next((x for x in load_cpas() if x['id'] == target_id), None)
    if not target:
        return jsonify({'error': '目标 CPA 不存在'}), 404
    result = _deploy_credentials_to_target(target, credential_ids)
    if result.get('error'):
        return jsonify({'error': result['error']}), int(result.get('status') or 400)
    return jsonify(result)


@app.post('/api/credentials/deploy-bulk')
def api_deploy_credentials_bulk():
    data = request.get_json(force=True) or {}
    target_id = str(data.get('target_id') or '').strip()
    credential_ids = data.get('credential_ids') or []
    target = next((x for x in load_cpas() if x['id'] == target_id), None)
    if not target:
        return jsonify({'error': '目标 CPA 不存在'}), 404
    result = _deploy_credentials_to_target(target, credential_ids)
    if result.get('error'):
        return jsonify({'error': result['error']}), int(result.get('status') or 400)
    return jsonify(result)


@app.post('/api/credentials/deploy-bulk/start')
def api_deploy_credentials_bulk_start():
    data = request.get_json(force=True) or {}
    target_id = str(data.get('target_id') or '').strip()
    credential_ids = data.get('credential_ids') or []
    target = next((x for x in load_cpas() if x['id'] == target_id), None)
    if not target:
        return jsonify({'error': '目标 CPA 不存在'}), 404
    task_id = uuid.uuid4().hex[:16]
    progress = {'task_id': task_id, 'type': 'deploy-bulk', 'stage': '已创建上传到 CPA 任务', 'total': 0, 'scanned': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'percent': 0, 'done': False, 'ok': False, 'error': None, 'elapsed_ms': 0}
    refresh_progress_store[task_id] = progress
    def runner():
        started = time.time()
        try:
            result = _deploy_credentials_to_target(target, credential_ids, progress=progress)
            progress['done'] = True
            progress['ok'] = True
            progress['percent'] = 100
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = result
        except Exception as exc:
            progress['done'] = True
            progress['ok'] = False
            progress['error'] = str(exc)
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'error': str(exc)}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@app.get('/api/cpas/<cpa_id>')
def api_get_cpa(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    return jsonify({'cpa': cpa_summary(target, live=False)})



@app.post('/api/cpas/<cpa_id>/refresh/start')
def api_refresh_cpa_start(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    task_id = uuid.uuid4().hex[:16]
    progress = {'task_id': task_id, 'cpa_id': cpa_id, 'stage': '已创建刷新任务', 'total': 0, 'scanned': 0, 'percent': 0, 'done': False, 'ok': False, 'error': None, 'elapsed_ms': 0}
    refresh_progress_store[task_id] = progress
    def runner():
        started = time.time()
        try:
            result = native_refresh_cpa(target, progress=progress)
            progress['done'] = True
            progress['ok'] = True
            progress['percent'] = 100
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'ok': True, 'scan': {'mode': 'native-refresh', 'metrics': result.get('metrics', {})}, 'cpa': result.get('summary') or cpa_summary(target, live=False), 'elapsed_ms': progress['elapsed_ms']}
        except Exception as exc:
            progress['done'] = True
            progress['ok'] = False
            progress['error'] = str(exc)
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'error': str(exc), 'cpa': cpa_summary(target, live=False), 'elapsed_ms': progress['elapsed_ms']}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@app.get('/api/cpas/refresh/<task_id>')
def api_refresh_cpa_progress(task_id: str):
    progress = refresh_progress_store.get(task_id)
    if not progress:
        return jsonify({'error': '刷新任务不存在'}), 404
    return jsonify(progress)


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
    bulk = _bulk_delete_cpa_auth_files(target, [file_name])
    result = (bulk.get('results') or [{}])[0]
    code = 200 if result.get('ok') else 500
    return jsonify({'result': result, 'cpa': bulk.get('cpa'), 'credentials': bulk.get('credentials'), 'cpas': bulk.get('cpas')}), code


@app.post('/api/cpas/<cpa_id>/auth-files/bulk-delete')
def api_bulk_delete_cpa_auth_files(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    data = request.get_json(force=True) or {}
    result = _bulk_delete_cpa_auth_files(target, data.get('file_names') or [])
    if result.get('error'):
        return jsonify({'error': result['error']}), int(result.get('status') or 400)
    return jsonify(result)


@app.post('/api/cpas/<cpa_id>/auth-files/bulk-delete/start')
def api_bulk_delete_cpa_auth_files_start(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    data = request.get_json(force=True) or {}
    file_names = data.get('file_names') or []
    task_id = uuid.uuid4().hex[:16]
    progress = {'task_id': task_id, 'type': 'bulk-delete', 'cpa_id': cpa_id, 'stage': '已创建删除任务', 'total': 0, 'scanned': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'percent': 0, 'done': False, 'ok': False, 'error': None, 'elapsed_ms': 0}
    refresh_progress_store[task_id] = progress
    def runner():
        started = time.time()
        try:
            result = _bulk_delete_cpa_auth_files(target, file_names, progress=progress)
            progress['done'] = True
            progress['ok'] = True
            progress['percent'] = 100
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = result
        except Exception as exc:
            progress['done'] = True
            progress['ok'] = False
            progress['error'] = str(exc)
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'error': str(exc)}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


def _bulk_save_cpa_auth_files_to_store(target: dict[str, Any], file_names: list[str], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    decoded_names = [urllib.parse.unquote(str(x or '').strip()) for x in file_names if str(x or '').strip()]
    if not decoded_names:
        return {'error': '没有选择凭证', 'status': 400}
    if progress is not None:
        progress['stage'] = '正在拉取并上传到仓库'
        progress['total'] = len(decoded_names)
        progress['scanned'] = 0
        progress['success'] = 0
        progress['failed'] = 0
        progress['skipped'] = 0
        progress['percent'] = 0
    def worker(name: str) -> dict[str, Any]:
        fetched = fetch_cpa_auth_file_content(target, name)
        return {'name': name, 'fetched': fetched}
    fetched_results = []
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_BULK_WORKERS, len(decoded_names)))) as pool:
        fut_map = {pool.submit(worker, name): name for name in decoded_names}
        for fut in as_completed(fut_map):
            item = fut.result()
            fetched_results.append(item)
            if progress is not None:
                progress['scanned'] = int(progress.get('scanned') or 0) + 1
                if item.get('fetched', {}).get('ok'):
                    progress['success'] = int(progress.get('success') or 0) + 1
                else:
                    progress['failed'] = int(progress.get('failed') or 0) + 1
                total = max(int(progress.get('total') or 0), 1)
                progress['percent'] = round((int(progress.get('scanned') or 0) / total) * 100)
    items = []
    failed = []
    for item in fetched_results:
        fetched = item['fetched']
        if not fetched.get('ok'):
            failed.append({'name': item['name'], 'error': fetched.get('text') or '拉取凭证失败', 'status_code': fetched.get('status_code')})
            continue
        items.append({
            'name': item['name'],
            'filename': item['name'],
            'content': fetched.get('text') or '',
            'note': f"from-cpa:{target.get('name')}",
            'tags': 'from-cpa',
        })
    result = save_credentials_to_store(items)
    if progress is not None:
        progress['success'] = len(result.get('saved', []))
        progress['skipped'] = len(result.get('skipped', []))
        progress['failed'] = len(failed)
        progress['percent'] = 100
        progress['stage'] = '正在汇总上传仓库结果'
    cpas = load_cpas()
    return {
        'ok': True,
        'saved': result.get('saved', []),
        'skipped': result.get('skipped', []),
        'failed': failed,
        'dedupe_removed': result.get('dedupe_removed', 0),
        'credentials': serialize_credentials_for_list(cpas),
        'cpas': cpas,
        'cpa': cpa_summary(target),
    }


def _bulk_delete_cpa_auth_files(target: dict[str, Any], file_names: list[str], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    decoded_names = [urllib.parse.unquote(str(x or '').strip()) for x in file_names if str(x or '').strip()]
    if not decoded_names:
        return {'error': '没有选择凭证', 'status': 400}
    if progress is not None:
        progress['stage'] = '正在删除 CPA 凭证'
        progress['total'] = len(decoded_names)
        progress['scanned'] = 0
        progress['success'] = 0
        progress['failed'] = 0
        progress['skipped'] = 0
        progress['percent'] = 0
    def worker(name: str) -> dict[str, Any]:
        res = delete_cpa_auth_file(target, name)
        return {'name': name, 'ok': bool(res.get('ok')), 'status_code': res.get('status_code'), 'text': res.get('text')}
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_BULK_WORKERS, len(decoded_names)))) as pool:
        fut_map = {pool.submit(worker, name): name for name in decoded_names}
        for fut in as_completed(fut_map):
            item = fut.result()
            results.append(item)
            if progress is not None:
                progress['scanned'] = int(progress.get('scanned') or 0) + 1
                if item.get('ok'):
                    progress['success'] = int(progress.get('success') or 0) + 1
                else:
                    progress['failed'] = int(progress.get('failed') or 0) + 1
                total = max(int(progress.get('total') or 0), 1)
                progress['percent'] = round((int(progress.get('scanned') or 0) / total) * 100)
    conn = get_conn()
    now = now_iso()
    for item in results:
        if item.get('ok'):
            conn.execute('UPDATE credential_store SET uploaded_to_cpa = 0, upload_status_text = ?, upload_error_detail = ?, updated_at = ? WHERE archived = 0 AND last_target_id = ? AND filename = ?', ('', '', now, target['id'], item['name']))
    conn.commit()
    conn.close()
    cpas = load_cpas()
    return {'ok': True, 'results': results, 'credentials': serialize_credentials_for_list(cpas), 'cpas': cpas, 'cpa': cpa_summary(target)}


@app.post('/api/cpas/<cpa_id>/auth-files/<path:file_name>/save-to-store')
def api_save_cpa_auth_file_to_store(cpa_id: str, file_name: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    result = _bulk_save_cpa_auth_files_to_store(target, [file_name])
    if result.get('error'):
        return jsonify({'error': result['error']}), int(result.get('status') or 400)
    return jsonify(result)


@app.post('/api/cpas/<cpa_id>/auth-files/bulk-save-to-store')
def api_bulk_save_cpa_auth_files_to_store(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    data = request.get_json(force=True) or {}
    result = _bulk_save_cpa_auth_files_to_store(target, data.get('file_names') or [])
    if result.get('error'):
        return jsonify({'error': result['error']}), int(result.get('status') or 400)
    return jsonify(result)


@app.post('/api/cpas/<cpa_id>/auth-files/bulk-save-to-store/start')
def api_bulk_save_cpa_auth_files_to_store_start(cpa_id: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    data = request.get_json(force=True) or {}
    file_names = data.get('file_names') or []
    task_id = uuid.uuid4().hex[:16]
    progress = {'task_id': task_id, 'type': 'bulk-save-to-store', 'cpa_id': cpa_id, 'stage': '已创建上传仓库任务', 'total': 0, 'scanned': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'percent': 0, 'done': False, 'ok': False, 'error': None, 'elapsed_ms': 0}
    refresh_progress_store[task_id] = progress
    def runner():
        started = time.time()
        try:
            result = _bulk_save_cpa_auth_files_to_store(target, file_names, progress=progress)
            progress['done'] = True
            progress['ok'] = True
            progress['percent'] = 100
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = result
        except Exception as exc:
            progress['done'] = True
            progress['ok'] = False
            progress['error'] = str(exc)
            progress['elapsed_ms'] = int((time.time() - started) * 1000)
            progress['result'] = {'error': str(exc)}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@app.get('/api/cpas/<cpa_id>/auth-files/<path:file_name>/export')
def api_export_single_cpa_auth_file(cpa_id: str, file_name: str):
    target = next((x for x in load_cpas() if x['id'] == cpa_id), None)
    if not target:
        return jsonify({'error': 'CPA 不存在'}), 404
    decoded_name = urllib.parse.unquote(file_name)
    fetched = fetch_cpa_auth_file_content(target, decoded_name)
    if not fetched.get('ok'):
        return jsonify({'error': fetched.get('text') or '拉取凭证失败', 'result': fetched}), 500
    return Response(
        fetched.get('text') or '',
        mimetype='application/json',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{urllib.parse.quote(decoded_name)}"},
    )


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

if __name__ == '__main__':
    init_db()
    app.run(host=HOST, port=PORT, debug=False)
