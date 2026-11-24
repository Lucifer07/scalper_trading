/* Tab Dashboard: polling status, topbar, engine, sparkline, laporan. */
import { $, $$, api, store, fmtDuration, fmtAgo, plainText, toast, withInst } from './core.js';

export function initDashboard() {
  $$('.seg-item').forEach((btn) => btn.addEventListener('click', () => {
    store.reportKind = btn.dataset.report;
    $$('.seg-item').forEach((b) => b.classList.toggle('is-active', b === btn));
    loadReport();
  }));
  $('#report-refresh').addEventListener('click', loadReport);
  $('#btn-layering').addEventListener('click', toggleLayering);
}

async function toggleLayering() {
  const st = store.status;
  if (!st || !st.orders || !st.orders.armed) return;
  const next = !st.orders.layering;
  try {
    await api(withInst('/api/layering'), { method: 'POST', body: { enabled: next } });
    toast(`layering ${next ? 'ON' : 'OFF'}`);
    await pollState();
  } catch (err) {
    toast(`layering gagal: ${err.message}`, true);
  }
}

export async function pollState() {
  try {
    const data = await api(withInst('/api/state'), { guard: false });
    store.status = data.status;
    store.monitor = data.monitor;
    store.controlOk = data.control.ok;
    renderTopbar(data);
    renderDashboard(data);
  } catch (err) {
    store.controlOk = false;
    renderOffline(err.message);
  }
}

function renderOffline(message) {
  $('#pill-monitor').textContent = 'monitor: offline';
  $('#pill-monitor').className = 'pill pill-warn';
  $('#pill-source').textContent = 'sumber: mt4 · —';
  $('#side-control').textContent = `control: ${message}`;
  $('#btn-start').disabled = false;
  $('#btn-stop').disabled = true;
}

/* Presisi harga mengikuti digits broker dari payload status (fallback:
   readiness, lalu 2). EURUSD/GBPUSD = 5, XAUUSD = 2. */
function priceDigits(st) {
  const rd = st && st.readiness ? Object.values(st.readiness)[0] : null;
  const d = Number(st && st.digits != null ? st.digits : (rd && rd.digits));
  return Number.isInteger(d) && d >= 0 && d <= 8 ? d : 2;
}

function renderTopbar(data) {
  const mon = data.monitor || {};
  const pill = $('#pill-monitor');
  if (mon.running) {
    pill.textContent = `monitor: pid ${mon.pid}`;
    pill.className = 'pill pill-ok';
  } else if (data.control.ok) {
    pill.textContent = 'monitor: eksternal';
    pill.className = 'pill pill-gold';
  } else {
    pill.textContent = 'monitor: berhenti';
    pill.className = 'pill pill-warn';
  }
  $('#btn-start').disabled = Boolean(data.control.ok);
  $('#btn-stop').disabled = !mon.running;
  $('#btn-restart').disabled = !mon.running && Boolean(data.control.ok);
  const external = !mon.running && data.control.ok;
  const hint = external ? 'Monitor dikelola di luar panel (systemd / terminal lain)' : '';
  for (const id of ['#btn-start', '#btn-stop', '#btn-restart']) $(id).title = hint;

  const st = data.status;
  if (!st) {
    $('#price').textContent = '—';
    $('#price-delta').textContent = '';
    $('#side-control').textContent = data.control.error || 'control: —';
    return;
  }
  const price = Number(st.price) || 0;
  const d = priceDigits(st);
  $('#price').textContent = price ? price.toFixed(d) : '—';
  $('#side-control').textContent = data.control.ok ? 'control: ok' : `control: ${data.control.error || 'mati'}`;
  $('#side-symbol').textContent = st.symbol;
  $('#symbol-current').textContent = st.symbol || '—';
  $('#pill-uptime').textContent = `uptime ${fmtDuration(st.uptime_s)}`;
  const orders = st.orders || {};
  const lay = $('#btn-layering');
  lay.textContent = `layering: ${orders.layering ? 'ON' : 'OFF'}`;
  lay.className = `pill ${orders.layering ? 'pill-ok' : 'pill-quiet'}`;
  lay.disabled = !orders.armed;
  lay.title = orders.armed
    ? `Klik untuk ${orders.layering ? 'memadamkan' : 'menyalakan'} layering (min ${orders.layering_min ?? 2} layer)`
    : 'order belum armed — layering tidak bisa diubah';
  const conn = st.broker ? Boolean(st.broker.connected) : false;
  $('#pill-source').textContent = `sumber: mt4 · ${conn ? 'EA ok' : 'EA putus'}`;
  $('#pill-source').className = `pill ${conn ? 'pill-gold' : 'pill-warn'}`;

  if (price > 0) {
    store.prices.push(price);
    if (store.prices.length > 240) store.prices.shift();
    const prev = store.prices.length > 1 ? store.prices[store.prices.length - 2] : price;
    const delta = price - prev;
    const node = $('#price-delta');
    node.textContent = delta ? `${delta > 0 ? '+' : ''}${delta.toFixed(d)}` : '';
    node.className = `delta ${delta > 0 ? 'up' : delta < 0 ? 'down' : ''}`;
  }
  drawSpark();
}

function renderDashboard(data) {
  const st = data.status;
  if (!st) {
    $('#engines').innerHTML = '<p class="muted">menunggu monitor …</p>';
    $('#readiness').innerHTML = '<p class="muted">menunggu monitor …</p>';
    return;
  }
  renderReadiness(st);
  const tick = st.tick || {};
  $('#m-rate').textContent = `${tick.rate ?? 0}/s`;
  $('#m-mt4').textContent = String(tick.mt4_ticks ?? 0);
  $('#m-age').textContent = fmtAgo(tick.mt4_age_s);
  $('#m-ea').textContent = st.broker
    ? (st.broker.connected ? 'tersambung' : 'EA belum ada')
    : '—';
  $('#m-orders').textContent = st.orders && st.orders.state ? st.orders.state : '—';
  $('#m-broker').textContent = st.broker
    ? `${st.broker.symbol || '—'} · ${st.broker.connected ? 'EA tersambung' : 'EA belum ada'}`
    : '—';
  $('#m-ea-error').textContent = (st.broker && st.broker.last_error) || '—';

  const list = $('#engines');
  const engines = st.engines || {};
  const modes = Object.keys(engines);
  if (!modes.length) {
    list.innerHTML = '<p class="muted">engine belum siap (menunggu seed/candle)</p>';
    return;
  }
  list.innerHTML = '';
  for (const mode of modes) {
    const eng = engines[mode];
    const row = document.createElement('div');
    row.className = 'engine';
    const m = document.createElement('span');
    m.className = 'mode';
    m.textContent = mode;
    const s = document.createElement('span');
    s.className = `state state-${eng.state}`;
    s.textContent = eng.state;
    const b = document.createElement('span');
    b.className = 'blocked';
    b.textContent = eng.blocked || '—';
    row.append(m, s, b);
    list.append(row);
  }
}

function addPair(dl, label, value) {
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  dd.textContent = value;
  dl.append(dt, dd);
}

function renderReadiness(st) {
  const box = $('#readiness');
  const data = (st && st.readiness) || {};
  const modes = Object.keys(data);
  if (!modes.length) {
    box.innerHTML = '<p class="muted">menunggu snapshot candle…</p>';
    return;
  }
  const d = priceDigits(st);
  box.textContent = '';
  for (const mode of modes) {
    const rd = data[mode];
    const pct = Math.max(0, Math.min(100, rd.percent ?? 0));
    const el = document.createElement('div');
    el.className = `ready${pct >= 100 ? ' is-ready' : ''}`;

    const head = document.createElement('div');
    head.className = 'ready-head';
    const m = document.createElement('span');
    m.className = 'ready-mode';
    m.textContent = mode;
    const big = document.createElement('span');
    big.className = 'ready-pct';
    big.textContent = `${Math.round(pct)}%`;
    const sub = document.createElement('span');
    sub.className = 'ready-sub';
    sub.textContent =
      `kurang ${Math.round(rd.remaining_pct ?? 100)}% · ${rd.gates_passed ?? 0}/${rd.gates_total ?? 0} gate`;
    head.append(m, big, sub);

    const bar = document.createElement('div');
    bar.className = 'bar';
    const fill = document.createElement('span');
    fill.style.width = `${pct}%`;           // CSSOM, bukan atribut style (CSP)
    bar.append(fill);

    const meta = document.createElement('dl');
    meta.className = 'ready-meta';
    addPair(meta, 'Target', rd.trigger_price == null ? '—' : Number(rd.trigger_price).toFixed(d));
    const dist = rd.distance_pct;
    const arah = dist == null ? '—' : dist > 0 ? 'naik' : dist < 0 ? 'turun' : 'di harga';
    addPair(meta, 'Jarak', dist == null ? '—' : `${dist > 0 ? '+' : ''}${Number(dist).toFixed(2)}% · ${arah}`);
    addPair(meta, 'Berikutnya', rd.next_label || '—');
    if (rd.side) addPair(meta, 'Arah', rd.side);
    if ((rd.zones || []).length) {
      const price = Number(store.status.price) || 0;
      const near = [...rd.zones]
        .sort((a, b) => Math.abs(a - price) - Math.abs(b - price))
        .slice(0, 3)
        .map((v) => Number(v).toFixed(d));
      addPair(meta, 'Kandidat zona', near.join(' / '));
    }
    if (rd.extreme) {
      const ex = rd.extreme;
      const pos = Number(ex.pos).toFixed(1);
      let needText;
      if (ex.need == null) {
        if (ex.pos <= 20) needText = 'sudah ≤20% (sisi SELL)';
        else if (ex.pos >= 80) needText = 'sudah ≥80% (sisi BUY)';
        else needText = 'butuh ≤20% atau ≥80%';
      } else {
        needText = `butuh ≤${ex.need}%`;
      }
      const gap = ex.need == null ? '' : ex.ok ? ' · lolos' : ` · kurang ${ex.gap} poin`;
      addPair(meta, `Ekstrem ${ex.tf}`, `${pos}% · ${needText}${gap}`);
    }
    if (rd.trigger_price == null && rd.watch) {
      const parts = [];
      if ((rd.watch.res_above || []).length) parts.push(`res ${Number(rd.watch.res_above[0]).toFixed(d)}`);
      if ((rd.watch.sup_below || []).length) parts.push(`sup ${Number(rd.watch.sup_below[0]).toFixed(d)}`);
      if (parts.length) addPair(meta, 'Level pantau', parts.join(' / '));
    }
    if (rd.plan) {
      const tps = (rd.plan.tps || []).map((t) => Number(t).toFixed(d)).join(' / ') || '—';
      addPair(meta, 'CL / TP', `${Number(rd.plan.stop).toFixed(d)} · ${tps}`);
    }

    const note = document.createElement('p');
    note.className = 'ready-note';
    note.textContent = rd.note || '';
    el.append(head, bar, meta, note);
    box.append(el);
  }
}

function drawSpark() {  const canvas = $('#spark');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 720, h = canvas.clientHeight || 180;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const data = store.prices;
  if (data.length < 2) {
    ctx.fillStyle = '#8ba2b8';
    ctx.font = '12px ui-monospace, monospace';
    ctx.fillText('menunggu tick…', 12, 24);
    return;
  }
  const min = Math.min(...data), max = Math.max(...data);
  const pad = 14, span = max - min || 1;
  const x = (i) => pad + (i / (data.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - ((v - min) / span) * (h - pad * 2);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(240,180,41,0.28)');
  grad.addColorStop(1, 'rgba(240,180,41,0)');
  ctx.beginPath();
  ctx.moveTo(x(0), y(data[0]));
  data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(data.length - 1), h - pad);
  ctx.lineTo(x(0), h - pad);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(x(0), y(data[0]));
  data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.strokeStyle = '#f0b429';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();

  ctx.fillStyle = '#8ba2b8';
  ctx.font = '11px ui-monospace, monospace';
  const d = priceDigits(store.status);
  ctx.fillText(max.toFixed(d), pad, 14);
  ctx.fillText(min.toFixed(d), pad, h - 4);
  $('#chart-range').textContent =
    `${data.length} sampel · ${fmtDuration(store.status ? store.status.uptime_s : 0)}`;
}

export async function loadReport() {
  try {
    const data = await api(withInst(`/api/report?kind=${store.reportKind}`), { guard: false });
    $('#report').textContent = plainText(data.text || '') || '(kosong)';
  } catch (err) {
    $('#report').textContent = `Tidak bisa memuat laporan: ${err.message}`;
  }
}
