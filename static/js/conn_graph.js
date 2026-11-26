/* conn_graph.js — the growing web behind /connections.
 *
 * Two small, pure parts (no DOM, so they can be tested in node):
 *
 *   ConnGraph.create()            a graph that webs from the server are merged into
 *       g.addWeb(web)             add a record's neighbourhood; a neighbour that is already there is
 *                                 joined to, never doubled, so branches that share an artist connect
 *       g.nodes / g.edges         plain arrays/maps the page draws
 *   ConnGraph.tick(g, alpha)      one step of a force layout: neighbours pull together, everything
 *                                 pushes apart, pinned nodes stay put. Returns the energy left.
 *
 * Nothing here knows about colours or fonts; the page owns the look.
 */
(function (root) {
  'use strict';

  var CAPS = { labels: 4, genres: 4, albums: 6, related: 8, credits: 6 };       // per group, per expansion
  var RADIUS = { artist: 46, album: 46, label: 44, genre: 40 };
  var REST = { artist: 215, album: 195, label: 140, genre: 130 };               // how far a neighbour likes to sit

  function keyOf(n) {
    var named = n.type === 'label' || n.type === 'genre';
    return n.type + ':' + (named ? String(n.name || '').toLowerCase() : n.id);
  }

  // a tiny seeded random, so a layout is repeatable (and testable)
  function rng(seed) {
    var s = seed >>> 0 || 1;
    return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  }

  function create(seed) {
    var rand = rng(seed || 7);
    var g = { nodes: new Map(), edges: [], _edgeKeys: new Set(), rand: rand };

    function node(spec, near) {
      var key = keyOf(spec), n = g.nodes.get(key);
      if (n) {                                                   // already there: keep what we know, fill gaps
        ['image', 'sub', 'year', 'fans', 'url', 'deezer', 'tracks', 'albums', 'kind', 'role'].forEach(function (f) {
          if (n[f] == null || n[f] === '') if (spec[f] != null) n[f] = spec[f];
        });
        return n;
      }
      var a = rand() * Math.PI * 2, d = 60 + rand() * 40;
      n = {
        key: key, type: spec.type, id: spec.id, name: spec.name || '', sub: spec.sub || '', image: spec.image || '',
        year: spec.year || '', fans: spec.fans, url: spec.url || '', deezer: spec.deezer || '', tracks: spec.tracks,
        albums: spec.albums, kind: spec.kind || '', role: spec.role || '',
        x: (near ? near.x : 0) + Math.cos(a) * d, y: (near ? near.y : 0) + Math.sin(a) * d,
        vx: 0, vy: 0, pinned: false, expanded: false
      };
      g.nodes.set(key, n);
      return n;
    }

    function link(a, b) {
      if (a === b) return false;
      var k = a.key < b.key ? a.key + '|' + b.key : b.key + '|' + a.key;
      if (g._edgeKeys.has(k)) return false;
      g._edgeKeys.add(k);
      g.edges.push({ a: a, b: b });
      return true;
    }

    // Merge one record's neighbourhood. `web` is the server's {center, groups}. Returns what is new.
    g.addWeb = function (web, opts) {
      var caps = Object.assign({}, CAPS, (opts && opts.caps) || {});
      var c = node(web.center, null);
      var fresh = [], was = g.nodes.size;
      c.expanded = true;
      (web.groups || []).forEach(function (grp) {
        var cap = caps[grp.key] != null ? caps[grp.key] : 6;
        (grp.nodes || []).slice(0, cap).forEach(function (spec) {
          var before = g.nodes.size, n = node(spec, c);
          if (g.nodes.size > before) fresh.push(n.key);
          link(c, n);
        });
      });
      return { center: c.key, added: fresh, total: g.nodes.size, grew: g.nodes.size - was };
    };

    g.neighbours = function (key) {
      var out = [];
      g.edges.forEach(function (e) { if (e.a.key === key) out.push(e.b); else if (e.b.key === key) out.push(e.a); });
      return out;
    };
    g.degree = function (key) { return g.neighbours(key).length; };
    return g;
  }

  // One step of the layout. alpha (1 → 0) is how hard to push; it cools as the web settles.
  function tick(g, alpha) {
    var list = Array.from(g.nodes.values()), i, j, n, m, dx, dy, d2, d, f, energy = 0;
    // everything pushes apart (and never overlaps)
    for (i = 0; i < list.length; i++) {
      n = list[i];
      for (j = i + 1; j < list.length; j++) {
        m = list[j];
        dx = m.x - n.x; dy = m.y - n.y; d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = (g.rand() - .5); dy = (g.rand() - .5); d2 = dx * dx + dy * dy + 0.01; }
        d = Math.sqrt(d2);
        var need = (RADIUS[n.type] || 46) + (RADIUS[m.type] || 46) + 30;
        f = 9000 / d2 * alpha;
        if (d < need) f += (need - d) * 0.35;                           // overlap: shove
        dx /= d; dy /= d;
        if (!n.pinned) { n.vx -= dx * f; n.vy -= dy * f; }
        if (!m.pinned) { m.vx += dx * f; m.vy += dy * f; }
      }
    }
    // neighbours pull toward a comfortable distance
    g.edges.forEach(function (e) {
      dx = e.b.x - e.a.x; dy = e.b.y - e.a.y; d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var rest = Math.max(REST[e.b.type] || 170, REST[e.a.type] || 170) * (e.a.type === 'label' || e.a.type === 'genre' ? 0.9 : 1);
      f = (d - rest) * 0.045 * Math.max(alpha, 0.15);
      dx /= d; dy /= d;
      if (!e.a.pinned) { e.a.vx += dx * f; e.a.vy += dy * f; }
      if (!e.b.pinned) { e.b.vx -= dx * f; e.b.vy -= dy * f; }
    });
    // a faint pull to the middle keeps stray branches from drifting off
    list.forEach(function (n) {
      if (n.pinned) { n.vx = n.vy = 0; return; }
      n.vx -= n.x * 0.004 * alpha; n.vy -= n.y * 0.004 * alpha;
      n.vx *= 0.8; n.vy *= 0.8;
      var sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
      if (sp > 40) { n.vx *= 40 / sp; n.vy *= 40 / sp; }                  // never fling
      n.x += n.vx; n.y += n.vy;
      energy += sp;
    });
    return list.length ? energy / list.length : 0;
  }

  // The box all nodes sit in, for "fit to screen".
  function bounds(g, pad) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    g.nodes.forEach(function (n) {
      var r = (RADIUS[n.type] || 34) + 90;
      x0 = Math.min(x0, n.x - r); y0 = Math.min(y0, n.y - r * .7); x1 = Math.max(x1, n.x + r); y1 = Math.max(y1, n.y + r * .7);
    });
    if (x0 === Infinity) return { x: -200, y: -200, w: 400, h: 400 };
    pad = pad || 0;
    return { x: x0 - pad, y: y0 - pad, w: x1 - x0 + pad * 2, h: y1 - y0 + pad * 2 };
  }

  var api = { create: create, tick: tick, bounds: bounds, keyOf: keyOf, CAPS: CAPS, RADIUS: RADIUS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ConnGraph = api;
})(typeof window !== 'undefined' ? window : this);
