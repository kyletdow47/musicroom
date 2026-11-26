/* wall_music.js — music on the wall.
 *
 * The wall is one mind map. Albums, artists, labels and genres are blocks on it (type "node") like any other, so
 * you can move them, put them on a layer, and join them to your own words, quotes, images and videos with lines.
 *
 * Starting: Music in the + menu — the song playing now, one of your liked songs, or an album you type. That puts a
 * record on the wall with its neighbours around it. Growing: click an album or an artist and its neighbours appear
 * around it, kept clear of everything already there. Hearing and keeping: select a node for thirty-second previews,
 * and (for an album) a button that finds it on Spotify and puts its songs in My Room.
 *
 * Two halves:
 *   WallMusic.pure   planning and placing, with no page needed (this is what the tests run)
 *   WallMusic.attach the panels and the clicks, joined to a mounted wall (room_canvas.js calls it)
 *
 * The data comes from the app's own /api/connections (Deezer), /api/spotify/* and /api/find/*.
 */
(function (root) {
  'use strict';

  var CG = root.ConnGraph, CS = root.ConnStart, RA = root.RoomAdd;
  var CAPS = { labels: 4, genres: 4, albums: 6, related: 8, credits: 6 };           // per group, per growth (as in ConnGraph)
  var DEPTH = { albums: 0, credits: 0, related: -1, labels: -2, genres: -2 };        // context sits farther back than the record itself
  var KINDS = { album: 'album', artist: 'artist', label: 'label', genre: 'genre' };

  // ── pure: what a web becomes ──────────────────────────────────────────────
  var isTag = function (kind) { return kind === 'label' || kind === 'genre'; };
  function identOf(d) { d = d || {}; return d.kind + ':' + (isTag(d.kind) ? String(d.name || '').toLowerCase() : d.rid); }

  // a node as the server describes it -> what a music block holds
  function specOf(n) {
    if (!n || !KINDS[n.type] || !String(n.name || '').trim()) return null;
    if (!isTag(n.type) && !(+n.id > 0)) return null;
    return { kind: n.type, rid: isTag(n.type) ? null : +n.id, name: String(n.name).trim().slice(0, 200), sub: String(n.sub || '').slice(0, 200),
             image: /^https:\/\//.test(n.image || '') ? n.image : '', year: /^\d{4}$/.test(n.year || '') ? n.year : '',
             fans: typeof n.fans === 'number' ? n.fans : null, role: String(n.role || '').slice(0, 60), url: String(n.url || ''), deezer: String(n.deezer || ''),
             tracks: n.tracks || null, albums: n.albums || null, grown: false };
  }

  // the record in the middle and the neighbours to put round it (capped per group, so a click never floods the wall)
  function planWeb(web, caps) {
    caps = Object.assign({}, CAPS, caps || {});
    var center = specOf(web && web.center); if (!center) return null;
    var items = [];
    ((web && web.groups) || []).forEach(function (g) {
      var cap = caps[g.key] != null ? caps[g.key] : 6, depth = DEPTH[g.key] != null ? DEPTH[g.key] : 0;
      (g.nodes || []).slice(0, cap).forEach(function (n) { var spec = specOf(n); if (spec) items.push({ spec: spec, group: g.key, d: depth }); });
    });
    return { center: center, items: items };
  }

  // how big a music block is, by what it holds
  function sizeOf(data) {
    if (isTag(data.kind)) return { w: 160, h: 46 };
    return data.home ? { w: 164, h: 150 } : { w: 148, h: 128 };
  }
  var radiusOf = function (b) { return Math.max(b.w, b.h) / 2 + 14; };

  // Where do new blocks go? Round the one they grew from, pushed apart from each other and from everything already
  // on the wall (which does not move). `existing` = [{id, x, y, r}] (middles), `news` = [{id, r}].
  // Returns { id: {x, y} } (middles). `outward` is the direction to prefer (radians): away from what is already round the parent.
  function settle(existing, parentId, news, opts) {
    opts = opts || {};
    var g = CG.create(opts.seed || 5); g.noGravity = true; var put = function (id, x, y, r, pinned) { var n = { key: id, type: 'artist', x: x, y: y, vx: 0, vy: 0, r: r, pinned: !!pinned }; g.nodes.set(id, n); return n; };
    existing.forEach(function (e) { put(e.id, e.x, e.y, e.r, true); });
    var parent = g.nodes.get(parentId); if (!parent) return {};
    var count = news.length, out = opts.outward || 0;
    // They fan out over an arc facing `outward`, never all the way round (the far side is where the parent's other neighbours are).
    // The ring is as wide as it must be for that many blocks to fit on that arc without touching.
    var arc = Math.min(1.6 * Math.PI, 0.62 * count), gap = count > 1 ? arc / (count - 1) : arc;
    var rMax = news.reduce(function (m, n) { return Math.max(m, n.r); }, 60);
    var ring = Math.max(opts.ring || 250, count > 1 ? 1.12 * (rMax + 15) / Math.sin(Math.min(gap, Math.PI) / 2) : 0, parent.r + rMax + 40);
    news.forEach(function (n, i) {
      var a = count === 1 ? out : out - arc / 2 + arc * i / (count - 1);
      var nn = put(n.id, parent.x + Math.cos(a) * ring, parent.y + Math.sin(a) * ring, n.r, false);
      g.edges.push({ a: parent, b: nn, rest: ring });
    });
    var alpha = 1; for (var i = 0; i < 280; i++) { CG.tick(g, Math.max(alpha, 0.3)); alpha *= 0.985; }
    var res = {}; news.forEach(function (n) { var p = g.nodes.get(n.id); res[n.id] = { x: p.x, y: p.y }; });
    return res;
  }

  // A free place for something new near `mid`: nothing already on the wall (rects = [{x, y, w, h}]) within `pad` of a
  // footprint of this size. It looks outward in rings; null if the wall is too full to find room.
  function freeSpot(rects, mid, size, pad) {
    pad = pad == null ? 40 : pad;
    var hw = size.w / 2 + pad, hh = size.h / 2 + pad;
    var clash = function (x, y) { return rects.some(function (r) { return Math.abs(x - (r.x + r.w / 2)) < hw + r.w / 2 && Math.abs(y - (r.y + r.h / 2)) < hh + r.h / 2; }); };
    if (!clash(mid.x, mid.y)) return { x: mid.x, y: mid.y };
    for (var ring = 1; ring <= 30; ring++) {
      var rad = ring * 140, n = Math.max(8, ring * 8);
      for (var i = 0; i < n; i++) { var a = i * 2 * Math.PI / n, x = mid.x + Math.cos(a) * rad, y = mid.y + Math.sin(a) * rad; if (!clash(x, y)) return { x: x, y: y }; }
    }
    return null;
  }

  // the direction away from a node's existing neighbours (so growth fans out into free space)
  function outwardFrom(parent, neighbours) {
    if (!neighbours.length) return -Math.PI / 4;
    var cx = 0, cy = 0; neighbours.forEach(function (n) { cx += n.x; cy += n.y; }); cx /= neighbours.length; cy /= neighbours.length;
    var dx = parent.x - cx, dy = parent.y - cy; return (Math.abs(dx) + Math.abs(dy) < 1) ? -Math.PI / 4 : Math.atan2(dy, dx);
  }

  // an old frozen Web block's picture -> music nodes. `web` = { nodes:[{type,id,name,sub,image,year,x,y}], edges:[[i,j]], home }.
  // Positions are kept (shifted to sit where the block was, a little smaller), and its joins become suggestions.
  function unpackPlan(web, mid, scale) {
    var nodes = (web && Array.isArray(web.nodes)) ? web.nodes : [], s = scale || 0.9, out = [], idx = {};
    var okN = nodes.map(function (n) { return n && isFinite(n.x) && isFinite(n.y) ? n : null; }).filter(Boolean);
    if (!okN.length) return { nodes: [], edges: [] };
    var cx = 0, cy = 0; okN.forEach(function (n) { cx += n.x; cy += n.y; }); cx /= okN.length; cy /= okN.length;
    nodes.forEach(function (n, i) {
      var spec = n && isFinite(n.x) && isFinite(n.y) ? specOf(n) : null; if (!spec) return;
      if (i === web.home) spec.home = true;
      idx[i] = out.length; out.push({ spec: spec, x: mid.x + (n.x - cx) * s, y: mid.y + (n.y - cy) * s, home: i === web.home });
    });
    var edges = []; (Array.isArray(web.edges) ? web.edges : []).forEach(function (e) { if (Array.isArray(e) && idx[e[0]] != null && idx[e[1]] != null && e[0] !== e[1]) edges.push([idx[e[0]], idx[e[1]]]); });
    return { nodes: out, edges: edges };
  }

  // What a node's record page shows, worked out from the wall: its facts (the labels and genres joined to it count as facts),
  // the things of YOUR OWN joined to it (words, quotes, links, pictures, videos), and the other music near it.
  function recordModel(b, blocks, links) {
    var d = (b && b.data) || {}, by = {}; (blocks || []).forEach(function (x) { if (x && x.id) by[x.id] = x; });
    var labels = [], genres = [], near = [], mine = [], seen = {};
    (links || []).forEach(function (l) {
      if (!l || (l.a !== b.id && l.b !== b.id)) return;
      var o = by[l.a === b.id ? l.b : l.a]; if (!o || o.id === b.id || seen[o.id]) return; seen[o.id] = 1;
      var od = o.data || {};
      if (o.type === 'node') {
        if (!od.kind) return;                                           // a music block with nothing in it is not a neighbour
        if (od.kind === 'label') labels.push(od.name); else if (od.kind === 'genre') genres.push(od.name);
        else near.push({ id: o.id, n: o.n || 0, name: od.name || '', kind: od.kind, line: l.label || '', mine: l.kind === 'mine' });
      } else mine.push({ id: o.id, n: o.n || 0, type: o.type, data: od, line: l.label || '' });
    });
    var byN = function (x, y) { return x.n - y.n; }; near.sort(byN); mine.sort(byN);
    var facts = d.kind === 'album'
      ? [['Artist', d.sub], ['Year', d.year], ['Songs', d.tracks ? String(d.tracks) : ''], ['Label', labels.join(', ')], ['Genres', genres.join(', ')]]
      : [['Fans', d.fans != null ? String(d.fans) : ''], ['Records', d.albums ? String(d.albums) : ''], ['Label', labels.join(', ')], ['Genres', genres.join(', ')]];
    return { id: b.id, n: b.n || 0, kind: d.kind, title: d.name || '', image: d.image || '', facts: facts.filter(function (f) { return f[1]; }), mine: mine, near: near,
             song: d.song || null, spotify: d.url || '', deezer: d.deezer || '' };
  }
  // the records you can step between with the arrow keys: albums and artists, in the order they were numbered
  function recordOrder(blocks) {
    return (blocks || []).filter(function (b) { return b && b.type === 'node' && b.data && (b.data.kind === 'album' || b.data.kind === 'artist'); })
      .sort(function (x, y) { return (x.n || 0) - (y.n || 0); }).map(function (b) { return b.id; });
  }
  function stepRecord(order, id, dir) {
    var i = order.indexOf(id); if (i < 0 || !order.length) return order[0] || null;
    return order[(i + dir + order.length) % order.length];
  }

  var pure = { recordModel: recordModel, recordOrder: recordOrder, stepRecord: stepRecord, freeSpot: freeSpot, identOf: identOf, specOf: specOf, planWeb: planWeb, sizeOf: sizeOf, radiusOf: radiusOf, settle: settle, outwardFrom: outwardFrom, unpackPlan: unpackPlan, CAPS: CAPS, DEPTH: DEPTH };

  // ── the page half ─────────────────────────────────────────────────────────
  var api = { pure: pure, attach: function () {}, openStart: function () {}, unpack: function () {} };
  if (typeof document === 'undefined') { if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.WallMusic = api; return; }

  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); };
  var fansText = function (n) { return n == null ? '' : (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1).replace('.0', '') + 'k' : String(n)) + ' fans'; };
  var clock = function (t) { t = Math.round(t || 0); return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0'); };

  api.attach = function (wall) {
    var host = wall.host, LinkOps = root.MRRoomCanvas.LinkOps, room, sel = null;
    var start = document.createElement('div'), info = document.createElement('div');
    start.className = 'rc-music'; info.className = 'rc-info'; host.appendChild(start); host.appendChild(info);
    var status = host.querySelector('#rc-status'), say = function (t) { if (status) status.textContent = t || ''; };
    var centreOf = function (b) { return { x: b.x + b.w / 2, y: b.y + b.h / 2 }; };
    var focusOn = function (b) { if (wall.flyTo) wall.flyTo(b); else { var c = centreOf(b); wall.panTo(c.x, c.y); } };          // (the Void flies there; the old wall pans)
    var nodeBlock = function (b) { return b && b.type === 'node' && b.data; };

    async function fetchWeb(q) {
      var r = await fetch('/api/connections?' + new URLSearchParams(q)), d = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(d.error || 'Could not look that up.');
      return d;
    }

    // put a web on the wall: the record in the middle (reused if it is already there), its neighbours round it
    function placeWeb(web, o) {
      o = o || {};
      var plan = planWeb(web); if (!plan) throw new Error('Nothing to put on the wall.');
      var find = function (spec) { return wall.blocks.find(function (b) { return nodeBlock(b) && identOf(b.data) === identOf(spec); }); };
      var cb = o.anchor || find(plan.center), mid;
      var nearTo = function (anchor) { return wall.nearBlocks ? wall.nearBlocks(anchor || null) : wall.blocks; };   // (in the Void: only what is at about that depth)
      if (cb) mid = centreOf(cb);
      else {                                                           // a new record goes where the person is looking, but not on top of what is there (room for its neighbours too)
        var want = o.at || wall.viewCentreWorld(), rects = nearTo(null).map(function (b) { return { x: b.x, y: b.y, w: b.w, h: b.h }; });
        mid = freeSpot(rects, want, { w: 560, h: 520 }) || freeSpot(rects, want, { w: 200, h: 180 }) || want;
      }
      if (!cb) { var sz = sizeOf(Object.assign({}, plan.center, { home: !!o.home })); cb = wall.addNode('node', Object.assign({}, plan.center, { home: !!o.home }), mid, 0, sz); }
      cb.data.grown = true; if (o.home) { cb.data.home = true; var s2 = sizeOf(cb.data); cb.w = s2.w; cb.h = s2.h; }
      if (web.song) cb.data.song = web.song;
      var fresh = [], pairs = [];
      plan.items.forEach(function (it) {
        var nb = find(it.spec);
        if (!nb) { nb = wall.addNode('node', it.spec, mid, it.d, sizeOf(it.spec), cb.c); fresh.push(nb); }          // (a neighbour belongs to its record's moment)
        if (nb !== cb) pairs.push([cb.id, nb.id]);
      });
      var obst = nearTo(cb).filter(function (b) { return fresh.indexOf(b) < 0; }); if (obst.indexOf(cb) < 0) obst.push(cb);
      var existing = obst.map(function (b) { var c = centreOf(b); return { id: b.id, x: c.x, y: c.y, r: radiusOf(b) }; });
      var around = wall.links.map(function (l) { return l.a === cb.id ? l.b : (l.b === cb.id ? l.a : null); }).filter(Boolean)
        .map(function (id) { var b = wall.blocks.find(function (x) { return x.id === id; }); return b ? centreOf(b) : null; }).filter(Boolean);
      var pos = settle(existing, cb.id, fresh.map(function (b) { return { id: b.id, r: radiusOf(b) }; }), { outward: outwardFrom(centreOf(cb), around), seed: 11 });
      fresh.forEach(function (b) { var p = pos[b.id]; if (p) { b.x = wall.snap(p.x - b.w / 2); b.y = wall.snap(p.y - b.h / 2); } });
      var ls = wall.links; pairs.forEach(function (p) { ls = LinkOps.add(ls, p[0], p[1], 'near').links; }); wall.setLinks(ls);
      wall.changed();
      return { center: cb, fresh: fresh };
    }

    // ── growing: click a record or an artist ──
    async function grow(b) {
      var d = nodeBlock(b); if (!d || d.grown || isTag(d.kind)) return;
      var el = host.querySelector('.rc-block[data-id="' + b.id + '"]'); if (el) el.classList.add('busy'); say('Looking around ' + d.name + '…');
      try { var web = await fetchWeb({ kind: d.kind, id: d.rid }); var r = placeWeb(web, { anchor: b }); say(r.fresh.length ? '' : d.name + ' has nothing more to add.'); if (r.fresh.length && wall.flyToGroup) wall.flyToGroup([b].concat(r.fresh)); }
      catch (e) { say(e.message); }
      if (el) el.classList.remove('busy');
      if (sel === b.id) showInfo(b);
    }
    wall.on('click', function (b) {
      var d = nodeBlock(b); if (!d) return;
      if (isTag(d.kind)) { if (d.kind === 'label') window.open('https://www.discogs.com/search/?q=' + encodeURIComponent(d.name) + '&type=label', '_blank', 'noopener'); return; }
      grow(b);
    });
    var shown = null;                                                    // (the wall re-announces the selection on every redraw: rebuild the card only for a real change)
    wall.on('select', function (b) {
      var id = b ? b.id : null; sel = id; if (id === shown) return; shown = id;
      if (nodeBlock(b) && !isTag(b.data.kind)) showInfo(b); else hideInfo();
    });

    // ── the quick card for a selected record or artist, and its full page (the record) ──
    // Both show the same songs and the same "Add to My Room", so those are drawn into whichever is open (see data-prev / data-add).
    var previews = {}, adds = {}, waiting = '', audio = new Audio(), playing = null; audio.preload = 'none';
    var rec = document.createElement('div'); rec.className = 'rc-page'; host.appendChild(rec);
    var recId = null, pad = function (n) { return n > 0 ? String(n).padStart(3, '0') : ''; };
    var panes = function () { return [info, rec]; };
    var activeRoot = function () { return rec.classList.contains('on') ? rec : info; };
    var blockById = function (id) { return wall.blocks.find(function (x) { return x.id === id; }); };
    var safeUrl = function (u) { return /^https?:\/\//.test(u || '') ? u : ''; };
    room = RA.create(null, { onWait: function (t) { waiting = t; var b = blockById(sel); if (b && addOf(b).state === 'finding') renderAdd(b); } });
    var addOf = function (b) { return adds[b.id] || (adds[b.id] = { state: 'idle' }); };
    function hideInfo() { stopPreview(); info.classList.remove('on'); info.innerHTML = ''; }
    function showInfo(b) {
      var d = b.data; info.classList.add('on');
      var meta = d.kind === 'album' ? [d.sub, d.year, d.tracks ? d.tracks + ' songs' : ''].filter(Boolean).join(' · ') : [d.role, fansText(d.fans), d.albums ? d.albums + ' records' : ''].filter(Boolean).join(' · ');
      info.innerHTML = '<button class="rc-info__x" data-a="close" title="Close">×</button>' + (d.image ? '<img src="' + esc(d.image) + '" alt="">' : '') +
        '<h2>' + esc(d.name) + '</h2><p>' + esc(meta) + '</p>' + (d.song ? '<p class="rc-info__song">Started from the song “' + esc(d.song.title) + '”.</p>' : '') +
        '<div class="rc-info__row"><button data-a="openrec">Open record</button>' + (d.grown ? '' : '<button data-a="grow">Grow from here</button>') + (d.url ? '<a href="' + esc(d.url) + '" target="_blank" rel="noopener">Spotify ↗</a>' : '') +
        (d.deezer ? '<a href="' + esc(d.deezer) + '" target="_blank" rel="noopener">Deezer ↗</a>' : '') + '</div>' +
        '<div class="rc-info__prev" data-prev></div>' + (d.kind === 'album' ? '<div class="rc-info__add" data-add></div>' : '');
      loadPreview(b); if (d.kind === 'album') renderAdd(b);
    }
    function stopPreview() { audio.pause(); playing = null; host.querySelectorAll('.rc-song button.on').forEach(function (x) { x.classList.remove('on'); x.textContent = '▶'; }); host.querySelectorAll('.rc-bar i').forEach(function (i) { i.style.width = '0'; }); }
    audio.addEventListener('ended', stopPreview);
    audio.addEventListener('timeupdate', function () { if (!playing || !audio.duration) return; var i = activeRoot().querySelector('.rc-song[data-i="' + playing.i + '"] .rc-bar i'); if (i) i.style.width = (audio.currentTime / audio.duration * 100) + '%'; });
    window.addEventListener('pagehide', stopPreview);

    async function loadPreview(b) {
      var d = b.data, key = d.kind + ':' + d.rid, boxes = function () { return panes().map(function (p) { return p.querySelector('[data-prev]'); }).filter(Boolean); };
      if (!previews[key]) {
        boxes().forEach(function (box) { box.innerHTML = '<h4>Listen</h4><p class="rc-info__msg">Finding songs…</p>'; });
        var res; try { var r = await fetch('/api/connections/preview?' + new URLSearchParams({ kind: d.kind, id: d.rid })); res = await r.json(); if (!r.ok) res = { items: [], note: res.error || '' }; } catch (e) { res = { items: [], note: 'Could not load the songs.' }; }
        previews[key] = res;
      }
      if (sel !== b.id) return;
      var pv = previews[key], items = pv.items || [];
      var mine = function (t) { return d.song && root.RoomAdd.fold(t.title) === root.RoomAdd.fold(d.song.title); };
      var html = '<h4>Listen · 30 seconds each</h4>' + (items.length ? items.map(function (t, i) {
        return '<div class="rc-song' + (mine(t) ? ' mine' : '') + '" data-i="' + i + '"><button data-a="play" data-i="' + i + '"' + (t.preview ? '' : ' disabled title="No preview for this one"') + '>▶</button><span><b>' + esc(t.title) + '</b><small>' + esc(t.sub || '') + '</small><div class="rc-bar"><i></i></div></span><small>' + (t.duration_sec ? clock(t.duration_sec) : '') + '</small></div>';
      }).join('') : '<p class="rc-info__msg">' + esc(pv.note || 'Nothing to hear.') + '</p>') + (pv.note && items.length ? '<p class="rc-info__msg">' + esc(pv.note) + '</p>' : '');
      boxes().forEach(function (box) { box.innerHTML = html; });
      if (playing && playing.id === b.id) { var pb = activeRoot().querySelector('.rc-song[data-i="' + playing.i + '"] button'); if (pb) { pb.classList.add('on'); pb.textContent = '❚❚'; } }
    }
    async function togglePlay(b, i) {
      var pv = previews[b.data.kind + ':' + b.data.rid], t = pv && pv.items && pv.items[i]; if (!t || !t.preview) return;
      if (playing && playing.id === b.id && playing.i === i) { stopPreview(); return; }
      stopPreview(); fetch('/api/seinnteoir/pause', { method: 'POST' }).catch(function () {});        // never play over the app's own player
      audio.src = t.preview; playing = { id: b.id, i: i };
      var btn = activeRoot().querySelector('.rc-song[data-i="' + i + '"] button'); if (btn) { btn.classList.add('on'); btn.textContent = '❚❚'; }
      try { await audio.play(); } catch (e) { stopPreview(); say('The preview would not play.'); }
    }

    function renderAdd(b) { panes().forEach(function (p) { var box = p.querySelector('[data-add]'); if (box) drawAdd(box, b); }); }
    function drawAdd(box, b) {
      var a = addOf(b), d = b.data, link = '<a href="https://open.spotify.com/search/' + encodeURIComponent((d.sub + ' ' + d.name).trim()) + '/albums" target="_blank" rel="noopener">search Spotify</a>';
      if (a.state === 'idle') box.innerHTML = '<div class="rc-info__row"><button class="go" data-a="addstart">Add to My Room</button></div>';
      else if (a.state === 'finding') box.innerHTML = '<p>' + esc(waiting || 'Finding it on Spotify…') + '</p>';
      else if (a.state === 'error') box.innerHTML = '<p class="err">' + esc(a.msg) + '</p><p>' + (a.notConnected ? '<a href="/spotify">Connect Spotify</a>' : link) + '</p><div class="rc-info__row"><button data-a="addstart">Try again</button></div>';
      else if (a.state === 'confirm') {
        var m = a.match;
        box.innerHTML = '<h4>Add this to My Room?</h4><p><span class="rc-info__t">' + esc(m.title) + '</span><br>' + esc([m.subtitle, m.year, m.tracks ? m.tracks + ' songs' : ''].filter(Boolean).join(' · ')) +
          (a.whole ? '<br>Spotify would not list the songs, so it goes in as one record.' : '<br>' + a.items.length + ' songs.') + '</p>' +
          '<select class="rc-g"><option value="">A new grúpa…</option>' + a.grupas.map(function (g) { return '<option value="' + g.id + '"' + (g.id === a.pick ? ' selected' : '') + '>' + esc(g.name) + (g.has ? ' (already has it)' : '') + '</option>'; }).join('') + '</select>' +
          '<input type="text" class="rc-gn" value="' + esc(m.title) + '"' + (a.pick ? ' hidden' : '') + '><div class="rc-info__row"><button class="go" data-a="addgo">Add</button><button data-a="addno">Not this one</button></div>';
        var g = box.querySelector('.rc-g'); if (g) g.onchange = function () { box.querySelector('.rc-gn').hidden = !!g.value; };
      } else if (a.state === 'adding') box.innerHTML = '<p>Adding…</p>';
      else if (a.state === 'done') box.innerHTML = '<p>' + (a.added ? 'Added ' + a.added + (a.added === 1 ? ' song' : ' songs') + ' to My Room.' : 'Already in My Room.') + ' <a href="/room?grupa=' + a.grupaId + '">Open it →</a></p>';
    }
    async function addStart(b) {
      var a = addOf(b); a.state = 'finding'; renderAdd(b);
      var m = await room.match(b.data.sub, b.data.name);
      if (m.error) { Object.assign(a, { state: 'error', msg: m.error, notConnected: !!m.notConnected }); if (sel === b.id) renderAdd(b); return; }
      var sg = await room.songs(m.album), list = await room.grupas(sg.items[0] && sg.items[0].url), has = list.find(function (x) { return x.has; });
      Object.assign(a, { state: 'confirm', match: m.album, items: sg.items, whole: sg.whole, grupas: list, pick: has ? has.id : 0 });
      if (sel === b.id) renderAdd(b);
    }
    async function addGo(b) {
      var a = addOf(b), r0 = activeRoot(), gs = r0.querySelector('.rc-g'), gid = gs ? +gs.value : 0, name = (r0.querySelector('.rc-gn') || {}).value || a.match.title;
      a.state = 'adding'; renderAdd(b);
      var r = await room.add({ items: a.items, grupaId: gid || 0, name: name, note: 'From the wall' });
      if (r.error) Object.assign(a, { state: 'error', msg: r.error }); else Object.assign(a, { state: 'done', grupaId: r.grupaId, added: r.added });
      if (sel === b.id) renderAdd(b);
    }

    // ── the record: a page for one node, in three columns ──
    // left: what is joined to it (your words, quotes, links, pictures, videos) and the music near it; middle: the cover; right: the facts, the songs, and keeping it
    function mineHtml(it) {
      var d = it.data || {}, body;
      if (it.type === 'quote') body = '<p class="rc-page__q">“' + esc(d.text || '') + '”</p><p class="rc-page__by">' + esc([d.who, d.source].filter(Boolean).join(', ')) + (safeUrl(d.url) ? ' <button data-a="openurl" data-url="' + esc(d.url) + '">[ open ]</button>' : '') + '</p>';
      else if (it.type === 'text') body = (d.heading ? '<p class="rc-page__h">' + esc(d.heading) + '</p>' : '') + (d.body ? '<p>' + esc(d.body) + '</p>' : '');
      else if (it.type === 'image') body = /^(https?:\/\/|\/)/.test(d.src || '') ? '<img src="' + esc(d.src) + '" alt="' + esc(d.alt || '') + '">' : '<p>a picture</p>';
      else if (it.type === 'mix' || it.type === 'link') body = '<p class="rc-page__h">' + esc(d.title || d.url || 'a link') + '</p>' + (safeUrl(d.url) ? '<button data-a="openurl" data-url="' + esc(d.url) + '">[ open ]</button>' : '');
      else body = '<p>' + esc(it.type) + '</p>';
      return '<article class="rc-page__item"><span class="rc-page__no">' + pad(it.n) + (it.line ? ' · ' + esc(it.line) : '') + '</span>' + body + '</article>';
    }
    function renderRecord(b) {
      var m = recordModel(b, wall.blocks, wall.links), d = b.data;
      rec.innerHTML = '<div class="rc-page__top"><span class="rc-page__num">' + pad(m.n) + '</span><span class="rc-page__nav"><button data-a="recprev">[ Prev ]</button><button data-a="recnext">[ Next ]</button><button data-a="recclose">[ Close ]</button></span></div>' +
        '<div class="rc-page__cols">' +
        '<section class="rc-page__left"><h3>Linked' + (m.mine.length ? ' <i>' + m.mine.length + '</i>' : '') + '</h3>' +
          (m.mine.length ? m.mine.map(mineHtml).join('') : '<p class="rc-page__none">Nothing of yours is joined to this yet. Draw a line to it from a quote, a note or a video, or add a note here.</p>') +
          '<button class="rc-page__add" data-a="note">[ + Note ]</button><div class="rc-page__note" hidden><textarea rows="4" maxlength="2000" placeholder="what this makes you think of"></textarea><button data-a="notesave">[ Save ]</button></div>' +
          (m.near.length ? '<h3>Near <i>' + m.near.length + '</i></h3>' + m.near.map(function (n) { return '<button class="rc-page__near" data-a="jump" data-id="' + esc(n.id) + '"><span>' + pad(n.n) + '</span>' + esc(n.name) + '</button>'; }).join('') : '') + '</section>' +
        '<section class="rc-page__mid">' + (m.image ? '<img src="' + esc(m.image) + '" alt="' + esc(m.title) + '">' : '<div class="rc-page__ph">' + esc((m.title || '?').charAt(0)) + '</div>') + '</section>' +
        '<section class="rc-page__right"><h2>' + esc(m.title) + '</h2>' + (m.song ? '<p class="rc-page__song">Started from the song “' + esc(m.song.title) + '”.</p>' : '') +
          '<dl class="rc-page__facts">' + m.facts.map(function (f) { return '<div><dt>' + esc(f[0]) + '</dt><dd>' + esc(f[1]) + '</dd></div>'; }).join('') + '</dl>' +
          '<div class="rc-page__prev" data-prev></div>' + (d.kind === 'album' ? '<div class="rc-page__addroom" data-add></div>' : '') +
          '<div class="rc-page__links">' + (m.spotify ? '<a href="' + esc(m.spotify) + '" target="_blank" rel="noopener">[ Spotify ↗ ]</a>' : '') + (m.deezer ? '<a href="' + esc(m.deezer) + '" target="_blank" rel="noopener">[ Deezer ↗ ]</a>' : '') + (d.grown ? '' : '<button data-a="grow">[ Grow ]</button>') + '</div></section></div>';
      loadPreview(b); if (d.kind === 'album') renderAdd(b);
    }
    function openRecord(b) {
      if (!nodeBlock(b) || isTag(b.data.kind)) return;
      recId = b.id; stopPreview(); wall.select(b.id);                                  // (selecting shows the quick card; the page replaces it)
      info.classList.remove('on'); rec.classList.add('on'); renderRecord(b); rec.scrollTop = 0;
    }
    function closeRecord() { var b = blockById(recId); rec.classList.remove('on'); rec.innerHTML = ''; recId = null; stopPreview(); if (b && blockById(sel)) showInfo(b); }
    function stepTo(dir) {
      var next = blockById(stepRecord(recordOrder(wall.blocks), recId, dir)); if (!next) return;
      focusOn(next); openRecord(next);
    }
    function saveNote() {
      var ta = rec.querySelector('.rc-page__note textarea'), b = blockById(recId), text = ta && ta.value.trim(); if (!text || !b) return;
      var c = centreOf(b), rects = wall.blocks.map(function (x) { return { x: x.x, y: x.y, w: x.w, h: x.h }; });
      var spot = freeSpot(rects, { x: c.x + 320, y: c.y }, { w: 340, h: 200 }, 30) || { x: c.x + 320, y: c.y };
      var nb = wall.addNode('text', { heading: '', body: text.slice(0, 2000) }, spot, 0, { w: 320, h: 160 });
      wall.setLinks(LinkOps.add(wall.links, b.id, nb.id, 'mine').links); wall.changed(); renderRecord(b);
    }

    function onAct(e) {
      var btn = e.target.closest('button'); if (!btn || !btn.dataset.a) return;
      var act = btn.dataset.a, b = blockById(sel);
      if (act === 'close') { wall.select(null); return; }
      if (act === 'recclose') { closeRecord(); return; }
      if (act === 'recprev' || act === 'recnext') { stepTo(act === 'recnext' ? 1 : -1); return; }
      if (act === 'jump') { var t = blockById(btn.dataset.id); if (t) { focusOn(t); openRecord(t); } return; }
      if (act === 'note') { var nb = rec.querySelector('.rc-page__note'); if (nb) { nb.hidden = !nb.hidden; if (!nb.hidden) nb.querySelector('textarea').focus(); } return; }
      if (act === 'notesave') { saveNote(); return; }
      if (act === 'openurl') { var u = safeUrl(btn.dataset.url); if (u) fetch('/api/open-url', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: u }) }).catch(function () {}); return; }
      if (!b) return;
      if (act === 'openrec') openRecord(b);
      else if (act === 'grow') grow(b).then(function () { if (rec.classList.contains('on') && recId === b.id) renderRecord(b); });
      else if (act === 'play') togglePlay(b, +btn.dataset.i);
      else if (act === 'addstart') addStart(b); else if (act === 'addgo') addGo(b); else if (act === 'addno') { adds[b.id] = { state: 'idle' }; renderAdd(b); }
    }
    info.addEventListener('click', onAct); rec.addEventListener('click', onAct);
    wall.on('dblclick', function (b) { if (nodeBlock(b) && !isTag(b.data.kind)) openRecord(b); });
    // Esc closes the record (and nothing else); the arrows step between records; Enter opens the quick card's record
    window.addEventListener('keydown', function (e) {
      if (!host.classList.contains('on')) return;
      var typing = e.target.matches && e.target.matches('input, textarea, select, [contenteditable]');
      if (rec.classList.contains('on')) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); closeRecord(); }
        else if (!typing && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { e.preventDefault(); stepTo(e.key === 'ArrowRight' ? 1 : -1); }
      } else if (!typing && e.key === 'Enter' && info.classList.contains('on') && !(e.target.closest && e.target.closest('button, a'))) {
        var b = blockById(sel); if (b) { e.preventDefault(); openRecord(b); }
      }
    }, true);

    // ── starting: the song playing now, a liked song, an album you type ──
    var liked = [], likedMore = true, likedBusy = false, hits = null, startAt = null;
    function openStart(at) {
      startAt = at || null; start.classList.add('on'); hideInfo();
      start.innerHTML = '<button class="rc-info__x" data-s="close" title="Close">×</button><h2>Add music to the map</h2><p class="rc-info__msg">Where do you want to start?</p>' +
        '<div class="rc-info__row"><button class="go" data-s="now">The song playing now</button><button class="go" data-s="liked">One of my liked songs</button><button data-s="type">An album I type in</button></div>' +
        '<p class="rc-info__msg" id="rc-sm"></p><div id="rc-lb" hidden><input type="text" id="rc-lf" placeholder="Filter the liked songs below, or search all of Spotify" autocomplete="off"><div id="rc-ll" class="rc-rows"></div>' +
        '<div class="rc-info__row"><button id="rc-more" data-s="more" hidden>Show more of my liked songs</button><button id="rc-all" data-s="all" hidden>Search all of Spotify</button></div></div>' +
        '<div id="rc-ask" hidden><input type="text" id="rc-aa" placeholder="Artist" autocomplete="off"><input type="text" id="rc-ab" placeholder="Album" autocomplete="off"><div class="rc-info__row"><button class="go" data-s="go">Put it on the wall</button></div></div>';
    }
    api.openStart = openStart;
    var sm = function (t) { var e = start.querySelector('#rc-sm'); if (e) e.textContent = t || ''; };
    async function begin(q) {
      sm('Looking around…');
      try {
        var web = await fetchWeb(q), r = placeWeb(web, { at: startAt, home: true });
        start.classList.remove('on'); say(''); wall.select(r.center.id);
        if (wall.flyToGroup) wall.flyToGroup([r.center].concat(r.fresh)); else focusOn(r.center);          // (in the Void: stand back far enough to see what grew round it)
      } catch (e) { sm(e.message); }
    }
    var rowHtml = function (x, i) { return '<div class="rc-mrow" data-i="' + i + '">' + (x.image ? '<img src="' + esc(x.image) + '" alt="" loading="lazy">' : '<span class="ph"></span>') + '<div><b>' + esc(x.title) + '</b><small>' + esc([x.artist, x.album].filter(Boolean).join(' · ')) + '</small></div></div>'; };
    function drawLiked() {
      var f = (start.querySelector('#rc-lf').value || '').trim().toLowerCase();
      var list = hits || (f ? liked.filter(function (x) { return (x.title + ' ' + x.artist + ' ' + x.album).toLowerCase().indexOf(f) >= 0; }) : liked), ll = start.querySelector('#rc-ll');
      ll.innerHTML = list.length ? list.map(rowHtml).join('') : '<p class="rc-info__msg">' + (f ? 'None of the ' + liked.length + ' liked songs loaded so far match.' : 'No liked songs.') + '</p>'; ll._list = list;
      start.querySelector('#rc-more').hidden = !!hits || !likedMore || !!f; start.querySelector('#rc-all').hidden = !f || f.length < 2;
    }
    async function loadLiked() {
      if (likedBusy) return; likedBusy = true; sm('Loading your liked songs…');
      try {
        var r, d; for (var n = 0; n < 3; n++) { r = await fetch('/api/spotify/saved-tracks?limit=50&offset=' + liked.length); d = await r.json().catch(function () { return {}; }); if (r.ok || r.status === 401) break; await new Promise(function (res) { setTimeout(res, n ? 1500 : 700); }); }
        if (!r.ok) { sm(r.status === 401 ? 'Spotify is not connected. Connect it in Tools → Spotify.' : (d.error || 'Spotify would not list them just now.')); return; }
        liked = liked.concat((d.items || []).map(CS.fromLiked).filter(Boolean)); likedMore = !!d.has_more; sm(liked.length + ' of ' + (d.total || liked.length) + ' liked songs loaded.'); drawLiked();
      } catch (e) { sm('Could not reach Spotify.'); } finally { likedBusy = false; }
    }
    start.addEventListener('input', function (e) { if (e.target.id === 'rc-lf') { hits = null; drawLiked(); } });
    start.addEventListener('click', async function (e) {
      var row = e.target.closest('.rc-mrow');
      if (row) { var song = row.parentElement._list[+row.dataset.i]; if (song) begin(Object.fromEntries(CS.query(song).entries())); return; }
      var btn = e.target.closest('button'); if (!btn) return; var s = btn.dataset.s;
      if (s === 'close') start.classList.remove('on');
      else if (s === 'type') { start.querySelector('#rc-ask').hidden = false; start.querySelector('#rc-lb').hidden = true; start.querySelector('#rc-aa').focus(); }
      else if (s === 'go') { var a = start.querySelector('#rc-aa').value.trim(), b = start.querySelector('#rc-ab').value.trim(); if (a && b) begin({ kind: 'album', artist: a, album: b }); }
      else if (s === 'liked') { start.querySelector('#rc-lb').hidden = false; start.querySelector('#rc-ask').hidden = true; hits = null; if (!liked.length) loadLiked(); else drawLiked(); }
      else if (s === 'more') loadLiked();
      else if (s === 'all') {
        var q = start.querySelector('#rc-lf').value.trim(); sm('Searching Spotify…');
        try { var d = await (await fetch('/api/spotify/search?q=' + encodeURIComponent(q))).json(); if (d.connected === false) { sm('Spotify is not connected. Connect it in Tools → Spotify.'); return; }
          hits = (d.results || []).map(CS.fromSearch).filter(Boolean); sm(hits.length ? 'Songs on Spotify for “' + q + '”:' : 'Nothing found on Spotify for “' + q + '”.'); drawLiked(); } catch (er) { sm('Could not reach Spotify.'); }
      }
      else if (s === 'now') {
        sm('Asking the player…'); var st = null; try { st = await (await fetch('/api/seinnteoir/state')).json(); } catch (er) {}
        var song2 = CS.nowPlaying(st); if (!song2) { sm('Nothing is playing right now. Play a song, or pick one from your liked songs.'); return; }
        sm((song2.playing ? 'Playing on ' : 'Last played on ') + song2.where + ': ' + song2.title + (song2.artist ? ' by ' + song2.artist : '') + '. Looking around…');
        begin(Object.fromEntries(CS.query(song2).entries()));
      }
    });

    // ── an old frozen Web block becomes real nodes on the wall ──
    api.unpack = function (blockId) {
      var wb = wall.blocks.find(function (b) { return b.id === blockId; }); if (!wb || wb.type !== 'web' || !wb.data || !wb.data.web) return;
      var plan = unpackPlan(wb.data.web, centreOf(wb), 0.9), made = [], ls = wall.links;
      plan.nodes.forEach(function (n) {
        var ex = wall.blocks.find(function (b) { return nodeBlock(b) && identOf(b.data) === identOf(n.spec); });
        var b = ex || wall.addNode('node', Object.assign(n.spec, { grown: false, home: n.home }), { x: n.x, y: n.y }, isTag(n.spec.kind) ? -2 : 0, sizeOf(Object.assign({}, n.spec, { home: n.home })));
        if (ex && n.home) ex.data.home = true; made.push(b);
      });
      plan.edges.forEach(function (e) { ls = LinkOps.add(ls, made[e[0]].id, made[e[1]].id, 'near').links; });
      wall.setLinks(ls); wall.removeBlocks([blockId]); wall.changed();
    };

    // ── from an address: ?music=1 opens the panel; ?seed=kind%3Dalbum%26artist%3D… puts that record on the wall ──
    wall.ready.then(function () {
      var q = new URLSearchParams(location.search);
      if (q.get('seed')) { try { startAt = null; begin(Object.fromEntries(new URLSearchParams(q.get('seed')).entries())); } catch (_) {} }
      else if (q.get('music')) openStart(null);
    });
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.WallMusic = api;
})(typeof window !== 'undefined' ? window : this);
