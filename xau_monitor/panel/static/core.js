/* Inti panel: state bersama, fetch ber-guard, format, toast. */
export const GUARD = { 'X-Xau-Panel': '1' };

export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => Array.from(document.querySelectorAll(sel));

export const store = {
  tab: 'dashboard',
  instance: null,
  status: null,
  monitor: null,
  controlOk: false,
  prices: [],
  reportKind: 'planning',
  logSource: 'monitor',
  logCursor: 0,
  logLines: [],
  files: [],
  currentFile: null,
  dirty: false,
  timers: {},
};

export function toast(message, isError = false) {
  const box = $('#toast');
  box.textContent = message;
  box.classList.toggle('error', isError);
  box.classList.add('show');
  clearTimeout(box._timer);
  box._timer = setTimeout(() => box.classList.remove('show'), 4200);
}

export async function api(path, { method = 'GET', body, guard = true } = {}) {
  const headers = guard ? { ...GUARD } : {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method, headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const ctype = res.headers.get('content-type') || '';
  const data = ctype.includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    const message = (data && data.error) || (data && data.message) || `HTTP ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function fmtDuration(sec) {
  if (sec === null || sec === undefined) return '—';
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}j ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}

export function fmtAgo(sec) {
  if (sec === null || sec === undefined) return 'belum pernah';
  return `${sec < 1 ? '<1' : Math.round(sec)}s lalu`;
}

export function plainText(htmlText) {
  const doc = new DOMParser().parseFromString(htmlText, 'text/html');
  return (doc.body.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
}

export function notifyRefresh() {
  document.dispatchEvent(new CustomEvent('xau:refresh'));
}

/* Tambahkan parameter instance (simbol monitor aktif) ke path API panel. */
export function withInst(path) {
  if (!store.instance) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}instance=${encodeURIComponent(store.instance)}`;
}
