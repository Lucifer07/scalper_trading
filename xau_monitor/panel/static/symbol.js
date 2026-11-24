/* Tab Simbol: pindah simbol runtime lewat control API (tick tetap dari MT4). */
import { $, api, toast, notifyRefresh, withInst } from './core.js';

export function initSymbol() {
  $('#symbol-apply').addEventListener('click', applySymbol);
  $('#symbol-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') applySymbol();
  });
}

async function applySymbol() {
  const input = $('#symbol-input');
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) {
    toast('Isi simbol dulu (mis. EURUSD)', true);
    return;
  }
  try {
    const data = await api(withInst('/api/symbol'), { method: 'POST', body: { symbol } });
    toast(data.message || `Simbol: ${symbol}`);
    input.value = '';
    notifyRefresh();
  } catch (err) {
    toast(`Gagal pindah simbol: ${err.message}`, true);
  }
}
