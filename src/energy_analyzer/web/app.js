/* Energy Analyzer dashboard. Vanilla ES2020, no dependencies, CSP friendly (no inline styles).
 *
 * Sections: state, api, utils/format, charts, components, views, router.
 * Every API text goes through esc()/html`` (or textContent) before it reaches the DOM.
 */
'use strict';

/* =========================================================================================
 * state
 * ======================================================================================= */

const state = {
  meta: null, // /api/meta
  events: [], // /api/events
  token: 0, // increments per route render; stale async renders check it
  cleanup: [], // functions to run when leaving a view (timers)
  charts: new Set(), // redraw functions of the charts on the current view
  view: '', // name of the current view
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const alive = (token) => token === state.token;

/* =========================================================================================
 * api
 * ======================================================================================= */

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v != null && v !== '') p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
}

async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new ApiError(0, 'The server is not reachable. Is "energy-analyzer serve" still running?');
  }
  if (res.status === 401) {
    showNotice('Login required: reload the page and sign in with the dashboard user (WEB_USER / WEB_PASSWORD).');
    throw new ApiError(401, 'Login required');
  }
  if (res.status === 204) return null;
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || `Request failed (HTTP ${res.status})`);
  return data;
}

const API = {
  meta: () => api('/api/meta'),
  summary: (from, to) => api(`/api/summary${qs({ from, to })}`),
  day: (date) => api(`/api/day${qs({ date })}`),
  heatmap: (from, to) => api(`/api/heatmap${qs({ from, to })}`),
  live: () => api('/api/live'),
  events: () => api('/api/events'),
  createEvent: (body) => api('/api/events', { method: 'POST', body }),
  updateEvent: (id, body) => api(`/api/events/${encodeURIComponent(id)}`, { method: 'PUT', body }),
  deleteEvent: (id) => api(`/api/events/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};

async function loadMeta() {
  state.meta = await API.meta();
  renderHeader();
}

async function loadEvents() {
  const data = await API.events();
  state.events = (data && data.events) || [];
}

/* =========================================================================================
 * utils / format
 * ======================================================================================= */

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

/** Trusted HTML fragment produced by html``. */
class Safe {
  constructor(s) {
    this.s = s;
  }
  toString() {
    return this.s;
  }
}
const raw = (s) => new Safe(String(s));
function part(v) {
  if (v instanceof Safe) return v.s;
  if (Array.isArray(v)) return v.map(part).join('');
  if (v == null || v === false) return '';
  return esc(v);
}
/** Tagged template: interpolations are escaped unless they are Safe (or arrays of Safe). */
function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += part(vals[i]) + strings[i + 1];
  return new Safe(out);
}
function setHTML(el, content) {
  el.innerHTML = content instanceof Safe ? content.s : esc(content == null ? '' : content);
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const f1 = (v) => (Math.round(v * 10) / 10).toString();
const pad2 = (n) => String(n).padStart(2, '0');
const ISO = /^\d{4}-\d{2}-\d{2}$/;
const NBSP = ' ';
const DASH = '–';
function debounce(fn, ms) {
  let t = 0;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}
function median(arr) {
  const v = arr.filter(isNum).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// numbers
const nfCache = new Map();
function nf(digits) {
  if (!nfCache.has(digits)) {
    nfCache.set(digits, new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return nfCache.get(digits);
}
const fmt = {
  num: (v, d = 0) => (isNum(v) ? nf(d).format(v) : DASH),
  kwh(v, d) {
    if (!isNum(v)) return DASH;
    if (d == null) d = Math.abs(v) >= 100 ? 0 : 1;
    return `${nf(d).format(v)}${NBSP}kWh`;
  },
  w: (v) => (isNum(v) ? `${nf(0).format(v)}${NBSP}W` : DASH),
  power(v) {
    if (!isNum(v)) return DASH;
    return Math.abs(v) >= 1000 ? `${nf(2).format(v / 1000)}${NBSP}kW` : `${nf(0).format(v)}${NBSP}W`;
  },
  pct: (r, d = 0) => (isNum(r) ? `${nf(d).format(r * 100)}${NBSP}%` : DASH),
  signed(v, d, unit) {
    if (!isNum(v)) return DASH;
    const s = v > 0 ? '+' : v < 0 ? '−' : '±';
    return `${s}${nf(d).format(Math.abs(v))}${unit ? NBSP + unit : ''}`;
  },
};

// dates ('YYYY-MM-DD' strings are handled as UTC dates, never shifted by time zones)
const DAY_MS = 864e5;
function parseDay(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d || 1));
}
const isoDay = (dt) => dt.toISOString().slice(0, 10);
const addDays = (s, n) => isoDay(new Date(parseDay(s).getTime() + n * DAY_MS));
const daysBetween = (a, b) => Math.round((parseDay(b) - parseDay(a)) / DAY_MS);
const weekday = (s) => parseDay(s).getUTCDay();
function todayLocal() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
const dtf = (o) => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', ...o });
const DF = {
  long: dtf({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
  short: dtf({ weekday: 'short', day: 'numeric', month: 'short' }),
  dm: dtf({ day: 'numeric', month: 'short' }),
  dmy: dtf({ day: 'numeric', month: 'short', year: 'numeric' }),
  mon: dtf({ month: 'short' }),
  monY: dtf({ month: 'short', year: 'numeric' }),
  monLong: dtf({ month: 'long', year: 'numeric' }),
  year: dtf({ year: 'numeric' }),
};
const LOCAL_DT = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const LOCAL_T = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
Object.assign(fmt, {
  day: (s) => (s ? DF.long.format(parseDay(s)) : DASH),
  dayShort: (s) => (s ? DF.short.format(parseDay(s)) : DASH),
  dm: (s) => (s ? DF.dm.format(parseDay(s)) : DASH),
  month: (m) => (m ? DF.monY.format(parseDay(`${m}-01`)) : DASH),
  monthShort: (m) => DF.mon.format(parseDay(`${m}-01`)),
  range(a, b) {
    if (!a) return DASH;
    if (!b || a === b) return DF.dmy.format(parseDay(a));
    try {
      return DF.dmy.formatRange(parseDay(a), parseDay(b));
    } catch (err) {
      return `${DF.dmy.format(parseDay(a))} ${DASH} ${DF.dmy.format(parseDay(b))}`;
    }
  },
  dateTime(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? DASH : LOCAL_DT.format(d);
  },
  time(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? DASH : LOCAL_T.format(d);
  },
});
/** "412 kWh" -> number with a smaller unit span. */
function vu(text) {
  const i = text.lastIndexOf(NBSP);
  return i < 0 ? html`${text}` : html`${text.slice(0, i)}<span class="u">${text.slice(i + 1)}</span>`;
}
const toMin = (t) => {
  const [h, m] = String(t).split(':').map(Number);
  return h * 60 + (m || 0);
};
const minLabel = (v) => (v >= 1440 ? '24:00' : `${pad2(Math.floor(v / 60))}:${pad2(v % 60)}`);

// domain vocabulary
const KIND_LABEL = { baseline: 'Standby', consumption: 'Consumption', night_spike: 'Night spike', not_lower: 'Not lower' };
const anomalyUnit = (kind) => (kind === 'consumption' || kind === 'not_lower' ? 'kWh' : 'W');
function anomalyValue(kind, v) {
  return anomalyUnit(kind) === 'kWh' ? fmt.kwh(v, 1) : fmt.w(v);
}
const EFFECT = {
  expect_lower: {
    label: 'Expect lower',
    desc: 'Expect less use: these days stay out of the reference, a low day counts as expected and a day that is not lower gets flagged.',
  },
  expect_higher: {
    label: 'Expect higher',
    desc: 'Expect more use: these days stay out of the reference and a high day counts as expected.',
  },
  regime_change: {
    label: 'New normal',
    desc: 'From this day on the household behaves differently: later days are only compared with days after the change.',
  },
  ignore: {
    label: 'Ignore',
    desc: 'The data is not representative: no warnings for these days and they never count as reference.',
  },
  info: { label: 'Note only', desc: 'Just a note on the charts; the analysis is not changed.' },
};
const effectLabel = (e) => (EFFECT[e] ? EFFECT[e].label : e);
const kindInfo = (k) => ((state.meta && state.meta.event_kinds) || []).find((x) => x.kind === k);
const eventEnd = (e) => e.end_day || e.start_day;
const eventCovers = (e, day) => e.start_day <= day && day <= eventEnd(e);
function eventPeriod(e) {
  if (e.effect === 'regime_change') return `from ${fmt.day(e.start_day)}`;
  return fmt.range(e.start_day, eventEnd(e));
}

// misc DOM
function showNotice(text) {
  const n = $('#notice');
  n.textContent = text;
  n.hidden = false;
}
function linkDay(day, label) {
  return html`<a href="#/day/${day}">${label || fmt.dayShort(day)}</a>`;
}

/* =========================================================================================
 * charts
 * ======================================================================================= */

// --- tooltip (one shared, absolutely positioned div) ---
function showTip(content, x, y) {
  const t = $('#tooltip');
  setHTML(t, content);
  t.hidden = false;
  const r = t.getBoundingClientRect();
  let left = x + 14;
  if (left + r.width > window.innerWidth - 8) left = x - 14 - r.width;
  left = clamp(left, 8, Math.max(8, window.innerWidth - r.width - 8));
  let top = y + 14;
  if (top + r.height > window.innerHeight - 8) top = y - 14 - r.height;
  top = clamp(top, 8, Math.max(8, window.innerHeight - r.height - 8));
  t.style.left = `${Math.round(left)}px`;
  t.style.top = `${Math.round(top)}px`;
}
function hideTip() {
  const t = $('#tooltip');
  if (t) t.hidden = true;
}
/** Tooltip body: title, rows of {name, value, cls, type}, optional notes. */
function ttBody(title, rows, notes) {
  return html`<div class="tt-title">${title}</div>
    <table class="tt-rows">${rows.filter(Boolean).map(
      (r) => html`<tr><td>${r.cls ? html`<span class="sw sw-${r.type || 'bar'} ${r.cls}"></span>` : ''}${r.name}</td><td class="num">${r.value}</td></tr>`,
    )}</table>${notes || ''}`;
}

// --- scales ---
function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
function decimals(step) {
  let d = 0;
  while (d < 6 && Math.abs(Math.round(step * 10 ** d) - step * 10 ** d) > 1e-6) d++;
  return d;
}
function niceScale(lo, hi, count = 5) {
  if (!isNum(lo) || !isNum(hi)) {
    lo = 0;
    hi = 1;
  }
  if (lo === hi) {
    if (hi === 0) hi = 1;
    else if (hi > 0) lo = 0;
    else hi = 0;
  }
  const step = niceStep((hi - lo) / count);
  const min = Math.floor(lo / step + 1e-9) * step;
  const max = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let v = min; v <= max + step / 2; v += step) ticks.push(Math.abs(v) < step * 1e-6 ? 0 : Number(v.toFixed(8)));
  return { min, max, step, ticks, digits: decimals(step) };
}

/** X tick generator for a list of ISO days used as band categories. */
function dayTicks(days) {
  return (iw) => {
    const n = days.length;
    if (!n) return [];
    const maxLabels = Math.max(2, Math.floor(iw / 62));
    if (n > 75) {
      let t = [];
      days.forEach((d, i) => {
        if (d.endsWith('-01')) t.push({ v: i, label: d.slice(5, 7) === '01' ? DF.year.format(parseDay(d)) : DF.mon.format(parseDay(d)) });
      });
      if (t.length >= 2) {
        const step = Math.ceil(t.length / maxLabels);
        if (step > 1) t = t.filter((_, k) => k % step === 0);
        return t;
      }
    }
    const nice = [1, 2, 3, 7, 14, 28, 56, 91];
    const want = Math.ceil(n / maxLabels);
    const step = nice.find((s) => s >= want) || want;
    const t = [];
    for (let i = n - 1; i >= 0; i -= step) t.push({ v: i, label: DF.dm.format(parseDay(days[i])) });
    return t.reverse();
  };
}
/** X tick generator for minutes of a day (0..1440). */
function hourTicks(iw) {
  const step = iw < 360 ? 360 : iw < 640 ? 180 : 120;
  const t = [];
  for (let v = 0; v <= 1440; v += step) t.push({ v, label: v === 1440 ? '24:00' : `${pad2(v / 60)}:00` });
  return t;
}

/** Redraw a chart whenever its plot element changes width (viewport, layout, scrollbar). */
let resizeObs = null;
function registerChart(redraw, plot) {
  state.charts.add(redraw);
  if (!resizeObs && window.ResizeObserver) {
    resizeObs = new ResizeObserver(debounce(() => redrawCharts(false), 60));
  }
  if (resizeObs && plot) resizeObs.observe(plot);
}
function redrawCharts(force) {
  for (const r of state.charts) r(force);
}

/**
 * Generic XY chart: bars, areas, lines and points over band (categorical) or linear x.
 * cfg = {
 *   ariaLabel, height, n,
 *   x: { type: 'band' | 'linear', domain: [x0, x1] (linear), ticks: (innerWidth) => [{v, label}] },
 *   xs: [x values] (linear), maxGap (linear: break lines when x jumps more than this),
 *   y: { unit, zero (default true), min }, y2: { unit, min, max, ticks, fmt } | null,
 *   series: [{ key, name, cls, type: 'bar'|'area'|'line'|'points', values, axis: 'y'|'y2',
 *              dashed, thin, hidden, noLegend }],
 *   bands: [{ from, to, cls, label }]  (band: indices inclusive; linear: x values)
 *   vmarks: [{ x, cls, label }]         (band: index, drawn at the left edge)
 *   marks: [{ i, cls }]                 small triangles above the plot
 *   refs: [{ y, axis, cls, label }]     horizontal dashed reference lines
 *   legendExtra: [{ name, type, cls }], tooltip: (i) => Safe, onClick: (i) => void, emptyText
 * }
 */
function mountChart(host, cfg) {
  host.classList.add('chart');
  const hidden = new Set(cfg.series.filter((s) => s.hidden).map((s) => s.key));
  const legend = document.createElement('div');
  legend.className = 'legend';
  const plot = document.createElement('div');
  plot.className = 'plot';
  host.replaceChildren(legend, plot);
  let geo = null;
  let hover = -1;
  let lastPointer = 'mouse';
  let lastW = 0;

  function drawLegend() {
    const items = cfg.series
      .filter((s) => !s.noLegend)
      .map(
        (s) => html`<button type="button" class="lg" data-key="${s.key}" aria-pressed="${hidden.has(s.key) ? 'false' : 'true'}"
          ><span class="sw sw-${s.type === 'bar' ? 'bar' : s.type}${s.dashed ? ' dash' : ''} ${s.cls}" aria-hidden="true"></span>${s.name}</button>`,
      );
    const extra = (cfg.legendExtra || []).map(
      (x) => html`<span class="lg static"><span class="sw sw-${x.type || 'band'} ${x.cls || ''}" aria-hidden="true"></span>${x.name}</span>`,
    );
    setHTML(legend, html`${items}${extra}`);
  }
  legend.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-key]');
    if (!b) return;
    const k = b.dataset.key;
    if (hidden.has(k)) hidden.delete(k);
    else hidden.add(k);
    drawLegend();
    draw();
  });

  function draw() {
    const W = Math.max(240, Math.round(plot.clientWidth || host.clientWidth || 600));
    lastW = plot.clientWidth;
    const H = Math.round((cfg.height || 260) * (W < 480 ? 0.85 : 1));
    const n = cfg.n || 0;
    const vis = cfg.series.filter((s) => !hidden.has(s.key));
    const hasData = n > 0 && cfg.series.some((s) => s.values.some(isNum));
    if (!hasData) {
      geo = null;
      setHTML(plot, html`<div class="empty">${cfg.emptyText || 'No data for this period.'}</div>`);
      return;
    }
    const m = { l: cfg.marginLeft || 46, r: cfg.y2 ? 40 : 12, t: 16, b: 24 };
    const iw = W - m.l - m.r;
    const ih = H - m.t - m.b;
    const band = cfg.x.type === 'band';
    const bw = band ? iw / n : 0;
    const x0 = band ? 0 : cfg.x.domain[0];
    const x1 = band ? 1 : cfg.x.domain[1];
    const XV = (v) => (band ? m.l + (v + 0.5) * bw : m.l + ((v - x0) / (x1 - x0)) * iw);
    const XI = (i) => (band ? XV(i) : XV(cfg.xs[i]));

    // y domain from the visible series on the left axis
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of vis) {
      if (s.axis === 'y2') continue;
      for (const v of s.values) {
        if (!isNum(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    for (const r of cfg.refs || []) {
      if (r.axis === 'y2' || !isNum(r.y)) continue;
      lo = Math.min(lo, r.y);
      hi = Math.max(hi, r.y);
    }
    if (lo === Infinity) {
      lo = 0;
      hi = 1;
    }
    if (cfg.y.zero !== false) {
      lo = Math.min(lo, 0);
      hi = Math.max(hi, 0);
    } else if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    } else {
      const padY = (hi - lo) * 0.06;
      lo -= padY;
      hi += padY;
    }
    if (isNum(cfg.y.min)) lo = cfg.y.min;
    const ys = niceScale(lo, hi, ih < 190 ? 4 : 5);
    const Y = (v) => m.t + ih - ((v - ys.min) / (ys.max - ys.min)) * ih;
    const y2 = cfg.y2 || null;
    const Y2 = (v) => m.t + ih - ((v - y2.min) / (y2.max - y2.min)) * ih;
    const yOf = (s) => (s.axis === 'y2' && y2 ? Y2 : Y);
    const out = [];

    // event / night bands
    for (const b of cfg.bands || []) {
      const xa = clamp(band ? m.l + b.from * bw : XV(b.from), m.l, m.l + iw);
      const xb = clamp(band ? m.l + (b.to + 1) * bw : XV(b.to), m.l, m.l + iw);
      if (xb - xa <= 0.2) continue;
      out.push(`<rect class="band ${esc(b.cls || '')}" x="${f1(xa)}" y="${m.t}" width="${f1(Math.max(1, xb - xa))}" height="${ih}"/>`);
      if (b.label && xb - xa > 44) {
        const maxChars = Math.floor((xb - xa - 8) / 6);
        const text = b.label.length > maxChars ? `${b.label.slice(0, Math.max(1, maxChars - 1))}…` : b.label;
        out.push(`<text class="band-label" x="${f1(xa + 4)}" y="${m.t + 11}">${esc(text)}</text>`);
      }
    }

    // grid and y axis
    for (const t of ys.ticks) {
      const y = f1(Y(t));
      out.push(`<line class="grid${t === 0 ? ' zero' : ''}" x1="${m.l}" x2="${m.l + iw}" y1="${y}" y2="${y}"/>`);
      out.push(`<text x="${m.l - 6}" y="${y}" dy="0.32em" text-anchor="end">${esc(fmt.num(t, ys.digits))}</text>`);
    }
    if (cfg.y.unit) out.push(`<text class="unit" x="${m.l - 6}" y="9" text-anchor="end">${esc(cfg.y.unit)}</text>`);
    if (y2) {
      for (const t of y2.ticks) {
        out.push(`<text x="${m.l + iw + 6}" y="${f1(Y2(t))}" dy="0.32em">${esc(y2.fmt ? y2.fmt(t) : fmt.num(t))}</text>`);
      }
      if (y2.unit) out.push(`<text class="unit" x="${m.l + iw + 6}" y="9">${esc(y2.unit)}</text>`);
    }

    // bars (band x only), grouped when there is room, else overlapped
    const bars = vis.filter((s) => s.type === 'bar');
    if (bars.length && band) {
      const inner = Math.min(bw - 2 * (bw > 6 ? bw * 0.16 : bw > 3 ? bw * 0.08 : 0), 34 * bars.length);
      const pad = (bw - inner) / 2;
      const grouped = bars.length === 1 || inner / bars.length >= 3;
      bars.forEach((s, k) => {
        let w;
        let off;
        if (grouped) {
          w = inner / bars.length;
          off = pad + k * w;
          if (w > 4 && bars.length > 1) w -= 1;
        } else {
          w = k === 0 ? inner : inner * 0.5;
          off = pad + (inner - w) / 2;
        }
        w = Math.max(w, 0.6);
        const Yf = yOf(s);
        let d = '';
        s.values.forEach((v, i) => {
          if (!isNum(v) || v === 0) return;
          const ya = Yf(Math.max(0, v));
          const yb = Yf(Math.min(0, v));
          d += `M${f1(m.l + i * bw + off)} ${f1(ya)}h${f1(w)}V${f1(Math.max(yb, ya + 0.5))}h${f1(-w)}Z`;
        });
        const back = !grouped && k === 0 ? ' back' : '';
        if (d) out.push(`<path class="bar ${esc(s.cls)}${back}" d="${d}"/>`);
      });
    }

    // contiguous index runs (split at nulls and at x jumps larger than maxGap)
    const runs = (s) => {
      const res = [];
      let cur = [];
      s.values.forEach((v, i) => {
        const gap = !band && cfg.maxGap && i > 0 && cfg.xs[i] - cfg.xs[i - 1] > cfg.maxGap;
        if (!isNum(v) || gap) {
          if (cur.length) res.push(cur);
          cur = [];
        }
        if (isNum(v)) cur.push(i);
      });
      if (cur.length) res.push(cur);
      return res;
    };
    const linePath = (s, Yf) =>
      runs(s)
        .map((r) => `M${r.map((i) => `${f1(XI(i))} ${f1(Yf(s.values[i]))}`).join('L')}${r.length === 1 ? 'h0.1' : ''}`)
        .join('');

    for (const s of vis.filter((x) => x.type === 'area')) {
      const Yf = yOf(s);
      const base = f1(Yf(0));
      let da = '';
      for (const r of runs(s)) {
        da += `M${f1(XI(r[0]))} ${base}L${r.map((i) => `${f1(XI(i))} ${f1(Yf(s.values[i]))}`).join('L')}L${f1(XI(r[r.length - 1]))} ${base}Z`;
      }
      out.push(`<path class="area ${esc(s.cls)}" d="${da}"/><path class="line thin ${esc(s.cls)}" d="${linePath(s, Yf)}"/>`);
    }
    const drawPoints = (s) => {
      const Yf = yOf(s);
      const r = s.r || (n > 200 ? 1.7 : n > 90 ? 2.2 : 2.8);
      const pts = [];
      s.values.forEach((v, i) => {
        if (isNum(v)) pts.push(`<circle class="pt" cx="${f1(XI(i))}" cy="${f1(Yf(v))}" r="${r}"/>`);
      });
      out.push(`<g class="${esc(s.cls)}">${pts.join('')}</g>`);
    };
    vis.filter((x) => x.type === 'points' && !x.top).forEach(drawPoints);
    for (const s of vis.filter((x) => x.type === 'line')) {
      const cls = `line ${s.dashed ? 'dashed ' : ''}${s.thin ? 'thin ' : ''}${esc(s.cls)}`;
      const Yf = yOf(s);
      out.push(`<path class="${cls}" d="${linePath(s, Yf)}"/>`);
      const singles = runs(s).filter((r) => r.length === 1);
      if (singles.length && singles.length < 40) {
        const dots = singles.map(([i]) => `<circle class="pt" cx="${f1(XI(i))}" cy="${f1(Yf(s.values[i]))}" r="2.2"/>`);
        out.push(`<g class="${esc(s.cls)}">${dots.join('')}</g>`);
      }
    }
    vis.filter((x) => x.type === 'points' && x.top).forEach(drawPoints);

    // reference lines, vertical markers, top marks
    for (const r of cfg.refs || []) {
      if (!isNum(r.y)) continue;
      const y = f1(r.axis === 'y2' && y2 ? Y2(r.y) : Y(r.y));
      out.push(`<g class="${esc(r.cls || '')}"><line class="ref" x1="${m.l}" x2="${m.l + iw}" y1="${y}" y2="${y}"/>`);
      if (r.label) out.push(`<text class="ref-label" x="${m.l + iw - 4}" y="${Number(y) - 4}" text-anchor="end">${esc(r.label)}</text>`);
      out.push('</g>');
    }
    for (const v of cfg.vmarks || []) {
      const x = band ? m.l + v.x * bw : XV(v.x);
      if (x < m.l - 0.5 || x > m.l + iw + 0.5) continue;
      const right = x > m.l + iw - 90;
      out.push(
        `<g class="vmark ${esc(v.cls || '')}"><line x1="${f1(x)}" x2="${f1(x)}" y1="${m.t}" y2="${m.t + ih}"/>` +
          `<text x="${f1(right ? x - 4 : x + 4)}" y="${m.t + ih - 5}" text-anchor="${right ? 'end' : 'start'}">${esc(v.label || '')}</text></g>`,
      );
    }
    for (const mk of cfg.marks || []) {
      const x = XI(mk.i);
      out.push(`<path class="mark ${esc(mk.cls || '')}" d="M${f1(x - 4)} ${m.t - 10}h8l-4 6.5z"/>`);
    }

    // x axis
    for (const t of cfg.x.ticks(iw)) {
      const x = XV(t.v);
      if (x < m.l - 1 || x > m.l + iw + 1) continue;
      const half = t.label.length * 3.1;
      const cx = clamp(x, half + 1, W - half - 1);
      out.push(`<line class="xtick" x1="${f1(x)}" x2="${f1(x)}" y1="${m.t + ih}" y2="${m.t + ih + 4}"/>`);
      out.push(`<text x="${f1(cx)}" y="${H - 6}" text-anchor="middle">${esc(t.label)}</text>`);
    }

    // hover furniture + hit area
    out.push(
      band
        ? `<rect class="hover-band" x="0" y="${m.t}" width="${f1(Math.max(bw, 1))}" height="${ih}" visibility="hidden"/>`
        : `<line class="guide" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/>`,
    );
    out.push(`<rect class="hit${cfg.onClick ? ' clickable' : ''}" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}"/>`);

    plot.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" tabindex="0" ` +
      `aria-label="${esc(cfg.ariaLabel || 'Chart')}">${out.join('')}</svg>`;
    geo = { W, m, iw, band, bw, XI, n };
    hover = -1;
  }

  const svgEl = () => plot.querySelector('svg');
  function idxAt(clientX) {
    const s = svgEl();
    if (!s || !geo) return -1;
    const r = s.getBoundingClientRect();
    const px = ((clientX - r.left) * geo.W) / r.width;
    if (px < geo.m.l - 2 || px > geo.m.l + geo.iw + 2) return -1;
    if (geo.band) return clamp(Math.floor((px - geo.m.l) / geo.bw), 0, geo.n - 1);
    let a = 0;
    let b = geo.n - 1;
    while (b - a > 1) {
      const mid = (a + b) >> 1;
      if (geo.XI(mid) < px) a = mid;
      else b = mid;
    }
    return Math.abs(geo.XI(a) - px) <= Math.abs(geo.XI(b) - px) ? a : b;
  }
  function setHover(i, cx, cy) {
    const s = svgEl();
    if (!s || !geo) return;
    hover = i;
    const mark = s.querySelector('.hover-band, .guide');
    if (i < 0) {
      if (mark) mark.setAttribute('visibility', 'hidden');
      hideTip();
      return;
    }
    const x = geo.XI(i);
    if (geo.band) mark.setAttribute('x', f1(x - geo.bw / 2));
    else {
      mark.setAttribute('x1', f1(x));
      mark.setAttribute('x2', f1(x));
    }
    mark.setAttribute('visibility', 'visible');
    const content = cfg.tooltip ? cfg.tooltip(i) : null;
    if (!content) return hideTip();
    if (cx == null) {
      const r = s.getBoundingClientRect();
      cx = r.left + (x * r.width) / geo.W;
      cy = r.top + r.height * 0.25;
    }
    showTip(content, cx, cy);
  }
  plot.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'touch') return;
    setHover(idxAt(e.clientX), e.clientX, e.clientY);
  });
  plot.addEventListener('pointerleave', (e) => {
    if (e.pointerType !== 'touch') setHover(-1);
  });
  plot.addEventListener('pointerdown', (e) => {
    lastPointer = e.pointerType;
  });
  plot.addEventListener('click', (e) => {
    const i = idxAt(e.clientX);
    if (i < 0) return;
    if (lastPointer === 'touch' && hover !== i) {
      setHover(i, e.clientX, e.clientY); // first tap shows the values, second tap opens
      return;
    }
    if (cfg.onClick) {
      hideTip();
      cfg.onClick(i);
    }
  });
  plot.addEventListener('keydown', (e) => {
    if (!geo) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      const step = (e.shiftKey ? 10 : 1) * (e.key === 'ArrowRight' ? 1 : -1);
      const i = hover < 0 ? (step > 0 ? 0 : geo.n - 1) : hover + step;
      setHover(clamp(i, 0, geo.n - 1));
    } else if (e.key === 'Enter' && hover >= 0 && cfg.onClick) {
      cfg.onClick(hover);
    } else if (e.key === 'Escape') setHover(-1);
  });
  plot.addEventListener('focusout', () => setHover(-1));

  drawLegend();
  draw();
  registerChart((force) => {
    if (!plot.isConnected) return;
    if (force || plot.clientWidth !== lastW) draw();
  }, plot);
}

// --- heatmap ---
function parseHex(c) {
  const m = /^#?([0-9a-f]{6})$/i.exec(c.trim());
  if (!m) return [128, 128, 128];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function heatRamp() {
  const cs = getComputedStyle(document.documentElement);
  const stops = [0, 1, 2, 3, 4].map((k) => parseHex(cs.getPropertyValue(`--heat-${k}`) || '#888888'));
  return (t) => {
    t = clamp(t, 0, 1) * (stops.length - 1);
    const k = Math.min(stops.length - 2, Math.floor(t));
    const f = t - k;
    const c = stops[k].map((a, j) => Math.round(a + (stops[k + 1][j] - a) * f));
    return `#${c.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
  };
}
let gradientSeq = 0;

/**
 * Day x hour heatmap. cfg = { days, values, max, marks: Map(day -> [effect]), ariaLabel,
 * tooltip: (row, hour) => Safe, onClick: (row) => void }. Colors use a sqrt scale.
 */
function mountHeatmap(host, cfg) {
  host.classList.add('chart');
  const plot = document.createElement('div');
  plot.className = 'plot';
  const legend = document.createElement('div');
  legend.className = 'hm-legend';
  host.replaceChildren(plot, legend);
  let geo = null;
  let lastW = 0;
  let lastPointer = 'mouse';
  let hoverKey = '';

  function draw() {
    const W = Math.max(260, Math.round(plot.clientWidth || 600));
    lastW = plot.clientWidth;
    const n = cfg.days.length;
    if (!n) {
      geo = null;
      setHTML(plot, html`<div class="empty">No load data for this period.</div>`);
      legend.replaceChildren();
      return;
    }
    const narrow = W < 520;
    const lw = narrow ? 50 : 88;
    const gut = 9;
    const top = 18;
    const gx = lw + gut;
    const cw = (W - gx - 2) / 24;
    const rh = n <= 45 ? 16 : n <= 75 ? 12 : n <= 140 ? 7 : 4;
    const H = top + n * rh + 2;
    const max = cfg.max || 1;
    const ramp = heatRamp();
    const color = (v) => ramp(Math.sqrt(Math.max(0, v) / max));
    const gapX = cw > 8 ? 1 : 0;
    const gapY = rh > 6 ? 1 : 0;
    const out = [];
    const hstep = cw >= 24 ? 1 : cw >= 13 ? 3 : 6;
    for (let h = 0; h < 24; h += hstep) {
      out.push(`<text x="${f1(gx + (h + 0.5) * cw)}" y="11" text-anchor="middle">${pad2(h)}</text>`);
    }
    const every = Math.max(1, Math.ceil(13 / rh));
    cfg.days.forEach((d, i) => {
      const y = top + i * rh;
      if ((n - 1 - i) % every === 0) {
        const label = narrow ? DF.dm.format(parseDay(d)) : DF.short.format(parseDay(d));
        out.push(`<text class="hm-day" x="${lw - 2}" y="${f1(y + rh / 2)}" dy="0.32em" text-anchor="end">${esc(label)}</text>`);
      }
      const effs = cfg.marks.get(d);
      if (effs && effs.length) {
        out.push(`<rect class="hm-mark e-${esc(effs[0])}" x="${lw + 2}" y="${y}" width="4" height="${Math.max(1, rh - gapY)}"/>`);
      }
      const row = cfg.values[i] || [];
      for (let h = 0; h < 24; h++) {
        const v = row[h];
        const x = f1(gx + h * cw);
        const wd = f1(Math.max(0.5, cw - gapX));
        const ht = Math.max(1, rh - gapY);
        if (isNum(v)) out.push(`<rect x="${x}" y="${y}" width="${wd}" height="${ht}" fill="${color(v)}"/>`);
        else out.push(`<rect class="hm-null" x="${x}" y="${y}" width="${wd}" height="${ht}"/>`);
      }
    });
    out.push(`<rect class="hm-row" x="0" y="0" width="${f1(lw)}" height="${rh}" visibility="hidden"/>`);
    out.push(`<rect class="hm-hover" x="0" y="0" width="${f1(cw)}" height="${rh}" visibility="hidden"/>`);
    out.push(`<rect class="hit${cfg.onClick ? ' clickable' : ''}" x="0" y="${top}" width="${W}" height="${n * rh}"/>`);
    plot.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" tabindex="0" ` +
      `aria-label="${esc(cfg.ariaLabel || 'Heatmap')}">${out.join('')}</svg>`;
    geo = { W, top, gx, cw, rh, n, lw };
    drawLegend(ramp, max);
  }

  function drawLegend(ramp, max) {
    const id = `hmg${++gradientSeq}`;
    const lgW = 180;
    const stops = [];
    for (let k = 0; k <= 10; k++) stops.push(`<stop offset="${k / 10}" stop-color="${ramp(k / 10)}"/>`);
    const sc = niceScale(0, max, 4);
    const ticks = sc.ticks
      .filter((v) => v <= max)
      .map((v) => {
        const x = Math.sqrt(v / max) * lgW;
        return `<line x1="${f1(x)}" x2="${f1(x)}" y1="12" y2="16" stroke="currentColor"/><text x="${f1(x)}" y="27" text-anchor="middle">${esc(fmt.num(v, sc.digits))}</text>`;
      });
    const svg = raw(
      `<svg width="${lgW + 24}" height="30" viewBox="-12 0 ${lgW + 24} 30" aria-hidden="true">` +
        `<defs><linearGradient id="${id}" x1="0" x2="1" y1="0" y2="0">${stops.join('')}</linearGradient></defs>` +
        `<rect x="0" y="2" width="${lgW}" height="10" rx="2" fill="url(#${id})"/>${ticks.join('')}</svg>`,
    );
    const effects = new Set();
    cfg.marks.forEach((v) => v.forEach((e) => effects.add(e)));
    setHTML(
      legend,
      html`<span class="lg static">Mean load per hour (W)</span>${svg}
        ${cfg.values.some((row) => row.some((v) => !isNum(v))) ? html`<span class="lg static"><span class="sw sw-null" aria-hidden="true"></span>no data</span>` : ''}
        ${[...effects].map((e) => html`<span class="lg static"><span class="sw mk e-${e}" aria-hidden="true"></span>${effectLabel(e)}</span>`)}`,
    );
  }

  const svgEl = () => plot.querySelector('svg');
  function cellAt(e) {
    const s = svgEl();
    if (!s || !geo) return null;
    const r = s.getBoundingClientRect();
    const px = ((e.clientX - r.left) * geo.W) / r.width;
    const py = ((e.clientY - r.top) * geo.W) / r.width;
    const row = Math.floor((py - geo.top) / geo.rh);
    if (row < 0 || row >= geo.n) return null;
    const h = px >= geo.gx ? clamp(Math.floor((px - geo.gx) / geo.cw), 0, 23) : -1;
    return { row, h };
  }
  function setHover(c, e) {
    const s = svgEl();
    if (!s || !geo) return;
    const box = s.querySelector('.hm-hover');
    const rowMark = s.querySelector('.hm-row');
    if (!c) {
      hoverKey = '';
      box.setAttribute('visibility', 'hidden');
      rowMark.setAttribute('visibility', 'hidden');
      hideTip();
      return;
    }
    hoverKey = `${c.row}:${c.h}`;
    rowMark.setAttribute('y', f1(geo.top + c.row * geo.rh));
    rowMark.setAttribute('visibility', 'visible');
    if (c.h >= 0) {
      box.setAttribute('x', f1(geo.gx + c.h * geo.cw));
      box.setAttribute('y', f1(geo.top + c.row * geo.rh));
      box.setAttribute('visibility', 'visible');
    } else box.setAttribute('visibility', 'hidden');
    showTip(cfg.tooltip(c.row, c.h), e.clientX, e.clientY);
  }
  plot.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'touch') setHover(cellAt(e), e);
  });
  plot.addEventListener('pointerleave', (e) => {
    if (e.pointerType !== 'touch') setHover(null);
  });
  plot.addEventListener('pointerdown', (e) => {
    lastPointer = e.pointerType;
  });
  plot.addEventListener('click', (e) => {
    const c = cellAt(e);
    if (!c) return;
    if (lastPointer === 'touch' && hoverKey !== `${c.row}:${c.h}`) {
      setHover(c, e);
      return;
    }
    if (cfg.onClick) {
      hideTip();
      cfg.onClick(c.row);
    }
  });

  draw();
  registerChart((force) => {
    if (!plot.isConnected) return;
    if (force || plot.clientWidth !== lastW) draw();
  }, plot);
}

// --- small charts ---
function sparkline(values, cls) {
  const vals = values.map((v) => (isNum(v) ? v : null));
  const nums = vals.filter(isNum);
  if (nums.length < 2) return raw('');
  let lo = Math.min(...nums);
  let hi = Math.max(...nums);
  if (hi - lo < 1e-9) {
    lo -= 1;
    hi += 1;
  }
  const n = Math.max(1, vals.length - 1);
  const X = (i) => f1((i / n) * 100);
  const Y = (v) => f1(26 - ((v - lo) / (hi - lo)) * 23);
  let line = '';
  let area = '';
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      line += `M${run.map((i) => `${X(i)} ${Y(vals[i])}`).join('L')}`;
      area += `M${X(run[0])} 28L${run.map((i) => `${X(i)} ${Y(vals[i])}`).join('L')}L${X(run[run.length - 1])} 28Z`;
    }
    run = [];
  };
  vals.forEach((v, i) => (isNum(v) ? run.push(i) : flush()));
  flush();
  return raw(
    `<svg class="spark ${esc(cls)}" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">` +
      `<path class="spark-area" d="${area}"/><path class="spark-line" d="${line}" vector-effect="non-scaling-stroke"/></svg>`,
  );
}
function gauge(soc) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const v = isNum(soc) ? clamp(soc, 0, 100) : 0;
  return raw(
    `<svg class="gauge" viewBox="0 0 76 76" role="img" aria-label="Battery state of charge ${esc(fmt.num(soc, 0))} percent">` +
      `<circle class="g-track" cx="38" cy="38" r="${r}"/>` +
      `<circle class="g-val" cx="38" cy="38" r="${r}" stroke-dasharray="${((c * v) / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 38 38)"/>` +
      `<text class="g-text" x="38" y="36" text-anchor="middle">${esc(isNum(soc) ? `${fmt.num(soc, 0)}%` : DASH)}</text>` +
      `<text class="g-cap" x="38" y="50" text-anchor="middle">SoC</text></svg>`,
  );
}

/* =========================================================================================
 * components
 * ======================================================================================= */

const PRESET_DAYS = { '7d': 7, '30d': 30, '60d': 60, '90d': 90, '1y': 365 };
const PRESET_LABEL = { '7d': '7 d', '30d': '30 d', '60d': '60 d', '90d': '90 d', '1y': '1 y', all: 'All' };

/** Range from hash params (?r=30d or ?from=&to=), clamped to the data range. */
function resolveRange(params, def) {
  const { first_day: first, last_day: last } = state.meta;
  let from = params.get('from');
  let to = params.get('to');
  if (from && to && ISO.test(from) && ISO.test(to)) {
    if (to < from) [from, to] = [to, from];
    return { from, to, preset: 'custom' };
  }
  let preset = params.get('r');
  if (!(preset in PRESET_LABEL)) preset = def;
  if (!last) return { from: null, to: null, preset };
  if (preset === 'all') return { from: first, to: last, preset };
  let start = addDays(last, -(PRESET_DAYS[preset] - 1));
  if (first && start < first) start = first;
  return { from: start, to: last, preset };
}

function mountRangePicker(host, view, range, presets) {
  const { first_day: first, last_day: last } = state.meta;
  const days = range.from && range.to ? daysBetween(range.from, range.to) + 1 : 0;
  setHTML(
    host,
    html`<div class="seg" role="group" aria-label="Period">${presets.map(
      (p) => html`<button type="button" data-p="${p}" aria-pressed="${range.preset === p ? 'true' : 'false'}">${PRESET_LABEL[p]}</button>`,
    )}</div>
    <form class="custom" aria-label="Custom period">
      <label>From <input type="date" name="from" value="${range.from || ''}" min="${first || ''}" max="${last || ''}" required></label>
      <label>to <input type="date" name="to" value="${range.to || ''}" min="${first || ''}" max="${last || ''}" required></label>
      <button type="submit" class="btn sm">Apply</button>
    </form>
    <span class="range-note">${days ? `${days} ${days === 1 ? 'day' : 'days'}` : ''}</span>`,
  );
  host.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-p]');
    if (b) go(view, [], { r: b.dataset.p });
  });
  $('form', host).addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget.elements;
    if (ISO.test(f.from.value) && ISO.test(f.to.value)) go(view, [], { from: f.from.value, to: f.to.value });
  });
}

function tile({ label, value, sub, spark, tone, href, dotCls }) {
  const inner = html`<div class="kpi-label">${dotCls ? html`<span class="dot ${dotCls}" aria-hidden="true"></span>` : ''}${label}</div>
    <div class="kpi-value">${value}</div>${sub ? html`<div class="kpi-sub">${sub}</div>` : ''}${spark || ''}`;
  const cls = `kpi${tone ? ` tone-${tone}` : ''}`;
  return href ? html`<a class="${cls}" href="${href}">${inner}</a>` : html`<div class="${cls}">${inner}</div>`;
}

function trendText(t) {
  if (!isNum(t)) return html`no trend yet`;
  const cls = t > 2 ? 'trend-up' : t < -2 ? 'trend-down' : '';
  const arrow = t > 2 ? '↗' : t < -2 ? '↘' : '→';
  return html`<span class="${cls}">${arrow} ${fmt.signed(t, 1, 'W')}</span> / 30 d`;
}

function anomalyBadge(a) {
  return a.expected_by ? html`<span class="badge ok">expected: ${a.expected_by}</span>` : html`<span class="badge warn">unexpected</span>`;
}
function anomalyList(list, { limit = 8, showDay = true } = {}) {
  if (!list.length) return html`<p class="muted">No anomalies${showDay ? ' in this period' : ' on this day'}.</p>`;
  return html`<ul class="alist">${list.slice(0, limit).map(
    (a) => html`<li>${showDay ? html`<span class="when">${linkDay(a.day)}</span>` : ''}
      <span class="badge">${KIND_LABEL[a.kind] || a.kind}</span>
      <span class="msg">${a.message}</span>${anomalyBadge(a)}</li>`,
  )}</ul>`;
}
const sortAnomalies = (list) => [...list].sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : Math.abs(b.score) - Math.abs(a.score)));

function eventMini(e) {
  return html`<li><span class="when">${e.effect === 'regime_change' ? `from ${fmt.dm(e.start_day)}` : rangeShort(e)}</span>
    <span class="msg">${e.title}</span><span class="badge e-${e.effect}">${effectLabel(e.effect)}</span></li>`;
}
function rangeShort(e) {
  const a = e.start_day;
  const b = eventEnd(e);
  if (a === b) return fmt.dm(a);
  try {
    return DF.dm.formatRange(parseDay(a), parseDay(b));
  } catch (err) {
    return `${fmt.dm(a)} ${DASH} ${fmt.dm(b)}`;
  }
}

/** Event shading for a band chart over the given days. */
function eventOverlays(events, days) {
  const bands = [];
  const vmarks = [];
  const effects = new Set();
  if (!days.length) return { bands, vmarks, legend: [] };
  for (const e of events || []) {
    if (e.effect === 'regime_change') {
      const i = days.findIndex((d) => d >= e.start_day);
      if (i >= 0 && e.start_day <= days[days.length - 1]) {
        vmarks.push({ x: i, label: e.title });
        effects.add('regime_change');
      }
      continue;
    }
    let a = -1;
    let b = -1;
    days.forEach((d, i) => {
      if (eventCovers(e, d)) {
        if (a < 0) a = i;
        b = i;
      }
    });
    if (a >= 0) {
      bands.push({ from: a, to: b, cls: `e-${e.effect}`, label: e.title });
      effects.add(e.effect);
    }
  }
  const legend = [...effects].map((eff) =>
    eff === 'regime_change' ? { name: 'New normal from here', type: 'vmark', cls: '' } : { name: effectLabel(eff), type: 'band', cls: `e-${eff}` },
  );
  return { bands, vmarks, legend };
}

function eventNotes(events, day) {
  const on = (events || []).filter((e) => eventCovers(e, day));
  return on.map((e) => html`<div class="tt-note">${e.title} (${effectLabel(e.effect)})</div>`);
}

function renderError(main, err) {
  setHTML(
    main,
    html`<section class="card"><h2>Something went wrong</h2><p class="muted">${err && err.message ? err.message : String(err)}</p>
      <div class="actions"><button type="button" class="btn" id="retry">Try again</button></div></section>`,
  );
  const b = $('#retry', main);
  if (b) b.addEventListener('click', () => route());
}
function renderNoData(main) {
  setHTML(
    main,
    html`<section class="card"><h2>No data yet</h2>
      <p class="muted">The database is empty. Fetch data first (for example with the backfill and daily jobs), then reload this page.</p></section>`,
  );
}
function loading(main) {
  setHTML(main, html`<p class="loading">Loading…</p>`);
}

/* =========================================================================================
 * views
 * ======================================================================================= */

// ---------------------------------------------------------------- overview
async function viewOverview(main, r, token) {
  document.title = 'Overview · Energy Analyzer';
  const meta = state.meta;
  if (!meta.last_day) return renderNoData(main);
  const range = resolveRange(r.params, '30d');
  setHTML(
    main,
    html`<div class="view-head"><div><h1>Overview</h1><p class="sub">${fmt.range(range.from, range.to)}</p></div>
        <div class="rangebar" id="rp"></div></div>
      <section class="card live" id="live" aria-label="Live power" hidden></section>
      <section class="kpis" id="kpis" aria-label="Key figures"><p class="loading">Loading…</p></section>
      <section class="card" aria-labelledby="h-daily">
        <div class="card-head"><h2 id="h-daily">Daily energy</h2><span class="sub">Click a day for details. Shaded: events.</span></div>
        <div id="daily"></div></section>
      <div class="cols">
        <section class="card" aria-labelledby="h-anom"><div class="card-head"><h2 id="h-anom">Latest anomalies</h2><a class="sub" href="#/analysis${qs(range.preset === 'custom' ? { from: range.from, to: range.to } : { r: range.preset })}">All anomalies</a></div><div id="anoms"></div></section>
        <section class="card" aria-labelledby="h-ev"><div class="card-head"><h2 id="h-ev">Events</h2><a class="sub" href="#/events">Manage events</a></div><div id="evs"></div></section>
      </div>`,
  );
  mountRangePicker($('#rp', main), 'overview', range, ['7d', '30d', '90d', '1y', 'all']);
  if (meta.live_available) startLive($('#live', main), token);

  const data = await API.summary(range.from, range.to);
  if (!alive(token)) return;
  if (!data.kpis) return renderNoData(main);
  renderKpis($('#kpis', main), data);
  renderDailyChart($('#daily', main), data);
  setHTML($('#anoms', main), anomalyList(sortAnomalies(data.anomalies), { limit: 8 }));
  renderOverviewEvents($('#evs', main), data);
}

function renderKpis(host, data) {
  const k = data.kpis;
  const d = data.days;
  const col = (key) => d.map((x) => x[key]);
  const n = k.days || d.length || 1;
  const explained = k.anomalies - k.anomalies_unexpected;
  setHTML(
    host,
    html`${tile({ label: 'PV generation', dotCls: 's-pv', value: vu(fmt.kwh(k.pv_kwh)), sub: `avg ${fmt.kwh(k.pv_kwh / n, 1)} / day`, spark: sparkline(col('pv_kwh'), 's-pv') })}
    ${tile({ label: 'Consumption', dotCls: 's-load', value: vu(fmt.kwh(k.load_kwh)), sub: `avg ${fmt.kwh(k.load_kwh / n, 1)} / day`, spark: sparkline(col('load_kwh'), 's-load') })}
    ${tile({ label: 'Grid import', dotCls: 's-imp', value: vu(fmt.kwh(k.grid_import_kwh)), sub: k.load_kwh > 0 ? `${fmt.pct(k.grid_import_kwh / k.load_kwh, 1)} of consumption` : '', spark: sparkline(col('grid_import_kwh'), 's-imp') })}
    ${tile({ label: 'Grid export', dotCls: 's-exp', value: vu(fmt.kwh(k.grid_export_kwh)), sub: k.pv_kwh > 0 ? `${fmt.pct(k.grid_export_kwh / k.pv_kwh)} of PV` : '', spark: sparkline(col('grid_export_kwh'), 's-exp') })}
    ${tile({ label: 'Self-consumption', value: vu(fmt.pct(k.self_consumption)), sub: 'PV used on site', spark: sparkline(col('self_consumption'), 's-pv') })}
    ${tile({ label: 'Autarky', value: vu(fmt.pct(k.self_sufficiency)), sub: 'consumption not from grid', spark: sparkline(col('self_sufficiency'), 's-exp') })}
    ${tile({ label: 'Standby baseline', dotCls: 's-base', value: vu(fmt.w(k.baseline_w)), sub: trendText(k.baseline_trend_w_per_30d), spark: sparkline(col('baseline_w'), 's-base') })}
    ${tile({
      label: 'Unexpected anomalies',
      value: String(k.anomalies_unexpected),
      tone: k.anomalies_unexpected > 0 ? 'warn' : 'good',
      sub: `${k.anomalies} flagged${explained ? `, ${explained} explained by events` : ''}`,
      href: '#/analysis',
    })}`,
  );
}

function renderDailyChart(host, data) {
  const days = data.days.map((d) => d.day);
  const anomByDay = new Map();
  for (const a of data.anomalies) {
    if (!anomByDay.has(a.day)) anomByDay.set(a.day, []);
    anomByDay.get(a.day).push(a);
  }
  const ov = eventOverlays(data.events, days);
  const marks = [];
  days.forEach((d, i) => {
    const list = anomByDay.get(d);
    if (list) marks.push({ i, cls: list.some((a) => !a.expected_by) ? '' : 'expected' });
  });
  const legendExtra = [...ov.legend];
  if (marks.length) legendExtra.push({ name: 'Anomaly', type: 'mark' });
  mountChart(host, {
    ariaLabel: `Daily PV generation and consumption with grid import and export, ${fmt.range(data.from, data.to)}`,
    height: 280,
    n: days.length,
    x: { type: 'band', ticks: dayTicks(days) },
    y: { unit: 'kWh' },
    series: [
      { key: 'pv', name: 'PV', cls: 's-pv', type: 'bar', values: data.days.map((d) => d.pv_kwh) },
      { key: 'load', name: 'Consumption', cls: 's-load', type: 'bar', values: data.days.map((d) => d.load_kwh) },
      { key: 'imp', name: 'Grid import', cls: 's-imp', type: 'line', values: data.days.map((d) => d.grid_import_kwh) },
      { key: 'exp', name: 'Grid export', cls: 's-exp', type: 'line', values: data.days.map((d) => d.grid_export_kwh) },
    ],
    bands: ov.bands,
    vmarks: ov.vmarks,
    marks,
    legendExtra,
    tooltip: (i) => {
      const d = data.days[i];
      const anoms = anomByDay.get(d.day) || [];
      return ttBody(
        fmt.day(d.day),
        [
          { name: 'PV', cls: 's-pv', value: fmt.kwh(d.pv_kwh, 1) },
          { name: 'Consumption', cls: 's-load', value: fmt.kwh(d.load_kwh, 1) },
          { name: 'Grid import', cls: 's-imp', type: 'line', value: fmt.kwh(d.grid_import_kwh, 1) },
          { name: 'Grid export', cls: 's-exp', type: 'line', value: fmt.kwh(d.grid_export_kwh, 1) },
          { name: 'Autarky', value: fmt.pct(d.self_sufficiency) },
          { name: 'Standby', value: fmt.w(d.baseline_w) },
        ],
        html`${eventNotes(data.events, d.day)}${anoms.map((a) => html`<div class="tt-note${a.expected_by ? '' : ' bad'}">${a.message}</div>`)}<div class="tt-hint">Click to open the day</div>`,
      );
    },
    onClick: (i) => go('day', [days[i]]),
  });
}

function renderOverviewEvents(host, data) {
  const today = todayLocal();
  const upcoming = state.events.filter((e) => eventEnd(e) >= today).sort((a, b) => (a.start_day < b.start_day ? -1 : 1));
  const inRange = data.events || [];
  setHTML(
    host,
    html`<h3 class="subhead">Coming up</h3>
      ${upcoming.length ? html`<ul class="alist">${upcoming.slice(0, 4).map(eventMini)}</ul>` : html`<p class="muted">Nothing planned. <a href="#/events">Tell the analyzer what's coming</a>.</p>`}
      <h3 class="subhead">In this period</h3>
      ${inRange.length ? html`<ul class="alist">${inRange.slice(0, 6).map(eventMini)}</ul>` : html`<p class="muted">No events in this period.</p>`}`,
  );
}

function startLive(host, token) {
  let failed = 0;
  async function load() {
    let d;
    try {
      d = await API.live();
    } catch (err) {
      d = { available: false, error: err.message };
    }
    if (!alive(token) || !host.isConnected) return;
    host.hidden = false;
    if (!d.available) {
      failed++;
      host.classList.add('unavailable');
      setHTML(host, html`<span class="pulse off" aria-hidden="true"></span><span>Live values unavailable${d.error ? `: ${d.error}` : ''}.</span>`);
      return;
    }
    failed = 0;
    host.classList.remove('unavailable');
    const grid = d.grid_w;
    const bat = d.battery_w;
    const gridLabel = !isNum(grid) ? 'Grid' : grid > 0 ? 'Grid import' : grid < 0 ? 'Grid export' : 'Grid';
    const gridCls = isNum(grid) && grid < 0 ? 's-exp' : 's-imp';
    const batLabel = !isNum(bat) ? 'Battery' : bat < 0 ? 'Battery charging' : bat > 0 ? 'Battery discharging' : 'Battery idle';
    const batCls = isNum(bat) && bat < 0 ? 's-chg' : 's-dis';
    const share = isNum(d.load_w) && d.load_w > 0 && isNum(grid) ? clamp(1 - Math.max(0, grid) / d.load_w, 0, 1) : null;
    setHTML(
      host,
      html`<div class="live-head"><span class="pulse" aria-hidden="true"></span><h2>Live</h2>
          <span class="sub">updated ${fmt.time(d.fetched_at)} · refreshes every minute</span></div>
        ${gauge(d.soc)}
        <div class="live-grid">
          <div><div class="lv-label"><span class="sw s-pv" aria-hidden="true"></span>PV</div><div class="lv-value">${vu(fmt.power(d.pv_w))}</div></div>
          <div><div class="lv-label"><span class="sw s-load" aria-hidden="true"></span>Load</div><div class="lv-value">${vu(fmt.power(d.load_w))}</div>
            <div class="lv-sub">${share != null ? `${fmt.pct(share)} self-supplied` : ''}</div></div>
          <div><div class="lv-label"><span class="sw ${gridCls}" aria-hidden="true"></span>${gridLabel}</div><div class="lv-value">${vu(fmt.power(isNum(grid) ? Math.abs(grid) : null))}</div></div>
          <div><div class="lv-label"><span class="sw ${batCls}" aria-hidden="true"></span>${batLabel}</div><div class="lv-value">${vu(fmt.power(isNum(bat) ? Math.abs(bat) : null))}</div></div>
        </div>`,
    );
  }
  load();
  const id = setInterval(load, 60000);
  state.cleanup.push(() => clearInterval(id));
}

// ---------------------------------------------------------------- day detail
async function viewDay(main, r, token) {
  const meta = state.meta;
  if (!meta.last_day) return renderNoData(main);
  const want = ISO.test(r.args[0] || '') ? r.args[0] : null;
  let data;
  try {
    data = await API.day(want);
  } catch (err) {
    if (!alive(token)) return;
    if (err.status === 404) return renderNoData(main);
    throw err;
  }
  if (!alive(token)) return;
  document.title = `${data.date} · Day · Energy Analyzer`;
  const nw = data.night_window || {};
  const t = data.totals;
  const s = data.samples || [];
  const peak = (key) => s.reduce((best, x) => (isNum(x[key]) && (!best || x[key] > best[key]) ? x : best), null);
  const pvPeak = peak('pv');
  const loadPeak = peak('load');
  const prevBtn = data.prev ? html`<a class="btn" href="#/day/${data.prev}" aria-label="Previous day">‹ Prev</a>` : html`<button type="button" class="btn" disabled aria-label="Previous day">‹ Prev</button>`;
  const nextBtn = data.next ? html`<a class="btn" href="#/day/${data.next}" aria-label="Next day">Next ›</a>` : html`<button type="button" class="btn" disabled aria-label="Next day">Next ›</button>`;

  setHTML(
    main,
    html`<div class="view-head"><div><h1>${fmt.day(data.date)}</h1>
        <p class="sub">5-minute values · night window ${nw.start || DASH}${DASH}${nw.end || DASH}${data.date === todayLocal() ? ' · today, still incomplete' : ''}</p></div>
        <div class="toolbar daynav">${prevBtn}
          <label class="sr-label"><span class="muted">Date</span> <input type="date" id="daypick" value="${data.date}" min="${meta.first_day || ''}" max="${meta.last_day || ''}" aria-label="Pick a day"></label>
          ${nextBtn}${data.date !== meta.last_day ? html`<a class="btn" href="#/day/${meta.last_day}">Latest</a>` : ''}</div></div>
      <section class="kpis sm" aria-label="Daily totals">${
        t
          ? html`${tile({ label: 'PV', dotCls: 's-pv', value: vu(fmt.kwh(t.pv_kwh, 1)), sub: pvPeak && pvPeak.pv > 0 ? `peak ${fmt.power(pvPeak.pv)} at ${pvPeak.t}` : '' })}
            ${tile({ label: 'Consumption', dotCls: 's-load', value: vu(fmt.kwh(t.load_kwh, 1)), sub: loadPeak ? `peak ${fmt.power(loadPeak.load)} at ${loadPeak.t}` : '' })}
            ${tile({ label: 'Grid import', dotCls: 's-imp', value: vu(fmt.kwh(t.grid_import_kwh, 1)) })}
            ${tile({ label: 'Grid export', dotCls: 's-exp', value: vu(fmt.kwh(t.grid_export_kwh, 1)) })}
            ${tile({ label: 'Battery charged', dotCls: 's-chg', value: vu(fmt.kwh(t.battery_charge_kwh, 1)) })}
            ${tile({ label: 'Battery discharged', dotCls: 's-dis', value: vu(fmt.kwh(t.battery_discharge_kwh, 1)) })}
            ${tile({ label: 'Self-consumption', value: vu(fmt.pct(t.self_consumption)) })}
            ${tile({ label: 'Autarky', value: vu(fmt.pct(t.self_sufficiency)) })}
            ${tile({ label: 'Standby baseline', dotCls: 's-base', value: vu(fmt.w(data.baseline_w)), sub: 'median night load' })}`
          : html`<div class="card muted">No daily totals for this day.</div>`
      }</section>
      <section class="card" aria-labelledby="h-power"><div class="card-head"><h2 id="h-power">Power over the day</h2>
        <span class="sub">Above zero: into the house · below zero: export and battery charging · click a series to hide it</span></div>
        <div id="daychart"></div></section>
      <div class="cols">
        <section class="card" aria-labelledby="h-da"><h2 id="h-da">Anomalies</h2>${anomalyList(sortAnomalies(data.anomalies || []), { showDay: false, limit: 20 })}</section>
        <section class="card" aria-labelledby="h-de"><div class="card-head"><h2 id="h-de">Events on this day</h2><a class="sub" href="#/events">Manage events</a></div>
          ${(data.events || []).length ? html`<ul class="alist">${data.events.map(eventMini)}</ul>` : html`<p class="muted">No events cover this day.</p>`}</section>
      </div>`,
  );
  $('#daypick', main).addEventListener('change', (e) => {
    if (ISO.test(e.target.value)) go('day', [e.target.value]);
  });

  const xs = s.map((x) => toMin(x.t));
  const val = (key, sign = 1) => s.map((x) => (isNum(x[key]) ? sign * x[key] : null));
  const bands = [];
  if (nw.start && nw.end) {
    const a = toMin(nw.start);
    const b = toMin(nw.end);
    if (a < b) bands.push({ from: a, to: b, cls: 'night', label: 'Night' });
    else {
      bands.push({ from: a, to: 1440, cls: 'night', label: 'Night' });
      bands.push({ from: 0, to: b, cls: 'night', label: 'Night' });
    }
  }
  const legendExtra = [{ name: 'Night window', type: 'band', cls: 'night' }];
  if (isNum(data.baseline_w)) legendExtra.push({ name: 'Standby baseline', type: 'line dash', cls: 's-base' });
  mountChart($('#daychart', main), {
    ariaLabel: `Power on ${fmt.day(data.date)}: PV, load, grid and battery in watts, battery state of charge in percent`,
    height: 340,
    n: s.length,
    xs,
    maxGap: 20,
    x: { type: 'linear', domain: [0, 1440], ticks: hourTicks },
    y: { unit: 'W' },
    y2: { unit: 'SoC %', min: 0, max: 100, ticks: [0, 25, 50, 75, 100], fmt: (v) => String(v) },
    emptyText: 'No 5-minute samples for this day.',
    series: [
      { key: 'pv', name: 'PV', cls: 's-pv', type: 'area', values: val('pv') },
      { key: 'load', name: 'Load', cls: 's-load', type: 'line', values: val('load') },
      { key: 'imp', name: 'Grid import', cls: 's-imp', type: 'area', values: val('grid_import') },
      { key: 'exp', name: 'Grid export', cls: 's-exp', type: 'area', values: val('grid_export', -1) },
      { key: 'dis', name: 'Battery discharge', cls: 's-dis', type: 'line', thin: true, values: val('battery_discharge') },
      { key: 'chg', name: 'Battery charge', cls: 's-chg', type: 'line', thin: true, values: val('battery_charge', -1) },
      { key: 'soc', name: 'SoC', cls: 's-soc', type: 'line', dashed: true, axis: 'y2', values: val('soc') },
    ],
    bands,
    refs: isNum(data.baseline_w) ? [{ y: data.baseline_w, cls: 's-base', label: `standby ${fmt.w(data.baseline_w)}` }] : [],
    legendExtra,
    tooltip: (i) => {
      const x = s[i];
      return ttBody(`${x.t} – ${minLabel(toMin(x.t) + 5)}`, [
        { name: 'PV', cls: 's-pv', type: 'area', value: fmt.power(x.pv) },
        { name: 'Load', cls: 's-load', type: 'line', value: fmt.power(x.load) },
        { name: 'Grid import', cls: 's-imp', type: 'area', value: fmt.power(x.grid_import) },
        { name: 'Grid export', cls: 's-exp', type: 'area', value: fmt.power(x.grid_export) },
        { name: 'Battery discharge', cls: 's-dis', type: 'line', value: fmt.power(x.battery_discharge) },
        { name: 'Battery charge', cls: 's-chg', type: 'line', value: fmt.power(x.battery_charge) },
        { name: 'SoC', cls: 's-soc', type: 'line', value: isNum(x.soc) ? `${fmt.num(x.soc, 1)}${NBSP}%` : DASH },
      ]);
    },
  });
}

// ---------------------------------------------------------------- heatmap
async function viewHeatmap(main, r, token) {
  document.title = 'Load heatmap · Energy Analyzer';
  if (!state.meta.last_day) return renderNoData(main);
  const range = resolveRange(r.params, '60d');
  setHTML(
    main,
    html`<div class="view-head"><div><h1>Load heatmap</h1><p class="sub">${fmt.range(range.from, range.to)} · mean household load per hour</p></div>
        <div class="rangebar" id="rp"></div></div>
      <section class="card" aria-labelledby="h-hm"><div class="card-head"><h2 id="h-hm">Days × hours</h2>
        <span class="sub">Click a row to open the day. Bars left of a row mark events.</span></div><div id="hm"><p class="loading">Loading…</p></div></section>`,
  );
  mountRangePicker($('#rp', main), 'heatmap', range, ['7d', '30d', '60d', '90d', '1y', 'all']);
  const data = await API.heatmap(range.from, range.to);
  if (!alive(token)) return;
  const marks = new Map();
  for (const d of data.days) {
    const effs = state.events.filter((e) => eventCovers(e, d)).map((e) => e.effect);
    if (effs.length) marks.set(d, effs);
  }
  mountHeatmap($('#hm', main), {
    days: data.days,
    values: data.values,
    max: data.max,
    marks,
    ariaLabel: `Heatmap of mean household load per hour, ${data.days.length} days from ${fmt.range(data.from, data.to)}`,
    tooltip: (row, h) => {
      const d = data.days[row];
      const evs = state.events.filter((e) => eventCovers(e, d));
      const rowVals = (data.values[row] || []).filter(isNum);
      const rows =
        h >= 0
          ? [
              { name: `${pad2(h)}:00 – ${pad2(h + 1)}:00`, value: fmt.w((data.values[row] || [])[h]) },
              { name: 'Day mean', value: fmt.w(rowVals.length ? rowVals.reduce((a, b) => a + b, 0) / rowVals.length : null) },
            ]
          : [{ name: 'Day mean', value: fmt.w(rowVals.length ? rowVals.reduce((a, b) => a + b, 0) / rowVals.length : null) }];
      return ttBody(fmt.day(d), rows, html`${evs.map((e) => html`<div class="tt-note">${e.title} (${effectLabel(e.effect)})</div>`)}<div class="tt-hint">Click to open the day</div>`);
    },
    onClick: (row) => go('day', [data.days[row]]),
  });
}

// ---------------------------------------------------------------- analysis
async function viewAnalysis(main, r, token) {
  document.title = 'Analysis · Energy Analyzer';
  if (!state.meta.last_day) return renderNoData(main);
  const range = resolveRange(r.params, 'all');
  setHTML(
    main,
    html`<div class="view-head"><div><h1>Analysis</h1><p class="sub">${fmt.range(range.from, range.to)}</p></div><div class="rangebar" id="rp"></div></div>
      <section class="card" aria-labelledby="h-base"><div class="card-head"><h2 id="h-base">Standby baseline</h2>
        <span class="sub">Median load in the night window while PV is 0</span></div><div id="basestats"></div><div id="basechart"><p class="loading">Loading…</p></div></section>
      <section class="card" aria-labelledby="h-at"><div class="card-head"><h2 id="h-at">Anomalies</h2><div class="table-tools" id="atools"></div></div><div id="atable"></div></section>
      <section class="card" aria-labelledby="h-mo"><div class="card-head"><h2 id="h-mo">Month by month</h2><span class="sub">Bars: average per day, so partial months compare fairly. Click a month to open it.</span></div>
        <div id="monthchart"></div><div class="table-wrap" id="mtable"></div></section>
      <section class="card" aria-labelledby="h-set"><div class="card-head"><h2 id="h-set">Analysis settings</h2><span class="sub">From config.toml / .env</span></div><div id="settings"></div></section>`,
  );
  mountRangePicker($('#rp', main), 'analysis', range, ['30d', '90d', '1y', 'all']);
  renderSettings($('#settings', main));
  const data = await API.summary(range.from, range.to);
  if (!alive(token)) return;
  if (!data.kpis) return renderNoData(main);
  renderBaseline(main, data);
  renderAnomalyTable(main, data);
  renderMonths(main, data);
}

function renderBaseline(main, data) {
  const days = data.days.map((d) => d.day);
  const base = data.days.map((d) => d.baseline_w);
  const win = Number(state.meta.settings.rolling_days) || 28;
  const rolling = days.map((d, i) => {
    const lo = addDays(d, -(win - 1));
    const vals = [];
    for (let j = i; j >= 0 && days[j] >= lo; j--) vals.push(base[j]);
    return vals.filter(isNum).length >= 3 ? median(vals) : null;
  });
  const slope = data.kpis.baseline_trend_w_per_30d;
  let trend = days.map(() => null);
  if (isNum(slope) && days.length > 1) {
    const perDay = slope / 30;
    const xs = days.map((d) => daysBetween(days[0], d));
    const icpt = median(base.map((v, i) => (isNum(v) ? v - perDay * xs[i] : null)));
    if (isNum(icpt)) trend = xs.map((x) => icpt + perDay * x);
  }
  const baseAnoms = new Map();
  for (const a of data.anomalies) if (a.kind === 'baseline' || a.kind === 'night_spike') {
    if (!baseAnoms.has(a.day)) baseAnoms.set(a.day, []);
    baseAnoms.get(a.day).push(a);
  }
  const flagged = data.days.map((d) => ((baseAnoms.get(d.day) || []).some((a) => a.kind === 'baseline') ? d.baseline_w : null));
  const valid = base.filter(isNum);
  setHTML(
    $('#basestats', main),
    html`<div class="statline"><span>Now <b>${fmt.w(data.kpis.baseline_w)}</b> (median of the last 7 days)</span>
      <span>Trend <b>${trendText(slope)}</b></span>
      <span>Range <b>${fmt.w(valid.length ? Math.min(...valid) : null)} ${DASH} ${fmt.w(valid.length ? Math.max(...valid) : null)}</b></span></div>`,
  );
  const ov = eventOverlays(data.events, days);
  mountChart($('#basechart', main), {
    ariaLabel: `Daily standby baseline in watts with rolling median and trend, ${fmt.range(data.from, data.to)}`,
    height: 260,
    n: days.length,
    x: { type: 'band', ticks: dayTicks(days) },
    y: { unit: 'W', zero: false },
    series: [
      { key: 'nmax', name: 'Night peak', cls: 's-faint', type: 'points', values: data.days.map((d) => d.night_max_w), hidden: true },
      { key: 'base', name: 'Daily baseline', cls: 's-base', type: 'points', values: base },
      { key: 'roll', name: `Rolling median (${win} d)`, cls: 's-load', type: 'line', values: rolling },
      { key: 'trend', name: `Trend ${isNum(slope) ? `(${fmt.signed(slope, 1, 'W')} / 30 d)` : ''}`, cls: 's-trend', type: 'line', dashed: true, values: trend },
      { key: 'flag', name: 'Baseline anomaly', cls: 's-bad', type: 'points', r: 4, top: true, values: flagged },
    ],
    bands: ov.bands,
    vmarks: ov.vmarks,
    legendExtra: ov.legend,
    tooltip: (i) => {
      const d = data.days[i];
      return ttBody(
        fmt.day(d.day),
        [
          { name: 'Baseline', cls: 's-base', type: 'points', value: fmt.w(d.baseline_w) },
          { name: 'Rolling median', cls: 's-load', type: 'line', value: fmt.w(rolling[i]) },
          isNum(trend[i]) ? { name: 'Trend', cls: 's-trend', type: 'line', value: fmt.w(trend[i]) } : null,
          { name: 'Night peak', cls: 's-faint', type: 'points', value: fmt.w(d.night_max_w) },
        ],
        html`${eventNotes(data.events, d.day)}${(baseAnoms.get(d.day) || []).map((a) => html`<div class="tt-note${a.expected_by ? '' : ' bad'}">${a.message}</div>`)}<div class="tt-hint">Click to open the day</div>`,
      );
    },
    onClick: (i) => go('day', [days[i]]),
  });
}

function renderAnomalyTable(main, data) {
  const all = sortAnomalies(data.anomalies);
  const tools = $('#atools', main);
  const unexpected = all.filter((a) => !a.expected_by).length;
  setHTML(
    tools,
    html`<span>${all.length} flagged, ${unexpected} unexpected</span>
      <label><input type="checkbox" id="only-unexp"> only unexpected</label>`,
  );
  const draw = () => {
    const only = $('#only-unexp', tools).checked;
    const list = only ? all.filter((a) => !a.expected_by) : all;
    const host = $('#atable', main);
    if (!list.length) {
      setHTML(host, html`<p class="muted">No anomalies${only ? ' that events do not explain' : ''} in this period.</p>`);
      return;
    }
    setHTML(
      host,
      html`<div class="table-wrap"><table>
        <thead><tr><th scope="col">Date</th><th scope="col">Kind</th><th scope="col" class="num">Value</th><th scope="col" class="num">Typical</th>
          <th scope="col" class="num">Score</th><th scope="col">Message</th><th scope="col">Status</th></tr></thead>
        <tbody>${list.map(
          (a) => html`<tr><td>${linkDay(a.day)}</td><td>${KIND_LABEL[a.kind] || a.kind}</td>
            <td class="num">${anomalyValue(a.kind, a.value)}</td><td class="num">${anomalyValue(a.kind, a.reference)}</td>
            <td class="num" title="${a.kind === 'night_spike' ? 'ratio to standby' : 'robust z-score'}">${a.kind === 'night_spike' ? `${fmt.num(a.score, 1)}×` : fmt.num(a.score, 1)}</td>
            <td class="msg">${a.message}</td><td>${anomalyBadge(a)}</td></tr>`,
        )}</tbody></table></div>`,
    );
  };
  $('#only-unexp', tools).addEventListener('change', draw);
  draw();
}

function renderMonths(main, data) {
  const months = data.months || [];
  const keys = months.map((m) => m.month);
  mountChart($('#monthchart', main), {
    ariaLabel: 'Average daily consumption and PV per month in kWh per day, with autarky in percent',
    height: 240,
    n: months.length,
    x: {
      type: 'band',
      ticks: (iw) => {
        const step = Math.ceil(months.length / Math.max(1, Math.floor(iw / 56)));
        return keys.map((k, i) => ({ v: i, label: fmt.monthShort(k) })).filter((_, i) => (keys.length - 1 - i) % step === 0);
      },
    },
    y: { unit: 'kWh/d' },
    y2: { unit: '%', min: 0, max: 100, ticks: [0, 25, 50, 75, 100], fmt: (v) => `${v}` },
    series: [
      { key: 'load', name: 'Consumption per day', cls: 's-load', type: 'bar', values: months.map((m) => (m.days ? m.load_kwh / m.days : null)) },
      { key: 'pv', name: 'PV per day', cls: 's-pv', type: 'bar', values: months.map((m) => (m.days ? m.pv_kwh / m.days : null)) },
      { key: 'aut', name: 'Autarky', cls: 's-exp', type: 'line', axis: 'y2', values: months.map((m) => (isNum(m.self_sufficiency) ? m.self_sufficiency * 100 : null)) },
      { key: 'autp', name: 'Autarky', cls: 's-exp', type: 'points', axis: 'y2', noLegend: true, r: 3, top: true, values: months.map((m) => (isNum(m.self_sufficiency) ? m.self_sufficiency * 100 : null)) },
    ],
    tooltip: (i) => {
      const m = months[i];
      const prev = months[i - 1];
      const delta = prev && prev.load_kwh > 0 ? (m.load_kwh / m.days / (prev.load_kwh / prev.days) - 1) : null;
      return ttBody(
        DF.monLong.format(parseDay(`${m.month}-01`)),
        [
          { name: 'Consumption', cls: 's-load', value: `${fmt.kwh(m.load_kwh)} (${fmt.kwh(m.days ? m.load_kwh / m.days : null, 1)}/d)` },
          { name: 'PV', cls: 's-pv', value: `${fmt.kwh(m.pv_kwh)} (${fmt.kwh(m.days ? m.pv_kwh / m.days : null, 1)}/d)` },
          { name: 'Autarky', cls: 's-exp', type: 'line', value: fmt.pct(m.self_sufficiency) },
          { name: 'Days with data', value: String(m.days) },
          delta != null ? { name: 'Use per day vs previous', value: fmt.signed(delta * 100, 0, '%') } : null,
        ],
        html`<div class="tt-hint">Click to open the month</div>`,
      );
    },
    onClick: (i) => {
      const m = months[i].month;
      const start = `${m}-01`;
      const end = addDays(isoDay(new Date(Date.UTC(Number(m.slice(0, 4)), Number(m.slice(5, 7)), 1))), -1);
      go('overview', [], { from: start, to: end });
    },
  });
  if (!months.length) {
    setHTML($('#mtable', main), '');
    return;
  }
  setHTML(
    $('#mtable', main),
    html`<table><thead><tr><th scope="col">Month</th><th scope="col" class="num">Days</th><th scope="col" class="num">PV</th><th scope="col" class="num">Consumption</th>
      <th scope="col" class="num">Per day</th><th scope="col" class="num">Import</th><th scope="col" class="num">Export</th>
      <th scope="col" class="num">Self-cons.</th><th scope="col" class="num">Autarky</th><th scope="col" class="num">Standby</th></tr></thead>
      <tbody>${months.map(
        (m) => html`<tr><td>${fmt.month(m.month)}</td><td class="num">${m.days}</td><td class="num">${fmt.kwh(m.pv_kwh, 0)}</td>
          <td class="num">${fmt.kwh(m.load_kwh, 0)}</td><td class="num">${fmt.kwh(m.days ? m.load_kwh / m.days : null, 1)}</td>
          <td class="num">${fmt.kwh(m.grid_import_kwh, 1)}</td><td class="num">${fmt.kwh(m.grid_export_kwh, 0)}</td>
          <td class="num">${fmt.pct(m.self_consumption)}</td><td class="num">${fmt.pct(m.self_sufficiency)}</td><td class="num">${fmt.w(m.baseline_median_w)}</td></tr>`,
      )}</tbody></table>`,
  );
}

function renderSettings(host) {
  const s = state.meta.settings || {};
  const known = [
    ['night_window', 'Night window', (v) => v.replace('-', ` ${DASH} `), 'Standby baseline = median load here while PV is 0'],
    ['rolling_days', 'Reference window', (v) => `${v} days`, 'Each day is compared with the median of the days before'],
    ['z_threshold', 'z-score threshold', (v) => v, 'Robust z-score needed to flag a day'],
    ['min_rel_deviation', 'Minimum deviation', (v) => fmt.pct(Number(v)), '...and at least this far from the reference'],
    ['spike_factor', 'Night spike factor', (v) => `${v}× baseline`, 'Night load above this counts as a spike'],
    ['spike_min_w', 'Night spike minimum', (v) => fmt.w(Number(v)), 'Spikes must also exceed this power'],
    ['min_history_days', 'Minimum history', (v) => `${v} days`, 'Days needed before a day is judged'],
  ];
  const skip = new Set(['night_start', 'night_end', ...known.map((k) => k[0])]);
  const rows = known.filter(([k]) => k in s).map(([k, label, f, hint]) => ({ label, value: f(String(s[k])), hint }));
  for (const [k, v] of Object.entries(s)) if (!skip.has(k)) rows.push({ label: k, value: String(v), hint: '' });
  setHTML(
    host,
    html`<dl class="settings">${rows.map((r) => html`<div><dt>${r.label}</dt><dd>${r.value}${r.hint ? html`<small>${r.hint}</small>` : ''}</dd></div>`)}</dl>`,
  );
}

// ---------------------------------------------------------------- events
const QUICK = [
  {
    label: 'Holiday next week',
    make(t) {
      const mon = addDays(t, (8 - weekday(t)) % 7 || 7);
      return { kind: 'away', title: 'Holiday', start_day: mon, end_day: addDays(mon, 6) };
    },
  },
  {
    label: 'Guests this weekend',
    make(t) {
      const dow = weekday(t);
      if (dow === 0) return { kind: 'guests', title: 'Guests', start_day: t, end_day: t };
      const sat = addDays(t, 6 - dow);
      return { kind: 'guests', title: 'Guests', start_day: sat, end_day: addDays(sat, 1) };
    },
  },
  { label: 'Heating season starts today', make: (t) => ({ kind: 'heating_on', start_day: t }) },
  { label: 'Heating season ends today', make: (t) => ({ kind: 'heating_off', start_day: t }) },
  { label: 'Outage today', make: (t) => ({ kind: 'outage', title: 'Outage', start_day: t, end_day: t }) },
];

async function viewEvents(main, r, token) {
  document.title = 'Events · Energy Analyzer';
  const kinds = state.meta.event_kinds || [];
  const effects = state.meta.effects || Object.keys(EFFECT);
  setHTML(
    main,
    html`<div class="view-head"><div><h1>Tell the analyzer what's coming</h1>
        <p class="sub">Holidays, guests, heating seasons, outages: events explain unusual days and keep them out of the reference.</p></div></div>
      <section class="card" aria-labelledby="h-q"><h2 id="h-q">Quick add</h2>
        <div class="quick">${QUICK.map((q, i) => html`<button type="button" class="btn" data-q="${i}">${q.label}</button>`)}</div>
        <div class="status" id="quick-status" role="status" aria-live="polite"></div></section>
      <div class="cols cols-wide-narrow">
        <form class="card form" id="ev-form" novalidate aria-labelledby="form-title">
          <h2 id="form-title">Add an event</h2>
          <div class="form-grid">
            <label class="field"><span>Kind</span><select name="kind">${kinds.map((k) => html`<option value="${k.kind}">${k.label}</option>`)}</select></label>
            <label class="field"><span>Title</span><input type="text" name="title" maxlength="120" autocomplete="off"></label>
            <label class="field"><span id="start-label">Start date</span><input type="date" name="start_day" required></label>
            <label class="field" id="end-field"><span>End date <small>(empty = one day)</small></span><input type="date" name="end_day"></label>
            <label class="field wide"><span>Effect on the analysis</span><select name="effect"><option value="">Default</option>${effects.map(
              (e) => html`<option value="${e}">${effectLabel(e)}</option>`,
            )}</select></label>
            <label class="field wide"><span>Note <small>(optional)</small></span><textarea name="note" maxlength="2000" rows="2"></textarea></label>
          </div>
          <p class="hint" id="kind-hint"></p>
          <p class="effect-desc" id="effect-desc"></p>
          <div class="actions"><button type="submit" class="btn primary" id="save-btn">Add event</button>
            <button type="button" class="btn" id="cancel-edit" hidden>Cancel editing</button></div>
          <div class="status" id="form-status" role="status" aria-live="polite"></div>
        </form>
        <section class="card" aria-labelledby="h-eff"><h2 id="h-eff">What the effects do</h2>
          <dl class="effects">${effects.map((e) => html`<div><dt><span class="badge e-${e}">${effectLabel(e)}</span></dt><dd>${EFFECT[e] ? EFFECT[e].desc : ''}</dd></div>`)}</dl></section>
      </div>
      <section class="card" aria-labelledby="h-up"><h2 id="h-up">Upcoming and current</h2><div id="ev-up"></div></section>
      <section class="card" aria-labelledby="h-past"><h2 id="h-past">Past</h2><div id="ev-past"></div></section>`,
  );

  const form = $('#ev-form', main);
  const ctl = wireEventForm(form, async () => {
    await refreshEventLists();
  });

  async function refreshEventLists() {
    await loadEvents();
    if (!alive(token)) return;
    renderEventLists(main, ctl);
  }

  main.addEventListener('click', async (e) => {
    const q = e.target.closest('button[data-q]');
    if (q) return quickAdd(QUICK[Number(q.dataset.q)], q);
    const act = e.target.closest('button[data-act]');
    if (!act) return;
    const li = act.closest('[data-id]');
    const id = li ? Number(li.dataset.id) : null;
    const ev = state.events.find((x) => x.id === id);
    if (act.dataset.act === 'edit' && ev) ctl.edit(ev);
    else if (act.dataset.act === 'del' && ev) askDelete(li, ev);
    else if (act.dataset.act === 'keep') renderEventLists(main, ctl);
    else if (act.dataset.act === 'confirm-del' && ev) {
      act.disabled = true;
      try {
        await API.deleteEvent(ev.id);
        if (ctl.editingId() === ev.id) ctl.reset();
        setStatus($('#form-status', main), `Deleted “${ev.title}”.`, 'ok');
        setStatus($('#quick-status', main), '', '');
        await refreshEventLists();
      } catch (err) {
        setStatus($('#form-status', main), err.message, 'error');
        act.disabled = false;
      }
    } else if (act.dataset.act === 'undo') {
      act.disabled = true;
      try {
        await API.deleteEvent(Number(act.dataset.undo));
        setStatus($('#quick-status', main), 'Undone.', 'ok');
        await refreshEventLists();
      } catch (err) {
        setStatus($('#quick-status', main), err.message, 'error');
      }
    } else if (act.dataset.act === 'edit-new') {
      const created = state.events.find((x) => x.id === Number(act.dataset.id));
      if (created) ctl.edit(created);
    }
  });

  function askDelete(li, ev) {
    const box = $('.ev-actions', li);
    setHTML(
      box,
      html`<span class="confirm">Delete “${ev.title}”?</span>
        <button type="button" class="btn sm danger" data-act="confirm-del">Delete</button>
        <button type="button" class="btn sm" data-act="keep">Keep</button>`,
    );
    $('[data-act="keep"]', box).focus();
  }

  async function quickAdd(q, btn) {
    const body = q.make(todayLocal());
    const status = $('#quick-status', main);
    btn.disabled = true;
    try {
      const res = await API.createEvent(body);
      const ev = res.event;
      await refreshEventLists();
      setHTML(
        status,
        html`<span>Added “${ev.title}”, ${eventPeriod(ev)}.</span>
          <button type="button" class="btn sm link" data-act="edit-new" data-id="${ev.id}">Edit</button>
          <button type="button" class="btn sm link" data-act="undo" data-undo="${ev.id}">Undo</button>`,
      );
      status.className = 'status ok';
    } catch (err) {
      setStatus(status, err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  await refreshEventLists();
}

function setStatus(el, text, kind) {
  el.textContent = text;
  el.className = `status${kind ? ` ${kind}` : ''}`;
}

function wireEventForm(form, onSaved) {
  const f = form.elements;
  const kinds = state.meta.event_kinds || [];
  const status = $('#form-status', form);
  const saveBtn = $('#save-btn', form);
  const cancelBtn = $('#cancel-edit', form);
  let editing = null;
  let autoTitle = kinds.length ? kinds[0].label : '';

  const effective = () => f.effect.value || (kindInfo(f.kind.value) || {}).effect || 'info';
  function sync() {
    const k = kindInfo(f.kind.value);
    if (k) {
      $('#kind-hint', form).textContent = `${k.hint}. Default effect: ${effectLabel(k.effect)}.`;
      f.effect.options[0].textContent = `Default (${effectLabel(k.effect)})`;
    }
    const eff = effective();
    const point = eff === 'regime_change';
    $('#end-field', form).hidden = point;
    $('#start-label', form).textContent = point ? 'From date' : 'Start date';
    $('#effect-desc', form).textContent = EFFECT[eff] ? EFFECT[eff].desc : '';
  }
  function reset() {
    editing = null;
    form.reset();
    autoTitle = kindInfo(f.kind.value) ? kindInfo(f.kind.value).label : '';
    f.title.value = autoTitle;
    f.start_day.value = todayLocal();
    $('#form-title', form).textContent = 'Add an event';
    saveBtn.textContent = 'Add event';
    cancelBtn.hidden = true;
    $$('.ev.editing').forEach((x) => x.classList.remove('editing'));
    sync();
  }
  function edit(ev) {
    editing = ev.id;
    f.kind.value = ev.kind;
    f.title.value = ev.title;
    autoTitle = '';
    f.start_day.value = ev.start_day;
    f.end_day.value = ev.end_day || '';
    const k = kindInfo(ev.kind);
    f.effect.value = k && k.effect === ev.effect ? '' : ev.effect;
    f.note.value = ev.note || '';
    $('#form-title', form).textContent = `Edit “${ev.title}”`;
    saveBtn.textContent = 'Save changes';
    cancelBtn.hidden = false;
    setStatus(status, '', '');
    $$('.ev').forEach((x) => x.classList.toggle('editing', Number(x.dataset.id) === ev.id));
    sync();
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    f.title.focus({ preventScroll: true });
  }

  f.kind.addEventListener('change', () => {
    const k = kindInfo(f.kind.value);
    if (k && (!f.title.value.trim() || f.title.value === autoTitle)) {
      f.title.value = k.label;
      autoTitle = k.label;
    }
    sync();
  });
  f.effect.addEventListener('change', sync);
  cancelBtn.addEventListener('click', () => {
    reset();
    setStatus(status, '', '');
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const point = effective() === 'regime_change';
    const body = {
      kind: f.kind.value,
      title: f.title.value.trim(),
      start_day: f.start_day.value,
      end_day: point ? null : f.end_day.value || null,
      effect: f.effect.value || null,
      note: f.note.value.trim(),
    };
    if (!ISO.test(body.start_day)) {
      setStatus(status, 'Please pick a start date.', 'error');
      f.start_day.focus();
      return;
    }
    if (body.end_day && body.end_day < body.start_day) {
      setStatus(status, 'The end date is before the start date.', 'error');
      f.end_day.focus();
      return;
    }
    saveBtn.disabled = true;
    try {
      const res = editing != null ? await API.updateEvent(editing, body) : await API.createEvent(body);
      const verb = editing != null ? 'Saved' : 'Added';
      reset();
      setStatus(status, `${verb} “${res.event.title}” (${eventPeriod(res.event)}). Charts and anomalies use it from now on.`, 'ok');
      await onSaved();
    } catch (err) {
      setStatus(status, err.message, 'error');
    } finally {
      saveBtn.disabled = false;
    }
  });
  reset();
  return { edit, reset, editingId: () => editing };
}

function renderEventLists(main, ctl) {
  const today = todayLocal();
  const { first_day: first, last_day: last } = state.meta;
  const up = state.events.filter((e) => eventEnd(e) >= today).sort((a, b) => (a.start_day < b.start_day ? -1 : a.start_day > b.start_day ? 1 : 0));
  const past = state.events.filter((e) => eventEnd(e) < today).sort((a, b) => (a.start_day < b.start_day ? 1 : a.start_day > b.start_day ? -1 : 0));
  const item = (e) => {
    const k = kindInfo(e.kind);
    const len = e.effect === 'regime_change' ? null : daysBetween(e.start_day, eventEnd(e)) + 1;
    const inData = first && e.start_day <= last && eventEnd(e) >= first;
    const current = e.start_day <= today && today <= eventEnd(e) && e.effect !== 'regime_change';
    return html`<li class="ev${ctl.editingId() === e.id ? ' editing' : ''}" data-id="${e.id}">
      <div class="ev-main"><div class="ev-when">${eventPeriod(e)}${len ? ` · ${len} ${len === 1 ? 'day' : 'days'}` : ''}${current ? ' · now' : ''}</div>
        <div class="ev-title">${e.title}</div>
        <div class="ev-meta"><span class="badge e-${e.effect}">${effectLabel(e.effect)}</span><span>${k ? k.label : e.kind}</span>
          ${inData ? html`<a href="#/day/${e.start_day < first ? first : e.start_day}">View day</a>` : ''}</div>
        ${e.note ? html`<div class="ev-note">${e.note}</div>` : ''}</div>
      <div class="ev-actions"><button type="button" class="btn sm" data-act="edit">Edit</button>
        <button type="button" class="btn sm danger" data-act="del">Delete</button></div></li>`;
  };
  setHTML($('#ev-up', main), up.length ? html`<ul class="evlist">${up.map(item)}</ul>` : html`<p class="muted">Nothing planned. Add a holiday or guests above so the analyzer does not flag them.</p>`);
  setHTML($('#ev-past', main), past.length ? html`<ul class="evlist">${past.map(item)}</ul>` : html`<p class="muted">No past events.</p>`);
}

/* =========================================================================================
 * router
 * ======================================================================================= */

const ROUTES = { overview: viewOverview, day: viewDay, heatmap: viewHeatmap, analysis: viewAnalysis, events: viewEvents };

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = h.split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || 'overview', args: parts.slice(1), params: new URLSearchParams(query) };
}

function go(name, args = [], params = {}) {
  const target = `#/${[name, ...args].map(encodeURIComponent).join('/')}${qs(params)}`;
  if (location.hash === target) route();
  else location.hash = target;
}

async function route() {
  const r = parseHash();
  if (!ROUTES[r.name]) {
    location.replace('#/overview');
    return;
  }
  state.cleanup.forEach((fn) => fn());
  state.cleanup = [];
  state.charts.clear();
  if (resizeObs) resizeObs.disconnect();
  hideTip();
  const token = ++state.token;
  const sameView = state.view === r.name;
  state.view = r.name;
  if (!sameView) window.scrollTo(0, 0);
  $$('.nav a').forEach((a) => {
    if (a.dataset.route === r.name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  const main = $('#main');
  main.setAttribute('aria-busy', 'true');
  if (!sameView) loading(main);
  try {
    await ROUTES[r.name](main, r, token);
  } catch (err) {
    if (alive(token)) renderError(main, err);
  } finally {
    if (alive(token)) main.setAttribute('aria-busy', 'false');
  }
}

function renderHeader() {
  const m = state.meta;
  $('#demo-badge').hidden = !m.demo;
  const n = m.first_day && m.last_day ? daysBetween(m.first_day, m.last_day) + 1 : 0;
  setHTML(
    $('#meta'),
    html`<div><dt>System</dt><dd>${m.system || DASH}</dd></div>
      <div><dt>Data</dt><dd>${m.first_day ? html`${fmt.range(m.first_day, m.last_day)} <span class="muted">(${n} d)</span>` : 'none yet'}</dd></div>
      <div><dt>Last fetch</dt><dd>${m.last_fetch_at ? fmt.dateTime(m.last_fetch_at) : DASH}</dd></div>`,
  );
  $('#version').textContent = m.version ? `v${m.version}` : '';
}

async function init() {
  window.addEventListener('hashchange', route);
  window.addEventListener('resize', debounce(() => redrawCharts(false), 120));
  const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (mq && mq.addEventListener) mq.addEventListener('change', () => redrawCharts(true));
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('.plot')) hideTip();
  });
  document.addEventListener('scroll', hideTip, { passive: true });
  $('#skip').addEventListener('click', (e) => {
    e.preventDefault();
    $('#main').focus();
  });
  try {
    await Promise.all([loadMeta(), loadEvents()]);
  } catch (err) {
    renderError($('#main'), err);
    return;
  }
  setInterval(() => loadMeta().catch(() => {}), 5 * 60 * 1000);
  route();
}

init();
