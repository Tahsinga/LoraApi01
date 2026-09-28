(() => {
  document.querySelectorAll('.logout-form').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = 'Signing out...';
      form.setAttribute('aria-busy', 'true');
    });
  });

  const meter = document.querySelector('#api-bandwidth-meter');
  const details = document.querySelector('#bandwidth-details');
  if ((!meter && !details) || !('PerformanceObserver' in window)) return;

  const bytesLabel = meter?.querySelector('[data-bandwidth-bytes]');
  const countLabel = meter?.querySelector('[data-bandwidth-count]');
  const detailsBytesLabel = details?.querySelector('[data-bandwidth-bytes]');
  const detailsCountLabel = details?.querySelector('[data-bandwidth-count]');
  const today = () => {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  };
  const storageKey = 'lora-api-bandwidth';
  let usage = { date: today(), bytes: 0, responses: 0 };

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved?.date === today()) usage = saved;
  } catch (error) {
    // Keep counting in memory when browser storage is unavailable.
  }

  const formatBytes = bytes => {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
  };

  const render = () => {
    if (usage.date !== today()) usage = { date: today(), bytes: 0, responses: 0 };
    const bytes = formatBytes(usage.bytes);
    const responses = `${usage.responses} ${usage.responses === 1 ? 'response' : 'responses'}`;
    if (bytesLabel) bytesLabel.textContent = bytes;
    if (countLabel) countLabel.textContent = responses;
    if (detailsBytesLabel) detailsBytesLabel.textContent = bytes;
    if (detailsCountLabel) detailsCountLabel.textContent = responses;
  };

  const observer = new PerformanceObserver(entries => {
    render();
    for (const entry of entries.getEntries()) {
      if (!entry.name.startsWith(`${location.origin}/api/`)) continue;
      usage.bytes += entry.transferSize || 0;
      usage.responses += 1;
    }
    try {
      localStorage.setItem(storageKey, JSON.stringify(usage));
    } catch (error) {
      // The visible total remains available for this page session.
    }
    render();
  });

  observer.observe({ type: 'resource', buffered: true });
  window.addEventListener('storage', event => {
    if (event.key !== storageKey || !event.newValue) return;
    try {
      const saved = JSON.parse(event.newValue);
      if (saved.date === today()) {
        usage = saved;
        render();
      }
    } catch (error) {
      return;
    }
  });
  window.setInterval(render, 60000);
  render();
})();