const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout: { background: { color: '#1e222d' }, textColor: '#d1d4dc' },
  grid: { vertLines: { color: '#2a2e39' }, horzLines: { color: '#2a2e39' } },
  timeScale: { timeVisible: true },
});
const candleSeries = chart.addCandlestickSeries({
  upColor: '#089981', downColor: '#f23645', wickUpColor: '#089981', wickDownColor: '#f23645',
});

const symbolSel = document.getElementById('symbol');
const tfSel = document.getElementById('timeframe');
const statusEl = document.getElementById('status');
let fvgSeries = [];

function symbol() { return symbolSel.value; }
function tf() { return tfSel.value; }

async function loadCandles() {
  const r = await fetch(`/api/candles?symbol=${symbol()}&timeframe=${tf()}&limit=300`);
  candleSeries.setData(await r.json());
  await loadFvgs();
}

async function loadFvgs() {
  fvgSeries.forEach(s => chart.removeSeries(s));
  fvgSeries = [];
  const r = await fetch(`/api/fvgs?symbol=${symbol()}&timeframe=${tf()}&limit=50`);
  const fvgs = await r.json();
  const now = Math.floor(Date.now() / 1000);
  for (const f of fvgs.filter(f => f.status === 'open')) {
    const color = f.direction === 'bullish' ? 'rgba(8,153,129,0.25)' : 'rgba(242,54,69,0.25)';
    const s = chart.addHistogramSeries({ color, priceFormat: { type: 'price' }, lastValueVisible: false, priceLineVisible: false });
    const t0 = Math.floor(f.formed_at / 1000);
    s.setData([
      { time: t0, value: f.top },
      { time: now, value: f.top },
    ]);
    // segunda serie para el borde inferior
    const s2 = chart.addHistogramSeries({ color, lastValueVisible: false, priceLineVisible: false });
    s2.setData([{ time: t0, value: f.bottom }, { time: now, value: f.bottom }]);
    fvgSeries.push(s, s2);
  }
}

async function loadBook() {
  const r = await fetch(`/api/orderbook/${symbol()}`);
  const book = await r.json();
  const asks = (book.asks || []).slice(0, 12).reverse();
  const bids = (book.bids || []).slice(0, 12);
  const bidTotal = (book.bids || []).reduce((a, x) => a + x[1], 0);
  const askTotal = (book.asks || []).reduce((a, x) => a + x[1], 0);
  const ratio = bidTotal + askTotal > 0 ? bidTotal / (bidTotal + askTotal) : 0.5;
  document.getElementById('imb').innerHTML =
    `Imbalance: <b>${(ratio * 100).toFixed(1)}% bids</b>` +
    `<div class="bar-wrap"><div class="bar" style="width:${ratio * 100}%;background:#089981"></div></div>`;
  const maxQ = Math.max(...[...asks, ...bids].map(x => x[1]), 0);
  const wallQ = maxQ * 0.5;
  let html = '';
  asks.forEach(([p, q]) => {
    html += `<tr class="ask${q >= wallQ ? ' wall' : ''}"><td>${p.toFixed(2)}</td><td>${q.toFixed(3)}</td></tr>`;
  });
  html += '<tr><td colspan="2" style="text-align:center;color:#787b86">— spread —</td></tr>';
  bids.forEach(([p, q]) => {
    html += `<tr class="bid${q >= wallQ ? ' wall' : ''}"><td>${p.toFixed(2)}</td><td>${q.toFixed(3)}</td></tr>`;
  });
  document.getElementById('book').innerHTML = html;
}

function addAlert(type, payload) {
  const el = document.createElement('div');
  el.className = 'alert';
  el.innerHTML = `<span class="${type}"><b>${type}</b></span> ${JSON.stringify(payload)}`;
  document.getElementById('alert-list').prepend(el);
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { statusEl.textContent = 'en vivo'; statusEl.style.color = '#089981'; };
  ws.onclose = () => {
    statusEl.textContent = 'reconectando…'; statusEl.style.color = '#f23645';
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    addAlert(msg.type, msg.payload);
    if (msg.type.startsWith('fvg')) loadFvgs();
    if (msg.type === 'wall' || msg.type === 'imbalance') loadBook();
  };
}

async function refresh() { await loadCandles(); await loadBook(); }
symbolSel.onchange = refresh;
tfSel.onchange = refresh;
refresh();
setInterval(loadCandles, 30000);   // velas nuevas cada 30s
setInterval(loadBook, 5000);       // libro cada 5s
connectWs();
