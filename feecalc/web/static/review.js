// Review screen: the document viewer. Shows a page image, highlights the source of a value,
// and lets the reader move around it the way a PDF viewer does: drag to pan, wheel to zoom at the
// pointer, fit to width or to the whole page, widen the pane, keyboard for pages and zoom.
const el = (id) => document.getElementById(id);
const state = { file: null, page: 1, scale: 1, natural: { w: 1, h: 1 }, request: 0 };
const MIN_SCALE = 0.1, MAX_SCALE = 4;
const canvas = el('canvas'), sheet = el('sheet'), img = el('img');

function pagesOf(file) {
  const option = [...el('file').options].find((o) => o.value === file);
  return option ? Number(option.dataset.pages) : 1;
}

function fitWidthScale() { return (canvas.clientWidth - 4) / state.natural.w; }
function fitPageScale() { return Math.min(fitWidthScale(), (canvas.clientHeight - 4) / state.natural.h); }

function applyScale() {
  img.style.width = state.natural.w * state.scale + 'px';
  sheet.style.width = state.natural.w * state.scale + 'px';
  sheet.querySelectorAll('.hl').forEach((h) => {
    const r = JSON.parse(h.dataset.rect);
    h.style.left = r[0] * state.scale - 3 + 'px';
    h.style.top = r[1] * state.scale - 3 + 'px';
    h.style.width = r[2] * state.scale + 6 + 'px';
    h.style.height = r[3] * state.scale + 6 + 'px';
  });
  el('zoomlevel').textContent = Math.round(state.scale * 100) + '%';
}

// Zoom keeping the point under (cx, cy) - canvas coordinates - where it is.
function zoomTo(scale, cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2) {
  const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
  const x = (canvas.scrollLeft + cx) / state.scale, y = (canvas.scrollTop + cy) / state.scale;
  state.scale = next;
  applyScale();
  canvas.scrollLeft = x * next - cx;
  canvas.scrollTop = y * next - cy;
}

function fit(whole) {
  state.scale = Math.min(MAX_SCALE, whole ? fitPageScale() : fitWidthScale());
  applyScale();
  canvas.scrollTo({ left: 0, top: 0 });
}

function focusRects(rects) {
  // Zoom so that the whole snippet fits the pane and stays readable, then bring it to the middle.
  const x0 = Math.min(...rects.map((r) => r[0])), x1 = Math.max(...rects.map((r) => r[0] + r[2]));
  const y0 = Math.min(...rects.map((r) => r[1])), y1 = Math.max(...rects.map((r) => r[1] + r[3]));
  const wanted = (canvas.clientWidth - 80) / Math.max(x1 - x0, 1);
  // readable text: an average text line of about 16 px on screen
  const readable = 16 / Math.max(...rects.map((r) => r[3]), 1);
  state.scale = Math.max(Math.min(wanted, Math.max(readable, fitWidthScale())), fitWidthScale() * 0.9, MIN_SCALE);
  state.scale = Math.min(state.scale, MAX_SCALE);
  applyScale();
  canvas.scrollTo({
    left: Math.max(0, ((x0 + x1) / 2) * state.scale - canvas.clientWidth / 2),
    top: Math.max(0, ((y0 + y1) / 2) * state.scale - canvas.clientHeight / 2),
  });
}

async function show(file, page, snippet) {
  const request = ++state.request;          // a later click wins over a slow earlier one
  state.file = file;
  state.page = Math.max(1, Math.min(pagesOf(file), Number(page) || 1));
  el('file').value = file;
  el('page').value = state.page;
  el('pages').textContent = pagesOf(file);
  el('prev').disabled = state.page <= 1;
  el('next').disabled = state.page >= pagesOf(file);
  canvas.classList.add('loading');
  const q = `file=${encodeURIComponent(file)}&page=${state.page}`;
  const info = await (await fetch(`/jobs/${JOB_ID}/rects?${q}&snippet=${encodeURIComponent(snippet || '')}`)).json();
  if (request !== state.request) return;
  const src = `/jobs/${JOB_ID}/page-image?${q}`;
  if (img.dataset.src !== src) {
    await new Promise((resolve) => { img.onload = resolve; img.onerror = resolve; img.src = src; });
    img.dataset.src = src;
  }
  if (request !== state.request) return;
  state.natural = { w: info.width, h: info.height };
  sheet.querySelectorAll('.hl').forEach((h) => h.remove());
  for (const r of info.rects) {
    const h = document.createElement('div');
    h.className = 'hl';
    h.dataset.rect = JSON.stringify(r);
    sheet.appendChild(h);
  }
  el('sheetinfo').textContent = (info.sheet ? 'Varaq turi: ' + info.sheet : '') + (info.textless ? ' — MATNSIZ sahifa' : '');
  if (snippet) {
    el('snippetinfo').textContent = info.rects.length
      ? `Manba parchasi: «${snippet}»`
      : `Parcha sahifada koordinatasi bilan topilmadi: «${snippet}»`;
  } else {
    el('snippetinfo').textContent = 'Qiymatni bosing — manba parchasi sahifada sariq bilan belgilanadi.';
  }
  if (info.rects.length) focusRects(info.rects); else fit(true);
  canvas.classList.remove('loading');
}

function open(cell) {
  document.querySelectorAll('.src.active').forEach((c) => c.classList.remove('active'));
  cell.classList.add('active');
  show(cell.dataset.file, cell.dataset.page, cell.dataset.snippet);
}
document.addEventListener('click', (e) => {
  const cell = e.target.closest('.src');
  // Edit forms and links inside a source cell keep their own behaviour.
  if (!cell || e.target.closest('details.edit, form, a, button')) return;
  open(cell);
});
document.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('src')) {
    e.preventDefault();
    open(e.target);
  }
});

// ----- toolbar -----
el('file').addEventListener('change', () => show(el('file').value, 1, ''));
el('page').addEventListener('change', () => show(state.file, el('page').value, ''));
el('prev').addEventListener('click', () => show(state.file, state.page - 1, ''));
el('next').addEventListener('click', () => show(state.file, state.page + 1, ''));
el('zoomin').addEventListener('click', () => zoomTo(state.scale * 1.25));
el('zoomout').addEventListener('click', () => zoomTo(state.scale / 1.25));
el('fit').addEventListener('click', () => fit(false));
el('fitpage').addEventListener('click', () => fit(true));
el('wide').addEventListener('click', () => {
  const on = document.querySelector('.review').classList.toggle('wideviewer');
  el('wide').textContent = on ? 'Toraytirish' : 'Kengaytirish';
  el('wide').setAttribute('aria-pressed', on);
});

// ----- mouse: wheel zooms at the pointer (Ctrl or not), Shift+wheel and drag pan -----
canvas.addEventListener('wheel', (e) => {
  if (e.shiftKey) return;                    // the browser scrolls sideways
  e.preventDefault();
  const box = canvas.getBoundingClientRect();
  zoomTo(state.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX - box.left, e.clientY - box.top);
}, { passive: false });
let drag = null;
canvas.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
  canvas.setPointerCapture(e.pointerId);
  canvas.classList.add('dragging');
});
canvas.addEventListener('pointermove', (e) => {
  if (!drag) return;
  canvas.scrollLeft = drag.left - (e.clientX - drag.x);
  canvas.scrollTop = drag.top - (e.clientY - drag.y);
});
const drop = () => { drag = null; canvas.classList.remove('dragging'); };
canvas.addEventListener('pointerup', drop);
canvas.addEventListener('pointercancel', drop);
canvas.addEventListener('dblclick', (e) => {
  const box = canvas.getBoundingClientRect();
  zoomTo(state.scale * 1.6, e.clientX - box.left, e.clientY - box.top);
});

// ----- keyboard, when the viewer has focus -----
canvas.addEventListener('keydown', (e) => {
  const keys = {
    '+': () => zoomTo(state.scale * 1.25), '=': () => zoomTo(state.scale * 1.25), '-': () => zoomTo(state.scale / 1.25),
    '0': () => fit(true), PageDown: () => show(state.file, state.page + 1, ''), PageUp: () => show(state.file, state.page - 1, ''),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});
window.addEventListener('resize', () => applyScale());
show(el('file').value, 1, '');
