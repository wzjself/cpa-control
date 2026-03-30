let usageChart, trafficChart;

const els = {
  refreshAllBtn: document.getElementById('refreshAllBtn'),
  scanCpasBtn: document.getElementById('scanCpasBtn'),
  cpaForm: document.getElementById('cpaForm'),
  healthScore: document.getElementById('healthScore'),
  cpuNow: document.getElementById('cpuNow'),
  memNow: document.getElementById('memNow'),
  diskNow: document.getElementById('diskNow'),
  trafficNow: document.getElementById('trafficNow'),
  processList: document.getElementById('processList'),
  relayStats: document.getElementById('relayStats'),
  cpaList: document.getElementById('cpaList'),
};

const fmtPct = n => `${Number(n || 0).toFixed(1)}%`;
const fmtNum = n => new Intl.NumberFormat('zh-CN').format(Number(n || 0));
const fmtMb = n => `${Number(n || 0).toFixed(1)} MB`;

function renderUsageChart(history) {
  const labels = history.map(x => x.ts.slice(11, 16));
  const config = {
    type: 'line',
    data: {
      labels,
      datasets: [
        {label: 'CPU', data: history.map(x => x.cpu_percent), borderColor: '#60a5fa', tension: .25},
        {label: '内存', data: history.map(x => x.mem_percent), borderColor: '#34d399', tension: .25},
        {label: '磁盘', data: history.map(x => x.disk_percent), borderColor: '#f59e0b', tension: .25},
      ]
    },
    options: {responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200}
  };
  if (usageChart) usageChart.destroy();
  usageChart = new Chart(document.getElementById('usageChart'), config);
}

function renderTrafficChart(history) {
  const labels = history.map(x => x.ts.slice(11, 16));
  const config = {
    type: 'line',
    data: {
      labels,
      datasets: [
        {label: '下载累计 MB', data: history.map(x => x.net_rx_mb), borderColor: '#22c55e', tension: .25},
        {label: '上传累计 MB', data: history.map(x => x.net_tx_mb), borderColor: '#f472b6', tension: .25},
      ]
    },
    options: {responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200}
  };
  if (trafficChart) trafficChart.destroy();
  trafficChart = new Chart(document.getElementById('trafficChart'), config);
}

function renderProcesses(items) {
  els.processList.innerHTML = items.map(p => `
    <div class="process-item">
      <div><strong>${p.name}</strong> <span class="muted">PID ${p.pid}</span></div>
      <div class="muted">CPU ${p.cpu}% · 内存 ${p.mem}%</div>
    </div>
  `).join('');
}

function renderRelayStats(data) {
  if (!data.available) {
    els.relayStats.innerHTML = '<div class="relay-item">未找到 clirelay 数据源</div>';
    return;
  }
  const source = data.source === 'management-api' ? '管理接口' : 'usage.db';
  const system = data.system || {};
  const counts = data.counts || {};
  const topKeys = data.top_api_keys || [];
  els.relayStats.innerHTML = `
    <div class="relay-item">数据来源：<strong>${source}</strong></div>
    <div class="relay-item">总请求：<strong>${fmtNum(data.request_count)}</strong></div>
    <div class="relay-item">成功率：<strong>${data.success_rate}%</strong></div>
    <div class="relay-item">当前 RPM / TPM：<strong>${fmtNum(data.rpm)} / ${fmtNum(data.tpm)}</strong></div>
    <div class="relay-item">已使用 Token：<strong>${fmtNum(data.total_tokens)}</strong></div>
    <div class="relay-item">输入 / 输出 / 缓存：<strong>${fmtNum(data.input_tokens)} / ${fmtNum(data.output_tokens)} / ${fmtNum(data.cached_tokens)}</strong></div>
    <div class="relay-item">认证文件 / 提供商：<strong>${fmtNum(counts.auth_files || 0)} / ${fmtNum(counts.providers_total || 0)}</strong></div>
    <div class="relay-item">clirelay 进程 CPU / 内存：<strong>${Number(system.process_cpu_pct || 0).toFixed(1)}% / ${Number(system.process_mem_pct || 0).toFixed(1)}%</strong></div>
    <div class="relay-item">clirelay 总入站 / 出站：<strong>${fmtMb((system.net_bytes_recv || 0)/1024/1024)} / ${fmtMb((system.net_bytes_sent || 0)/1024/1024)}</strong></div>
  ` + topKeys.map(x => `
    <div class="relay-item"><strong>${x.name || x.api_key_name || '未命名'}</strong><div class="muted">请求 ${fmtNum(x.requests)} · Token ${fmtNum(x.tokens || x.total_tokens)}${x.success_count !== undefined ? ` · 成功 ${fmtNum(x.success_count)}` : ''}</div></div>
  `).join('');
}

function accountStatusClass(acc) {
  if (acc.invalid_401) return 'err';
  if (acc.quota_limited) return 'warn';
  return 'ok';
}

function renderCpas(cpas) {
  els.cpaList.innerHTML = cpas.map(cpa => `
    <div class="cpa-card">
      <div class="cpa-head">
        <div>
          <div><strong>${cpa.name}</strong> <span class="badge">${cpa.provider}</span></div>
          <div class="muted">${cpa.base_url}</div>
        </div>
        <button class="danger" onclick="deleteCpa('${cpa.id}')">删除 CPA</button>
      </div>
      <div class="mini-grid">
        <div class="mini-stat"><div class="muted">总凭证</div><div class="n">${fmtNum(cpa.total)}</div></div>
        <div class="mini-stat"><div class="muted">401</div><div class="n err">${fmtNum(cpa.invalid_401)}</div></div>
        <div class="mini-stat"><div class="muted">限额</div><div class="n warn">${fmtNum(cpa.quota_limited)}</div></div>
        <div class="mini-stat"><div class="muted">健康</div><div class="n ok">${fmtNum(cpa.healthy)}</div></div>
      </div>
      <div class="quota-row">
        <div class="quota-label"><span>总凭证额度使用情况</span><span>已用 ${cpa.used_ratio || 0}% / 总额度 100%</span></div>
        <div class="progress used"><span style="width:${cpa.used_ratio || 0}%"></span></div>
      </div>
      <div class="muted" style="margin:10px 0">凭证列表（最多展示 200 条）</div>
      <div class="list">
        ${(cpa.accounts || []).map(acc => `
          <div class="account-row">
            <div class="account-top">
              <strong>${acc.email || acc.name}</strong>
              <span class="${accountStatusClass(acc)}">${acc.invalid_401 ? '401' : acc.quota_limited ? '限额' : acc.status}</span>
            </div>
            <div class="progress"><span style="width:${acc.remaining_ratio || 0}%"></span></div>
            <div class="muted">剩余估计：${acc.remaining_ratio || 0}% ${acc.disabled ? '· 已禁用' : ''}</div>
          </div>
        `).join('') || '<div class="muted">还没有扫描数据，点上面的“刷新并扫描全部 CPA”即可。</div>'}
      </div>
    </div>
  `).join('');
}

async function loadAll() {
  const res = await fetch('/api/overview');
  const data = await res.json();
  const server = data.server;
  els.healthScore.textContent = server.health;
  els.cpuNow.textContent = fmtPct(server.latest.cpu_percent);
  els.memNow.textContent = fmtPct(server.latest.mem_percent);
  els.diskNow.textContent = fmtPct(server.latest.disk_percent);
  els.trafficNow.textContent = `${fmtMb(server.traffic_24h.rx_mb)} / ${fmtMb(server.traffic_24h.tx_mb)}`;
  renderUsageChart(server.history || []);
  renderTrafficChart(server.history || []);
  renderProcesses(server.top_processes || []);
  renderRelayStats(data.clirelay || {});
  renderCpas(data.cpas || []);
}

async function deleteCpa(id) {
  if (!confirm('确认删除这个 CPA 吗？')) return;
  await fetch(`/api/cpas/${id}`, {method:'DELETE'});
  await loadAll();
}
window.deleteCpa = deleteCpa;

els.refreshAllBtn.addEventListener('click', loadAll);
els.scanCpasBtn.addEventListener('click', async () => {
  els.scanCpasBtn.disabled = true;
  els.scanCpasBtn.textContent = '扫描中...';
  await fetch('/api/cpas/scan', {method:'POST'});
  await loadAll();
  els.scanCpasBtn.disabled = false;
  els.scanCpasBtn.textContent = '刷新并扫描全部 CPA';
});

els.cpaForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(els.cpaForm);
  const payload = Object.fromEntries(fd.entries());
  const res = await fetch('/api/cpas', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const data = await res.json();
    alert(data.error || '添加失败');
    return;
  }
  els.cpaForm.reset();
  await loadAll();
});

loadAll();
setInterval(loadAll, 60000);
