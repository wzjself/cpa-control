const state = {
  tabs: [],
  uploads: [],
  activeTabId: null,
  saveTimer: null,
  dragId: null,
};

const els = {
  tabList: document.getElementById('tabList'),
  editor: document.getElementById('editor'),
  titleInput: document.getElementById('titleInput'),
  saveStatus: document.getElementById('saveStatus'),
  uploadsList: document.getElementById('uploadsList'),
  newTabBtn: document.getElementById('newTabBtn'),
  deleteTabBtn: document.getElementById('deleteTabBtn'),
  docInput: document.getElementById('docInput'),
  mobileToggle: document.getElementById('mobileToggle'),
  sidebar: document.getElementById('sidebar'),
  template: document.getElementById('tabItemTemplate'),
};

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function activeTab() {
  return state.tabs.find(tab => tab.id === state.activeTabId);
}

function setStatus(text) {
  els.saveStatus.textContent = text;
}

function renderTabs() {
  els.tabList.innerHTML = '';
  for (const tab of state.tabs) {
    const node = els.template.content.firstElementChild.cloneNode(true);
    node.dataset.id = tab.id;
    node.classList.toggle('active', tab.id === state.activeTabId);
    node.querySelector('.tab-title').textContent = tab.title || 'Untitled';
    node.addEventListener('click', () => {
      state.activeTabId = tab.id;
      renderAll();
      if (window.innerWidth <= 900) els.sidebar.classList.remove('open');
    });
    node.addEventListener('dragstart', () => state.dragId = tab.id);
    node.addEventListener('dragover', (e) => e.preventDefault());
    node.addEventListener('drop', async (e) => {
      e.preventDefault();
      if (!state.dragId || state.dragId === tab.id) return;
      const ids = [...state.tabs.map(t => t.id)];
      const from = ids.indexOf(state.dragId);
      const to = ids.indexOf(tab.id);
      ids.splice(to, 0, ids.splice(from, 1)[0]);
      const res = await fetch('/api/tabs/reorder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids})
      });
      const data = await res.json();
      state.tabs = data.tabs;
      renderTabs();
    });
    els.tabList.appendChild(node);
  }
}

function renderUploads() {
  els.uploadsList.innerHTML = '';
  if (!state.uploads.length) {
    els.uploadsList.innerHTML = '<div class="upload-card"><strong>还没有文件</strong><div class="meta">上传后的文档会显示在这里。</div></div>';
    return;
  }
  for (const item of state.uploads) {
    const div = document.createElement('div');
    div.className = 'upload-card';
    div.innerHTML = `
      <div><strong>${item.original_name}</strong></div>
      <div class="meta">${item.kind} · ${formatBytes(item.size)}</div>
      <div class="meta"><a href="/uploads/${item.relative_path}" target="_blank">Open file</a></div>
    `;
    els.uploadsList.appendChild(div);
  }
}

function renderEditor() {
  const tab = activeTab();
  if (!tab) return;
  els.titleInput.value = tab.title || '';
  if (els.editor.innerHTML !== tab.content_html) {
    els.editor.innerHTML = tab.content_html || '';
  }
}

function renderAll() {
  renderTabs();
  renderEditor();
  renderUploads();
}

async function bootstrap() {
  const res = await fetch('/api/bootstrap');
  const data = await res.json();
  state.tabs = data.tabs;
  state.uploads = data.uploads;
  state.activeTabId = state.tabs[0]?.id || null;
  renderAll();
}

async function saveActiveTab() {
  const tab = activeTab();
  if (!tab) return;
  setStatus('保存中...');
  const payload = {
    title: els.titleInput.value.trim() || 'Untitled',
    content_html: els.editor.innerHTML,
  };
  const res = await fetch(`/api/tabs/${tab.id}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const updated = await res.json();
  const idx = state.tabs.findIndex(t => t.id === tab.id);
  state.tabs[idx] = updated;
  setStatus('已保存');
  renderTabs();
}

function scheduleSave() {
  setStatus('编辑中...');
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveActiveTab, 500);
}

async function createTab() {
  const res = await fetch('/api/tabs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: '新标签'}),
  });
  const tab = await res.json();
  state.tabs.push(tab);
  state.activeTabId = tab.id;
  renderAll();
  els.titleInput.focus();
}

async function deleteTab() {
  const tab = activeTab();
  if (!tab) return;
  if (!confirm(`确认删除标签“${tab.title}”？`)) return;
  const res = await fetch(`/api/tabs/${tab.id}`, { method: 'DELETE' });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || '删除失败');
    return;
  }
  state.tabs = data.tabs;
  state.activeTabId = state.tabs[0]?.id || null;
  renderAll();
}

async function uploadDocuments(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/upload/document', { method: 'POST', body: fd });
    if (res.ok) {
      const item = await res.json();
      state.uploads.unshift(item);
    }
  }
  renderUploads();
}

async function uploadImage(file) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'paste.png');
  const res = await fetch('/api/upload/image', { method: 'POST', body: fd });
  if (!res.ok) throw new Error('image upload failed');
  return await res.json();
}

function insertHtmlAtCursor(html) {
  document.execCommand('insertHTML', false, html);
}

els.newTabBtn.addEventListener('click', createTab);
els.deleteTabBtn.addEventListener('click', deleteTab);
els.titleInput.addEventListener('input', scheduleSave);
els.editor.addEventListener('input', scheduleSave);
els.docInput.addEventListener('change', async (e) => {
  await uploadDocuments([...e.target.files]);
  e.target.value = '';
});
els.mobileToggle.addEventListener('click', () => els.sidebar.classList.toggle('open'));

els.editor.addEventListener('paste', async (event) => {
  const items = [...(event.clipboardData?.items || [])];
  const imageItem = items.find(item => item.type.startsWith('image/'));
  if (!imageItem) return;
  event.preventDefault();
  setStatus('上传图片中...');
  const file = imageItem.getAsFile();
  const image = await uploadImage(file);
  insertHtmlAtCursor(`<p><img src="${image.url}" alt="${image.name}"></p>`);
  scheduleSave();
});

window.addEventListener('beforeunload', () => clearTimeout(state.saveTimer));
bootstrap();
