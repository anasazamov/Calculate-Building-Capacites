// Review screen: page viewer with highlighted source snippets.
const el = (id) => document.getElementById(id);
const state = { file: null, page: 1, scale: 1, natural: { w: 1, h: 1 } };

function pagesOf(file) {
  const opt = [...el('file').options].find((o) => o.value === file);
  return opt ? Number(opt.dataset.pages) : 1;
}

function applyScale() {
  el('img').style.width = state.natural.w * state.scale + 'px';
  el('sheet').querySelectorAll('.hl').forEach((h) => {
    const r = JSON.parse(h.dataset.rect);
    h.style.left = r[0] * state.scale - 3 + 'px';
    h.style.top = r[1] * state.scale - 3 + 'px';
    h.style.width = r[2] * state.scale + 6 + 'px';
    h.style.height = r[3] * state.scale + 6 + 'px';
  });
}

function fit() {
  state.scale = Math.min(1, (el('canvas').clientWidth - 4) / state.natural.w);
  applyScale();
}

async function show(file, page, snippet) {
  state.file = file;
  state.page = Math.max(1, Math.min(pagesOf(file), Number(page) || 1));
  el('file').value = file;
  el('page').value = state.page;
  el('pages').textContent = pagesOf(file);
  const q = `file=${encodeURIComponent(file)}&page=${state.page}`;
  const info = await (await fetch(`/jobs/${JOB_ID}/rects?${q}&snippet=${encodeURIComponent(snippet || '')}`)).json();
  state.natural = { w: info.width, h: info.height };
  const img = el('img');
  await new Promise((resolve) => { img.onload = resolve; img.src = `/jobs/${JOB_ID}/page-image?${q}`; });
  el('sheet').querySelectorAll('.hl').forEach((h) => h.remove());
  for (const r of info.rects) {
    const h = document.createElement('div');
    h.className = 'hl';
    h.dataset.rect = JSON.stringify(r);
    el('sheet').appendChild(h);
  }
  el('sheetinfo').textContent = (info.sheet ? 'Varaq turi: ' + info.sheet : '') + (info.textless ? ' — MATNSIZ sahifa' : '');
  const fitScale = Math.min(1, (el('canvas').clientWidth - 4) / state.natural.w);
  if (snippet) {
    el('snippetinfo').textContent = info.rects.length
      ? `Manba parchasi: «${snippet}»`
      : `Parcha sahifada koordinatasi bilan topilmadi: «${snippet}»`;
  } else {
    el('snippetinfo').textContent = 'Qiymatni bosing — manba parchasi sahifada sariq bilan belgilanadi.';
  }
  state.scale = fitScale;
  if (info.rects.length) {
    // Zoom so that the whole snippet fits the pane and stays readable, then scroll to it.
    const x0 = Math.min(...info.rects.map((r) => r[0])), x1 = Math.max(...info.rects.map((r) => r[0] + r[2]));
    const y0 = Math.min(...info.rects.map((r) => r[1]));
    const wanted = (el('canvas').clientWidth - 60) / Math.max(x1 - x0, 1);
    state.scale = Math.max(fitScale, Math.min(1, wanted));
    let left = x0 * state.scale - 30;
    if (state.scale < 0.6) {
      // The snippet spans a wide table: keep the text readable and bring its last part (the value) into view.
      state.scale = 0.75;
      left = x1 * state.scale - el('canvas').clientWidth + 40;
    }
    applyScale();
    el('canvas').scrollTo({ left: Math.max(0, left), top: Math.max(0, y0 * state.scale - el('canvas').clientHeight / 3) });
  } else {
    applyScale();
  }
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
el('file').addEventListener('change', () => show(el('file').value, 1, ''));
el('page').addEventListener('change', () => show(state.file, el('page').value, ''));
el('prev').addEventListener('click', () => show(state.file, state.page - 1, ''));
el('next').addEventListener('click', () => show(state.file, state.page + 1, ''));
el('zoomin').addEventListener('click', () => { state.scale *= 1.25; applyScale(); });
el('zoomout').addEventListener('click', () => { state.scale /= 1.25; applyScale(); });
el('fit').addEventListener('click', fit);
show(el('file').value, 1, '');
