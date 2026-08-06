/* First-party browser behavior. Server-rendered HTML remains usable without it. */
(() => {
  const status = (text) => { const node = document.querySelector('#connection-status'); if (node) node.textContent = text; };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-csrf-form]').forEach((form) => form.addEventListener('submit', (event) => {
      event.preventDefault();
      const csrf = form.querySelector('input[name="csrf_token"]');
      fetch(form.action, {method: 'POST', headers: {'X-CSRF-Token': csrf ? csrf.value : '', 'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams(new FormData(form))}).then((response) => {
        if (response.redirected) window.location.assign(response.url);
        else if (response.ok) response.text().then((html) => { document.open(); document.write(html); document.close(); });
        else status('Action refused (' + response.status + ') — review the server response');
      }).catch(() => status('Disconnected — action was not submitted'));
    }));
    document.querySelectorAll('[sse-connect]').forEach((node) => {
      const endpoint = node.getAttribute('sse-connect'); let cursor = Number(new URL(endpoint, window.location.href).searchParams.get('since_id') || 0); let source;
      const connect = () => {
        source = new EventSource(endpoint + (endpoint.includes('?') ? '&' : '?') + 'since_id=' + cursor);
        status('Connected to event stream');
        source.onmessage = (event) => {
          if (event.lastEventId) cursor = Number(event.lastEventId);
          try {
            const data = JSON.parse(event.data); const li = document.createElement('li'); const strong = document.createElement('strong'); strong.textContent = data.outcome || data.kind; li.append(strong, ' ', document.createTextNode(new Date((data.ts || 0) * 1000).toISOString())); if (data.data && data.data.detail) { const p = document.createElement('p'); p.textContent = data.data.detail; li.append(p); } node.prepend(li);
          } catch (_) { status('Received an event that could not be displayed'); }
        };
        source.onerror = () => { source.close(); status('Disconnected — reconnecting from cursor ' + cursor); window.setTimeout(connect, 1000); };
      };
      connect();
    });
  });
})();
