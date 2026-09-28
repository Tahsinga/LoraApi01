(() => {
  const meter = document.querySelector('#api-bandwidth-meter');
  if (!meter || !('PerformanceObserver' in window)) return;

  const bytesLabel = meter.querySelector('[data-bandwidth-bytes]');
  const countLabel = meter.querySelector('[data-bandwidth-count]');
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
    bytesLabel.textContent = formatBytes(usage.bytes);
    countLabel.textContent = `${usage.responses} ${usage.responses === 1 ? 'response' : 'responses'}`;
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