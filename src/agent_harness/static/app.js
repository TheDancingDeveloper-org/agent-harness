/* First-party browser behavior. Server-rendered HTML remains usable without it. */
(() => {
  'use strict';

  const status = (text) => {
    const node = document.querySelector('#connection-status');
    if (node) node.textContent = text;
  };

  const svgElement = (name, attributes = {}) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  };

  const graphKey = (kind, identity) => `${kind}:${identity}`;

  const initializeDependencyGraph = (root) => {
    const canvas = root.querySelector('[data-graph-canvas]');
    const search = root.querySelector('[data-graph-search]');
    const searchStatus = root.querySelector('[data-graph-search-status]');
    const focusSummary = root.querySelector('[data-graph-focus-summary]');
    if (!canvas || !search || !searchStatus || !focusSummary) return;

    const edgeRows = Array.from(document.querySelectorAll('[data-graph-edge]'));
    const itemRows = Array.from(document.querySelectorAll('[data-graph-item]'));
    const cycleRows = Array.from(document.querySelectorAll('[data-graph-cycle]'));
    const readiness = new Map();
    itemRows.forEach((row) => {
      readiness.set(row.dataset.graphItem, {
        ready: row.dataset.graphReady === 'true',
        explanation: row.dataset.graphExplanation || '',
      });
    });
    const cycleMembers = new Set();
    cycleRows.forEach((row) => {
      try {
        JSON.parse(row.dataset.graphCycle || '[]').forEach((identity) => cycleMembers.add(identity));
      } catch (_) {
        // The server renders the cycle text even if enhancement cannot parse it.
      }
    });

    const edges = edgeRows.map((row) => ({
      row,
      source: row.dataset.source || '',
      target: row.dataset.target || '',
      kind: row.dataset.kind || 'local_work',
      state: row.dataset.state || 'unresolved',
      required: row.dataset.required === 'true',
      resolver: row.dataset.resolver || '',
      evidence: row.dataset.evidence || '',
    }));
    const nodes = new Map();
    const addNode = (kind, identity) => {
      const key = graphKey(kind, identity);
      if (!nodes.has(key)) nodes.set(key, {key, kind, identity, incoming: [], outgoing: []});
      return nodes.get(key);
    };
    readiness.forEach((_, identity) => addNode('local_work', identity));
    edges.forEach((edge) => {
      const source = addNode('local_work', edge.source);
      const target = addNode(edge.kind, edge.target);
      source.outgoing.push(edge);
      target.incoming.push(edge);
      edge.sourceKey = source.key;
      edge.targetKey = target.key;
    });

    const columns = ['local_work', 'human_decision', 'external_reference', 'cross_project_work'];
    const grouped = new Map(columns.map((kind) => [kind, []]));
    nodes.forEach((node) => {
      if (!grouped.has(node.kind)) grouped.set(node.kind, []);
      grouped.get(node.kind).push(node);
    });
    grouped.forEach((values) => values.sort((left, right) => left.identity.localeCompare(right.identity)));
    const occupied = columns.filter((kind) => (grouped.get(kind) || []).length > 0);
    const marginX = 105;
    const columnGap = 285;
    const rowGap = 105;
    const width = Math.max(680, marginX * 2 + Math.max(1, occupied.length - 1) * columnGap + 180);
    const height = Math.max(
      360,
      130 + Math.max(1, ...occupied.map((kind) => (grouped.get(kind) || []).length)) * rowGap,
    );
    occupied.forEach((kind, columnIndex) => {
      const values = grouped.get(kind) || [];
      const x = occupied.length === 1 ? width / 2 : marginX + columnIndex * ((width - marginX * 2) / (occupied.length - 1));
      values.forEach((node, rowIndex) => {
        node.x = x;
        node.y = 85 + rowIndex * rowGap;
      });
    });

    const svg = svgElement('svg', {
      class: 'dependency-svg',
      viewBox: `0 0 ${width} ${height}`,
      role: 'group',
      'aria-label': `Dependency graph revision ${root.dataset.graphRevision || ''}`,
    });
    const definitions = svgElement('defs');
    const marker = svgElement('marker', {
      id: 'dependency-arrow',
      viewBox: '0 0 10 10',
      refX: '9',
      refY: '5',
      markerWidth: '7',
      markerHeight: '7',
      orient: 'auto-start-reverse',
    });
    marker.append(svgElement('path', {d: 'M 0 0 L 10 5 L 0 10 z', class: 'graph-arrow'}));
    definitions.append(marker);
    svg.append(definitions);

    const edgeLayer = svgElement('g', {class: 'graph-edge-layer'});
    const nodeLayer = svgElement('g', {class: 'graph-node-layer'});
    edges.forEach((edge) => {
      const source = nodes.get(edge.sourceKey);
      const target = nodes.get(edge.targetKey);
      if (!source || !target) return;
      let pathData;
      if (source.key === target.key) {
        pathData = `M ${source.x + 70} ${source.y} C ${source.x + 145} ${source.y - 70}, ${source.x + 145} ${source.y + 70}, ${source.x + 70} ${source.y + 8}`;
      } else if (source.x === target.x) {
        const bend = source.x + 115;
        pathData = `M ${source.x} ${source.y + 23} C ${bend} ${source.y + 23}, ${bend} ${target.y - 23}, ${target.x} ${target.y - 23}`;
      } else {
        const direction = target.x > source.x ? 1 : -1;
        const startX = source.x + direction * 76;
        const endX = target.x - direction * 76;
        const middle = (startX + endX) / 2;
        pathData = `M ${startX} ${source.y} C ${middle} ${source.y}, ${middle} ${target.y}, ${endX} ${target.y}`;
      }
      const path = svgElement('path', {
        d: pathData,
        class: `graph-edge state-${edge.state} ${edge.required ? 'edge-required' : 'edge-advisory'}`,
        'marker-end': 'url(#dependency-arrow)',
      });
      const title = svgElement('title');
      title.textContent = `${edge.source} waits on ${edge.target}: ${edge.state}; ${edge.required ? 'required' : 'advisory'}. ${edge.evidence}`;
      path.append(title);
      edgeLayer.append(path);
      edge.element = path;
    });
    svg.append(edgeLayer);

    const nodeElements = new Map();
    const focusNode = (node) => {
      nodeElements.forEach((element) => element.classList.toggle('graph-focused', element.dataset.nodeKey === node.key));
      itemRows.forEach((row) => row.classList.toggle('graph-focused', node.kind === 'local_work' && row.dataset.graphItem === node.identity));
      edges.forEach((edge) => {
        const adjacent = edge.sourceKey === node.key || edge.targetKey === node.key;
        edge.row.classList.toggle('graph-focused', adjacent);
        edge.element.classList.toggle('graph-focused', adjacent);
      });
      const state = readiness.get(node.identity);
      const kind = node.kind.replaceAll('_', ' ');
      const cycle = node.kind === 'local_work' && cycleMembers.has(node.identity);
      const readinessText = state ? state.explanation : 'This target has no local readiness row.';
      focusSummary.textContent = `${node.identity} — ${kind}. ${readinessText} ${node.outgoing.length} outgoing and ${node.incoming.length} incoming edge(s).${cycle ? ' This item belongs to a required cycle.' : ''}`;
    };
    nodes.forEach((node) => {
      const state = readiness.get(node.identity);
      const classes = ['graph-node', `kind-${node.kind}`];
      if (state) classes.push(state.ready ? 'node-ready' : 'node-not-ready');
      if (node.kind === 'local_work' && cycleMembers.has(node.identity)) classes.push('node-cycle');
      const group = svgElement('g', {
        class: classes.join(' '),
        transform: `translate(${node.x} ${node.y})`,
        tabindex: '0',
        role: 'button',
        'data-node-key': node.key,
        'aria-label': `${node.identity}, ${node.kind.replaceAll('_', ' ')}${state ? `, ${state.explanation}` : ''}${cycleMembers.has(node.identity) ? ', cycle member' : ''}`,
      });
      group.append(svgElement('rect', {x: '-76', y: '-29', width: '152', height: '58', rx: '10', class: 'graph-node-shape'}));
      const identity = svgElement('text', {x: '0', y: '-2', 'text-anchor': 'middle', class: 'graph-node-id'});
      identity.textContent = node.identity;
      const kind = svgElement('text', {x: '0', y: '16', 'text-anchor': 'middle', class: 'graph-node-kind'});
      kind.textContent = node.kind.replaceAll('_', ' ');
      group.append(identity, kind);
      group.addEventListener('click', () => focusNode(node));
      group.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          focusNode(node);
        }
      });
      nodeLayer.append(group);
      node.element = group;
      nodeElements.set(node.key, group);
    });
    svg.append(nodeLayer);
    canvas.replaceChildren(svg);

    const initialView = {x: 0, y: 0, width, height};
    const view = {...initialView};
    const applyView = () => svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.width} ${view.height}`);
    const zoom = (factor) => {
      const nextWidth = Math.max(width * 0.3, Math.min(width * 2.5, view.width * factor));
      const nextHeight = nextWidth * (height / width);
      view.x += (view.width - nextWidth) / 2;
      view.y += (view.height - nextHeight) / 2;
      view.width = nextWidth;
      view.height = nextHeight;
      applyView();
    };
    const pan = (x, y) => {
      view.x += view.width * x;
      view.y += view.height * y;
      applyView();
    };
    const reset = () => {
      Object.assign(view, initialView);
      applyView();
    };
    root.querySelector('[data-graph-zoom-in]').addEventListener('click', () => zoom(0.8));
    root.querySelector('[data-graph-zoom-out]').addEventListener('click', () => zoom(1.25));
    root.querySelector('[data-graph-pan-left]').addEventListener('click', () => pan(-0.12, 0));
    root.querySelector('[data-graph-pan-right]').addEventListener('click', () => pan(0.12, 0));
    root.querySelector('[data-graph-pan-up]').addEventListener('click', () => pan(0, -0.12));
    root.querySelector('[data-graph-pan-down]').addEventListener('click', () => pan(0, 0.12));
    root.querySelector('[data-graph-reset]').addEventListener('click', reset);
    canvas.addEventListener('keydown', (event) => {
      const actions = {
        ArrowLeft: () => pan(-0.08, 0),
        ArrowRight: () => pan(0.08, 0),
        ArrowUp: () => pan(0, -0.08),
        ArrowDown: () => pan(0, 0.08),
        '+': () => zoom(0.8),
        '=': () => zoom(0.8),
        '-': () => zoom(1.25),
        '0': reset,
      };
      if (actions[event.key]) {
        event.preventDefault();
        actions[event.key]();
      }
    });

    const applySearch = () => {
      const term = search.value.trim().toLocaleLowerCase();
      let matchingEdges = 0;
      let matchingNodes = 0;
      edges.forEach((edge) => {
        const text = [edge.source, edge.target, edge.kind, edge.state, edge.required ? 'required' : 'advisory', edge.resolver, edge.evidence].join(' ').toLocaleLowerCase();
        const matches = !term || text.includes(term);
        edge.row.classList.toggle('graph-search-muted', !matches);
        edge.element.classList.toggle('graph-search-muted', !matches);
        if (matches) matchingEdges += 1;
      });
      nodes.forEach((node) => {
        const state = readiness.get(node.identity);
        const adjacent = [...node.incoming, ...node.outgoing].map((edge) => `${edge.state} ${edge.evidence} ${edge.resolver}`).join(' ');
        const text = `${node.identity} ${node.kind} ${state ? state.explanation : ''} ${adjacent}`.toLocaleLowerCase();
        const matches = !term || text.includes(term);
        node.element.classList.toggle('graph-search-muted', !matches);
        node.element.setAttribute('tabindex', matches ? '0' : '-1');
        if (matches) matchingNodes += 1;
      });
      itemRows.forEach((row) => {
        const text = `${row.dataset.graphItem || ''} ${row.dataset.graphExplanation || ''}`.toLocaleLowerCase();
        row.classList.toggle('graph-search-muted', Boolean(term) && !text.includes(term));
      });
      searchStatus.textContent = term
        ? `${matchingNodes} node(s) and ${matchingEdges} edge(s) match “${search.value.trim()}”. Nonmatches remain visible but muted.`
        : `All ${nodes.size} node(s) and ${edges.length} edge(s) shown.`;
      return Array.from(nodes.values()).find((node) => !node.element.classList.contains('graph-search-muted'));
    };
    search.addEventListener('input', applySearch);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        const first = applySearch();
        if (first) {
          first.element.focus();
          focusNode(first);
        }
      } else if (event.key === 'Escape') {
        search.value = '';
        applySearch();
      }
    });
    applySearch();
    root.hidden = false;
  };

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
    document.querySelectorAll('[data-dependency-graph]').forEach(initializeDependencyGraph);
  });
})();
