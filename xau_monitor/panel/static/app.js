/* Bootstrap panel: navigasi tab, kontrol monitor, timer polling.
   Modul lain: core (state/util), dashboard, symbol, logs, files. */
import { $, $$, api, store, toast } from './core.js';
import { initDashboard, pollState, loadReport } from './dashboard.js';
import { initSymbol } from './symbol.js';
import { initLogs, loadLogs } from './logs.js';
import { initFiles, loadFiles, saveFromShortcut } from './files.js';

function selectTab(name) {
  store.tab = name;
  $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.tab === name));
  $$('.tab').forEach((s) => s.classList.toggle('is-active', s.id === `tab-${name}`));
  if (name === 'logs') loadLogs(true);
  if (name === 'files' && !store.files.length) loadFiles();
  if (name === 'dashboard') loadReport();
}

function bindNav() {
  $$('.nav-item').forEach((btn) => btn.addEventListener('click', () => selectTab(btn.dataset.tab)));
}

function bindMonitor() {
  for (const action of ['start', 'stop', 'restart']) {
    $(`#btn-${action}`).addEventListener('click', async () => {
      if (action !== 'start' && !confirm(`Yakin ${action} monitor?`)) return;
      try {
        const data = await api(`/api/monitor/${action}`, { method: 'POST', body: {} });
        toast(data.message || action);
        setTimeout(pollState, 600);
      } catch (err) {
        toast(`Gagal ${action}: ${err.message}`, true);
      }
    });
  }
}

function bindKeyboard() {
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's'
        && store.tab === 'files' && saveFromShortcut()) {
      e.preventDefault();
    }
  });
  window.addEventListener('beforeunload', (e) => {
    if (store.dirty) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

function startTimers() {
  pollState();
  setTimeout(loadReport, 400);
  store.timers.state = setInterval(pollState, 2000);
  store.timers.report = setInterval(() => {
    if (store.tab === 'dashboard' && store.controlOk) loadReport();
  }, 20000);
  store.timers.logs = setInterval(() => {
    if (store.tab === 'logs' && $('#log-follow').checked) loadLogs(false);
  }, 2500);
  store.timers.logsSlow = setInterval(() => {
    if (store.tab === 'logs' && !$('#log-follow').checked) loadLogs(false);
  }, 10000);
}

async function loadInstances() {
  try {
    const data = await api('/api/symbols', { guard: false });
    const sel = $('#instance-select');
    sel.innerHTML = '';
    data.instances.forEach((sym) => {
      const opt = document.createElement('option');
      opt.value = sym;
      opt.textContent = sym;
      sel.appendChild(opt);
    });
    store.instance = data.instances.includes(data.default)
      ? data.default : data.instances[0];
    sel.value = store.instance;
    sel.addEventListener('change', () => {
      store.instance = sel.value;
      store.status = null;
      store.prices = [];
      store.logCursor = 0;
      store.logLines = [];
      $('#log-view').textContent = '';
      pollState();
      loadReport();
      loadLogs(true);
    });
  } catch (err) {
    toast(`Daftar instance gagal dimuat: ${err.message}`, true);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initDashboard();
  initSymbol();
  initLogs();
  initFiles();
  bindNav();
  bindMonitor();
  bindKeyboard();
  document.addEventListener('xau:refresh', pollState);
  loadInstances();
  startTimers();
});
