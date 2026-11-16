// Product rename notifications shown under the "API received today" meter.
// Pin, close and the popup settings (set on the Renamed products page) are
// remembered in this browser only.
(function () {
  const STORE_KEY = 'loraRenameNotices';
  const SETTINGS_KEY = 'loraRenameNoticeSettings';
  const POLL_MS = 30000;
  const DEFAULT_SETTINGS = { enabled: true, pinNew: true, closeSeconds: 10, days: 3, hideCompleted: false };
  const STATE_LABELS = { updated: 'Updated', waiting: 'Updating', offline: 'Offline' };
  const timers = new Map();
  let stack = null;
  let loading = false;
  let lastRenames = [];

  const clamp = (value, min, max, fallback) => {
    const number = Math.round(Number(value));
    return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
  };
  function readSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') || {}; } catch (error) { saved = {}; }
    return {
      enabled: saved.enabled !== false,
      pinNew: saved.pinNew !== false,
      closeSeconds: clamp(saved.closeSeconds, 3, 120, DEFAULT_SETTINGS.closeSeconds),
      days: clamp(saved.days, 1, 14, DEFAULT_SETTINGS.days),
      hideCompleted: saved.hideCompleted === true,
    };
  }
  function readStore() {
    try {
      const data = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {};
      const list = value => Array.isArray(value) ? value.map(Number) : [];
      return { closed: list(data.closed), unpinned: list(data.unpinned), pinned: list(data.pinned) };
    } catch (error) {
      return { closed: [], unpinned: [], pinned: [] };
    }
  }
  function writeStore() {
    try {
      // Only recent ids matter; rename notices are at most two weeks old.
      localStorage.setItem(STORE_KEY, JSON.stringify({
        closed: store.closed.slice(-300), unpinned: store.unpinned.slice(-300), pinned: store.pinned.slice(-300),
      }));
    } catch (error) { /* private window or blocked storage: choices last for this page only */ }
  }
  let settings = readSettings();
  let store = readStore();

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
  const formatTime = value => value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '';
  const closeMs = () => settings.closeSeconds * 1000;
  function isPinned(id) {
    if (store.pinned.includes(id)) return true;
    if (store.unpinned.includes(id)) return false;
    return settings.pinNew;
  }
  const isComplete = rename => rename.branch_count > 0 && rename.updated_count === rename.branch_count;

  function closeNotice(id) {
    if (!store.closed.includes(id)) store.closed.push(id);
    writeStore();
    clearTimeout(timers.get(id));
    timers.delete(id);
    const card = stack && stack.querySelector(`[data-rename-notice="${id}"]`);
    if (card) {
      card.classList.add('closing');
      setTimeout(() => { card.remove(); if (stack) stack.hidden = !stack.children.length; }, 180);
    }
  }
  function scheduleClose(id, card) {
    clearTimeout(timers.get(id));
    card.style.setProperty('--rename-notice-close', `${closeMs()}ms`);
    const bar = card.querySelector('.rename-notice-timer');
    if (bar) bar.replaceWith(bar.cloneNode()); // restart the countdown bar
    timers.set(id, setTimeout(() => closeNotice(id), closeMs()));
  }
  function setPinned(id, pinned) {
    store.pinned = store.pinned.filter(item => item !== id);
    store.unpinned = store.unpinned.filter(item => item !== id);
    (pinned ? store.pinned : store.unpinned).push(id);
    writeStore();
    const card = stack && stack.querySelector(`[data-rename-notice="${id}"]`);
    if (!card) return;
    card.classList.toggle('pinned', pinned);
    const button = card.querySelector('[data-rename-pin]');
    button.setAttribute('aria-pressed', String(pinned));
    button.title = pinned ? `Unpin: closes by itself after ${settings.closeSeconds} seconds` : 'Pin: keep this notification open';
    button.querySelector('span').textContent = pinned ? 'Pinned' : 'Pin';
    if (pinned) {
      clearTimeout(timers.get(id));
      timers.delete(id);
    } else {
      scheduleClose(id, card);
    }
  }

  function cardHtml(rename) {
    const pinned = isPinned(rename.id);
    const percent = rename.branch_count ? Math.round(rename.updated_count * 100 / rename.branch_count) : 0;
    const chips = rename.branches.map(item => {
      const detail = item.state === 'updated'
        ? `Updated ${formatTime(item.confirmed_at)}`
        : item.last_seen ? `Last online ${formatTime(item.last_seen)}` : 'Branch app has not connected yet';
      return `<span class="rename-notice-chip ${escapeHtml(item.state)}" title="${escapeHtml(detail)}">${escapeHtml(item.branch)} &middot; ${escapeHtml(STATE_LABELS[item.state] || item.state)}</span>`;
    }).join('');
    return `<div class="rename-notice-head"><strong>${isComplete(rename) ? 'Rename complete' : 'Product renamed'}</strong><div class="rename-notice-actions">`
      + `<button type="button" class="rename-notice-pin" data-rename-pin aria-pressed="${pinned}" title="${pinned ? `Unpin: closes by itself after ${settings.closeSeconds} seconds` : 'Pin: keep this notification open'}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3l6 6-3 1-4 4 1 5-2 2-4-4-5 5-1-1 5-5-4-4 2-2 5 1 4-4z"/></svg><span>${pinned ? 'Pinned' : 'Pin'}</span></button>`
      + `<button type="button" class="rename-notice-close" data-rename-close aria-label="Close notification" title="Close">&times;</button></div></div>`
      + `<p class="rename-notice-names"><s>${escapeHtml(rename.old_name)}</s> &rarr; <b>${escapeHtml(rename.new_name)}</b></p>`
      + `<p class="rename-notice-meta">${escapeHtml(formatTime(rename.renamed_at))}${rename.renamed_by ? ` by ${escapeHtml(rename.renamed_by)}` : ''} &middot; ${rename.updated_count} of ${rename.branch_count} branch${rename.branch_count === 1 ? '' : 'es'} updated</p>`
      + `<div class="rename-notice-progress" aria-hidden="true"><span style="width:${percent}%"></span></div>`
      + `<div class="rename-notice-chips">${chips || '<small>No branches have this product yet.</small>'}</div>`
      + '<div class="rename-notice-timer" aria-hidden="true"></div>';
  }

  function render(renames) {
    if (!stack) return;
    const visible = settings.enabled
      ? renames.filter(rename => !store.closed.includes(rename.id) && !(settings.hideCompleted && isComplete(rename)))
      : [];
    const ids = new Set(visible.map(rename => rename.id));
    stack.querySelectorAll('[data-rename-notice]').forEach(card => {
      const id = Number(card.dataset.renameNotice);
      if (!ids.has(id)) { clearTimeout(timers.get(id)); timers.delete(id); card.remove(); }
    });
    visible.slice().reverse().forEach(rename => {
      let card = stack.querySelector(`[data-rename-notice="${rename.id}"]`);
      const isNew = !card;
      if (isNew) {
        card = document.createElement('section');
        card.className = 'rename-notice';
        card.dataset.renameNotice = rename.id;
        card.setAttribute('role', 'status');
        stack.prepend(card);
      }
      card.innerHTML = cardHtml(rename);
      const pinned = isPinned(rename.id);
      card.classList.toggle('pinned', pinned);
      if (pinned) { clearTimeout(timers.get(rename.id)); timers.delete(rename.id); }
      else if (isNew || !timers.has(rename.id)) scheduleClose(rename.id, card);
    });
    stack.hidden = !stack.children.length;
  }

  async function refresh() {
    if (loading || document.hidden) return;
    if (!settings.enabled) { render([]); return; }
    loading = true;
    try {
      const response = await fetch(`/api/products/renames/?recent=1&days=${settings.days}`, { cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!response.ok || !(response.headers.get('content-type') || '').includes('json')) return;
      const data = await response.json();
      lastRenames = data.renames || [];
      render(lastRenames);
    } catch (error) {
      /* offline or signed out: try again on the next poll */
    } finally {
      loading = false;
    }
  }

  function reload() {
    settings = readSettings();
    store = readStore();
    render(lastRenames);
    return refresh();
  }

  function start() {
    stack = document.createElement('div');
    stack.id = 'rename-notice-stack';
    stack.setAttribute('aria-label', 'Product rename notifications');
    stack.hidden = true;
    document.body.appendChild(stack);
    // Same width as the "API received today" box, starting just below it.
    const meter = document.getElementById('api-bandwidth-meter');
    const matchMeter = () => {
      if (!meter || !meter.offsetWidth) return;
      const box = meter.getBoundingClientRect();
      stack.style.setProperty('--rename-notice-width', `${Math.round(box.width)}px`);
      stack.style.setProperty('--rename-notice-top', `${Math.round(box.bottom + 8)}px`);
    };
    matchMeter();
    if (meter && window.ResizeObserver) new ResizeObserver(matchMeter).observe(meter);
    window.addEventListener('resize', matchMeter);
    stack.addEventListener('click', event => {
      const card = event.target.closest('[data-rename-notice]');
      if (!card) return;
      const id = Number(card.dataset.renameNotice);
      if (event.target.closest('[data-rename-close]')) closeNotice(id);
      else if (event.target.closest('[data-rename-pin]')) setPinned(id, !isPinned(id));
    });
    // Pause the countdown of an unpinned notice while the pointer is over it.
    stack.addEventListener('mouseover', event => {
      const card = event.target.closest('[data-rename-notice]');
      if (card && !card.classList.contains('pinned')) {
        clearTimeout(timers.get(Number(card.dataset.renameNotice)));
        card.classList.add('hold');
      }
    });
    stack.addEventListener('mouseout', event => {
      const card = event.target.closest('[data-rename-notice]');
      if (card && !card.contains(event.relatedTarget) && !card.classList.contains('pinned')) {
        card.classList.remove('hold');
        scheduleClose(Number(card.dataset.renameNotice), card);
      }
    });
    window.addEventListener('storage', event => {
      if (event.key === STORE_KEY || event.key === SETTINGS_KEY) reload();
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    refresh();
    setInterval(refresh, POLL_MS);
  }

  window.LoraRenameNotices = {
    defaults: { ...DEFAULT_SETTINGS },
    // Pages call this right after a rename so the notice shows at once.
    refresh: reload,
    getSettings: () => ({ ...settings }),
    saveSettings(next) {
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...settings, ...next })); } catch (error) { /* storage blocked */ }
      settings = { ...readSettings(), ...(localStorageWorks() ? {} : next) };
      render(lastRenames);
      return refresh();
    },
    // Bring a rename's popup back, pinned.
    show(id) {
      id = Number(id);
      store.closed = store.closed.filter(item => item !== id);
      store.unpinned = store.unpinned.filter(item => item !== id);
      if (!store.pinned.includes(id)) store.pinned.push(id);
      writeStore();
      return reload();
    },
    closedCount: () => store.closed.length,
    showClosedAgain() {
      store.closed = [];
      writeStore();
      return reload();
    },
  };
  function localStorageWorks() {
    try { localStorage.setItem('loraRenameNoticeTest', '1'); localStorage.removeItem('loraRenameNoticeTest'); return true; } catch (error) { return false; }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
