/* Tab Log: tail inkremental monitor.log / monitor.stdio.log. */
import { $, api, store, withInst } from './core.js';

export function initLogs() {
  $('#log-source').addEventListener('change', (e) => {
    store.logSource = e.target.value;
    $('#log-download').href = `/api/logs/download?source=${store.logSource}`;
    loadLogs(true);
  });
  $('#log-filter').addEventListener('input', renderLogs);
  $('#log-refresh').addEventListener('click', () => loadLogs(true));
  $('#log-view').addEventListener('scroll', () => {
    const v = $('#log-view');
    if (v.scrollTop + v.clientHeight >= v.scrollHeight - 40) $('#log-follow').checked = true;
  });
}

export async function loadLogs(reset = false) {
  if (reset) {
    store.logCursor = 0;
    store.logLines = [];
    $('#log-view').textContent = '';
  }
  try {
    const data = await api(
      withInst(`/api/logs?source=${store.logSource}&cursor=${store.logCursor}&limit=400`),
      { guard: false });
    store.logCursor = data.cursor;
    if (data.missing) {
      $('#log-meta').textContent = 'log belum ada — monitor belum pernah jalan?';
      return;
    }
    if (data.reset) store.logLines = [];
    if (data.lines.length) {
      data.lines.forEach((line) => store.logLines.push(line));
      if (store.logLines.length > 3000) store.logLines = store.logLines.slice(-3000);
      renderLogs();
    }
    $('#log-meta').textContent =
      `${store.logLines.length} baris · ${(data.size / 1024).toFixed(1)} KB`;
  } catch (err) {
    $('#log-meta').textContent = `gagal memuat log: ${err.message}`;
  }
}

function renderLogs() {
  const filter = ($('#log-filter').value || '').toLowerCase();
  const view = $('#log-view');
  const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
  view.textContent = '';
  const frag = document.createDocumentFragment();
  for (const line of store.logLines) {
    if (filter && !line.toLowerCase().includes(filter)) continue;
    const span = document.createElement('span');
    span.className = 'log-line';
    const level = (line.match(/\b(ERROR|WARNING|CRITICAL|INFO|DEBUG)\b/) || [])[1];
    if (level) span.classList.add(`log-${level}`);
    span.textContent = line;
    frag.append(span);
  }
  view.append(frag);
  if ($('#log-follow').checked || atBottom) view.scrollTop = view.scrollHeight;
}
