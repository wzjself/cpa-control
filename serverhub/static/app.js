let usageChart, trafficChart;
let currentRange = '24h';
const selectedCredentialIds = new Set();
let credentialSearch = '';
let credentialUploadFilter = 'all';
let latestCredentials = [];
let latestCpas = [];
const recentlyHighlightedCredentialIds = new Set();

const els = {
  healthScore: document.getElementById('healthScore'),
  cpuNow: document.getElementById('cpuNow'),
  memNow: document.getElementById('memNow'),
  diskNow: document.getElementById('diskNow'),
  trafficNow: document.getElementById('trafficNow'),
  cpaList: document.getElementById('cpaList'),
  cpaForm: document.getElementById('cpaForm'),
  refreshServerBtn: document.getElementById('refreshServerBtn'),
  refreshAllBtn: document.getElementById('refreshAllBtn'),
  scanCpasBtn: document.getElementById('scanCpasBtn'),
  usageChartTitle: document.getElementById('usageChartTitle'),
  trafficChartTitle: document.getElementById('trafficChartTitle'),
  timeRangeSwitch: document.getElementById('timeRangeSwitch'),
  credentialStoreList: document.getElementById('credentialStoreList'),
  syncCredentialStatusBtn: document.getElementById('syncCredentialStatusBtn'),
  selectAllCredentialsBtn: document.getElementById('selectAllCredentialsBtn'),
  deploySelectedBtn: document.getElementById('deploySelectedBtn'),
  credentialTargetSelect: document.getElementById('credentialTargetSelect'),
  credentialSearchInput: document.getElementById('credentialSearchInput'),
  credentialUploadFilter: document.getElementById('credentialUploadFilter'),
  credentialFilesInput: document.getElementById('credentialFilesInput'),
  uploadCredentialFilesBtn: document.getElementById('uploadCredentialFilesBtn'),
  credentialStoreCount: document.getElementById('credentialStoreCount'),
  credentialStoreHint: document.getElementById('credentialStoreHint'),
};

const fmtPct = n => `${Number(n ?? 0).toFixed(1)}%`;
const fmtMaybePct = n => (n === null || n === undefined || Number.isNaN(Number(n))) ? '未知' : `${Number(n).toFixed(1)}%`;
const fmtNum = n => new Intl.NumberFormat('zh-CN').format(Number(n || 0));
const fmtMb = n => `${Number(n || 0).toFixed(1)} MB`;
const rangeLabel = key => ({'3h':'3小时','24h':'24小时','7d':'7天','30d':'30天','all':'所有时间'}[key] || '24小时');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = s => {
  if (!s) return '未知';
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return String(s);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
};
function setButtonLoading(button, loading, loadingText = '处理中...') {
  if (!button) return;
  if (loading) {
    if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.classList.add('is-loading');
    button.textContent = loadingText;
  } else {
    button.disabled = false;
    button.classList.remove('is-loading');
    button.textContent = button.dataset.originalText || button.textContent;
  }
}
function withButtonLoading(button, loadingText, fn) {
  return async (...args) => {
    setButtonLoading(button, true, loadingText);
    try { return await fn(...args); }
    finally { setButtonLoading(button, false); }
  };
}
function matchesCredentialSearch(item, keyword) {
  const q = String(keyword || '').trim().toLowerCase();
  if (!q) return true;
  return [item.name, item.filename, item.note, item.tags, item.last_target_name].some(x => String(x || '').toLowerCase().includes(q));
}
function matchesUploadFilter(item) {
  if (credentialUploadFilter === 'uploaded') return Boolean(item.uploaded_to_cpa);
  if (credentialUploadFilter === 'pending') return !item.uploaded_to_cpa;
  return true;
}
function accountStatusText(acc) {
  if (acc.invalid_401) return '401失效';
  if (acc.quota_limited) return '额度耗尽';
  const status = String(acc.status || '').toLowerCase();
  if (status === 'active') return '正常';
  if (status === 'disabled') return '已禁用';
  if (status === 'error') return '异常';
  return status ? status : '未知';
}
function accountStatusClass(acc) {
  if (acc.invalid_401) return 'err';
  if (acc.quota_limited) return 'warn';
  return 'ok';
}
function setActiveRangeBtn() { document.querySelectorAll('.range-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.range === currentRange)); }
function downsample(arr, maxPoints = 120) { if (!Array.isArray(arr) || arr.length <= maxPoints) return arr || []; const step = Math.ceil(arr.length / maxPoints); return arr.filter((_, i) => i % step === 0 || i === arr.length - 1); }
function chartLabels(history) { return history.map(item => { const d = new Date(item.ts); return (currentRange === '7d' || currentRange === '30d' || currentRange === 'all') ? `${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` : `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; }); }
function destroyCanvasChart(canvas) { if (!canvas) return; const ctx = canvas.getContext && canvas.getContext('2d'); if (ctx) ctx.clearRect(0, 0, canvas.width || 0, canvas.height || 0); }
function drawSimpleLineChart(canvas, datasets, opts = {}) { if (!canvas) return; const dpr = Math.max(1, window.devicePixelRatio || 1), cssWidth = Math.max(320, canvas.clientWidth || canvas.parentElement?.clientWidth || 600), cssHeight = Math.max(220, opts.height || 240); canvas.width = Math.floor(cssWidth * dpr); canvas.height = Math.floor(cssHeight * dpr); canvas.style.width = `${cssWidth}px`; canvas.style.height = `${cssHeight}px`; const ctx = canvas.getContext('2d'); if (!ctx) return; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, cssWidth, cssHeight); const pad = { top: 12, right: 10, bottom: 24, left: 34 }, w = cssWidth - pad.left - pad.right, h = cssHeight - pad.top - pad.bottom; if (w <= 10 || h <= 10) return; const values = datasets.flatMap(ds => ds.data || []).filter(v => Number.isFinite(v)); if (!values.length) return; let minY = Number.isFinite(opts.minY) ? opts.minY : Math.min(...values), maxY = Number.isFinite(opts.maxY) ? opts.maxY : Math.max(...values); if (minY === maxY) { minY -= 1; maxY += 1; } if (opts.beginAtZero) minY = Math.min(0, minY); const toX = (idx, total) => pad.left + (total <= 1 ? 0 : (w * idx / (total - 1))), toY = val => pad.top + h - ((val - minY) / (maxY - minY)) * h; ctx.strokeStyle = 'rgba(158,177,206,0.18)'; ctx.lineWidth = 1; for (let i = 0; i <= 4; i++) { const y = pad.top + (h * i / 4); ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + w, y); ctx.stroke(); } ctx.strokeStyle = 'rgba(158,177,206,0.35)'; ctx.beginPath(); ctx.moveTo(pad.left, pad.top); ctx.lineTo(pad.left, pad.top + h); ctx.lineTo(pad.left + w, pad.top + h); ctx.stroke(); datasets.forEach(ds => { const data = ds.data || []; ctx.strokeStyle = ds.color || '#60a5fa'; ctx.lineWidth = 2; ctx.beginPath(); data.forEach((v, i) => { const x = toX(i, data.length), y = toY(Number(v) || 0); if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); }); ctx.stroke(); }); ctx.fillStyle = '#9eb1ce'; ctx.font = '12px Inter, system-ui, sans-serif'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; for (let i = 0; i <= 4; i++) { const v = maxY - ((maxY - minY) * i / 4), y = pad.top + (h * i / 4); ctx.fillText(`${Math.round(v * 10) / 10}`, pad.left - 6, y); } const labels = opts.labels || [], tickCount = Math.min(6, labels.length); if (tickCount > 0) { ctx.textAlign = 'center'; ctx.textBaseline = 'top'; for (let i = 0; i < tickCount; i++) { const idx = Math.round((labels.length - 1) * (tickCount === 1 ? 0 : i / (tickCount - 1))); ctx.fillText(String(labels[idx] || ''), toX(idx, labels.length), pad.top + h + 6); } } }
function renderChartFallback(canvas, message) { if (!canvas) return; const parent = canvas.parentElement; if (!parent) return; let note = parent.querySelector('.chart-fallback'); if (!note) { note = document.createElement('div'); note.className = 'muted chart-fallback'; note.style.marginTop = '8px'; parent.appendChild(note); } note.textContent = message; }
function clearChartFallback(canvas) { const note = canvas?.parentElement?.querySelector('.chart-fallback'); if (note) note.remove(); }
function renderUsageChart(historyRaw) { const history = downsample(historyRaw, currentRange === '3h' ? 90 : currentRange === '24h' ? 120 : 160), canvas = document.getElementById('usageChart'); if (!canvas || !history.length) { renderChartFallback(canvas, '暂无历史数据'); return; } clearChartFallback(canvas); destroyCanvasChart(canvas); drawSimpleLineChart(canvas, [{ label: 'CPU', data: history.map(x => Number(x.cpu_percent) || 0), color: '#60a5fa' }, { label: '内存', data: history.map(x => Number(x.mem_percent) || 0), color: '#34d399' }, { label: '磁盘', data: history.map(x => Number(x.disk_percent) || 0), color: '#f59e0b' }], { labels: chartLabels(history), minY: 0, maxY: 100, beginAtZero: true, height: 240 }); }
function seriesRatePerSecond(history, key) { if (!Array.isArray(history) || history.length < 2) return []; const out = []; for (let i = 1; i < history.length; i++) { const prev = history[i - 1], cur = history[i]; const prevVal = Number(prev?.[key]), curVal = Number(cur?.[key]); const prevTs = new Date(prev?.ts || 0).getTime(), curTs = new Date(cur?.ts || 0).getTime(); if (!Number.isFinite(prevVal) || !Number.isFinite(curVal) || !prevTs || !curTs || curTs <= prevTs) { out.push(0); continue; } const diffMb = Math.max(0, curVal - prevVal), seconds = (curTs - prevTs) / 1000; out.push(seconds > 0 ? (diffMb * 1024) / seconds : 0); } return out; }
function renderTrafficChart(historyRaw) { const history = downsample(historyRaw, currentRange === '3h' ? 90 : currentRange === '24h' ? 120 : 160), canvas = document.getElementById('trafficChart'); if (!canvas || history.length < 2) { renderChartFallback(canvas, '暂无历史速率数据'); return; } clearChartFallback(canvas); destroyCanvasChart(canvas); const rx = seriesRatePerSecond(history, 'net_rx_mb'), tx = seriesRatePerSecond(history, 'net_tx_mb'), labels = chartLabels(history).slice(1), allVals = rx.concat(tx); drawSimpleLineChart(canvas, [{ label: '下载速率', data: rx, color: '#22c55e' }, { label: '上传速率', data: tx, color: '#f472b6' }], { labels, minY: 0, maxY: Math.max(1, ...allVals), height: 240 }); renderChartFallback(canvas, '单位：网络速率（KB/s）'); }
function renderCredentialStore(credentials = [], cpas = []) {
  latestCredentials = credentials || []; latestCpas = cpas || [];
  if (els.credentialTargetSelect) {
    const prev = els.credentialTargetSelect.value;
    els.credentialTargetSelect.innerHTML = '<option value="">选择目标 CPA</option>' + latestCpas.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    if ([...els.credentialTargetSelect.options].some(o => o.value === prev)) els.credentialTargetSelect.value = prev;
  }
  const filtered = latestCredentials.filter(item => matchesCredentialSearch(item, credentialSearch) && matchesUploadFilter(item));
  if (els.credentialStoreCount) els.credentialStoreCount.textContent = `仓库已有 ${latestCredentials.length} 个凭证`;
  if (els.credentialStoreHint) els.credentialStoreHint.textContent = filtered.length === latestCredentials.length ? `当前显示 ${filtered.length} 个` : `筛选后 ${filtered.length} / ${latestCredentials.length} 个`;
  els.credentialStoreList.innerHTML = filtered.map(item => `
    <label class="credential-item ${recentlyHighlightedCredentialIds.has(item.id) ? 'recent-upload-highlight' : ''}" data-cred-id="${item.id}">
      <input type="checkbox" ${selectedCredentialIds.has(item.id) ? 'checked' : ''} onchange="toggleCredentialSelect('${item.id}', this.checked)">
      <div class="credential-main">
        <strong title="${esc(item.name)}">${esc(item.name)}</strong>
        <div class="muted">${esc(item.filename)}</div>
      </div>
      <div class="muted">上传 ${fmtTime(item.uploaded_at)}</div>
      <div class="muted">最近投放：${item.last_target_name ? `${esc(item.last_target_name)} · ${fmtTime(item.last_used_at)}` : '暂无'}</div>
      <div class="cred-status ${item.uploaded_to_cpa ? 'ok' : 'pending'}">${item.uploaded_to_cpa ? '<span class="cred-check">√</span><span>已上传</span>' : '<span>未上传</span>'}</div>
      <div class="credential-actions"><button type="button" class="danger small-btn" onclick="deleteCredential('${item.id}')">删除</button></div>
    </label>
  `).join('') || '<div class="muted">没有匹配的凭证。</div>';
}
function renderCpaCard(cpa) { const knownQuotaAccounts = (cpa.accounts || []).filter(acc => acc.remaining_ratio !== null && acc.remaining_ratio !== undefined).length; const unknownQuotaAccounts = Math.max(0, (cpa.total || 0) - knownQuotaAccounts); return `<div class="cpa-card" data-cpa-id="${cpa.id}"><div class="cpa-head"><div><div><strong>${cpa.name}</strong> <span class="badge">${cpa.provider}</span></div><div class="muted">${cpa.base_url}</div></div><div class="cpa-actions"><button class="ghost small-btn js-refresh-cpa" onclick="refreshCpa('${cpa.id}', this)">刷新当前 CPA</button><button class="ghost small-btn js-delete-401" onclick="delete401('${cpa.id}', this)">一键删除401</button><button class="danger small-btn" onclick="deleteCpa('${cpa.id}')">删除 CPA</button></div></div><div class="mini-grid"><div class="mini-stat"><div class="muted">总凭证</div><div class="n">${fmtNum(cpa.total)}</div></div><div class="mini-stat"><div class="muted">401</div><div class="n err">${fmtNum(cpa.invalid_401)}</div></div><div class="mini-stat"><div class="muted">limit</div><div class="n warn">${fmtNum(cpa.quota_limited)}</div></div><div class="mini-stat"><div class="muted">健康</div><div class="n ok">${fmtNum(cpa.healthy)}</div></div></div><div class="quota-row"><div class="quota-label"><span>总凭证额度使用情况</span><span>${cpa.remaining_ratio === null || cpa.remaining_ratio === undefined ? `已知额度 ${knownQuotaAccounts} 个 · 未知 ${unknownQuotaAccounts} 个` : `剩余 ${fmtMaybePct(cpa.remaining_ratio)} / 已用 ${fmtMaybePct(cpa.used_ratio)} · 未知 ${unknownQuotaAccounts} 个`}</span></div><div class="progress remain"><span style="width:${cpa.remaining_ratio ?? 0}%"></span></div></div><div class="muted" style="margin:10px 0">当前凭证状态（实时读取 CPA 后台）</div><div class="account-grid compact-account-grid">${(cpa.accounts || []).map(acc => `<div class="account-chip"><div class="account-chip-top"><strong title="${esc(acc.email || acc.name)}">${esc(acc.email || acc.name)}</strong><span class="${accountStatusClass(acc)}">${accountStatusText(acc)}</span></div><div class="progress mini-remain"><span style="width:${acc.remaining_ratio ?? 0}%"></span></div><div class="muted">剩余 ${fmtMaybePct(acc.remaining_ratio)}</div><div class="muted">${acc.quota_checked_at ? `更新 ${fmtTime(acc.quota_checked_at)}` : '未取到时间'}</div><div class="account-actions"><button class="danger small-btn" onclick="deleteAuthFile('${cpa.id}', '${encodeURIComponent(acc.name || acc.email || '')}')">删除</button></div></div>`).join('') || '<div class="muted">暂无状态数据，点“刷新并扫描全部 CPA”重试。</div>'}</div></div>`; }
function replaceCpaCard(cpa) { const old = els.cpaList.querySelector(`[data-cpa-id="${cpa.id}"]`), html = renderCpaCard(cpa); if (old) old.outerHTML = html; else els.cpaList.insertAdjacentHTML('afterbegin', html); }
function renderCpas(cpas) { els.cpaList.innerHTML = cpas.map(renderCpaCard).join(''); }
function renderServer(server) { els.healthScore.textContent = server.health; els.cpuNow.textContent = fmtPct(server.latest.cpu_percent); els.memNow.textContent = fmtPct(server.latest.mem_percent); els.diskNow.textContent = fmtPct(server.latest.disk_percent); els.trafficNow.textContent = `${fmtMb(server.traffic_24h.rx_mb)} / ${fmtMb(server.traffic_24h.tx_mb)}`; els.usageChartTitle.textContent = `CPU / 内存 / 磁盘（${rangeLabel(currentRange)}）`; els.trafficChartTitle.textContent = `网络上行 / 下载历史（${rangeLabel(currentRange)}）`; setActiveRangeBtn(); try { renderUsageChart(server.history || []); } catch (e) { console.error('usage chart render failed', e); } try { renderTrafficChart(server.history || []); } catch (e) { console.error('traffic chart render failed', e); } }
async function loadServerStatus(forceBust = false) { const url = `/api/server-status?range=${encodeURIComponent(currentRange)}${forceBust ? `&_t=${Date.now()}` : ''}`; const res = await fetch(url, { cache: 'no-store' }); const data = await res.json(); renderServer(data.server); }
async function loadCredentials() { const res = await fetch('/api/credentials', { cache: 'no-store' }); const data = await res.json(); renderCredentialStore(data.credentials || [], data.cpas || []); }
async function loadAll(forceBust = false) { const url = `/api/overview?range=${encodeURIComponent(currentRange)}${forceBust ? `&_t=${Date.now()}` : ''}`; const res = await fetch(url, { cache: 'no-store' }); const data = await res.json(); renderServer(data.server); renderCpas(data.cpas || []); await loadCredentials(); }
async function deleteCpa(id) { if (!confirm('确认删除这个 CPA 吗？')) return; await fetch(`/api/cpas/${id}`, {method:'DELETE'}); const card = els.cpaList.querySelector(`[data-cpa-id="${id}"]`); if (card) card.remove(); loadCredentials(); }
async function refreshCpa(id, button) { const run = async () => { const res = await fetch(`/api/cpas/${id}/refresh`, {method:'POST'}); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); }; return withButtonLoading(button, '刷新中...', run)(); }
async function delete401(id, button) { if (!confirm('确认一键删除这个 CPA 里的 401 凭证吗？')) return; const run = async () => { const res = await fetch(`/api/cpas/${id}/delete-401`, {method:'POST'}); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); if (data.credentials) renderCredentialStore(data.credentials, latestCpas); }; return withButtonLoading(button, '清理中...', run)(); }
async function deleteAuthFile(cpaId, encodedName) { if (!confirm('确认删除这个凭证吗？')) return; const res = await fetch(`/api/cpas/${cpaId}/auth-files/${encodedName}`, {method:'DELETE'}); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); if (data.credentials) renderCredentialStore(data.credentials, latestCpas); if (!res.ok) alert((data.result && data.result.text) || '删除失败'); }
function toggleCredentialSelect(id, checked) { if (checked) selectedCredentialIds.add(id); else selectedCredentialIds.delete(id); }
async function deleteCredential(id) { if (!confirm('确认从仓库删除这个凭证吗？')) return; const res = await fetch(`/api/credentials/${id}`, { method: 'DELETE' }); const data = await res.json(); selectedCredentialIds.delete(id); renderCredentialStore(data.credentials || [], data.cpas || latestCpas); }
function markRecentlyUploaded(ids = []) { ids.filter(Boolean).forEach(id => recentlyHighlightedCredentialIds.add(id)); renderCredentialStore(latestCredentials, latestCpas); setTimeout(() => { ids.forEach(id => recentlyHighlightedCredentialIds.delete(id)); renderCredentialStore(latestCredentials, latestCpas); }, 5000); }
function summarizeDeployResults(results = []) { const okItems = results.filter(x => x.ok); const failItems = results.filter(x => !x.ok); const failText = failItems.slice(0, 3).map(x => `${x.filename || x.name}（${x.status_code || 'ERR'}）`).join('、'); return { okItems, failItems, message: failItems.length ? `上传完成：成功 ${okItems.length} 个，失败 ${failItems.length} 个${failText ? `\n失败项：${failText}` : ''}` : `上传成功：${okItems.length} 个`, }; }
async function importCredentialFiles(files, button) { const fileList = Array.from(files || []).filter(Boolean); if (!fileList.length) return; const run = async () => { const targetId = els.credentialTargetSelect?.value || ''; if (!targetId) { if (els.credentialFilesInput) els.credentialFilesInput.value = ''; return alert('先选择目标 CPA，再上传凭证文件'); } const items = await Promise.all(fileList.map(async file => ({ name: file.name, filename: file.name, content: await file.text() }))); const importRes = await fetch('/api/credentials/import', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ items }) }); const importData = await importRes.json(); if (!importRes.ok) { if (els.credentialFilesInput) els.credentialFilesInput.value = ''; return alert(importData.error || '入仓失败'); } const importedIds = (importData.saved || []).map(x => x.id).filter(Boolean); if (!importedIds.length) { if (els.credentialFilesInput) els.credentialFilesInput.value = ''; renderCredentialStore(importData.credentials || [], latestCpas); return alert('文件已读取，但没有成功入仓'); } const deployRes = await fetch('/api/credentials/deploy', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_id: targetId, credential_ids: importedIds }) }); const deployData = await deployRes.json(); if (deployData.cpa) replaceCpaCard(deployData.cpa); renderCredentialStore(deployData.credentials || importData.credentials || [], latestCpas); const summary = summarizeDeployResults(deployData.results || []); importedIds.forEach(id => { if (summary.okItems.some(x => x.id === id)) selectedCredentialIds.delete(id); else selectedCredentialIds.add(id); }); if (els.credentialFilesInput) els.credentialFilesInput.value = ''; alert(summary.message); }; return withButtonLoading(button, '上传中...', run)(); }
async function deploySelectedCredentials() { const run = async () => { const targetId = els.credentialTargetSelect.value, ids = Array.from(selectedCredentialIds); if (!targetId) return alert('先选择目标 CPA'); if (!ids.length) return alert('先勾选仓库里的凭证'); const res = await fetch('/api/credentials/deploy', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_id: targetId, credential_ids: ids }) }); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); renderCredentialStore(data.credentials || [], latestCpas); const summary = summarizeDeployResults(data.results || []); summary.failItems.forEach(x => selectedCredentialIds.add(x.id)); summary.okItems.forEach(x => selectedCredentialIds.delete(x.id)); alert(summary.message); }; return withButtonLoading(els.deploySelectedBtn, '上传中...', run)(); }
async function addCpa(e) { e.preventDefault(); const fd = new FormData(els.cpaForm); const payload = Object.fromEntries(fd.entries()); await fetch('/api/cpas', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}); els.cpaForm.reset(); await loadAll(true); }
async function scanCpas() { const run = async () => { await fetch('/api/cpas/scan', {method:'POST'}); await loadAll(true); }; return withButtonLoading(els.scanCpasBtn, '扫描中...', run)(); }
async function refreshAll() { const run = async () => { await loadAll(true); }; return withButtonLoading(els.refreshAllBtn, '刷新中...', run)(); }
async function syncCredentialStatus() { const run = async () => { const res = await fetch('/api/credentials/sync-upload-status', { method:'POST' }); const data = await res.json(); renderCredentialStore(data.credentials || [], data.cpas || latestCpas); alert(`状态已更新：匹配到 ${data.matched || 0} 个已上传凭证`); }; return withButtonLoading(els.syncCredentialStatusBtn, '更新中...', run)(); }
async function refreshServerOnly() { const run = async () => { await loadServerStatus(true); }; return withButtonLoading(els.refreshServerBtn, '刷新中...', run)(); }
window.deleteCpa = deleteCpa; window.refreshCpa = refreshCpa; window.delete401 = delete401; window.deleteAuthFile = deleteAuthFile; window.toggleCredentialSelect = toggleCredentialSelect; window.deleteCredential = deleteCredential;
els.cpaForm?.addEventListener('submit', addCpa);
els.refreshServerBtn?.addEventListener('click', refreshServerOnly);
els.refreshAllBtn?.addEventListener('click', refreshAll);
els.scanCpasBtn?.addEventListener('click', scanCpas);
els.syncCredentialStatusBtn?.addEventListener('click', syncCredentialStatus);
els.selectAllCredentialsBtn?.addEventListener('click', () => { const boxes = Array.from(document.querySelectorAll('#credentialStoreList input[type="checkbox"]')); const allChecked = boxes.length > 0 && boxes.every(el => el.checked); boxes.forEach(el => { const next = !allChecked; el.checked = next; const id = el.closest('.credential-item')?.dataset.credId; if (id) toggleCredentialSelect(id, next); }); });
els.deploySelectedBtn?.addEventListener('click', deploySelectedCredentials);
els.credentialSearchInput?.addEventListener('input', e => { credentialSearch = e.target.value || ''; renderCredentialStore(latestCredentials, latestCpas); });
els.credentialUploadFilter?.addEventListener('change', e => { credentialUploadFilter = e.target.value || 'all'; renderCredentialStore(latestCredentials, latestCpas); });
els.uploadCredentialFilesBtn?.addEventListener('click', () => els.credentialFilesInput?.click());
els.credentialFilesInput?.addEventListener('change', e => importCredentialFiles(e.target.files, els.uploadCredentialFilesBtn));
els.timeRangeSwitch?.querySelectorAll('.range-btn').forEach(btn => btn.addEventListener('click', async () => { currentRange = btn.dataset.range || '24h'; await loadAll(true); }));
loadAll(true);
setInterval(() => loadServerStatus(true), 2000);
setInterval(() => loadAll(true), 30000);
