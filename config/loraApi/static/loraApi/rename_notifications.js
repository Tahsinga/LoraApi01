// Product rename notifications shown under the "API received today" meter.
// New renames arrive pinned and stay until closed. Unpinned ones close by themselves
// after a few seconds. Pin and close choices are remembered in this browser only.
(function () {
  const STORE_KEY = 'loraRenameNotices';
  const POLL_MS = 30000;
  const UNPINNED_CLOSE_MS = 10000;
  const STATE_LABELS = { updated: 'Updated', waiting: 'Updating', offline: 'Offline' };
  const timers = new Map();
  let stack = null;
  let pollTimer = null;
  let loading = false;

  function readStore() {
    try {
      const data = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
      return { closed: Array.isArray(data.closed) ? data.closed : [], unpinned: Array.isArray(data.unpinned) ? data.unpinned : [] };
    } catch (error) {
      return { closed: [], unpinned: [] };
    }
  }
  function writeStore(store) {
    try {
      // Only the newest ids matter; rename notices are at most a few days old.
      localStorage.setItem(STORE_KEY, JSON.stringify({ closed: store.closed.slice(-200), unpinned: store.unpinned.slice(-200) }));
    } catch (error) { /* private window or blocked storage: choices last for this page only */ }
  }
  let store = readStore();

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
  const formatTime = value => value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '';
  const isPinned = id => !store.unpinned.includes(id);

  function closeNotice(id) {
    if (!store.closed.includes(id)) store.closed.push(id);
    writeStore(store);
    clearTimeout(timers.get(id));
    timers.delete(id);
    const card = stack && stack.querySelector(`[data-rename-notice="${id}"]`);
    if (card) {
      card.classList.add('closing');
      setTimeout(() => card.remove(), 180);
    }
  }
  function scheduleClose(id, card) {
    clearTimeout(timers.get(id));
    card.style.setProperty('--rename-notice-close', `${UNPINNED_CLOSE_MS}ms`);
    const bar = card.querySelector('.rename-notice-timer');
    if (bar) bar.replaceWith(bar.cloneNode()); // restart the countdown bar
    timers.set(id, setTimeout(() => closeNotice(id), UNPINNED_CLOSE_MS));
  }
  function setPinned(id, pinned) {
    store.unpinned = store.unpinned.filter(item => item !== id);
    if (!pinned) store.unpinned.push(id);
    writeStore(store);
    const card = stack.querySelector(`[data-rename-notice="${id}"]`);
    if (!card) return;
    card.classList.toggle('pinned', pinned);
    const button = card.querySelector('[data-rename-pin]');
    button.setAttribute('aria-pressed', String(pinned));
    button.title = pinned ? 'Unpin: closes by itself in a few seconds' : 'Pin: keep this notification open';
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
    const done = rename.branch_count > 0 && rename.updated_count === rename.branch_count;
    const percent = rename.branch_count ? Math.round(rename.updated_count * 100 / rename.branch_count) : 0;
    const chips = rename.branches.map(item => {
      const detail = item.state === 'updated'
        ? `Updated ${formatTime(item.confirmed_at)}`
        : item.last_seen ? `Last online ${formatTime(item.last_seen)}` : 'Branch app has not connected yet';
      return `<span class="rename-notice-chip ${escapeHtml(item.state)}" title="${escapeHtml(detail)}">${escapeHtml(item.branch)} &middot; ${escapeHtml(STATE_LABELS[item.state] || item.state)}</span>`;
    }).join('');
    return `<div class="rename-notice-head"><strong>${done ? 'Rename complete' : 'Product renamed'}</strong><div class="rename-notice-actions">`
      + `<button type="button" class="rename-notice-pin" data-rename-pin aria-pressed="${pinned}" title="${pinned ? 'Unpin: closes by itself in a few seconds' : 'Pin: keep this notification open'}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3l6 6-3 1-4 4 1 5-2 2-4-4-5 5-1-1 5-5-4-4 2-2 5 1 4-4z"/></svg><span>${pinned ? 'Pinned' : 'Pin'}</span></button>`
      + `<button type="button" class="rename-notice-close" data-rename-close aria-label="Close notification" title="Close">&times;</button></div></div>`
      + `<p class="rename-notice-names"><s>${escapeHtml(rename.old_name)}</s> &rarr; <b>${escapeHtml(rename.new_name)}</b></p>`
      + `<p class="rename-notice-meta">${escapeHtml(formatTime(rename.renamed_at))}${rename.renamed_by ? ` by ${escapeHtml(rename.renamed_by)}` : ''} &middot; ${rename.updated_count} of ${rename.branch_count} branch${rename.branch_count === 1 ? '' : 'es'} updated</p>`
      + `<div class="rename-notice-progress" aria-hidden="true"><span style="width:${percent}%"></span></div>`
      + `<div class="rename-notice-chips">${chips || '<small>No branches have this product yet.</small>'}</div>`
      + '<div class="rename-notice-timer" aria-hidden="true"></div>';
  }

  function render(renames) {
    const visible = renames.filter(rename => !store.closed.includes(rename.id));
    const ids = new Set(visible.map(rename => rename.id));
    stack.querySelectorAll('[data-rename-notice]').forEach(card => {
      if (!ids.has(Number(card.dataset.renameNotice))) card.remove();
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
      card.classList.toggle('pinned', isPinned(rename.id));
      if (isNew && !isPinned(rename.id)) scheduleClose(rename.id, card);
    });
    stack.hidden = !stack.children.length;
  }

  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const response = await fetch('/api/products/renames/?recent=1', { cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!response.ok || !(response.headers.get('content-type') || '').includes('json')) return;
      const data = await response.json();
      render(data.renames || []);
    } catch (error) {
      /* offline or signed out: try again on the next poll */
    } finally {
      loading = false;
    }
  }

  function start() {
    stack = document.createElement('div');
    stack.id = 'rename-notice-stack';
    stack.setAttribute('aria-label', 'Product rename notifications');
    stack.hidden = true;
    document.body.appendChild(stack);
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
      if (event.key === STORE_KEY) { store = readStore(); refresh(); }
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    refresh();
    pollTimer = setInterval(refresh, POLL_MS);
  }

  // Pages call this right after a rename so the notice shows at once, pinned.
  window.LoraRenameNotices = {
    refresh() {
      store = readStore();
      return refresh();
    },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
