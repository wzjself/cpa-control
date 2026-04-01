let usageChart, trafficChart;
let currentRange = '24h';
const selectedCredentialIds = new Set();
let latestServerPayload = null;
let credentialSearch = '';
let credentialUploadFilter = 'all';
let latestCredentials = [];
let latestCpas = [];
let credentialStoreExpanded = false;
let credentialVisibleCount = 50;
const CREDENTIAL_PAGE_SIZE = 50;
let credentialAutoLoadBusy = false;
const recentlyHighlightedCredentialIds = new Set();
const selectedCpaAuthIds = new Set();

const els = {
  healthScore: document.getElementById('healthScore'),
  cpuNow: document.getElementById('cpuNow'),
  memNow: document.getElementById('memNow'),
  diskNow: document.getElementById('diskNow'),
  trafficNow: document.getElementById('trafficNow'),
  netRateNow: document.getElementById('netRateNow'),
  relayRequestNow: document.getElementById('relayRequestNow'),
  relayTokenNow: document.getElementById('relayTokenNow'),
  relayRateNow: document.getElementById('relayRateNow'),
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
  autoRefreshHint: document.getElementById('autoRefreshHint'),
  toggleCredentialFoldBtn: document.getElementById('toggleCredentialFoldBtn'),
  uiNotice: document.getElementById('uiNotice'),
  uiNoticeTitle: document.getElementById('uiNoticeTitle'),
  uiNoticeBody: document.getElementById('uiNoticeBody'),
  uiNoticeCloseBtn: document.getElementById('uiNoticeCloseBtn'),
  progressPanel: document.getElementById('progressPanel'),
  progressPanelHint: document.getElementById('progressPanelHint'),
  progressLogList: document.getElementById('progressLogList'),
  clearProgressLogBtn: document.getElementById('clearProgressLogBtn'),
};

const fmtPct = n => `${Number(n ?? 0).toFixed(1)}%`;
const fmtMaybePct = n => (n === null || n === undefined || Number.isNaN(Number(n))) ? '未知' : `${Number(n).toFixed(1)}%`;
const fmtNum = n => new Intl.NumberFormat('zh-CN').format(Number(n || 0));
const fmtMb = n => `${Number(n || 0).toFixed(1)} MB`;
const fmtKbps = n => `${Number(n || 0).toFixed(1)} KB/s`;
const setText = (el, value) => { if (!el) return; const next = String(value ?? ''); if (el.textContent !== next) el.textContent = next; };
const rangeLabel = key => ({'3h':'3小时','24h':'24小时','7d':'7天','30d':'30天','all':'所有时间'}[key] || '24小时');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = s => {
  if (!s) return '未知';
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return String(s);
  return `${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
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
let uiNoticeTimer = null;
let progressLogSeq = 0;
let credentialSearchDebounceTimer = null;
const BULK_OP_CONCURRENCY = 40;
function showUiNotice(title, lines, autoHideMs = 0) {
  if (!els.uiNotice || !els.uiNoticeTitle || !els.uiNoticeBody) return;
  els.uiNoticeTitle.textContent = title || '提示';
  els.uiNoticeBody.textContent = Array.isArray(lines) ? lines.filter(Boolean).join('\n') : String(lines || '');
  els.uiNotice.classList.remove('hidden');
  if (uiNoticeTimer) clearTimeout(uiNoticeTimer);
  if (autoHideMs > 0) uiNoticeTimer = setTimeout(() => els.uiNotice?.classList.add('hidden'), autoHideMs);
}
function hideUiNotice() {
  if (uiNoticeTimer) clearTimeout(uiNoticeTimer);
  els.uiNotice?.classList.add('hidden');
}
function appendProgressLog(tag, message, level = 'info') {
  if (!els.progressLogList) return;
  if (els.progressPanelHint) els.progressPanelHint.textContent = `最新：${tag} · ${message}`;
  progressLogSeq += 1;
  const item = document.createElement('div');
  item.className = `progress-log-item ${level === 'error' ? 'err' : level === 'success' ? 'ok' : level === 'warn' ? 'warn' : ''}`;
  item.dataset.seq = String(progressLogSeq);
  item.dataset.tag = String(tag || '状态');
  item.innerHTML = `<div class="top"><span class="tag">${esc(tag || '状态')}</span><span class="time">${new Date().toLocaleTimeString('zh-CN', { hour12: false })}</span></div><div class="msg">${esc(message || '')}</div>`;
  els.progressLogList.prepend(item);
  while (els.progressLogList.children.length > 120) els.progressLogList.removeChild(els.progressLogList.lastChild);
}
function upsertProgressLog(tag, message, level = 'info') {
  if (!els.progressLogList) return;
  const tagKey = String(tag || '状态');
  const existing = els.progressLogList.querySelector(`.progress-log-item[data-tag="${tagKey.replace(/"/g, '&quot;')}"]`);
  if (!existing) return appendProgressLog(tag, message, level);
  existing.className = `progress-log-item ${level === 'error' ? 'err' : level === 'success' ? 'ok' : level === 'warn' ? 'warn' : ''}`;
  const timeEl = existing.querySelector('.time');
  const msgEl = existing.querySelector('.msg');
  if (timeEl) timeEl.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  if (msgEl) msgEl.textContent = String(message || '');
  if (els.progressPanelHint) els.progressPanelHint.textContent = `最新：${tag} · ${message}`;
}
function clearProgressLog() {
  if (els.progressLogList) els.progressLogList.innerHTML = '';
  if (els.progressPanelHint) els.progressPanelHint.textContent = '显示上传到 CPA、刷新 CPA、仓库同步等实时记录，可滚动回看历史';
}
async function mapLimit(items, limit, worker) {
  const list = Array.isArray(items) ? items : [];
  const concurrency = Math.max(1, Number(limit) || 1);
  const results = new Array(list.length);
  let cursor = 0;
  async function runOne() {
    while (true) {
      const index = cursor;
      cursor += 1;
      if (index >= list.length) return;
      results[index] = await worker(list[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, list.length) }, () => runOne()));
  return results;
}
function cpaAuthKey(cpaId, name) { return `${cpaId}::${decodeURIComponent(String(name || ''))}`; }
function refreshCpaCardSelectionUi(cpaId = '') {
  const cards = cpaId ? Array.from(document.querySelectorAll(`.cpa-card[data-cpa-id="${cpaId}"]`)) : Array.from(document.querySelectorAll('.cpa-card'));
  cards.forEach(card => {
    const currentId = card.dataset.cpaId || '';
    const count = Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${currentId}::`)).length;
    const countEl = card.querySelector('.selected-cpa-auth-count');
    const hintEl = card.querySelector('.selected-cpa-auth-hint');
    if (countEl) countEl.textContent = `已选择 ${count} 个凭证`;
    if (hintEl) hintEl.textContent = count ? '下一步：删除 / 上传仓库 / 下载凭证文件' : '可选择全部 / 异常 / 401';
    card.querySelectorAll('.account-pick input[type="checkbox"]').forEach(box => {
      const encodedName = box.dataset.encodedName || '';
      box.checked = selectedCpaAuthIds.has(cpaAuthKey(currentId, encodedName));
    });
  });
}
function updateSelectedCpaAuthMeta() { refreshCpaCardSelectionUi(''); }
function toggleCpaAuthSelect(cpaId, encodedName, checked) {
  const key = cpaAuthKey(cpaId, encodedName);
  if (checked) selectedCpaAuthIds.add(key); else selectedCpaAuthIds.delete(key);
  const cpa = latestCpas.find(x => String(x.id) === String(cpaId));
  if (cpa) replaceCpaCard(cpa);
  else refreshCpaCardSelectionUi(cpaId);
}
function getSelectedCpaAuthItems(cpaId = '') {
  return Array.from(selectedCpaAuthIds).map(key => { const [id, ...rest] = key.split('::'); return { cpa_id: id, file_name: rest.join('::') }; }).filter(x => x.cpa_id && x.file_name && (!cpaId || String(x.cpa_id) === String(cpaId)));
}
function classifySelectedCpaAuth(acc) {
  if (acc.invalid_401) return '401';
  const status = String(acc.status || '').toLowerCase();
  if ((!acc.invalid_401) && (!acc.quota_limited) && ['error','exception','abnormal','unknown'].includes(status)) return 'abnormal';
  return 'other';
}
function selectCpaAuthsByMode(cpaId, mode) {
  Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${cpaId}::`)).forEach(key => selectedCpaAuthIds.delete(key));
  const cpa = latestCpas.find(x => String(x.id) === String(cpaId));
  (cpa?.accounts || []).forEach(acc => {
    const kind = classifySelectedCpaAuth(acc);
    const shouldPick = mode === 'all' ? true : mode === '401' ? kind === '401' : mode === 'abnormal' ? kind === 'abnormal' : false;
    if (shouldPick) selectedCpaAuthIds.add(cpaAuthKey(cpaId, encodeURIComponent(acc.name || acc.email || '')));
  });
  if (cpa) replaceCpaCard(cpa);
  else refreshCpaCardSelectionUi(cpaId);
}
function clearCpaAuthSelection(cpaId) {
  Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${cpaId}::`)).forEach(key => selectedCpaAuthIds.delete(key));
  const cpa = latestCpas.find(x => String(x.id) === String(cpaId));
  if (cpa) replaceCpaCard(cpa);
  else refreshCpaCardSelectionUi(cpaId);
}
function matchesCredentialSearch(item, keyword) {
  const q = String(keyword || '').trim().toLowerCase();
  if (!q) return true;
  return [item.name, item.filename, item.note, item.tags, item.last_target_name].some(x => String(x || '').toLowerCase().includes(q));
}
function matchesUploadFilter(item) {
  const activeTargetId = els.credentialTargetSelect?.value || '';
  const presentIn = Array.isArray(item?.present_in_cpas) ? item.present_in_cpas : [];
  const matchedCurrentTarget = activeTargetId ? presentIn.some(x => String(x?.cpa_id) === String(activeTargetId)) : Boolean(item.uploaded_to_cpa);
  if (credentialUploadFilter === 'uploaded') return matchedCurrentTarget;
  if (credentialUploadFilter === 'pending') return !matchedCurrentTarget;
  return true;
}
function accountStatusText(acc) {
  if (acc.invalid_401) return '401失效';
  if (acc.quota_limited) return '额度耗尽';
  const status = String(acc.status || '').toLowerCase();
  if (status === 'active') return '正常';
  if (status === 'disabled') return '已禁用';
  if (['error','exception','abnormal','unknown'].includes(status)) return '异常';
  return status ? status : '未知';
}
function accountStatusClass(acc) {
  if (acc.invalid_401) return 'err';
  if (acc.quota_limited) return 'warn';
  const status = String(acc.status || '').toLowerCase();
  if (['error','exception','abnormal','unknown'].includes(status)) return 'warn';
  return 'ok';
}
function setActiveRangeBtn() { document.querySelectorAll('.range-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.range === currentRange)); }
function downsample(arr, maxPoints = 120) { if (!Array.isArray(arr) || arr.length <= maxPoints) return arr || []; const step = Math.ceil(arr.length / maxPoints); return arr.filter((_, i) => i % step === 0 || i === arr.length - 1); }
function chartLabels(history) { return history.map(item => { const d = new Date(item.ts); return (currentRange === '7d' || currentRange === '30d' || currentRange === 'all') ? `${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` : `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; }); }
function ensureChartSvgHost(canvas) {
  if (!canvas) return null;
  const parent = canvas.parentElement;
  if (!parent) return null;
  let host = parent.querySelector('.chart-svg-host');
  if (!host) {
    host = document.createElement('div');
    host.className = 'chart-svg-host';
    parent.appendChild(host);
  }
  canvas.style.display = 'none';
  return host;
}
function destroyCanvasChart(canvas) {
  if (!canvas) return;
  try {
    if (typeof Chart !== 'undefined') {
      const chart = Chart.getChart(canvas);
      if (chart) chart.destroy();
    }
  } catch (_) {}
  const ctx = canvas.getContext && canvas.getContext('2d');
  if (ctx) ctx.clearRect(0, 0, canvas.width || 0, canvas.height || 0);
  const host = canvas.parentElement?.querySelector('.chart-svg-host');
  if (host) host.innerHTML = '';
}
function drawFastLineChart(canvas, datasets, opts = {}) {
  if (!canvas) return;
  destroyCanvasChart(canvas);
  const host = ensureChartSvgHost(canvas);
  if (!host) return;
  const labels = opts.labels || [];
  const width = Math.max(320, canvas.clientWidth || host.clientWidth || canvas.parentElement?.clientWidth || 600);
  const height = Math.max(220, opts.height || 240);
  const pad = { top: 12, right: 10, bottom: 26, left: 42 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const values = datasets.flatMap(ds => ds.data || []).map(v => Number(v)).filter(v => Number.isFinite(v));
  if (!values.length || innerW <= 10 || innerH <= 10) return;
  let minY = Number.isFinite(opts.minY) ? Number(opts.minY) : Math.min(...values);
  let maxY = Number.isFinite(opts.maxY) ? Number(opts.maxY) : Math.max(...values);
  if (opts.beginAtZero) minY = Math.min(0, minY);
  if (minY === maxY) { minY -= 1; maxY += 1; }
  const escXml = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const toX = (idx, total) => pad.left + (total <= 1 ? 0 : (innerW * idx / (total - 1)));
  const toY = (val) => pad.top + innerH - ((Number(val) - minY) / (maxY - minY)) * innerH;
  const grid = Array.from({ length: 5 }, (_, i) => {
    const y = pad.top + (innerH * i / 4);
    const v = maxY - ((maxY - minY) * i / 4);
    return `
      <line x1="${pad.left}" y1="${y}" x2="${pad.left + innerW}" y2="${y}" stroke="rgba(158,177,206,0.12)" stroke-width="1" />
      <text x="${pad.left - 6}" y="${y}" fill="#9eb1ce" font-size="12" text-anchor="end" dominant-baseline="middle">${Math.round(v)}</text>`;
  }).join('');
  const tickCount = Math.min(6, labels.length || 0);
  const xTicks = tickCount ? Array.from({ length: tickCount }, (_, i) => {
    const idx = Math.round((labels.length - 1) * (tickCount === 1 ? 0 : i / (tickCount - 1)));
    const x = toX(idx, labels.length);
    return `<text x="${x}" y="${pad.top + innerH + 18}" fill="#9eb1ce" font-size="12" text-anchor="middle">${escXml(labels[idx] || '')}</text>`;
  }).join('') : '';
  const paths = datasets.map(ds => {
    const data = (ds.data || []).map(v => Number(v) || 0);
    const d = data.map((v, i) => `${i === 0 ? 'M' : 'L'} ${toX(i, data.length).toFixed(2)} ${toY(v).toFixed(2)}`).join(' ');
    return `<path d="${d}" fill="none" stroke="${escXml(ds.color || '#60a5fa')}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`;
  }).join('');
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" aria-label="chart">
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent" />
    ${grid}
    <line x1="${pad.left}" y1="${pad.top + innerH}" x2="${pad.left + innerW}" y2="${pad.top + innerH}" stroke="rgba(158,177,206,0.3)" stroke-width="1" />
    <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${pad.top + innerH}" stroke="rgba(158,177,206,0.3)" stroke-width="1" />
    ${paths}
    ${xTicks}
  </svg>`;
}
function renderChartFallback(canvas, message) { if (!canvas) return; const parent = canvas.parentElement; if (!parent) return; let note = parent.querySelector('.chart-fallback'); if (!note) { note = document.createElement('div'); note.className = 'muted chart-fallback'; note.style.marginTop = '8px'; parent.appendChild(note); } note.textContent = message; }
function clearChartFallback(canvas) { const note = canvas?.parentElement?.querySelector('.chart-fallback'); if (note) note.remove(); }
function renderUsageChart(historyRaw) { const history = downsample(historyRaw, currentRange === '3h' ? 90 : currentRange === '24h' ? 120 : 160), canvas = document.getElementById('usageChart'); if (!canvas || !history.length) { renderChartFallback(canvas, '暂无历史数据'); return; } clearChartFallback(canvas); drawFastLineChart(canvas, [{ label: 'CPU', data: history.map(x => Number(x.cpu_percent) || 0), color: '#60a5fa' }, { label: '内存', data: history.map(x => Number(x.mem_percent) || 0), color: '#34d399' }, { label: '磁盘', data: history.map(x => Number(x.disk_percent) || 0), color: '#f59e0b' }], { labels: chartLabels(history), minY: 0, maxY: 100, beginAtZero: true, height: 240 }); }
function seriesRatePerSecond(history, key) { if (!Array.isArray(history) || history.length < 2) return []; const out = []; for (let i = 1; i < history.length; i++) { const prev = history[i - 1], cur = history[i]; const prevVal = Number(prev?.[key]), curVal = Number(cur?.[key]); const prevTs = new Date(prev?.ts || 0).getTime(), curTs = new Date(cur?.ts || 0).getTime(); if (!Number.isFinite(prevVal) || !Number.isFinite(curVal) || !prevTs || !curTs || curTs <= prevTs) { out.push(0); continue; } const diffMb = Math.max(0, curVal - prevVal), seconds = (curTs - prevTs) / 1000; out.push(seconds > 0 ? (diffMb * 1024) / seconds : 0); } return out; }
function renderTrafficChart(historyRaw) { const history = downsample(historyRaw, currentRange === '3h' ? 90 : currentRange === '24h' ? 120 : 160), canvas = document.getElementById('trafficChart'); if (!canvas || history.length < 2) { renderChartFallback(canvas, '暂无历史速率数据'); return; } clearChartFallback(canvas); const rx = seriesRatePerSecond(history, 'net_rx_mb'), tx = seriesRatePerSecond(history, 'net_tx_mb'), labels = chartLabels(history).slice(1), allVals = rx.concat(tx); drawFastLineChart(canvas, [{ label: '下载速率', data: rx, color: '#22c55e' }, { label: '上传速率', data: tx, color: '#f472b6' }], { labels, minY: 0, maxY: Math.max(1, ...allVals), height: 240 }); renderChartFallback(canvas, '单位：网络速率（KB/s）'); }
function renderCredentialStore(credentials = [], cpas = []) {
  latestCredentials = Array.isArray(credentials) ? credentials : [];
  if (Array.isArray(cpas) && cpas.some(c => Array.isArray(c?.accounts))) latestCpas = cpas;
  const currentTargetId = els.credentialTargetSelect?.value || '';
  if (els.credentialTargetSelect) {
    const prev = currentTargetId;
    els.credentialTargetSelect.innerHTML = latestCpas.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    const freeOpt = latestCpas.find(c => String(c.name || '').trim().toLowerCase() === 'free');
    const nextValue = ([...els.credentialTargetSelect.options].some(o => o.value === prev) && prev) ? prev : (freeOpt ? freeOpt.id : latestCpas[0]?.id || '');
    if (nextValue) els.credentialTargetSelect.value = nextValue;
  }
  const activeTargetId = els.credentialTargetSelect?.value || '';
  const activeTarget = latestCpas.find(c => String(c.id) === String(activeTargetId));
  let filtered = latestCredentials.filter(item => matchesCredentialSearch(item, credentialSearch) && matchesUploadFilter(item));
  if (!filtered.length && latestCredentials.length && (credentialSearch || credentialUploadFilter !== 'all')) {
    credentialSearch = '';
    credentialUploadFilter = 'all';
    credentialVisibleCount = CREDENTIAL_PAGE_SIZE;
    if (els.credentialSearchInput) els.credentialSearchInput.value = '';
    if (els.credentialUploadFilter) els.credentialUploadFilter.value = 'all';
    filtered = latestCredentials.slice();
  }
  const visibleCount = Math.min(Math.max(CREDENTIAL_PAGE_SIZE, credentialVisibleCount), filtered.length || CREDENTIAL_PAGE_SIZE);
  const visibleItems = filtered.slice(0, visibleCount);
  const hasMore = filtered.length > visibleItems.length;
  if (els.credentialStoreCount) els.credentialStoreCount.textContent = `仓库已有 ${latestCredentials.length} 个凭证`;
  if (els.credentialStoreHint) {
    const scopeText = filtered.length === latestCredentials.length ? `当前显示 ${visibleItems.length} / ${latestCredentials.length} 个凭证` : `筛选后显示 ${visibleItems.length} / ${filtered.length} 个（总 ${latestCredentials.length} 个）`;
    els.credentialStoreHint.textContent = activeTarget ? `当前目标：${activeTarget.name} · ${scopeText}` : scopeText;
  }
  if (els.toggleCredentialFoldBtn) els.toggleCredentialFoldBtn.textContent = credentialStoreExpanded ? '折叠' : '展开';
  els.credentialStoreList?.classList.toggle('credential-store-folded', !credentialStoreExpanded);
  els.credentialStoreList?.classList.toggle('credential-store-expanded', credentialStoreExpanded);
  const itemsHtml = visibleItems.map(item => {
    const presentIn = Array.isArray(item.present_in_cpas) ? item.present_in_cpas : [];
    const currentHit = presentIn.find(x => String(x.cpa_id) === String(activeTargetId));
    const currentStateClass = currentHit ? (currentHit.good ? 'ok' : 'bad') : 'pending';
    const currentStateText = currentHit ? `${currentHit.cpa_name}：${currentHit.status_text}` : `未上传到 ${activeTarget?.name || '当前目标 CPA'}`;
    const presenceTags = presentIn.length
      ? presentIn.map(x => `<span class="presence-tag ${String(x.cpa_id) === String(activeTargetId) ? 'is-current' : ''} ${x.good ? 'ok' : 'bad'}">${esc(x.cpa_name)} · ${esc(x.status_text)}</span>`).join('')
      : '<span class="presence-tag pending">未在缓存名单中发现</span>';
    return `
    <label class="credential-item ${recentlyHighlightedCredentialIds.has(item.id) ? 'recent-upload-highlight' : ''} ${currentHit ? 'is-present-in-target' : 'is-missing-in-target'}" data-cred-id="${item.id}">
      <input type="checkbox" ${selectedCredentialIds.has(item.id) ? 'checked' : ''} onchange="toggleCredentialSelect('${item.id}', this.checked)">
      <div class="credential-main">
        <strong title="${esc(item.name)}">${esc(item.name)}</strong>
        <div class="muted">${esc(item.filename)}</div>
      </div>
      <div class="muted">上传 ${fmtTime(item.uploaded_at)}</div>
      <div class="muted">最近投放：${item.last_target_name ? `${esc(item.last_target_name)} · ${fmtTime(item.last_used_at)}` : '暂无'}</div>
      <div class="credential-presence-block">
        <div class="cred-status current-target-status ${currentStateClass}">${currentHit && currentHit.good ? '<span class=\"cred-check\">√</span>' : ''}<span>${esc(currentStateText)}</span>${item.upload_error_detail && String(item.last_target_id) === String(activeTargetId) ? `<span class="muted"> · ${esc(item.upload_error_detail)}</span>` : ''}</div>
        <div class="credential-presence-tags">${presenceTags}</div>
      </div>
      <div class="credential-actions"><button type="button" class="danger small-btn" onclick="deleteCredential('${item.id}')">删除</button></div>
    </label>
  `;
  }).join('');
  const moreHtml = hasMore ? `<div class="credential-load-more"><button type="button" class="ghost" id="loadMoreCredentialsBtn">加载更多（还有 ${filtered.length - visibleItems.length} 个）</button></div>` : '';
  els.credentialStoreList.innerHTML = itemsHtml || '<div class="muted">没有匹配的凭证。</div>';
  if (moreHtml) els.credentialStoreList.insertAdjacentHTML('beforeend', moreHtml);
  document.getElementById('loadMoreCredentialsBtn')?.addEventListener('click', () => {
    credentialVisibleCount += CREDENTIAL_PAGE_SIZE;
    renderCredentialStore(latestCredentials, latestCpas);
  });
  els.credentialStoreList?.removeEventListener('scroll', handleCredentialStoreScroll);
  els.credentialStoreList?.addEventListener('scroll', handleCredentialStoreScroll, { passive: true });
}
function handleCredentialStoreScroll() {
  if (!els.credentialStoreList || credentialAutoLoadBusy) return;
  const el = els.credentialStoreList;
  const nearBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 80);
  const totalFiltered = latestCredentials.filter(item => matchesCredentialSearch(item, credentialSearch) && matchesUploadFilter(item)).length;
  if (!nearBottom || credentialVisibleCount >= totalFiltered) return;
  credentialAutoLoadBusy = true;
  credentialVisibleCount += CREDENTIAL_PAGE_SIZE;
  renderCredentialStore(latestCredentials, latestCpas);
  setTimeout(() => { credentialAutoLoadBusy = false; }, 80);
}

function renderCpaCard(cpa) {
  const knownQuotaAccounts = (cpa.accounts || []).filter(acc => acc.remaining_ratio !== null && acc.remaining_ratio !== undefined).length;
  const unknownQuotaAccounts = Math.max(0, (cpa.total || 0) - knownQuotaAccounts);
  const expanded = Boolean(cpa.expanded);
  const visibleAccounts = cpa.accounts || [];
  const hiddenCount = Math.max(0, (cpa.accounts || []).length - 12);
  const selectedCount = Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${cpa.id}::`)).length;
  const accountHtml = visibleAccounts.map(acc => {
    const encodedName = encodeURIComponent(acc.name || acc.email || '');
    const checked = selectedCpaAuthIds.has(cpaAuthKey(cpa.id, encodedName));
    return `<div class="account-chip">
      <label class="account-pick">
        <input type="checkbox" data-encoded-name="${encodedName}" ${checked ? 'checked' : ''} onchange="toggleCpaAuthSelect('${cpa.id}', '${encodedName}', this.checked)">
      </label>
      <div class="account-chip-top"><strong title="${esc(acc.email || acc.name)}">${esc(acc.email || acc.name)}</strong><span class="${accountStatusClass(acc)}">${accountStatusText(acc)}</span></div>
      <div class="progress mini-remain"><span style="width:${acc.remaining_ratio ?? 0}%"></span></div>
      <div class="muted">剩余 ${fmtMaybePct(acc.remaining_ratio)}</div>
      <div class="muted">${acc.quota_reset_at ? `下次额度刷新 ${fmtTime(acc.quota_reset_at)}` : '下次额度刷新时间未知'}</div>
      <div class="muted">${(acc.refreshed_at || acc.quota_checked_at) ? `更新 ${fmtTime(acc.refreshed_at || acc.quota_checked_at)}` : '未取到时间'}</div>
    </div>`;
  }).join('') || '<div class="muted">暂无状态数据，点“刷新并扫描全部 CPA”重试。</div>';
  return `<div class="cpa-card" data-cpa-id="${cpa.id}"><div class="cpa-head"><div class="cpa-title-block"><div class="order-tools"><button class="ghost small-btn order-btn" onclick="moveCpa('${cpa.id}', 'up')">↑</button><button class="ghost small-btn order-btn" onclick="moveCpa('${cpa.id}', 'down')">↓</button></div><div><div><strong>${cpa.name}</strong> <span class="badge">${cpa.provider}</span></div><div class="muted">${cpa.base_url}</div></div></div><div class="cpa-actions"><button class="ghost small-btn js-toggle-expand" onclick="toggleCpaExpand('${cpa.id}', ${expanded ? 'false' : 'true'}, this)">${expanded ? '收起' : '展开'}</button><button class="ghost small-btn" onclick="renameCpa('${cpa.id}', '${esc(cpa.name)}')">重命名</button><button class="ghost small-btn js-refresh-cpa" onclick="refreshCpa('${cpa.id}', this)">刷新当前 CPA</button><button class="danger small-btn" onclick="deleteCpa('${cpa.id}')">删除 CPA</button></div></div><div class="mini-grid"><div class="mini-stat"><div class="muted">总凭证</div><div class="n">${fmtNum(cpa.total)}</div></div><div class="mini-stat"><div class="muted">401</div><div class="n err">${fmtNum(cpa.invalid_401)}</div></div><div class="mini-stat"><div class="muted">异常</div><div class="n warn">${fmtNum(cpa.abnormal)}</div></div><div class="mini-stat"><div class="muted">limit</div><div class="n warn">${fmtNum(cpa.quota_limited)}</div></div><div class="mini-stat"><div class="muted">健康</div><div class="n ok">${fmtNum(cpa.healthy)}</div></div></div><div class="quota-row"><div class="quota-label"><span>总凭证额度使用情况</span><span>${cpa.remaining_ratio === null || cpa.remaining_ratio === undefined ? `总 ${fmtNum(cpa.total)} · 异常 ${fmtNum(cpa.abnormal)} · 已知额度 ${knownQuotaAccounts} 个 · 未知 ${unknownQuotaAccounts} 个` : `总 ${fmtNum(cpa.total)} · 异常 ${fmtNum(cpa.abnormal)} · 剩余 ${fmtMaybePct(cpa.remaining_ratio)} / 已用 ${fmtMaybePct(cpa.used_ratio)} · 未知 ${unknownQuotaAccounts} 个`}</span></div><div class="progress remain"><span style="width:${cpa.remaining_ratio ?? 0}%"></span></div></div><div class="muted" style="margin:10px 0">当前凭证状态（实时读取 CPA 后台）${!expanded && hiddenCount > 0 ? ` · 收起模式，可滚动查看全部 ${fmtNum(cpa.total)} 个` : ''}</div><div class="credential-store-head" style="margin:0 0 10px 0"><strong class="selected-cpa-auth-count">已选择 ${selectedCount} 个凭证</strong><div class="credential-store-tools"><span class="muted selected-cpa-auth-hint">${selectedCount ? '下一步：删除 / 上传仓库 / 下载凭证文件' : '可选择全部 / 异常 / 401'}</span><button type="button" class="ghost small-btn" onclick="selectCpaAuthsByMode('${cpa.id}', 'all')">选择全部</button><button type="button" class="ghost small-btn" onclick="selectCpaAuthsByMode('${cpa.id}', 'abnormal')">选择异常</button><button type="button" class="ghost small-btn" onclick="selectCpaAuthsByMode('${cpa.id}', '401')">选择401</button><button type="button" class="ghost small-btn" onclick="clearCpaAuthSelection('${cpa.id}')">取消选择</button><button type="button" class="small-btn" onclick="saveSelectedCpaAuths('${cpa.id}', this)">上传仓库</button><button type="button" class="ghost small-btn" onclick="exportSelectedCpaAuths('${cpa.id}')">下载凭证文件</button><button type="button" class="danger small-btn" onclick="deleteSelectedCpaAuths('${cpa.id}', this)">删除</button></div></div><div class="account-grid compact-account-grid ${expanded ? 'is-expanded' : 'is-collapsed'}">${accountHtml}</div></div>`;
}
function replaceCpaCard(cpa) { const old = els.cpaList.querySelector(`[data-cpa-id="${cpa.id}"]`), html = renderCpaCard(cpa); if (old) old.outerHTML = html; else els.cpaList.insertAdjacentHTML('afterbegin', html); }
function renderCpas(cpas) { latestCpas = Array.isArray(cpas) ? cpas : []; els.cpaList.innerHTML = latestCpas.map(renderCpaCard).join(''); }
async function toggleCpaExpand(id, expanded, button) { const run = async () => { const res = await fetch(`/api/cpas/${id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ expanded }) }); const data = await res.json(); if (!res.ok) return alert(data.error || '切换失败'); if (data.cpa) replaceCpaCard(data.cpa); }; const label = expanded ? '展开中...' : '收起中...'; return withButtonLoading(button, label, run)(); }
async function renameCpa(id, currentName) { const nextName = prompt('输入新的 CPA 名称', currentName || ''); if (nextName === null) return; const name = String(nextName || '').trim(); if (!name) return alert('名称不能为空'); const res = await fetch(`/api/cpas/${id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ name }) }); const data = await res.json(); if (!res.ok) return alert(data.error || '重命名失败'); if (data.cpa) replaceCpaCard(data.cpa); await loadCredentials(); }
function getCpaOrderIds() { return Array.from(els.cpaList.querySelectorAll('.cpa-card')).map(el => el.dataset.cpaId).filter(Boolean); }
async function moveCpa(id, direction) { const ids = getCpaOrderIds(); const index = ids.indexOf(id); if (index < 0) return; const targetIndex = direction === 'up' ? index - 1 : index + 1; if (targetIndex < 0 || targetIndex >= ids.length) return; const swap = ids[targetIndex]; ids[targetIndex] = ids[index]; ids[index] = swap; const res = await fetch('/api/cpas/reorder', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ ids }) }); const data = await res.json(); if (!res.ok) return alert(data.error || '移动失败'); if (data.cpas) renderCpas(data.cpas); await loadCredentials(); }
function renderServer(server, relay = {}, options = {}) {
  const { skipCharts = false } = options;
  latestServerPayload = { server, relay };
  setText(els.healthScore, relay.health_score ?? server.health);
  setText(els.cpuNow, fmtPct(server.latest.cpu_percent));
  setText(els.memNow, fmtPct(server.latest.mem_percent));
  setText(els.diskNow, fmtPct(server.latest.disk_percent));
  setText(els.netRateNow, `${fmtKbps(server.current_net_kbps?.rx)} / ${fmtKbps(server.current_net_kbps?.tx)}`);
  setText(els.trafficNow, `${fmtMb(server.traffic_24h.rx_mb)} / ${fmtMb(server.traffic_24h.tx_mb)}`);
  setText(els.relayRequestNow, fmtNum(relay.request_count || 0));
  setText(els.relayTokenNow, fmtNum(relay.total_tokens || 0));
  setText(els.relayRateNow, `${fmtNum(relay.rpm || 0)} / ${fmtNum(relay.tpm || 0)}`);
  setText(els.usageChartTitle, `CPU / 内存 / 磁盘（${rangeLabel(currentRange)}）`);
  setText(els.trafficChartTitle, `网络上行 / 下载历史（${rangeLabel(currentRange)}）`);
  setActiveRangeBtn();
  if (skipCharts) return;
  try { renderUsageChart(server.history || []); } catch (e) { console.error('usage chart render failed', e); }
  try { renderTrafficChart(server.history || []); } catch (e) { console.error('traffic chart render failed', e); }
}
async function loadServerStatus(forceBust = false, includeHistory = false) {
  const serverUrl = `/api/server-status?range=${encodeURIComponent(currentRange)}&include_history=${includeHistory ? '1' : '0'}${forceBust ? `&_t=${Date.now()}` : ''}`;
  const serverRes = await fetch(serverUrl, { cache: 'no-store' });
  const serverData = await serverRes.json();
  renderServer(serverData.server, serverData.clirelay || {}, { skipCharts: !includeHistory });
}
async function loadCredentials() { const res = await fetch('/api/credentials', { cache: 'no-store' }); const data = await res.json(); credentialVisibleCount = CREDENTIAL_PAGE_SIZE; renderCredentialStore(data.credentials || [], data.cpas || []); if (els.credentialStoreHint && Number(data.dedupe_removed || 0) > 0) els.credentialStoreHint.textContent = `刷新仓库时已检测并清理 ${data.dedupe_removed} 个同名凭证`; return data; }
async function loadAll(forceBust = false) {
  const url = `/api/overview?range=${encodeURIComponent(currentRange)}${forceBust ? `&_t=${Date.now()}` : ''}`;
  const res = await fetch(url, { cache: 'no-store' });
  const data = await res.json();
  renderServer(data.server, data.clirelay || {});
  renderCpas(data.cpas || []);
  await loadCredentials();
}
async function deleteCpa(id) { if (!confirm('确认删除这个 CPA 吗？')) return; await fetch(`/api/cpas/${id}`, {method:'DELETE'}); const card = els.cpaList.querySelector(`[data-cpa-id="${id}"]`); if (card) card.remove(); loadCredentials(); }
async function refreshCpa(id, button) { const run = async () => { const cpaName = button?.closest('.cpa-card')?.querySelector('strong')?.textContent?.trim() || id; upsertProgressLog('刷新单个 CPA', `${cpaName}：准备刷新`, 'info'); const startRes = await fetch(`/api/cpas/${id}/refresh/start`, { method:'POST' }); const startData = await startRes.json(); if (!startRes.ok || !startData.task_id) { upsertProgressLog('刷新单个 CPA', `${cpaName}：启动刷新失败：${startData.error || '启动失败'}`, 'error'); return alert(startData.error || '启动刷新失败'); } const taskId = startData.task_id; let finalData = null; while (true) { const progressRes = await fetch(`/api/cpas/refresh/${taskId}`, { cache:'no-store' }); const progressData = await progressRes.json(); if (!progressRes.ok) { upsertProgressLog('刷新单个 CPA', `${cpaName}：读取进度失败：${progressData.error || '进度读取失败'}`, 'error'); return alert(progressData.error || '进度读取失败'); } const total = Number(progressData.total || 0); const scanned = Number(progressData.scanned || 0); const percent = Number(progressData.percent || 0); const stage = progressData.stage || '正在刷新'; upsertProgressLog('刷新单个 CPA', `${cpaName}：${stage}${total > 0 ? ` · 已扫描 ${scanned}/${total} · ${percent}%` : ` · ${percent}%`}`, progressData.done ? (progressData.ok ? 'success' : 'error') : 'info'); if (progressData.done) { finalData = progressData.result || null; break; } await new Promise(r => setTimeout(r, 500)); } if (finalData?.cpa) replaceCpaCard(finalData.cpa); if (finalData?.error) { upsertProgressLog('刷新单个 CPA', `${cpaName}：刷新失败：${finalData.error}`, 'error'); return alert(finalData.error || '刷新失败'); } const ms = Number(finalData?.elapsed_ms || finalData?.scan?.metrics?.total_ms || 0); const fileCount = Number(finalData?.cpa?.auth_file_count || finalData?.cpa?.auth_file_total || finalData?.scan?.metrics?.files || 0); upsertProgressLog('刷新单个 CPA', `${cpaName}：刷新完成${fileCount > 0 ? ` · 凭证 ${fileCount} 个` : ''}${ms > 0 ? ` · ${ms}ms` : ''}`, 'success'); }; return withButtonLoading(button, '刷新中...', run)(); }
async function delete401(id, button) { if (!confirm('确认一键删除这个 CPA 里的 401 凭证吗？')) return; const run = async () => { const res = await fetch(`/api/cpas/${id}/delete-401`, {method:'POST'}); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); if (data.credentials) renderCredentialStore(data.credentials, latestCpas); }; return withButtonLoading(button, '清理中...', run)(); }
async function batchDeleteByKind(cpaId, kind, button) { const label = kind === '401' ? '401' : '异常'; if (!confirm(`确认一键删除这个 CPA 里的${label}凭证吗？`)) return; const run = async () => { const res = await fetch(`/api/cpas/${cpaId}/delete/${kind}`, { method:'POST' }); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); if (data.credentials) renderCredentialStore(data.credentials, latestCpas); if (!res.ok) return alert(data.error || '删除失败'); alert(`已删除 ${data.deleted?.length || 0} 个${label}凭证${data.failed?.length ? `，失败 ${data.failed.length} 个` : ''}`); }; return withButtonLoading(button, '删除中...', run)(); }
function exportByKind(cpaId, kind) { window.open(`/api/cpas/${cpaId}/export/${kind}`, '_blank'); }
async function deleteAuthFile(cpaId, encodedName) { if (!confirm('确认删除这个凭证吗？')) return; const res = await fetch(`/api/cpas/${cpaId}/auth-files/${encodedName}`, {method:'DELETE'}); const data = await res.json(); if (data.cpa) replaceCpaCard(data.cpa); if (data.credentials) renderCredentialStore(data.credentials, latestCpas); if (!res.ok) alert((data.result && data.result.text) || '删除失败'); }
async function saveCpaAuthToStore(cpaId, encodedName, button) { const run = async () => { appendProgressLog('上传仓库', `${decodeURIComponent(encodedName)} 开始处理`, 'info'); const res = await fetch(`/api/cpas/${cpaId}/auth-files/${encodedName}/save-to-store`, { method:'POST' }); const data = await res.json(); if (!res.ok) { appendProgressLog('上传仓库', `${decodeURIComponent(encodedName)} 失败：${data.error || '入仓失败'}`, 'error'); return showUiNotice('上传仓库失败', [data.error || '入仓失败']); } await loadAll(true); const added = Number(data.saved?.length || 0); const skipped = Number(data.skipped?.length || 0); const deduped = Number(data.dedupe_removed || 0); appendProgressLog('上传仓库', `${decodeURIComponent(encodedName)} 完成：成功 ${added}，重复 ${skipped}`, 'success'); showUiNotice('上传仓库完成', ['成功 ' + added + ' 个', '重复 ' + skipped + ' 个', '失败 0 个', deduped > 0 ? ('清理历史重复 ' + deduped + ' 个') : '']); }; return withButtonLoading(button, '入仓中...', run)(); }

function exportSingleCpaAuth(cpaId, encodedName) { window.open(`/api/cpas/${cpaId}/auth-files/${encodedName}/export`, '_blank'); }
async function saveSelectedCpaAuths(cpaId, button) { const items = getSelectedCpaAuthItems(cpaId); if (!items.length) return showUiNotice('提示', ['先勾选这个 CPA 的凭证']); const run = async () => { const startRes = await fetch(`/api/cpas/${cpaId}/auth-files/bulk-save-to-store/start`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ file_names: items.map(x => x.file_name) }) }); const startData = await startRes.json(); if (!startRes.ok || !startData.task_id) { upsertProgressLog('上传仓库', `启动失败：${startData.error || '启动失败'}`, 'error'); return showUiNotice('上传仓库失败', [startData.error || '启动失败']); } const taskId = startData.task_id; let finalData = null; while (true) { const progressRes = await fetch(`/api/cpas/refresh/${taskId}`, { cache:'no-store' }); const progressData = await progressRes.json(); if (!progressRes.ok) { upsertProgressLog('上传仓库', `读取进度失败：${progressData.error || '进度读取失败'}`, 'error'); return showUiNotice('上传仓库失败', [progressData.error || '进度读取失败']); } const total = Number(progressData.total || items.length || 0); const scanned = Number(progressData.scanned || 0); const success = Number(progressData.success || 0); const skipped = Number(progressData.skipped || 0); const failed = Number(progressData.failed || 0); const percent = Number(progressData.percent || 0); upsertProgressLog('上传仓库', `已导入 ${scanned}/${total} · ${percent}% · 成功 ${success} · 重复 ${skipped} · 失败 ${failed}`, progressData.done ? ((failed || skipped) ? 'warn' : 'success') : 'info'); if (progressData.done) { finalData = progressData.result || null; break; } await new Promise(r => setTimeout(r, 500)); } const saved = Number(finalData?.saved?.length || 0); const skipped = Number(finalData?.skipped?.length || 0); const failed = Number(finalData?.failed?.length || 0); Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${cpaId}::`)).forEach(key => selectedCpaAuthIds.delete(key)); if (finalData?.cpa) replaceCpaCard(finalData.cpa); renderCredentialStore(finalData?.credentials || [], finalData?.cpas || latestCpas); showUiNotice('上传仓库完成', ['成功 ' + saved + ' 个', '重复 ' + skipped + ' 个', '失败 ' + failed + ' 个', Number(finalData?.dedupe_removed || 0) > 0 ? ('清理历史重复 ' + finalData.dedupe_removed + ' 个') : '']); }; return withButtonLoading(button, '上传中...', run)(); }

function exportSelectedCpaAuths(cpaId) { const items = getSelectedCpaAuthItems(cpaId); if (!items.length) return alert('先勾选这个 CPA 的凭证'); items.forEach(item => window.open(`/api/cpas/${item.cpa_id}/auth-files/${encodeURIComponent(item.file_name)}/export`, '_blank')); }
async function deleteSelectedCpaAuths(cpaId, button) { const items = getSelectedCpaAuthItems(cpaId); if (!items.length) return showUiNotice('提示', ['先勾选这个 CPA 的凭证']); if (!confirm(`确认删除这 ${items.length} 个凭证吗？`)) return; const run = async () => { const startRes = await fetch(`/api/cpas/${cpaId}/auth-files/bulk-delete/start`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ file_names: items.map(x => x.file_name) }) }); const startData = await startRes.json(); if (!startRes.ok || !startData.task_id) { upsertProgressLog('删除 CPA 凭证', `启动失败：${startData.error || '启动失败'}`, 'error'); return showUiNotice('删除失败', [startData.error || '启动失败']); } const taskId = startData.task_id; let finalData = null; while (true) { const progressRes = await fetch(`/api/cpas/refresh/${taskId}`, { cache:'no-store' }); const progressData = await progressRes.json(); if (!progressRes.ok) { upsertProgressLog('删除 CPA 凭证', `读取进度失败：${progressData.error || '进度读取失败'}`, 'error'); return showUiNotice('删除失败', [progressData.error || '进度读取失败']); } const total = Number(progressData.total || items.length || 0); const scanned = Number(progressData.scanned || 0); const success = Number(progressData.success || 0); const failed = Number(progressData.failed || 0); const percent = Number(progressData.percent || 0); upsertProgressLog('删除 CPA 凭证', `已删除 ${scanned}/${total} · ${percent}% · 成功 ${success} · 失败 ${failed}`, progressData.done ? (failed ? 'warn' : 'success') : 'warn'); if (progressData.done) { finalData = progressData.result || null; break; } await new Promise(r => setTimeout(r, 500)); } const results = Array.isArray(finalData?.results) ? finalData.results : []; const failed = results.filter(x => !x.ok).length; Array.from(selectedCpaAuthIds).filter(key => key.startsWith(`${cpaId}::`)).forEach(key => selectedCpaAuthIds.delete(key)); if (finalData?.cpa) replaceCpaCard(finalData.cpa); renderCredentialStore(finalData?.credentials || [], finalData?.cpas || latestCpas); showUiNotice('删除完成', ['成功 ' + (results.length - failed) + ' 个', '失败 ' + failed + ' 个']); }; return withButtonLoading(button, '删除中...', run)(); }

async function addCpa(e) { e.preventDefault(); const fd = new FormData(els.cpaForm); const payload = Object.fromEntries(fd.entries()); await fetch('/api/cpas', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}); els.cpaForm.reset(); await loadAll(true); }
async function scanCpas() { const run = async () => { const targets = Array.isArray(latestCpas) ? latestCpas.slice() : []; if (!targets.length) { appendProgressLog('刷新全部 CPA', '当前没有可刷新的 CPA', 'warn'); return; } const pctText = (done, total) => `${done}/${total} · ${Math.round((done / Math.max(total, 1)) * 100)}%`; let completed = 0, failed = 0; appendProgressLog('刷新全部 CPA', `开始逐个刷新 ${pctText(0, targets.length)}`, 'info'); for (const cpa of targets) { const cpaName = cpa?.name || cpa?.id || '未知 CPA'; appendProgressLog('刷新全部 CPA', `处理中 · ${cpaName} · ${pctText(completed, targets.length)}`, 'info'); try { const res = await fetch(`/api/cpas/${cpa.id}/refresh`, { method:'POST' }); const data = await res.json(); completed += 1; if (!res.ok) { failed += 1; appendProgressLog('刷新全部 CPA', `刷新全部 CPA ${pctText(completed, targets.length)} · ${cpaName} 失败：${data.error || '刷新失败'}`, 'error'); continue; } if (data.cpa) replaceCpaCard(data.cpa); const ms = Number(data.elapsed_ms || data?.scan?.metrics?.total_ms || 0); appendProgressLog('刷新全部 CPA', `刷新全部 CPA ${pctText(completed, targets.length)} · ${cpaName} 完成${ms > 0 ? ` · ${ms}ms` : ''}`, 'success'); } catch (e) { completed += 1; failed += 1; appendProgressLog('刷新全部 CPA', `刷新全部 CPA ${pctText(completed, targets.length)} · ${cpaName} 异常：${e}`, 'error'); } } appendProgressLog('刷新全部 CPA', `刷新全部 CPA ${pctText(targets.length, targets.length)} · 全部完成：成功 ${targets.length - failed}，失败 ${failed}，正在刷新仓库视图`, failed ? 'warn' : 'success'); await loadCredentials(); }; return withButtonLoading(els.scanCpasBtn, '扫描中...', run)(); }
async function refreshAll() { const run = async () => { appendProgressLog('全量刷新', '开始刷新页面数据', 'info'); await loadAll(true); appendProgressLog('全量刷新', '页面数据刷新完成', 'success'); }; return withButtonLoading(els.refreshAllBtn, '刷新中...', run)(); }
async function syncCredentialStatus(options = {}) { const { silent = false, button = els.syncCredentialStatusBtn, logTag = '刷新仓库' } = options; const run = async () => { const targetId = els.credentialTargetSelect?.value || ''; if (!targetId) { if (!silent) alert('当前没有可用的目标 CPA'); return; } const targetName = els.credentialTargetSelect?.selectedOptions?.[0]?.text || '当前目标'; appendProgressLog(logTag, `开始刷新仓库并匹配 ${targetName}`, 'info'); if (els.credentialStoreHint) els.credentialStoreHint.textContent = `正在刷新仓库并匹配 ${targetName} ...`; const storeData = await loadCredentials(); const deduped = Number(storeData?.dedupe_removed || 0); appendProgressLog(logTag, `仓库清单已刷新，开始匹配 ${targetName}`, 'info'); const res = await fetch('/api/credentials/sync-upload-status', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_id: targetId }) }); const data = await res.json(); if (!res.ok) { if (els.credentialStoreHint) els.credentialStoreHint.textContent = data.error || '刷新失败'; appendProgressLog(logTag, data.error || '刷新失败', 'error'); if (!silent) showUiNotice(`${logTag}失败`, [data.error || '刷新失败']); return; } renderCredentialStore(data.credentials || [], data.cpas || latestCpas); if (els.credentialStoreHint) els.credentialStoreHint.textContent = `当前目标：${targetName} · 仓库已刷新，匹配到 ${data.matched || 0} 个${deduped > 0 ? ` · 清理同名 ${deduped} 个` : ''}`; appendProgressLog(logTag, `完成：匹配 ${data.matched || 0} 个${deduped > 0 ? '，清理同名 ' + deduped + ' 个' : ''}`, 'success'); if (!silent) showUiNotice(`${logTag}完成`, ['匹配到 ' + (data.matched || 0) + ' 个凭证', deduped > 0 ? ('检测并清理了 ' + deduped + ' 个同名重复凭证') : '未发现同名重复凭证']); }; if (button) return withButtonLoading(button, '刷新中...', run)(); return run(); }
async function refreshServerOnly() { const run = async () => { appendProgressLog('服务器刷新', '开始刷新服务器状态', 'info'); await loadServerStatus(true); appendProgressLog('服务器刷新', '服务器状态刷新完成', 'success'); }; return withButtonLoading(els.refreshServerBtn, '刷新中...', run)(); }
function toggleCredentialSelect(id, checked) { if (checked) selectedCredentialIds.add(id); else selectedCredentialIds.delete(id); }
function markRecentlyUploaded(ids = []) { ids.filter(Boolean).forEach(id => recentlyHighlightedCredentialIds.add(id)); renderCredentialStore(latestCredentials, latestCpas); setTimeout(() => { ids.forEach(id => recentlyHighlightedCredentialIds.delete(id)); renderCredentialStore(latestCredentials, latestCpas); }, 5000); }
function summarizeDeployResults(results = []) { const okItems = results.filter(x => x.ok); const failItems = results.filter(x => !x.ok); const failText = failItems.slice(0, 3).map(x => `${x.filename || x.name}（${x.status_code || 'ERR'}）`).join('、'); return { okItems, failItems, message: failItems.length ? `上传完成：成功 ${okItems.length} 个，失败 ${failItems.length} 个${failText ? `
失败项：${failText}` : ''}` : `上传成功：${okItems.length} 个`, }; }
async function importCredentialFiles(files, button) { const fileList = Array.from(files || []).filter(Boolean); if (!fileList.length) return; const run = async () => { const items = []; for (const file of fileList) { try { items.push({ name: file.name, filename: file.name, content: await file.text() }); } catch (e) { items.push({ name: file.name, filename: file.name, content: '' }); } } const startRes = await fetch('/api/credentials/import-bulk/start', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ items }) }); const startData = await startRes.json(); if (!startRes.ok || !startData.task_id) { upsertProgressLog('上传文件', `启动失败：${startData.error || '启动失败'}`, 'error'); return showUiNotice('上传文件失败', [startData.error || '启动失败']); } const taskId = startData.task_id; let finalData = null; while (true) { const progressRes = await fetch(`/api/cpas/refresh/${taskId}`, { cache:'no-store' }); const progressData = await progressRes.json(); if (!progressRes.ok) { upsertProgressLog('上传文件', `读取进度失败：${progressData.error || '进度读取失败'}`, 'error'); return showUiNotice('上传文件失败', [progressData.error || '进度读取失败']); } const total = Number(progressData.total || fileList.length || 0); const scanned = Number(progressData.scanned || 0); const success = Number(progressData.success || 0); const skipped = Number(progressData.skipped || 0); const failed = Number(progressData.failed || 0); const percent = Number(progressData.percent || 0); upsertProgressLog('上传文件', `已导入 ${scanned}/${total} · ${percent}% · 成功 ${success} · 重复 ${skipped} · 失败 ${failed}`, progressData.done ? ((failed || skipped) ? 'warn' : 'success') : 'info'); if (progressData.done) { finalData = progressData.result || null; break; } await new Promise(r => setTimeout(r, 500)); } if (els.credentialFilesInput) els.credentialFilesInput.value = ''; if (finalData?.credentials) renderCredentialStore(finalData.credentials || [], finalData.cpas || latestCpas); else await loadCredentials(); showUiNotice('上传文件完成', ['已导入 ' + Number(finalData?.saved?.length || 0) + ' 个', '重复 ' + Number(finalData?.skipped?.length || 0) + ' 个', '失败 ' + Math.max(0, fileList.length - Number(finalData?.saved?.length || 0) - Number(finalData?.skipped?.length || 0)) + ' 个', Number(finalData?.dedupe_removed || 0) > 0 ? ('清理历史重复 ' + finalData.dedupe_removed + ' 个') : '']); }; return withButtonLoading(button, '上传中...', run)(); }

async function deploySelectedCredentials() { const run = async () => { const targetId = els.credentialTargetSelect?.value || '', ids = Array.from(selectedCredentialIds); if (!targetId) return showUiNotice('提示', ['当前没有可用的目标 CPA']); if (!ids.length) return showUiNotice('提示', ['先勾选仓库里的凭证']); const startRes = await fetch('/api/credentials/deploy-bulk/start', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_id: targetId, credential_ids: ids }) }); const startData = await startRes.json(); if (!startRes.ok || !startData.task_id) { upsertProgressLog('仓库→CPA', `启动失败：${startData.error || '启动失败'}`, 'error'); return showUiNotice('批量上传到目标 CPA 失败', [startData.error || '启动失败']); } const taskId = startData.task_id; let finalData = null; while (true) { const progressRes = await fetch(`/api/cpas/refresh/${taskId}`, { cache:'no-store' }); const progressData = await progressRes.json(); if (!progressRes.ok) { upsertProgressLog('仓库→CPA', `读取进度失败：${progressData.error || '进度读取失败'}`, 'error'); return showUiNotice('批量上传到目标 CPA 失败', [progressData.error || '进度读取失败']); } const total = Number(progressData.total || ids.length || 0); const scanned = Number(progressData.scanned || 0); const success = Number(progressData.success || 0); const failed = Number(progressData.failed || 0); const percent = Number(progressData.percent || 0); upsertProgressLog('仓库→CPA', `已上传 ${scanned}/${total} · ${percent}% · 成功 ${success} · 失败 ${failed}`, progressData.done ? (failed ? 'warn' : 'success') : 'info'); if (progressData.done) { finalData = progressData.result || null; break; } await new Promise(r => setTimeout(r, 500)); } const results = Array.isArray(finalData?.results) ? finalData.results : []; const okCount = results.filter(x => x.ok).length; const failCount = results.length - okCount; const okIds = results.filter(x => x.ok).map(x => x.id); const failItems = results.filter(x => !x.ok); okIds.forEach(id => selectedCredentialIds.delete(id)); failItems.forEach(x => selectedCredentialIds.add(x.id)); markRecentlyUploaded(okIds); if (finalData?.cpa) replaceCpaCard(finalData.cpa); renderCredentialStore(finalData?.credentials || [], finalData?.cpas || latestCpas); const failText = failItems.slice(0, 3).map(x => `${x.filename || x.name}（${x.status_code || 'ERR'}）`).join('、'); showUiNotice('批量上传到目标 CPA', [failCount ? `上传完成：成功 ${okCount} 个，失败 ${failCount} 个${failText ? `\n失败项：${failText}` : ''}` : `上传成功：${okCount} 个`]); }; return withButtonLoading(els.deploySelectedBtn, '上传中...', run)(); }

async function deleteCredential(id) { if (!confirm('确认从仓库删除这个凭证吗？')) return; const res = await fetch(`/api/credentials/${id}`, { method: 'DELETE' }); const data = await res.json(); selectedCredentialIds.delete(id); renderCredentialStore(data.credentials || [], data.cpas || latestCpas); }
window.deleteCpa = deleteCpa; window.refreshCpa = refreshCpa; window.delete401 = delete401; window.batchDeleteByKind = batchDeleteByKind; window.exportByKind = exportByKind; window.deleteAuthFile = deleteAuthFile; window.saveCpaAuthToStore = saveCpaAuthToStore; window.exportSingleCpaAuth = exportSingleCpaAuth; window.toggleCpaAuthSelect = toggleCpaAuthSelect; window.selectCpaAuthsByMode = selectCpaAuthsByMode; window.clearCpaAuthSelection = clearCpaAuthSelection; window.saveSelectedCpaAuths = saveSelectedCpaAuths; window.exportSelectedCpaAuths = exportSelectedCpaAuths; window.deleteSelectedCpaAuths = deleteSelectedCpaAuths; window.toggleCredentialSelect = toggleCredentialSelect; window.deleteCredential = deleteCredential; window.toggleCpaExpand = toggleCpaExpand; window.renameCpa = renameCpa; window.moveCpa = moveCpa;
els.cpaForm?.addEventListener('submit', addCpa);
els.refreshServerBtn?.addEventListener('click', refreshServerOnly);
els.refreshAllBtn?.addEventListener('click', refreshAll);
els.scanCpasBtn?.addEventListener('click', scanCpas);
els.syncCredentialStatusBtn?.addEventListener('click', () => syncCredentialStatus());
els.selectAllCredentialsBtn?.addEventListener('click', () => { const boxes = Array.from(document.querySelectorAll('#credentialStoreList input[type="checkbox"]')); const allChecked = boxes.length > 0 && boxes.every(el => el.checked); boxes.forEach(el => { const next = !allChecked; el.checked = next; const id = el.closest('.credential-item')?.dataset.credId; if (id) toggleCredentialSelect(id, next); }); });
els.deploySelectedBtn?.addEventListener('click', deploySelectedCredentials);
els.credentialSearchInput?.addEventListener('input', e => {
  const next = e.target.value || '';
  if (credentialSearchDebounceTimer) clearTimeout(credentialSearchDebounceTimer);
  credentialSearchDebounceTimer = setTimeout(() => {
    credentialSearch = next;
    credentialVisibleCount = CREDENTIAL_PAGE_SIZE;
    renderCredentialStore(latestCredentials, latestCpas);
  }, 120);
});
els.credentialUploadFilter?.addEventListener('change', e => { credentialUploadFilter = e.target.value || 'all'; credentialVisibleCount = CREDENTIAL_PAGE_SIZE; renderCredentialStore(latestCredentials, latestCpas); });
els.credentialTargetSelect?.addEventListener('change', () => { credentialVisibleCount = CREDENTIAL_PAGE_SIZE; renderCredentialStore(latestCredentials, latestCpas); syncCredentialStatus({ silent: true, button: null }); });
els.uploadCredentialFilesBtn?.addEventListener('click', () => els.credentialFilesInput?.click());
els.credentialFilesInput?.addEventListener('change', e => importCredentialFiles(e.target.files, els.uploadCredentialFilesBtn));
els.toggleCredentialFoldBtn?.addEventListener('click', () => { credentialStoreExpanded = !credentialStoreExpanded; renderCredentialStore(latestCredentials, latestCpas); });
els.uiNoticeCloseBtn?.addEventListener('click', hideUiNotice);
els.clearProgressLogBtn?.addEventListener('click', clearProgressLog);
els.timeRangeSwitch?.querySelectorAll('.range-btn').forEach(btn => btn.addEventListener('click', async () => { currentRange = btn.dataset.range || '24h'; await loadAll(true); }));
let autoRefreshRunning = false;
async function runAutoRefreshCycle(forceBust = false) {
  if (autoRefreshRunning) return;
  autoRefreshRunning = true;
  if (els.autoRefreshHint) els.autoRefreshHint.textContent = '自动刷新中...';
  try {
    await fetch('/api/cpas/scan', { method:'POST' });
    await loadAll(forceBust);
    await syncCredentialStatus({ silent: true, button: null, logTag: '自动刷新·刷新仓库' });
    if (els.autoRefreshHint) els.autoRefreshHint.textContent = `自动刷新成功 · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  } catch (e) {
    console.error('auto refresh failed', e);
    if (els.autoRefreshHint) els.autoRefreshHint.textContent = '自动刷新失败';
  } finally {
    autoRefreshRunning = false;
  }
}
loadAll(true);
window.addEventListener('load', () => {
  setTimeout(() => { runAutoRefreshCycle(true); }, 1200);
});
setInterval(() => loadServerStatus(true, false), 1000);
setInterval(() => loadServerStatus(true, true), 10000);
setInterval(() => { runAutoRefreshCycle(false); }, 180000);
