// Shared behaviour of every page: one-time confirmations, dialogs, the staff-name prompt,
// file drop zones, and opening the section that an anchor points into.
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);

  // ----- toast: a short confirmation in the corner -----
  function toast(text) {
    const box = document.createElement('div');
    box.className = 'toast';
    box.setAttribute('role', 'status');
    box.textContent = text;
    $('#toasts').appendChild(box);
    setTimeout(() => box.remove(), 4500);
  }
  window.toast = toast;
  // The server leaves a one-time message in the `flash` cookie before a redirect.
  const flash = document.cookie.match(/(?:^|; )flash=([^;]*)/);
  if (flash) {
    toast(decodeURIComponent(flash[1]));
    document.cookie = 'flash=; Max-Age=0; path=/';
  }

  // ----- dialogs -----
  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-open-dialog]');
    if (opener) {
      $('#' + opener.dataset.openDialog).showModal();
      return;
    }
    const closer = e.target.closest('[data-close-dialog]');
    if (closer) closer.closest('dialog').close();
  });
  // The staff name is required for every upload, confirmation and change (it goes to the audit log).
  // Without a name the prompt opens by itself and cannot be dismissed, so nothing is ever silently disabled.
  const who = $('#who');
  if (who && who.hasAttribute('data-required')) {
    who.showModal();
    who.addEventListener('cancel', (e) => e.preventDefault());
  }

  // ----- forms: ask before an irreversible action, block double submission -----
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      e.preventDefault();
      const dialog = $('#confirm');
      $('[data-title]', dialog).textContent = form.dataset.confirmTitle || 'Tasdiqlaysizmi?';
      $('[data-text]', dialog).textContent = form.dataset.confirm;
      const ok = $('[data-ok]', dialog);
      ok.textContent = form.dataset.confirmOk || 'Ha';
      ok.onclick = () => {
        form.dataset.confirmed = '1';
        dialog.close();
        form.requestSubmit();
      };
      dialog.showModal();
      return;
    }
    if (form.method.toLowerCase() === 'post') {
      setTimeout(() => form.querySelectorAll('button[type=submit]').forEach((b) => { b.disabled = true; }), 0);
    }
  });

  // ----- file drop zones: list the chosen files, accept PDF only -----
  const size = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1).replace('.', ',') + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB');
  document.querySelectorAll('[data-dropzone]').forEach((zone) => {
    const input = $('input[type=file]', zone), list = $('[data-files]', zone), clear = $('[data-clear]', zone);
    function paint() {
      const files = [...input.files];
      const wrong = files.filter((f) => !f.name.toLowerCase().endsWith('.pdf'));
      input.setCustomValidity(wrong.length ? 'Faqat PDF fayl yuklash mumkin' : '');
      zone.classList.toggle('filled', files.length > 0 && !wrong.length);
      zone.classList.toggle('invalid', wrong.length > 0);
      list.textContent = '';
      for (const f of files) {
        const line = document.createElement('span');
        line.textContent = f.name + ' ';
        const s = document.createElement('span');
        s.className = 'size';
        s.textContent = '(' + size(f.size) + ')';
        line.appendChild(s);
        list.appendChild(line);
      }
      if (wrong.length) {
        const line = document.createElement('span');
        line.className = 'err';
        line.textContent = 'Faqat PDF fayl yuklash mumkin: ' + wrong.map((f) => f.name).join(', ');
        list.appendChild(line);
      }
      if (clear) clear.hidden = !files.length;
    }
    input.addEventListener('change', paint);
    ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, () => zone.classList.add('over')));
    ['dragleave', 'drop'].forEach((t) => zone.addEventListener(t, () => zone.classList.remove('over')));
    if (clear) {
      clear.addEventListener('click', (e) => {
        e.preventDefault();
        input.value = '';
        paint();
      });
    }
  });

  // ----- anchors: open the collapsed section that holds the target and show where it is -----
  function reveal() {
    if (location.hash.length < 2) return;
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!target) return;
    for (let p = target; p; p = p.parentElement) if (p.tagName === 'DETAILS') p.open = true;
    target.scrollIntoView({ block: 'center' });
    target.classList.add('flash');
    setTimeout(() => target.classList.remove('flash'), 2200);
  }
  window.addEventListener('hashchange', reveal);
  reveal();
})();
