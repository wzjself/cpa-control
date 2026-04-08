from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import urllib.parse
import uuid
import zipfile
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from services.core import *  # noqa: F401,F403

app = Flask(__name__, template_folder="templates", static_folder="static")

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
