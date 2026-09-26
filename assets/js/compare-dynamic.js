/*
 * Dynamic model selection for the comparison pages.
 * Shared by /compare/ (hub) and every authored comparison page.
 *
 * The build-time payload (assets/data/compare.json, fed by Jekyll site data)
 * contains models, capability dimensions, provider prices, and the rendered
 * verdict HTML of every published comparison. Selecting two models re-renders
 * the section stack in place; the pair is mirrored into ?a=&b= so any view is
 * shareable and reload-safe. Authored comparison pages keep their
 * server-rendered sections unless the selected pair differs.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-cmp-root]');
  if (!root) return;

  var a = document.getElementById('cmp-a');
  var b = document.getElementById('cmp-b');
  var swap = document.getElementById('cmp-swap');
  var hint = document.getElementById('cmp-hint');
  var isHub = root.getAttribute('data-page-kind') === 'hub';
  var pageUrl = root.getAttribute('data-page-url') || '/';

  var payload = null;   // fetched JSON, null until loaded
  var listed = null;    // cache: { key: true } for the models the pickers offer
  var pending = null;   // pair requested while the payload was loading
  var ssrPair = null;   // server-rendered pair (comparison pages only)

  if (root.getAttribute('data-pair-a') && root.getAttribute('data-pair-b')) {
    ssrPair = [root.getAttribute('data-pair-a'), root.getAttribute('data-pair-b')];
  }

  /* ------------------------------------------------------------------ */
  /* Helpers                                                             */
  /* ------------------------------------------------------------------ */

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* Liquid `round: 4`: integral values render with one decimal (4 -> "4.0"). */
  function fmtPrice(v) {
    var r = Math.round(v * 10000) / 10000;
    return r % 1 === 0 ? r.toFixed(1) : String(r);
  }

  /* Liquid `divided_by | round: 1`. */
  function fmtRatio(hi, lo) {
    return (Math.round(hi / lo * 10) / 10).toFixed(1);
  }

  /* Models the server decided to offer (payload.listed_models, computed from
   * the provider snapshot). A payload without the list -> every model counts,
   * so a cached older JSON cannot empty the pickers. */
  function isListed(k) {
    if (listed === null) {
      listed = {};
      var keys = payload.listed_models;
      if (keys) {
        for (var i = 0; i < keys.length; i++) listed[keys[i]] = true;
      } else {
        for (var key in payload.models) listed[key] = true;
      }
    }
    return !!listed[k];
  }

  function setHint(msg) {
    if (hint) hint.textContent = msg;
  }

  function param(name) {
    var m = location.search.match(new RegExp('[?&]' + name + '=([^&]*)'));
    if (!m) return null;
    try { return decodeURIComponent(m[1]); } catch (e) { return null; }
  }

  function pairId(k1, k2) {
    return [k1, k2].sort().join('|');
  }

  /* Order-insensitive: a reversed selection still shows the authored verdict. */
  function findComparison(k1, k2) {
    var id = pairId(k1, k2);
    var cs = payload.comparisons;
    for (var i = 0; i < cs.length; i++) {
      if (pairId(cs[i].models[0], cs[i].models[1]) === id) return cs[i];
    }
    return null;
  }

  function relatedFor(k1, k2) {
    var hit = findComparison(k1, k2);
    var cs = payload.comparisons;
    var out = [];
    for (var i = 0; i < cs.length; i++) {
      var c = cs[i];
      if (c === hit) continue;
      if (c.models.indexOf(k1) !== -1 || c.models.indexOf(k2) !== -1) out.push(c);
    }
    return out;
  }

  function chip(c) {
    return '<a class="cmp-related__chip" href="' + esc(c.url) + '">' + esc(c.title_short) + '&nbsp;&nearr;</a>';
  }

  /* ------------------------------------------------------------------ */
  /* Section builders (mirror the server-rendered markup)                */
  /* ------------------------------------------------------------------ */

  function heroHtml(m1, m2) {
    function model(m, tone) {
      return '<a class="cmp-hero__model cmp-hero__model--' + tone + '" href="' + esc(m.model_url) + '" target="_blank" rel="noopener">' +
        '<span class="cmp-hero__vendor">' + esc(m.vendor) + '</span>' +
        '<span class="cmp-hero__name">' + esc(m.name) + '</span>' +
        '<span class="cmp-hero__stats">' +
          '<span>' + esc(m.context_display) + ' ctx</span>' +
          '<span aria-hidden="true">&middot;</span>' +
          '<span>' + esc(m.pricing.input.label) + ' in / ' + esc(m.pricing.output.label) + ' out</span>' +
        '</span>' +
      '</a>';
    }
    return '<section class="cmp-hero">' +
      '<div class="cmp-hero__models">' + model(m1, 'm1') + model(m2, 'm2') + '</div>' +
      '<button type="button" class="cmp-highlight-toggle" aria-pressed="true">Hide highlights</button>' +
    '</section>';
  }

  function radarHtml(m1, m2) {
    function key(m, tone) {
      return '<span class="cmp-radar__key">' +
        '<span class="cmp-radar__swatch cmp-radar__swatch--' + tone + '" aria-hidden="true"></span>' +
        '<span>' + esc(m.name) + '<em>' + esc(m.vendor) + '</em></span>' +
      '</span>';
    }
    return '<section class="cmp-section" id="radar">' +
      '<h2 class="cmp-section__title">Capability radar</h2>' +
      '<p class="cmp-section__note">Shape = OpenCode normalized scores (0&ndash;100); a missing score plots at the center.</p>' +
      '<div class="cmp-radar">' +
        '<div class="cmp-radar__legend">' + key(m1, 'm1') + key(m2, 'm2') + '</div>' +
        '<div class="cmp-radar__mount" id="cmp-radar" hidden></div>' +
      '</div>' +
    '</section>';
  }

  function barcell(m, s, tone, other) {
    if (s == null) {
      return '<div class="cmp-barcell"><span class="cmp-nodata">' + esc(m.name) + ': no data</span></div>';
    }
    var best = other != null && s > other ? ' is-best' : '';
    return '<div class="cmp-barcell">' +
      '<div class="cmp-bar"><div class="cmp-bar__fill cmp-bar__fill--' + tone + best + '" style="width: ' + s + '%;"></div></div>' +
      '<span class="cmp-bar__label">' + s + '<span class="cmp-denom">/100</span></span>' +
    '</div>';
  }

  function scoresHtml(m1, m2) {
    var dims = payload.dimensions;
    var rows = '';
    for (var i = 0; i < dims.length; i++) {
      var d = dims[i];
      var s1 = m1.scores[d.key] == null ? null : m1.scores[d.key];
      var s2 = m2.scores[d.key] == null ? null : m2.scores[d.key];
      rows += '<div class="cmp-score-row">' +
        '<div class="cmp-dim">' + esc(d.label) + '</div>' +
        barcell(m1, s1, 'm1', s2) +
        barcell(m2, s2, 'm2', s1) +
      '</div>';
    }
    return '<section class="cmp-section" id="capability-scores">' +
      '<h2 class="cmp-section__title">Capability scores</h2>' +
      '<p class="cmp-section__note">Normalized capability scores (0&ndash;100) from ' + (m1.data_url ? '<a href="' + esc(m1.data_url) + '" target="_blank" rel="noopener">OpenCode data</a>' : 'OpenCode data') + '; &ldquo;No data&rdquo; mirrors the source.</p>' +
      '<div class="cmp-scores">' + rows + '</div>' +
    '</section>';
  }

  function overviewHtml(m1, m2) {
    function author(m) {
      return '<a href="' + esc(m.model_url) + '" target="_blank" rel="noopener">' + esc(m.vendor) + '</a>';
    }
    function weights(m) {
      return m.weights_url
        ? '<a href="' + esc(m.weights_url) + '" target="_blank" rel="noopener">Open weights&nbsp;&nearr;</a>'
        : '&mdash;';
    }
    var rows =
      '<tr><th scope="row">Author</th><td>' + author(m1) + '</td><td>' + author(m2) + '</td></tr>' +
      '<tr><th scope="row">Context length</th><td>' + esc(m1.context_display) + '</td><td>' + esc(m2.context_display) + '</td></tr>' +
      '<tr><th scope="row">Max output</th><td>' + esc(m1.output_display) + '</td><td>' + esc(m2.output_display) + '</td></tr>' +
      '<tr><th scope="row">Knowledge cutoff</th><td>' + esc(m1.knowledge_cutoff) + '</td><td>' + esc(m2.knowledge_cutoff) + '</td></tr>' +
      '<tr><th scope="row">Released</th><td>' + esc(m1.released) + '</td><td>' + esc(m2.released) + '</td></tr>' +
      '<tr><th scope="row">Reasoning</th><td>' + (m1.reasoning ? 'True' : 'False') + '</td><td>' + (m2.reasoning ? 'True' : 'False') + '</td></tr>' +
      '<tr><th scope="row">Input modalities</th><td>' + esc(m1.inputs.join(', ')) + '</td><td>' + esc(m2.inputs.join(', ')) + '</td></tr>' +
      '<tr><th scope="row">Output modalities</th><td>' + esc(m1.outputs.join(', ')) + '</td><td>' + esc(m2.outputs.join(', ')) + '</td></tr>';
    if (m1.weights_url || m2.weights_url) {
      rows += '<tr><th scope="row">Model weights</th><td>' + weights(m1) + '</td><td>' + weights(m2) + '</td></tr>';
    }
    return '<section class="cmp-section" id="overview">' +
      '<h2 class="cmp-section__title">Overview</h2>' +
      '<div class="cmp-tablewrap"><table class="cmp-table">' +
        '<thead><tr><th scope="col"><span class="visually-hidden">Attribute</span></th><th scope="col">' + esc(m1.name) + '</th><th scope="col">' + esc(m2.name) + '</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table></div>' +
    '</section>';
  }

  var PRICE_ROWS = [['input', 'Input'], ['output', 'Output'], ['cached', 'Cached input']];

  function pricingHtml(m1, m2) {
    var rows = '';
    for (var i = 0; i < PRICE_ROWS.length; i++) {
      var t = PRICE_ROWS[i][0], label = PRICE_ROWS[i][1];
      var p1 = m1.pricing[t], p2 = m2.pricing[t];
      var u1 = p1.usd, u2 = p2.usd;
      var both = u1 != null && u2 != null;
      rows += '<tr><th scope="row">' + label + '</th>' +
        '<td>' + (both && u1 < u2 ? '<span class="cmp-best-chip">Cheaper</span>' : '') + esc(p1.label) + '</td>' +
        '<td>' + (both && u2 < u1 ? '<span class="cmp-best-chip">Cheaper</span>' : '') + esc(p2.label) + '</td></tr>';
    }
    var html = '<section class="cmp-section" id="pricing">' +
      '<h2 class="cmp-section__title">Pricing</h2>' +
      '<p class="cmp-section__note">USD per 1M tokens; the cheaper side is highlighted.</p>' +
      '<div class="cmp-tablewrap"><table class="cmp-table">' +
        '<thead><tr><th scope="col"><span class="visually-hidden">Token type</span></th><th scope="col">' + esc(m1.name) + '</th><th scope="col">' + esc(m2.name) + '</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table></div>';
    var o1 = m1.pricing.output.usd, o2 = m2.pricing.output.usd;
    if (o1 != null && o2 != null && o1 !== o2) {
      var lo = o1, hi = o2, cheap = m1.name;
      if (o2 < o1) { lo = o2; hi = o1; cheap = m2.name; }
      var ratio = Math.round(hi / lo * 10) / 10;
      if (ratio > 1.05) {
        html += '<p class="cmp-pricing__note">' + esc(cheap) + ' costs <strong>' + fmtRatio(hi, lo) + '&times; less</strong> per output token.</p>';
      }
    }
    return html + '</section>';
  }

  function extHtml(k1, k2, m1, m2) {
    var extModels = payload.external.models || {};
    var keys = [k1, k2];
    var names = [m1.name, m2.name];
    var blocks = '';
    for (var i = 0; i < keys.length; i++) {
      var row = extModels[keys[i]] || {};
      var best = row.prices_best || [];
      if (!best.length) continue;
      blocks += '<h3 class="cmp-ext__h3">' + esc(names[i]) + ' <span class="cmp-ext__src">' + best.length + ' of ' + row.provider_total + ' providers</span></h3>' +
        '<div class="cmp-tablewrap"><table class="cmp-table">' +
          '<thead><tr><th>Provider</th><th>Tier</th><th>Input $/1M</th><th>Output $/1M</th><th>Cached $/1M</th></tr></thead>' +
          '<tbody>';
      for (var j = 0; j < best.length; j++) {
        var p = best[j];
        blocks += '<tr class="cmp-ext__price-row">' +
          '<th scope="row">' + (j === 0 ? '<span class="cmp-best-chip">cheapest here</span>' : '') + esc(p.provider) + '</th>' +
          '<td>' + (p.tier ? esc(p.tier) : '<span class="cmp-muted">&mdash;</span>') + '</td>' +
          '<td>$' + fmtPrice(p.input) + '</td>' +
          '<td>$' + fmtPrice(p.output) + (p.discount ? '<span class="cmp-ext__disc">*</span>' : '') + '</td>' +
          '<td>' + (p.cached ? '$' + fmtPrice(p.cached) : '<span class="cmp-muted">&mdash;</span>') + '</td>' +
        '</tr>';
      }
      blocks += '</tbody></table></div>';
    }
    return '<section class="cmp-section" id="provider-prices">' +
      '<h2 class="cmp-section__title">Provider prices</h2>' +
      '<p class="cmp-section__note">Charged USD per 1M tokens from major inference providers, collected independently of the OpenCode list prices above. Snapshot ' + esc(payload.external.generated) + '; <a href="/compare/benchmarks/">full provider list</a>.</p>' +
      blocks +
      '<p class="cmp-ext__foot">* = source-applied discount off its own list price. Rows list the cheapest tier per provider; &ldquo;cheapest here&rdquo; compares only the listed hosts.</p>' +
    '</section>';
  }

  function momentumCol(m, key) {
    return m.momentum.has_usage ? esc(m.momentum[key]) : '<span class="cmp-muted">No usage</span>';
  }

  function momentumOptCol(m, key) {
    var v = m.momentum[key];
    return v ? esc(v) : '&mdash;';
  }

  function momentumHtml(m1, m2) {
    var rows = '';
    if (m1.momentum.has_usage || m2.momentum.has_usage) {
      rows +=
        '<tr><th scope="row">Unique users</th><td>' + momentumCol(m1, 'users') + '</td><td>' + momentumCol(m2, 'users') + '</td></tr>' +
        '<tr><th scope="row">Completed sessions</th><td>' + momentumCol(m1, 'sessions') + '</td><td>' + momentumCol(m2, 'sessions') + '</td></tr>' +
        '<tr><th scope="row">Token share</th><td>' + momentumCol(m1, 'token_share') + '</td><td>' + momentumCol(m2, 'token_share') + '</td></tr>' +
        '<tr><th scope="row">Tokens</th><td>' + momentumCol(m1, 'tokens') + (m1.momentum.has_usage ? ' <span class="cmp-trend">' + esc(m1.momentum.tokens_trend) + '</span>' : '') + '</td><td>' + momentumCol(m2, 'tokens') + (m2.momentum.has_usage ? ' <span class="cmp-trend">' + esc(m2.momentum.tokens_trend) + '</span>' : '') + '</td></tr>' +
        '<tr><th scope="row">Week-1 retention</th><td>' + momentumCol(m1, 'retention') + '</td><td>' + momentumCol(m2, 'retention') + '</td></tr>';
      if (m1.momentum.cache_ratio || m2.momentum.cache_ratio) {
        rows += '<tr><th scope="row">Cache ratio</th><td>' + momentumOptCol(m1, 'cache_ratio') + '</td><td>' + momentumOptCol(m2, 'cache_ratio') + '</td></tr>';
      }
      if (m1.momentum.avg_session_cost || m2.momentum.avg_session_cost) {
        rows += '<tr><th scope="row">Avg cost / session</th><td>' + momentumOptCol(m1, 'avg_session_cost') + '</td><td>' + momentumOptCol(m2, 'avg_session_cost') + '</td></tr>';
      }
      if (m1.momentum.avg_session_tokens || m2.momentum.avg_session_tokens) {
        rows += '<tr><th scope="row">Avg tokens / session</th><td>' + momentumOptCol(m1, 'avg_session_tokens') + '</td><td>' + momentumOptCol(m2, 'avg_session_tokens') + '</td></tr>';
      }
    } else {
      rows += '<tr><td colspan="3" class="cmp-muted">Neither model has OpenCode usage rows yet.</td></tr>';
    }
    return '<section class="cmp-section" id="momentum">' +
      '<h2 class="cmp-section__title">Momentum <span class="cmp-section__title-note">OpenCode usage, last 2 mo</span></h2>' +
      '<div class="cmp-tablewrap"><table class="cmp-table">' +
        '<thead><tr><th scope="col"><span class="visually-hidden">Metric</span></th><th scope="col">' + esc(m1.name) + '</th><th scope="col">' + esc(m2.name) + '</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table></div>' +
    '</section>';
  }

  function verdictHtml(k1, k2) {
    var c = findComparison(k1, k2);
    var body;
    if (c) {
      body = c.verdict + '<p class="cmp-src">Read the full <a href="' + esc(c.url) + '">comparison page</a>.</p>';
    } else {
      var rel = relatedFor(k1, k2);
      var chips = '';
      for (var i = 0; i < rel.length; i++) chips += chip(rel[i]);
      body = '<p class="cmp-muted">We haven&rsquo;t written an editorial verdict for this pairing yet.</p>' +
        '<div class="cmp-related">' + chips + '</div>';
    }
    return '<section class="cmp-verdict"><h2 class="cmp-section__title" id="verdict">Verdict</h2>' + body + '</section>';
  }

  function relatedHtml(k1, k2) {
    var rel = relatedFor(k1, k2);
    var chips = '';
    for (var i = 0; i < rel.length; i++) chips += chip(rel[i]);
    return '<section class="cmp-section" id="related">' +
      '<h2 class="cmp-section__title">Related comparisons</h2>' +
      '<div class="cmp-related">' + chips + '</div>' +
    '</section>';
  }

  function srcHtml() {
    return '<p class="cmp-src">Specs, scores, and pricing collected ' + esc(payload.collected) + ' from <a href="https://opencode.ai/data" target="_blank" rel="noopener">opencode.ai/data</a>; usage figures cover the trailing two months on that platform. Newer catalog entries come from each vendor\'s published model list. List prices move often &mdash; verify with each provider before committing.</p>';
  }

  /* ------------------------------------------------------------------ */
  /* Radar SVG (ported verbatim from the former inline script)           */
  /* ------------------------------------------------------------------ */

  var NS = 'http://www.w3.org/2000/svg';
  var W = 560, H = 480, cx = W / 2, cy = H / 2, R = 168;
  function axis(i, f) { var ang = (Math.PI / 180) * (-90 + i * 60); return [cx + Math.cos(ang) * R * f, cy + Math.sin(ang) * R * f]; }
  function attrs(node, map) { for (var k in map) node.setAttribute(k, map[k]); return node; }

  function buildRadar(m1, m2) {
    var mount = document.getElementById('cmp-radar');
    if (!mount) return;
    var dims = payload.dimensions;
    var names = [m1.name, m2.name];
    var models = [m1, m2];
    var tones = ['--m1', '--m2'];

    var svg = attrs(document.createElementNS(NS, 'svg'), {
      viewBox: '0 0 ' + W + ' ' + H,
      'class': 'cmp-radar__svg',
      role: 'img',
      'aria-label': 'Capability radar: ' + names[0] + ' vs ' + names[1]
    });

    [0.25, 0.5, 0.75, 1].forEach(function (f) {
      var pts = [];
      for (var i = 0; i < 6; i++) { var p = axis(i, f); pts.push(p[0].toFixed(1) + ',' + p[1].toFixed(1)); }
      svg.appendChild(attrs(document.createElementNS(NS, 'polygon'), {
        points: pts.join(' '),
        'class': 'cmp-radar__ring' + (f === 1 ? ' cmp-radar__ring--outer' : '')
      }));
    });
    for (var s = 0; s < 6; s++) {
      var p = axis(s, 1);
      svg.appendChild(attrs(document.createElementNS(NS, 'line'), {
        x1: cx, y1: cy, x2: p[0].toFixed(1), y2: p[1].toFixed(1),
        'class': 'cmp-radar__spoke'
      }));
    }

    [0, 1].forEach(function (mi) {
      var tone = tones[mi];
      var pts = [];
      for (var i = 0; i < 6; i++) {
        var v = models[mi].scores[dims[i].key];
        pts.push(axis(i, (v || 0) / 100));
      }
      svg.appendChild(attrs(document.createElementNS(NS, 'polygon'), {
        points: pts.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' '),
        'class': 'cmp-radar__area cmp-radar__area' + tone
      }));
      pts.forEach(function (p, i) {
        var v = models[mi].scores[dims[i].key];
        var c = attrs(document.createElementNS(NS, 'circle'), {
          cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 3.4,
          'class': 'cmp-radar__dot cmp-radar__dot' + tone
        });
        var t = document.createElementNS(NS, 'title');
        t.textContent = names[mi] + ' \u2014 ' + dims[i].label + ': ' + (v == null ? 'no data (plots 0)' : v + '/100');
        c.appendChild(t);
        svg.appendChild(c);
      });
    });

    for (var i = 0; i < 6; i++) {
      var lp = axis(i, 1.16);
      var anchor = (i === 0 || i === 3) ? 'middle' : (i < 3 ? 'start' : 'end');
      var dy = (i === 0) ? -2 : (i === 3 ? 16 : 5);
      var txt = attrs(document.createElementNS(NS, 'text'), {
        x: lp[0].toFixed(1), y: (lp[1] + dy).toFixed(1),
        'text-anchor': anchor, 'class': 'cmp-radar__label'
      });
      txt.textContent = dims[i].label;
      svg.appendChild(txt);
    }

    mount.appendChild(svg);
    mount.hidden = false;
  }

  /* ------------------------------------------------------------------ */
  /* Extras attached to the effective pair, whatever rendered it         */
  /* ------------------------------------------------------------------ */

  function attachToggle() {
    var btn = root.querySelector('.cmp-highlight-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var on = root.getAttribute('data-highlight') === 'on';
      root.setAttribute('data-highlight', on ? 'off' : 'on');
      btn.setAttribute('aria-pressed', on ? 'false' : 'true');
      btn.textContent = on ? 'Highlight best' : 'Hide highlights';
    });
  }

  function applyExtras(m1, m2) {
    attachToggle();
    buildRadar(m1, m2);
  }

  /* ------------------------------------------------------------------ */
  /* Pair resolution and rendering                                       */
  /* ------------------------------------------------------------------ */

  function pairFromSearch() {
    var k1 = param('a'), k2 = param('b');
    if (!payload || !payload.models[k1] || !payload.models[k2] || k1 === k2) return null;
    if (!isListed(k1) || !isListed(k2)) return null;
    return [k1, k2];
  }

  function renderPair(k1, k2) {
    var m1 = payload.models[k1], m2 = payload.models[k2];
    root.innerHTML =
      heroHtml(m1, m2) +
      radarHtml(m1, m2) +
      scoresHtml(m1, m2) +
      overviewHtml(m1, m2) +
      pricingHtml(m1, m2) +
      extHtml(k1, k2, m1, m2) +
      momentumHtml(m1, m2) +
      verdictHtml(k1, k2) +
      relatedHtml(k1, k2) +
      srcHtml();
    root.hidden = false;
    a.value = k1;
    b.value = k2;
    setHint('');
    document.title = m1.name + ' vs ' + m2.name + ' - The AI Metric';
    history.replaceState(null, '', pageUrl + '?a=' + encodeURIComponent(k1) + '&b=' + encodeURIComponent(k2));
    applyExtras(m1, m2);
  }

  /* Payload loaded: render the pending request, else resolve the effective
   * pair (valid URL pair beats the SSR pair). Pages whose server-rendered
   * pair already stands only need the radar build and toggle listener. */
  function onPayload() {
    if (isHub) { a.disabled = false; b.disabled = false; }
    if (pending) {
      var p = pending;
      pending = null;
      if (p[0] && p[1] && payload.models[p[0]] && payload.models[p[1]] && p[0] !== p[1]) {
        renderPair(p[0], p[1]);
      } else if (p[0] === p[1] && p[0] && payload.models[p[0]]) {
        setHint('Pick two different models.');
      } else {
        setHint('Pick two models to compare.');
      }
      return;
    }
    var target = pairFromSearch();
    if (target && (isHub || !ssrPair || target[0] !== ssrPair[0] || target[1] !== ssrPair[1])) {
      renderPair(target[0], target[1]);
      return;
    }
    if (!isHub && ssrPair) {
      applyExtras(payload.models[ssrPair[0]], payload.models[ssrPair[1]]);
      setHint('');
      return;
    }
    setHint('Pick two models to compare.');
  }

  function onSelection() {
    var k1 = a.value, k2 = b.value;
    if (!payload) {
      if (k1 || k2) {
        pending = [k1, k2];
        setHint('Loading model data…');
      }
      return;
    }
    if (!k1 || !k2 || !payload.models[k1] || !payload.models[k2] || !isListed(k1) || !isListed(k2)) {
      setHint('Pick two models to compare.');
      return;
    }
    if (k1 === k2) {
      setHint('Pick two different models.');
      return;
    }
    renderPair(k1, k2);
  }

  function boot() {
    if (!a || !b || !swap) return;
    /* Highlight toggle is pure DOM: attach before the payload so SSR pages
     * keep working even when the fetch fails. */
    attachToggle();
    a.addEventListener('change', onSelection);
    b.addEventListener('change', onSelection);
    swap.addEventListener('click', function () {
      var t = a.value; a.value = b.value; b.value = t;
      onSelection();
    });
    fetch(root.getAttribute('data-payload'))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        payload = data;
        onPayload();
      })
      .catch(function () {
        setHint('Could not load model data — browse the published comparisons below.');
        /* Hub selects stay disabled; comparison pages keep their SSR content. */
      });
  }

  boot();
})();
