/* Tab File & Aturan AI: daftar file, editor, simpan atomik dengan backup. */
import { $, $$, api, store, toast } from './core.js';

export function initFiles() {
  $('#files-refresh').addEventListener('click', loadFiles);
  $('#editor').addEventListener('input', () => {
    store.dirty = true;
    $('#editor').classList.add('is-dirty');
  });
  $('#editor-save').addEventListener('click', saveFile);
  $('#editor-reload').addEventListener('click', () => {
    if (store.currentFile) loadFile(store.currentFile.id);
  });
}

export async function loadFiles() {
  try {
    const data = await api('/api/files', { guard: false });
    store.files = data.files;
    const list = $('#file-list');
    list.textContent = '';
    for (const file of data.files) {
      const btn = document.createElement('button');
      btn.className = 'file-item';
      btn.dataset.id = file.id;
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = file.label;
      const path = document.createElement('span');
      path.className = 'path';
      path.textContent = file.path === file.label ? '' : file.path;
      btn.append(name, path);
      if (file.secret) {
        const flag = document.createElement('span');
        flag.className = 'flag';
        flag.textContent = 'rahasia disensor';
        btn.append(flag);
      }
      btn.addEventListener('click', () => loadFile(file.id));
      list.append(btn);
    }
  } catch (err) {
    toast(`Gagal memuat daftar file: ${err.message}`, true);
  }
}

export async function loadFile(id) {
  if (store.dirty && !confirm('Ada perubahan belum disimpan. Buang perubahan?')) return;
  try {
    const data = await api(`/api/file?id=${encodeURIComponent(id)}`, { guard: false });
    store.currentFile = data;
    store.dirty = false;
    $('#editor').value = data.content;
    $('#editor').disabled = false;
    $('#editor').classList.remove('is-dirty');
    $('#editor-title').textContent = data.label;
    $('#editor-badge').textContent = data.secret ? 'rahasia disensor' : 'teks';
    $('#editor-badge').className = `pill ${data.secret ? 'pill-gold' : 'pill-quiet'}`;
    $('#editor-meta').textContent = `${data.path} · ${data.size} byte · ` +
      (data.mtime ? new Date(data.mtime * 1000).toLocaleString('id-ID') : 'file baru');
    $('#editor-save').disabled = false;
    $('#editor-reload').disabled = false;
    $('#editor-status').textContent = '';
    $$('.file-item').forEach((b) => b.classList.toggle('is-active', b.dataset.id === id));
  } catch (err) {
    toast(`Gagal membaca file: ${err.message}`, true);
  }
}

async function saveFile() {
  if (!store.currentFile) return;
  const content = $('#editor').value;
  try {
    const data = await api('/api/file', {
      method: 'PUT',
      body: {
        id: store.currentFile.id,
        content,
        mtime_ns: store.currentFile.mtime_ns,
        force: $('#editor-force').checked,
      },
    });
    store.currentFile.mtime = data.mtime;
    store.currentFile.mtime_ns = data.mtime_ns;
    store.currentFile.size = data.size;
    store.dirty = false;
    $('#editor').classList.remove('is-dirty');
    $('#editor-meta').textContent =
      `${store.currentFile.path} · ${data.size} byte · baru disimpan`;
    $('#editor-status').textContent = data.message || 'tersimpan';
    toast(data.backup ? `Tersimpan · backup ${data.backup}` : 'File tersimpan');
    loadFiles();
  } catch (err) {
    $('#editor-status').textContent = err.message;
    toast(`Gagal menyimpan: ${err.message}`, true);
  }
}

export function saveFromShortcut() {
  if (!store.currentFile || $('#editor-save').disabled) return false;
  saveFile();
  return true;
}
