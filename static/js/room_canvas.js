// room_canvas.js — the user's own wall of BLOCKS in their music room.
//
// A block is a box the user makes, drags, resizes and fills: words, a quote
// (with where it came from, and the time in it), an image, one record, a playlist
// (a place, a set, or records picked by hand), a mix (a link that plays in place
// where the site allows it, from a time you choose) or a link. Blocks
// overlap freely (↑/↓ for front/back). Everything is direct manipulation on
// the canvas itself — no form on the side. The layout is saved per room
// (POST /api/room/canvas/<place_id>), images upload to the app's own folder.
//
// Mount: MRRoomCanvas.mount(hostEl, { placeId, getTracks, sntItem, openTrack })
//   getTracks() → the room's records (for picking); sntItem(t) → a player item;
//   openTrack(id) → open that record's detail (may throw if not in this room).
//
// Daithí coins the words. Labels here are functional placeholders.
window.MRRoomCanvas = (function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SNAP = 8, MIN_W = 120, MIN_H = 72;
  const TYPES = {
    text:     { label: 'Words',    hint: 'a heading and some words',             w: 360, h: 200 },
    quote:    { label: 'Quote',    hint: 'words someone said, and where from',   w: 380, h: 240 },
    image:    { label: 'Image',    hint: 'a picture from your computer or a link', w: 320, h: 320 },
    record:   { label: 'Record',   hint: 'one record from your room',              w: 240, h: 310 },
    playlist: { label: 'Playlist', hint: 'a place, a set, or records you pick',    w: 380, h: 380 },
    mix:      { label: 'Mix',      hint: 'a mix or video link that plays here',    w: 440, h: 280 },
    link:     { label: 'Link',     hint: 'any web page, as a card',                w: 320, h: 200 },
    node:     { label: 'Music',    hint: 'an album, artist, label or genre on the map', w: 148, h: 128, keepOnly: true },
    web:      { label: 'Web',      hint: 'a map kept from Connections',            w: 680, h: 500, keepOnly: true },
  };
  const snap = v => Math.max(0, Math.round(v / SNAP) * SNAP);
  const uid = () => 'k' + Math.random().toString(36).slice(2, 9);
  const coverUrl = (t, s) => t && (t.id || t.track_id) ? `/api/artwork/${t.id || t.track_id}?s=${s}&v=${t.art_v || 0}` : '';

  // ── times: "9:19", "1:02:03", "9m19s", "559" ─────────────────────────────
  function fmtClock(sec) {
    if (sec == null || sec === '' || !isFinite(sec) || sec < 0) return '';
    const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const p = n => (n < 10 ? '0' : '') + n;
    return h ? `${h}:${p(m)}:${p(r)}` : `${m}:${p(r)}`;
  }
  function parseClock(str) {
    str = String(str == null ? '' : str).trim().toLowerCase();
    if (!str) return null;
    let m, out = null;
    if ((m = /^(\d{1,3}):(\d{1,2}):(\d{1,2})$/.exec(str))) out = (m[2] < 60 && m[3] < 60) ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) : null;
    else if ((m = /^(\d{1,3}):(\d{1,2})$/.exec(str))) out = (m[2] < 60) ? (+m[1]) * 60 + (+m[2]) : null;
    else if ((m = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$/.exec(str)) && (m[1] || m[2] || m[3])) out = (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0);
    return out != null && out <= 360000 ? out : null;
  }
  // the time a link already carries: ?t=559 · ?t=9m19s · ?start=559 · #t=559s
  function startFromUrl(url) {
    try {
      const u = new URL(url);
      const raw = u.searchParams.get('t') || u.searchParams.get('start') || ((/^#t=(.+)$/.exec(u.hash) || [])[1]);
      return parseClock(raw);
    } catch (_) { return null; }
  }
  // a link that opens at a time (YouTube only; any other address is returned as it was)
  function withStart(url, sec) {
    try {
      const u = new URL(url);
      const h = u.hostname.replace(/^www\.|^m\./, '');
      if (!(h === 'youtube.com' || h === 'youtu.be') || !(sec > 0)) return url;
      u.searchParams.delete('start'); u.searchParams.set('t', String(Math.floor(sec)));
      return u.toString();
    } catch (_) { return url; }
  }
  // a quote's time: the one typed in, else the one its link carries
  const quoteAt = d => (d.at != null ? d.at : (d.url ? startFromUrl(d.url) : null));

  // A music node's inside: an album or artist (a picture and a name), or a label or genre (a small tag). Pure, and escaped.
  // d = { kind: 'album'|'artist'|'label'|'genre', name, sub, image, year, fans, role, home, grown }
  const fansText = n => (n == null ? '' : (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1).replace('.0', '') + 'k' : String(n)) + ' fans');
  function nodeHtml(d) {
    d = d || {};
    const name = String(d.name || '').trim() || '(untitled)';
    if (d.kind === 'label' || d.kind === 'genre') return `<div class="rc-node rc-node--tag rc-node--${d.kind === 'genre' ? 'genre' : 'label'}"><span class="rc-node__tag">${esc(name)}${d.kind === 'label' ? ' ↗' : ''}</span></div>`;
    const meta = d.kind === 'album' ? [d.year, d.sub].filter(Boolean).join(' · ') : (d.role || fansText(d.fans));
    const img = /^https:\/\//.test(d.image || '') ? `<img class="rc-node__im" src="${esc(d.image)}" alt="" draggable="false" loading="lazy">` : `<span class="rc-node__im rc-node__ph">${esc(name.charAt(0))}</span>`;
    return `<div class="rc-node rc-node--${d.kind === 'album' ? 'album' : 'artist'}${d.home ? ' rc-node--home' : ''}">${img}<b class="rc-node__n">${esc(name)}</b>${meta ? `<small class="rc-node__m">${esc(meta)}</small>` : ''}${d.grown ? '' : '<i class="rc-node__more" title="Click to grow its neighbours">+</i>'}</div>`;
  }

  // A kept web's inside, as an SVG drawn from the picture the page took (who sits where). It scales with the
  // block, needs no network to lay out, and everything in it is escaped. `d.web` = { nodes:[{type,name,sub,image,year,x,y}], edges:[[i,j]], home }.
  function webHtml(d) {
    const w = d && d.web, nodes = (w && Array.isArray(w.nodes) ? w.nodes : []).map(n => (n && typeof n === 'object' ? n : null));   // a bad entry keeps its place, so the joins still point the right way
    if (!nodes.some(Boolean)) return '<div class="rc-web__empty">An empty web.</div>';
    const num = v => (typeof v === 'number' && isFinite(v) ? v : 0);
    const S = n => (n === nodes[w.home] ? 84 : (n.type === 'artist' || n.type === 'album' ? 56 : 0));
    const label = n => (n.type === 'label' || n.type === 'genre');
    const halfW = n => (label(n) ? Math.min(110, 10 + String(n.name || '').length * 4.2) : Math.max(S(n) / 2, Math.min(60, String(n.name || '').length * 3.6)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    nodes.forEach(n => { if (!n) return; const hw = halfW(n), hh = label(n) ? 14 : S(n) / 2 + 22; x0 = Math.min(x0, num(n.x) - hw); x1 = Math.max(x1, num(n.x) + hw); y0 = Math.min(y0, num(n.y) - (label(n) ? 14 : S(n) / 2)); y1 = Math.max(y1, num(n.y) + hh); });
    const pad = 24, vb = [x0 - pad, y0 - pad - 30, (x1 - x0) + pad * 2, (y1 - y0) + pad * 2 + 30].map(v => Math.round(v * 10) / 10);
    const lines = (Array.isArray(w.edges) ? w.edges : []).map(e => { const a = nodes[e && e[0]], b = nodes[e && e[1]]; return a && b ? `<line x1="${num(a.x)}" y1="${num(a.y)}" x2="${num(b.x)}" y2="${num(b.y)}"${label(a) || label(b) ? ' class="soft"' : ''}/>` : ''; }).join('');
    const things = nodes.map((n, i) => {
      if (!n) return '';
      const x = num(n.x), y = num(n.y), home = i === w.home;
      if (label(n)) { const hw = halfW(n); return `<g transform="translate(${x} ${y})"><rect class="tag${n.type === 'genre' ? ' dash' : ''}" x="${-hw}" y="-13" width="${hw * 2}" height="26"/><text class="sm" y="4" text-anchor="middle">${esc(n.name)}</text></g>`; }
      const s = S(n), img = /^https:\/\//.test(n.image || '') ? `<image href="${esc(n.image)}" x="${-s / 2}" y="${-s / 2}" width="${s}" height="${s}" preserveAspectRatio="xMidYMid slice"/>` : `<text class="ph" y="8" text-anchor="middle">${esc(String(n.name || '?').charAt(0))}</text>`;
      return `<g transform="translate(${x} ${y})"><rect class="fr${home ? ' home' : ''}${n.type === 'album' ? ' alb' : ''}" x="${-s / 2}" y="${-s / 2}" width="${s}" height="${s}"/>${img}<text class="nm${home ? ' big' : ''}" y="${s / 2 + (home ? 20 : 16)}" text-anchor="middle">${esc(n.name)}</text></g>`;
    }).join('');
    const home = nodes[w.home] || {};
    return `<div class="rc-web"><svg viewBox="${vb.join(' ')}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc('A web around ' + (home.name || 'a record'))}"><g class="ln">${lines}</g>${things}</svg>` +
      `<div class="rc-web__bar"><span class="rc-web__t">${esc(d.title || home.name || '')}</span><button class="rc-web__go" data-act="unpack" title="Put these on the wall as real blocks you can move and join">unpack</button><button class="rc-web__go" data-act="openweb" title="Open it live, and keep exploring">explore ↗</button></div></div>`;
  }

  // a quote block's inside, as markup. Pure (and everything typed is escaped), so it can be tested without a page.
  function quoteHtml(d) {
    d = d || {};
    const from = [d.who, d.source].map(x => (x || '').trim()).filter(Boolean).join(', ');
    const at = quoteAt(d);
    const when = at != null
      ? (d.url ? `<button class="rc-quote__go" data-act="openurl" title="Open this moment">▶ ${esc(fmtClock(at))}</button>` : `<span class="rc-quote__at">${esc(fmtClock(at))}</span>`)
      : (d.url ? `<button class="rc-quote__go" data-act="openurl" title="Open the source">↗ source</button>` : '');
    return (d.text || '').trim() ? `
          <figure class="rc-quote">
            <blockquote class="rc-quote__t">${esc(d.text)}</blockquote>
            <figcaption class="rc-quote__src">${from ? `<span>— ${esc(from)}</span>` : '<span class="rc-quote__no">no source yet</span>'}${when}</figcaption>
          </figure>` : `<button class="rc-fillme" data-act="fill">+ add a quote</button>`;
  }

  // ── mix / link helpers ───────────────────────────────────────────────────
  function embedFor(url, start) {
    try {
      const u = new URL(url);
      const h = u.hostname.replace(/^www\.|^m\./, '');
      if (h === 'youtube.com' || h === 'youtu.be' || h === 'music.youtube.com') {
        const id = h === 'youtu.be' ? u.pathname.slice(1) : (u.searchParams.get('v') || (u.pathname.match(/\/(?:embed|shorts|live)\/([\w-]+)/) || [])[1]);
        if (id) {
          const t = start != null ? start : startFromUrl(url);
          return { src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}${t > 0 ? '?start=' + Math.floor(t) : ''}`, kind: 'video' };
        }
      }
      if (h === 'soundcloud.com' || h === 'on.soundcloud.com')
        return { src: `https://w.soundcloud.com/player/?url=${encodeURIComponent(url)}&color=%23c9a84c&auto_play=false&hide_related=true&show_comments=false&show_user=true&visual=true`, kind: 'audio' };
      if (h === 'open.spotify.com') {
        const m = u.pathname.match(/\/(track|album|playlist|artist|episode|show)\/([A-Za-z0-9]+)/);
        if (m) return { src: `https://open.spotify.com/embed/${m[1]}/${m[2]}`, kind: 'audio' };
      }
      if (h === 'mixcloud.com')
        return { src: `https://www.mixcloud.com/widget/iframe/?hide_cover=1&light=0&feed=${encodeURIComponent(u.pathname)}`, kind: 'audio' };
      if (h === 'vimeo.com') {
        const id = (u.pathname.match(/\/(\d+)/) || [])[1];
        if (id) {
          const t = start != null ? start : startFromUrl(url);
          return { src: `https://player.vimeo.com/video/${id}${t > 0 ? '#t=' + Math.floor(t) + 's' : ''}`, kind: 'video' };
        }
      }
    } catch (_) {}
    return null;
  }
  function hostOf(url) { try { return new URL(url).hostname.replace(/^www\./, ''); } catch (_) { return url; } }
  async function resolveLink(url) {
    try {
      const r = await fetch('/api/discover/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      const d = await r.json();
      return { title: d.raw_title || [d.artist, d.title].filter(Boolean).join(' — ') || hostOf(url), image: d.image || '', source: d.source || hostOf(url) };
    } catch (_) { return { title: hostOf(url), image: '', source: hostOf(url) }; }
  }
  function openUrl(url) {
    fetch('/api/open-url', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }).catch(() => {});
  }

  // ── the player (the room's server player; nothing plays in the browser) ──
  async function cue(items, autostart) {
    const its = (items || []).filter(t => t && t.file_id);
    if (!its.length) return;
    await fetch('/api/seinnteoir/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: its, autostart: !!autostart }) });
  }
  async function playNow(item) {
    if (!item || !item.file_id) return;
    await cue([item], false);
    try {
      const st = await (await fetch('/api/seinnteoir/state')).json();
      const q = st.queue || [];
      let idx = -1;
      for (let i = q.length - 1; i >= 0; i--) if (q[i].file_id === item.file_id) { idx = i; break; }
      if (idx >= 0) await fetch('/api/seinnteoir/play_at', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idx }) });
    } catch (_) {}
  }
  function playAll(items) { if (window.Seinnteoir) window.Seinnteoir.playAll(items); }

  // ── lines between blocks ─────────────────────────────────────────────────
  // The wall is a mind map: any two blocks can be joined by a line, yours ("mine", solid) or a suggestion that came
  // with a grown web ("near", faint). A line has no direction. These mirror wall_links.py (which re-checks on save).
  const linkKey = (a, b) => (a < b ? a + '|' + b : b + '|' + a);
  const LinkOps = {
    key: linkKey,
    has: (links, a, b) => { const k = linkKey(a, b); return links.some(l => linkKey(l.a, l.b) === k); },
    // -> { links, added, link }: joining a pair that is already joined adds nothing (a suggestion you join yourself becomes yours)
    add(links, a, b, kind, label) {
      if (!a || !b || a === b) return { links, added: false };
      const k = linkKey(a, b), i = links.findIndex(l => linkKey(l.a, l.b) === k);
      if (i >= 0) {
        if (kind !== 'near' && links[i].kind !== 'mine') { const next = links.slice(); next[i] = Object.assign({}, links[i], { kind: 'mine' }); return { links: next, added: true, link: next[i] }; }
        return { links, added: false, link: links[i] };
      }
      const link = { a, b, kind: kind === 'near' ? 'near' : 'mine', label: String(label || '').slice(0, 80) };
      return { links: links.concat([link]), added: true, link };
    },
    dropFor: (links, id) => links.filter(l => l.a !== id && l.b !== id),
    remove: (links, a, b) => { const k = linkKey(a, b); return links.filter(l => linkKey(l.a, l.b) !== k); },
  };

  // ── the camera ───────────────────────────────────────────────────────────
  // The wall is a world with a camera: pan, pinch-zoom, and depth. A block keeps its x, y, w, h (so a wall saved
  // before this loads exactly as it was) and gains a layer `d` from -3 (far) to +3 (near). Farther blocks draw
  // smaller and slide less when the view shifts; nearer ones the opposite. That is plain perspective: the view has a
  // vanishing point, and each block sits d * 120px in front of or behind the page. All of it is pure maths so it can
  // be tested: cam = { x, y, k, ox, oy, depth } — x/y the world's offset on screen, k its zoom, (ox, oy) the vanishing point.
  const PERSP = 1400, DEPTH_STEP = 120, DEPTH_MAX = 3, K_MIN = 0.15, K_MAX = 2.5;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const depthOf = b => { const d = Number(b && b.d); return isFinite(d) ? clamp(Math.round(d), -DEPTH_MAX, DEPTH_MAX) : 0; };
  // where a block sits in front of the page; the tiny rank term keeps blocks on one layer stacked in their order
  const zOf = (b, rank) => depthOf(b) * DEPTH_STEP + (rank || 0) * 0.4;
  const scaleAt = (z, on) => (on ? PERSP / (PERSP - z) : 1);
  function toScreen(cam, wx, wy, z) {
    const s = scaleAt(z, cam.depth), X = cam.x + cam.k * wx, Y = cam.y + cam.k * wy;
    return { x: cam.ox + (X - cam.ox) * s, y: cam.oy + (Y - cam.oy) * s };
  }
  function toWorld(cam, sx, sy, z) {
    const s = scaleAt(z, cam.depth), X = cam.ox + (sx - cam.ox) / s, Y = cam.oy + (sy - cam.oy) / s;
    return { x: (X - cam.x) / cam.k, y: (Y - cam.y) / cam.k };
  }
  // zoom by factor f keeping the point under (sx, sy) where it is (the page plane, z = 0)
  function zoomAt(cam, f, sx, sy) {
    const k = clamp(cam.k * f, K_MIN, K_MAX), r = k / cam.k;
    return Object.assign({}, cam, { k, x: sx - (sx - cam.x) * r, y: sy - (sy - cam.y) * r });
  }
  // a camera that shows every block, with room round the edge and clear of the top bar. With depth on, a near block
  // is drawn larger and a far one smaller than its flat size, so the fit is measured on where the corners really land.
  function fitCam(blocks, W, H, cam) {
    const bs = (blocks || []).filter(b => b && isFinite(b.x) && isFinite(b.y) && isFinite(b.w) && isFinite(b.h));
    const top = 110, pad = 48, availW = Math.max(200, W - pad * 2), availH = Math.max(160, H - top - pad - 70);
    if (!bs.length) return Object.assign({}, cam, { k: 1, x: pad, y: top + 24 });
    const rank = {}; bs.slice().sort((a, b) => (a.z || 0) - (b.z || 0)).forEach((b, i) => { rank[b.id] = i; });
    const x0 = Math.min(...bs.map(b => b.x)), y0 = Math.min(...bs.map(b => b.y)), x1 = Math.max(...bs.map(b => b.x + b.w)), y1 = Math.max(...bs.map(b => b.y + b.h));
    let c = Object.assign({}, cam);                      // (its vanishing point is the real one, so what is measured is what is drawn)
    c.k = clamp(Math.min(availW / Math.max(1, x1 - x0), availH / Math.max(1, y1 - y0)), K_MIN, 1.1);
    c.x = pad + (availW - (x1 - x0) * c.k) / 2 - x0 * c.k; c.y = top + (availH - (y1 - y0) * c.k) / 2 - y0 * c.k;
    if (c.depth) {                                       // measure the drawn corners and settle on a zoom that fits them
      for (let i = 0; i < 4; i++) {
        let mx0 = Infinity, my0 = Infinity, mx1 = -Infinity, my1 = -Infinity;
        bs.forEach(b => { const z = zOf(b, rank[b.id]); [[b.x, b.y], [b.x + b.w, b.y + b.h]].forEach(([wx, wy]) => { const p = toScreen(c, wx, wy, z); mx0 = Math.min(mx0, p.x); my0 = Math.min(my0, p.y); mx1 = Math.max(mx1, p.x); my1 = Math.max(my1, p.y); }); });
        const f = Math.min(availW / Math.max(1, mx1 - mx0), availH / Math.max(1, my1 - my0));
        c.k = clamp(c.k * f, K_MIN, 1.1);
        let nx0 = Infinity, ny0 = Infinity, nx1 = -Infinity, ny1 = -Infinity;
        bs.forEach(b => { const z = zOf(b, rank[b.id]); [[b.x, b.y], [b.x + b.w, b.y + b.h]].forEach(([wx, wy]) => { const p = toScreen(c, wx, wy, z); nx0 = Math.min(nx0, p.x); ny0 = Math.min(ny0, p.y); nx1 = Math.max(nx1, p.x); ny1 = Math.max(ny1, p.y); }); });
        c.x += (pad + availW / 2) - (nx0 + nx1) / 2; c.y += (top + availH / 2) - (ny0 + ny1) / 2;
      }
    }
    return { x: c.x, y: c.y, k: c.k, ox: cam.ox, oy: cam.oy, depth: cam.depth };
  }
  // a wheel turn in pixels, whatever unit the browser reports it in
  const wheelPx = (v, mode, page) => (mode === 1 ? v * 16 : mode === 2 ? v * (page || 800) : v);

  // ── mount ────────────────────────────────────────────────────────────────
  function mount(host, opts) {
    opts = opts || {};
    const placeId = opts.placeId;
    const getTracks = opts.getTracks || (() => []);
    const toItem = opts.sntItem || (t => ({ track_id: t.id, file_id: t.file_id, title: t.title, artist: t.artist_name }));
    let blocks = [], sel = null, saveT = null, sheetFor = null, loaded = false;

    host.classList.add('rc');
    host.innerHTML = `
      <div class="rc-view" id="rc-view"><svg class="rc-links" id="rc-links"></svg><div class="rc-stage" id="rc-stage"></div></div>
      <div class="rc-linkbox" id="rc-linkbox"></div>
      <div class="rc-hud" id="rc-hud">
        <button data-hud="fit" title="Fit everything on screen">Fit</button><button data-hud="in" title="Zoom in">+</button><button data-hud="out" title="Zoom out">−</button>
        <button data-hud="depth" id="rc-depth" title="Depth: blocks sit on layers, and the view looks around as you move">3D</button>
      </div>
      <div class="rc-empty" id="rc-empty"><div class="rc-empty__t">Nothing here yet.</div><div class="rc-empty__b">Make a block, drag it where you want it, pull its corner to size it, fill it.</div></div>
      <div class="rc-addwrap">
        <span class="rc-status" id="rc-status"></span>
        <button class="rc-add" id="rc-add" title="Make a new block">+ block</button>
        <div class="rc-addmenu" id="rc-addmenu"></div>
      </div>
      <div class="rc-sheet" id="rc-sheet"></div>`;
    const view = host.querySelector('#rc-view'), stage = host.querySelector('#rc-stage'), empty = host.querySelector('#rc-empty');
    const addBtn = host.querySelector('#rc-add'), addMenu = host.querySelector('#rc-addmenu');
    const sheet = host.querySelector('#rc-sheet'), status = host.querySelector('#rc-status');
    addMenu.innerHTML = '<button data-type="__music"><b>Music</b><small>from what is playing, a liked song, or an album you type</small></button>' + Object.keys(TYPES).filter(t => !TYPES[t].keepOnly).map(t => `<button data-type="${t}"><b>${esc(TYPES[t].label)}</b><small>${esc(TYPES[t].hint)}</small></button>`).join('');

    // ── the camera in use ──
    const reduced = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
    const camKey = 'rc_cam_' + placeId, depthKey = 'rc_depth';
    let depthOn = true; try { depthOn = localStorage.getItem(depthKey) !== '0'; } catch (_) {}
    const cam = { x: 0, y: 0, k: 1, ox: 0, oy: 0, depth: depthOn };
    let rankOf = {}, lookTo = null, lookRaf = 0, camSaveT = null, camSet = false;
    const viewSize = () => { const r = view.getBoundingClientRect(); return { W: r.width || innerWidth, H: r.height || innerHeight, l: r.left, t: r.top }; };
    function applyCam(glide) {
      stage.style.transform = `translate(${cam.x.toFixed(2)}px,${cam.y.toFixed(2)}px) scale(${cam.k.toFixed(4)})`;
      view.style.perspective = cam.depth ? PERSP + 'px' : 'none';
      view.style.perspectiveOrigin = cam.ox.toFixed(1) + 'px ' + cam.oy.toFixed(1) + 'px';
      view.classList.toggle('flat', !cam.depth);
      const g = 48 * cam.k;                                              // the dotted ground moves with the page plane
      view.style.backgroundSize = g + 'px ' + g + 'px';
      view.style.backgroundPosition = (cam.x % g) + 'px ' + (cam.y % g) + 'px';
      const d = host.querySelector('#rc-depth'); if (d) d.classList.toggle('on', cam.depth);
      drawLinks();
      clearTimeout(camSaveT); camSaveT = setTimeout(() => { try { localStorage.setItem(camKey, JSON.stringify({ x: cam.x, y: cam.y, k: cam.k })); } catch (_) {} }, 400);
    }
    function centreOrigin() { const v = viewSize(); cam.ox = v.W / 2; cam.oy = v.H / 2; lookTo = null; }
    function fitNow(glide) { const v = viewSize(); Object.assign(cam, fitCam(blocks, v.W, v.H, cam)); applyCam(glide); }
    function restoreCam() {
      const v = viewSize(); centreOrigin();
      let saved = null; try { saved = JSON.parse(localStorage.getItem(camKey) || 'null'); } catch (_) {}
      if (saved && isFinite(saved.x) && isFinite(saved.y) && isFinite(saved.k) && blocks.length) { cam.x = saved.x; cam.y = saved.y; cam.k = clamp(saved.k, K_MIN, K_MAX); applyCam(); }
      else fitNow();
      camSet = true;
    }
    const worldAtPointer = (e, z) => { const v = viewSize(); return toWorld(cam, e.clientX - v.l, e.clientY - v.t, z || 0); };
    const viewCentreWorld = () => { const v = viewSize(); return toWorld(cam, v.W / 2, v.H / 2, 0); };

    // ── lines: state, drawing (screen space, so they meet each block on its own layer), editing ──
    let links = [], selLink = null, linking = null, linkFrom = null;
    const svg = host.querySelector('#rc-links'), linkBox = host.querySelector('#rc-linkbox'), linkEls = new Map();
    const NS = 'http://www.w3.org/2000/svg', svgEl = t => document.createElementNS(NS, t);
    const centreScreen = b => toScreen(cam, b.x + b.w / 2, b.y + b.h / 2, zOf(b, rankOf[b.id]));
    function drawLinks() {
      const live = new Set();
      links.forEach(l => {
        const a = blocks.find(x => x.id === l.a), b = blocks.find(x => x.id === l.b); if (!a || !b) return;
        const k = linkKey(l.a, l.b); live.add(k);
        let g = linkEls.get(k);
        if (!g) { g = { root: svgEl('g'), line: svgEl('line'), hit: svgEl('line'), text: svgEl('text') }; g.root.dataset.k = k; g.hit.setAttribute('class', 'hit'); g.root.append(g.line, g.hit, g.text); svg.appendChild(g.root); linkEls.set(k, g); }
        const p = centreScreen(a), q = centreScreen(b);
        [g.line, g.hit].forEach(n => { n.setAttribute('x1', p.x.toFixed(1)); n.setAttribute('y1', p.y.toFixed(1)); n.setAttribute('x2', q.x.toFixed(1)); n.setAttribute('y2', q.y.toFixed(1)); });
        g.line.setAttribute('class', 'ln ' + (l.kind === 'near' ? 'near' : 'mine') + (selLink === k ? ' sel' : ''));
        g.text.setAttribute('x', ((p.x + q.x) / 2).toFixed(1)); g.text.setAttribute('y', ((p.y + q.y) / 2 - 5).toFixed(1)); g.text.setAttribute('text-anchor', 'middle');
        g.text.textContent = l.label || '';
      });
      linkEls.forEach((g, k) => { if (!live.has(k)) { g.root.remove(); linkEls.delete(k); } });
      let t = svg.querySelector('.tmp');                                   // the line being pulled out of a dot
      if (linking) {
        const a = blocks.find(x => x.id === linking.from), v = viewSize();
        if (a) { if (!t) { t = svgEl('line'); t.setAttribute('class', 'ln mine tmp'); svg.appendChild(t); } const p = centreScreen(a);
          t.setAttribute('x1', p.x); t.setAttribute('y1', p.y); t.setAttribute('x2', linking.x - v.l); t.setAttribute('y2', linking.y - v.t); }
      } else if (t) t.remove();
      if (selLink) placeLinkBox();
    }
    function placeLinkBox() {
      const l = links.find(x => linkKey(x.a, x.b) === selLink), a = l && blocks.find(x => x.id === l.a), b = l && blocks.find(x => x.id === l.b);
      if (!l || !a || !b) { linkBox.classList.remove('on'); return; }
      const p = centreScreen(a), q = centreScreen(b), v = viewSize();
      linkBox.style.left = Math.min(Math.max(8, v.l + (p.x + q.x) / 2 - 110), innerWidth - 240) + 'px'; linkBox.style.top = Math.min(Math.max(70, v.t + (p.y + q.y) / 2 + 10), innerHeight - 90) + 'px';
    }
    function selectLink(k) {
      selLink = k; if (k) { sel = null; stage.querySelectorAll('.rc-block').forEach(el => el.classList.remove('sel')); }
      const l = k && links.find(x => linkKey(x.a, x.b) === k);
      if (!l) { linkBox.classList.remove('on'); linkBox.innerHTML = ''; drawLinks(); return; }
      linkBox.innerHTML = `<input data-l="label" maxlength="80" placeholder="a few words (optional)" value="${esc(l.label || '')}"><div class="rc-linkbox__r"><span>${l.kind === 'near' ? 'a suggestion' : 'your line'}</span><button data-l="remove">remove line</button></div>`;
      linkBox.classList.add('on'); drawLinks();
    }
    function addLink(a, b, kind) {
      const r = LinkOps.add(links, a, b, kind); if (!r.added) { if (r.link) selectLink(linkKey(r.link.a, r.link.b)); return; }
      links = r.links; dirty(); selectLink(linkKey(a, b));
    }
    function removeLink(k) { const l = links.find(x => linkKey(x.a, x.b) === k); if (!l) return; links = LinkOps.remove(links, l.a, l.b); selLink = null; linkBox.classList.remove('on'); drawLinks(); dirty(); }
    function cancelLinking() { linkFrom = null; linking = null; host.classList.remove('linking'); if (status.textContent.startsWith('now click')) status.textContent = ''; drawLinks(); }

    // ── persistence ──
    async function load() {
      try {
        const d = await (await fetch(`/api/room/canvas/${placeId}`)).json();
        blocks = Array.isArray(d.blocks) ? d.blocks : [];
        links = Array.isArray(d.links) ? d.links.filter(l => l && typeof l.a === 'string' && typeof l.b === 'string') : [];
      } catch (_) { blocks = []; links = []; }
      loaded = true; renderAll(); if (!camSet) restoreCam();
    }
    function dirty() { status.textContent = '…'; clearTimeout(saveT); saveT = setTimeout(save, 600); }
    async function save() {
      try {
        const r = await fetch(`/api/room/canvas/${placeId}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ v: 1, blocks, links }) });
        if (!r.ok) throw new Error('save failed');
        status.textContent = 'saved'; setTimeout(() => { if (status.textContent === 'saved') status.textContent = ''; }, 1400);
      } catch (_) { status.textContent = 'not saved'; }
    }

    // ── blocks ──
    const byId = id => blocks.find(b => b.id === id);
    function addBlock(type, at) {
      const T = TYPES[type]; if (!T) return;
      const c = viewCentreWorld();
      const x = at ? snap(at.x - T.w / 2) : snap(c.x - T.w / 2 + (blocks.length % 5) * 24);          // a right-click puts the block's middle on the click
      const y = at ? snap(at.y - T.h / 2) : snap(c.y - T.h / 2 + (blocks.length % 5) * 24);
      const b = { id: uid(), type, x, y, w: T.w, h: T.h, z: blocks.length + 1, data: {} };
      blocks.push(b); renderAll(); select(b.id); dirty();
      if (type !== 'text') openSheet(b.id); else { const h = stage.querySelector(`[data-id="${b.id}"] [data-k="heading"]`); if (h) h.focus(); }
    }
    function removeBlock(id) {
      const i = blocks.findIndex(b => b.id === id); if (i < 0) return;
      blocks.splice(i, 1); links = LinkOps.dropFor(links, id); if (sel === id) sel = null; closeSheet(); renderAll(); dirty();
    }
    function bump(id, dir) {
      const zs = blocks.slice().sort((a, b) => a.z - b.z);
      const i = zs.findIndex(b => b.id === id); const j = i + dir;
      if (i < 0 || j < 0 || j >= zs.length) return;
      [zs[i].z, zs[j].z] = [zs[j].z, zs[i].z]; renderAll(); dirty();
    }
    const hooks = { select: [], click: [] };
    const fire = (kind, b) => hooks[kind].forEach(f => { try { f(b); } catch (_) {} });
    function select(id) {
      sel = id;
      stage.querySelectorAll('.rc-block').forEach(el => el.classList.toggle('sel', el.dataset.id === id));
      fire('select', id ? byId(id) : null);
    }

    // ── render ──
    function renderAll() {
      rankOf = {}; blocks.slice().sort((a, b) => a.z - b.z).forEach((b, i) => { rankOf[b.id] = i; });
      const keep = new Set();
      blocks.forEach(b => { keep.add(b.id); let el = stage.querySelector(`[data-id="${b.id}"]`); if (!el) { el = makeEl(b); stage.appendChild(el); } place(b, el); if (el.dataset.sig !== sig(b)) { el.dataset.sig = sig(b); fillBody(b, el); } });
      stage.querySelectorAll('.rc-block').forEach(el => { if (!keep.has(el.dataset.id)) el.remove(); });
      empty.style.display = blocks.length ? 'none' : '';
      select(sel); drawLinks();
    }
    const sig = b => b.type + '|' + JSON.stringify(b.data);
    function place(b, el, lift) {
      el.style.left = b.x + 'px'; el.style.top = b.y + 'px'; el.style.width = b.w + 'px'; el.style.height = b.h + 'px'; el.style.zIndex = String(b.z);
      const d = depthOf(b);
      el.style.transform = cam.depth ? `translateZ(${(zOf(b, rankOf[b.id]) + (lift ? 40 : 0)).toFixed(1)}px)` : '';
      el.style.opacity = cam.depth && d < 0 ? String(1 + d * 0.07) : '';          // far things are a little paler: air between
      const t = el.querySelector('.rc-tools__type'); if (t) t.textContent = TYPES[b.type].label + (d ? (d > 0 ? ' · near ' : ' · far ') + Math.abs(d) : '');
    }
    function makeEl(b) {
      const el = document.createElement('div');
      el.className = `rc-block rc-block--${b.type}`; el.dataset.id = b.id;
      el.innerHTML = `
        <div class="rc-tools">
          <span class="rc-tools__type">${esc(TYPES[b.type].label)}</span>
          ${b.type !== 'text' && b.type !== 'web' && b.type !== 'node' ? '<button data-act="fill" title="Fill this block">fill</button>' : ''}
          <button data-act="down" title="Send back">↓</button><button data-act="up" title="Bring forward">↑</button>
          <button data-act="link" title="Join this to another block with a line">link</button><button data-act="far" title="Push it farther away: smaller, and it moves less">far</button><button data-act="near" title="Pull it nearer: larger, and it moves more">near</button>
          <button data-act="del" title="Remove this block">✕</button>
        </div>
        <div class="rc-body"></div>
        ${b.type !== 'text' && b.type !== 'node' ? '<div class="rc-cap" contenteditable="true" spellcheck="false" data-k="caption" placeholder="a caption"></div>' : ''}
        <div class="rc-dot" data-act="dot" title="Drag this to another block to join them with a line"></div>
        <div class="rc-rz rc-rz--e"></div><div class="rc-rz rc-rz--s"></div><div class="rc-rz rc-rz--se"></div>`;
      return el;
    }
    function fillBody(b, el) {
      const body = el.querySelector('.rc-body'); const d = b.data || {};
      const cap = el.querySelector('.rc-cap'); if (cap && cap.innerText !== (d.caption || '')) cap.innerText = d.caption || '';
      if (cap) cap.classList.toggle('has', !!(d.caption || '').trim());
      if (b.type === 'text') {
        body.innerHTML = `<div class="rc-text"><div class="rc-text__h" contenteditable="true" spellcheck="false" data-k="heading" placeholder="A heading"></div><div class="rc-text__b" contenteditable="true" data-k="body" placeholder="Words"></div></div>`;
        body.querySelector('[data-k="heading"]').innerText = d.heading || '';
        body.querySelector('[data-k="body"]').innerText = d.body || '';
      } else if (b.type === 'image') {
        body.innerHTML = d.src ? `<img class="rc-img rc-img--${d.fit || 'cover'}" src="${esc(d.src)}" alt="${esc(d.alt || '')}" draggable="false">`
          : `<button class="rc-fillme" data-act="fill">+ add an image</button>`;
      } else if (b.type === 'record') {
        const t = d.track;
        body.innerHTML = t ? `
          <div class="rc-rec">
            <img class="rc-rec__cover" src="${coverUrl(t, 768)}" alt="" draggable="false" data-act="open">
            <div class="rc-rec__meta"><div class="rc-rec__t" data-act="open">${esc(t.title || 'Untitled')}</div><div class="rc-rec__a">${esc(t.artist_name || t.artist || '')}</div></div>
            <div class="rc-rec__acts"><button data-act="play" title="Play now">▶ play</button><button data-act="cue" title="Add to the cue">＋ cue</button></div>
          </div>` : `<button class="rc-fillme" data-act="fill">+ choose a record</button>`;
      } else if (b.type === 'playlist') {
        const ts = d.tracks || [];
        body.innerHTML = ts.length || d.name ? `
          <div class="rc-pl">
            <div class="rc-pl__head"><div class="rc-pl__name">${esc(d.name || 'Playlist')}</div><span class="rc-pl__n">${ts.length}</span>
              <div class="rc-pl__acts"><button data-act="playall" title="Play these, in order (replaces the cue)">▶ play all</button><button data-act="cueall" title="Add these to the cue">＋ cue all</button></div></div>
            <div class="rc-pl__rows">${ts.map((t, i) => `<div class="rc-pl__row" data-i="${i}"><img class="rc-pl__cover" src="${coverUrl(t, 192)}" alt="" loading="lazy" draggable="false"><div class="rc-pl__meta"><div class="rc-pl__t" data-act="open" data-tid="${t.id}">${esc(t.title || 'Untitled')}</div><div class="rc-pl__a">${esc(t.artist_name || t.artist || '')}</div></div><button class="rc-pl__play" data-act="playrow" data-i="${i}" title="Play now">▶</button></div>`).join('')}</div>
          </div>` : `<button class="rc-fillme" data-act="fill">+ choose a playlist</button>`;
      } else if (b.type === 'node') {
        body.innerHTML = nodeHtml(d);
      } else if (b.type === 'web') {
        body.innerHTML = webHtml(d);
      } else if (b.type === 'mix') {
        const em = d.url && d.embed !== false ? embedFor(d.url, d.start) : null;
        body.innerHTML = !d.url ? `<button class="rc-fillme" data-act="fill">+ paste a mix or video link</button>`
          : em ? `<iframe class="rc-embed rc-embed--${em.kind}" src="${esc(em.src)}" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>`
          : linkCard(d);
      } else if (b.type === 'link') {
        body.innerHTML = d.url ? linkCard(d) : `<button class="rc-fillme" data-act="fill">+ paste a link</button>`;
      } else if (b.type === 'quote') {
        body.innerHTML = quoteHtml(d);
      }
    }
    const linkCard = d => `<div class="rc-link" data-act="openurl" title="${esc(d.url)}">${d.image ? `<img class="rc-link__img" src="${esc(d.image)}" alt="" draggable="false">` : ''}<div class="rc-link__meta"><div class="rc-link__src">${esc(d.source || hostOf(d.url))} ↗</div><div class="rc-link__t">${esc(d.title || d.url)}</div></div></div>`;

    // ── the fill sheet (one popover, per type) ──
    function openSheet(id) {
      const b = byId(id); if (!b) return;
      sheetFor = id; select(id); sheet.classList.add('on');
      const d = b.data;
      if (b.type === 'image') sheet.innerHTML = `
        <div class="rc-sheet__h">Image</div>
        <label class="rc-btn rc-btn--primary">choose a file<input type="file" accept="image/*" hidden data-s="file"></label>
        <div class="rc-row"><input data-s="url" placeholder="or paste an image address" value="${esc(d.srcUrl || '')}"><button class="rc-btn" data-s="useurl">use</button></div>
        <div class="rc-row"><span class="rc-lbl">fit</span><button class="rc-btn ${(d.fit || 'cover') === 'cover' ? 'on' : ''}" data-s="fit" data-v="cover">fill the block</button><button class="rc-btn ${d.fit === 'contain' ? 'on' : ''}" data-s="fit" data-v="contain">show it all</button></div>
        ${d.src ? '<button class="rc-btn rc-btn--quiet" data-s="clear">remove the image</button>' : ''}
        <div class="rc-hint">You can also drop an image file onto the block.</div>`;
      else if (b.type === 'record') sheet.innerHTML = `
        <div class="rc-sheet__h">One record</div>
        <input data-s="q" placeholder="search your room — title or artist" autofocus>
        <div class="rc-list" data-s="list"></div>`;
      else if (b.type === 'playlist') sheet.innerHTML = `
        <div class="rc-sheet__h">A playlist</div>
        <div class="rc-row rc-tabs"><button class="rc-btn ${d.kind === 'place' ? 'on' : ''}" data-s="kind" data-v="place">a place</button><button class="rc-btn ${d.kind === 'set' ? 'on' : ''}" data-s="kind" data-v="set">a set</button><button class="rc-btn ${!d.kind || d.kind === 'picks' ? 'on' : ''}" data-s="kind" data-v="picks">records I pick</button></div>
        <div data-s="pane"></div>`;
      else if (b.type === 'quote') sheet.innerHTML = `
        <div class="rc-sheet__h">A quote</div>
        <textarea data-s="qtext" rows="4" placeholder="the words, as they were said or written">${esc(d.text || '')}</textarea>
        <div class="rc-row"><input data-s="who" placeholder="who said it" value="${esc(d.who || '')}"></div>
        <div class="rc-row"><input data-s="source" placeholder="where from: a talk, a book, a film" value="${esc(d.source || '')}"></div>
        <div class="rc-row"><input data-s="qurl" placeholder="a link to it (optional)" value="${esc(d.url || '')}"></div>
        <div class="rc-row"><span class="rc-lbl">at</span><input data-s="qat" placeholder="m:ss" value="${esc(quoteAt(d) != null ? fmtClock(quoteAt(d)) : '')}"></div>
        <div class="rc-hint">Keep quotes short, and say where they come from. A link with a time in it (…?t=559) fills in the time.</div>`;
      else if (b.type === 'mix' || b.type === 'link') sheet.innerHTML = `
        <div class="rc-sheet__h">${b.type === 'mix' ? 'A mix or video' : 'A link'}</div>
        <div class="rc-row"><input data-s="url" placeholder="paste the address" value="${esc(d.url || '')}"><button class="rc-btn rc-btn--primary" data-s="useurl">use</button></div>
        ${d.url ? `<div class="rc-row"><input data-s="title" placeholder="title" value="${esc(d.title || '')}"></div>` : ''}
        ${b.type === 'mix' && d.url ? `<div class="rc-row"><span class="rc-lbl">show</span><button class="rc-btn ${d.embed !== false ? 'on' : ''}" data-s="embed" data-v="1">the player</button><button class="rc-btn ${d.embed === false ? 'on' : ''}" data-s="embed" data-v="0">a card</button></div>` : ''}
        ${b.type === 'mix' && d.url && embedFor(d.url) && embedFor(d.url).kind === 'video' ? `<div class="rc-row"><span class="rc-lbl">starts at</span><input data-s="start" placeholder="0:00" value="${esc(d.start != null ? fmtClock(d.start) : '')}"></div>` : ''}
        <div class="rc-hint">${b.type === 'mix' ? 'YouTube, SoundCloud, Mixcloud and Vimeo play here, and a video can start at a time. Anything else becomes a card that opens in your browser.' : 'Opens in your browser.'}</div>`;
      sheet.innerHTML += `<button class="rc-sheet__x" data-s="close" title="Close">✕</button>`;
      positionSheet(b);
      if (b.type === 'quote') { const q = sheet.querySelector('[data-s="qtext"]'); if (q) setTimeout(() => q.focus(), 30); }
      if (b.type === 'record') { renderPicker(sheet.querySelector('[data-s="list"]'), '', t => { b.data.track = snap_t(t); renderAll(); dirty(); closeSheet(); }); const q = sheet.querySelector('[data-s="q"]'); q.oninput = () => renderPicker(sheet.querySelector('[data-s="list"]'), q.value, t => { b.data.track = snap_t(t); renderAll(); dirty(); closeSheet(); }); setTimeout(() => q.focus(), 30); }
      if (b.type === 'playlist') renderPlaylistPane(b);
    }
    const snap_t = t => ({ id: t.id, file_id: t.file_id, title: t.title, artist_name: t.artist_name || t.artist, art_v: t.art_v || 0 });
    function positionSheet(b) {
      const el = stage.querySelector(`[data-id="${b.id}"]`); if (!el) return;
      const r = el.getBoundingClientRect(), hr = host.getBoundingClientRect();
      let left = r.right + 12, top = r.top;
      if (left + 340 > hr.right - 12) left = Math.max(hr.left + 12, r.left - 352);
      if (left + 340 > hr.right - 12) left = hr.right - 352;
      top = Math.min(Math.max(hr.top + 72, top), hr.bottom - 24 - Math.min(460, hr.height - 100));
      sheet.style.left = left + 'px'; sheet.style.top = top + 'px';
    }
    function closeSheet() { sheetFor = null; sheet.classList.remove('on'); sheet.innerHTML = ''; }
    function renderPicker(listEl, q, pick) {
      const all = getTracks() || []; q = (q || '').trim().toLowerCase();
      const hits = []; for (const t of all) { if (!t || !t.file_id) continue; if (!q || (t.title || '').toLowerCase().includes(q) || (t.artist_name || '').toLowerCase().includes(q)) { hits.push(t); if (hits.length >= 40) break; } }
      listEl.innerHTML = hits.length ? hits.map(t => `<button class="rc-list__row" data-tid="${t.id}"><img src="${coverUrl(t, 192)}" alt="" loading="lazy"><span><b>${esc(t.title || 'Untitled')}</b><small>${esc(t.artist_name || '')}</small></span></button>`).join('') : `<div class="rc-hint">${all.length ? 'no match' : 'no records in this room'}</div>`;
      listEl.querySelectorAll('.rc-list__row').forEach(r => r.onclick = () => { const t = all.find(x => x.id === +r.dataset.tid); if (t) pick(t); });
    }
    async function renderPlaylistPane(b) {
      const pane = sheet.querySelector('[data-s="pane"]'); if (!pane) return;
      const d = b.data; const kind = d.kind || 'picks';
      if (kind === 'picks') {
        pane.innerHTML = `<div class="rc-picked" data-s="picked"></div><input data-s="q" placeholder="search your room — add records one by one"><div class="rc-list" data-s="list"></div>`;
        const drawPicked = () => { const p = pane.querySelector('[data-s="picked"]'); const ts = d.tracks || []; p.innerHTML = ts.length ? ts.map((t, i) => `<span class="rc-chip">${esc(t.title || 'Untitled')}<button data-i="${i}" title="take it out">✕</button></span>`).join('') : '<div class="rc-hint">nothing picked yet</div>'; p.querySelectorAll('button').forEach(x => x.onclick = () => { d.tracks.splice(+x.dataset.i, 1); drawPicked(); renderAll(); dirty(); }); };
        drawPicked();
        const q = pane.querySelector('[data-s="q"]'), list = pane.querySelector('[data-s="list"]');
        const pick = t => { d.tracks = d.tracks || []; if (!d.tracks.some(x => x.id === t.id)) d.tracks.push(snap_t(t)); d.name = d.name || 'My picks'; drawPicked(); renderAll(); dirty(); };
        renderPicker(list, '', pick); q.oninput = () => renderPicker(list, q.value, pick); setTimeout(() => q.focus(), 30);
      } else {
        pane.innerHTML = `<div class="rc-list" data-s="list"><div class="rc-hint">loading…</div></div>`;
        const list = pane.querySelector('[data-s="list"]');
        try {
          const rows = await (await fetch(kind === 'place' ? '/api/places' : '/api/sets')).json();
          const items = (rows || []).filter(r => (r.track_count || 0) > 0);
          list.innerHTML = items.length ? items.map(r => `<button class="rc-list__row rc-list__row--plain ${d.ref_id === r.id ? 'on' : ''}" data-rid="${r.id}"><span><b>${esc(r.name)}</b><small>${r.track_count} record${r.track_count === 1 ? '' : 's'}</small></span></button>`).join('') : `<div class="rc-hint">none with records yet</div>`;
          list.querySelectorAll('.rc-list__row').forEach(x => x.onclick = async () => {
            const r = items.find(i => i.id === +x.dataset.rid); if (!r) return;
            list.querySelectorAll('.rc-list__row').forEach(y => y.classList.toggle('on', y === x));
            const res = await (await fetch(kind === 'place' ? `/api/place/${r.id}/tracks` : `/api/set/${r.id}`)).json();
            const ts = (kind === 'place' ? (Array.isArray(res) ? res : (res.tracks || [])) : (res.tracks || [])).filter(t => t.file_id).map(snap_t);
            Object.assign(d, { kind, ref_id: r.id, name: r.name, tracks: ts }); renderAll(); dirty();
          });
        } catch (_) { list.innerHTML = '<div class="rc-hint">could not load</div>'; }
      }
    }

    // ── uploads ──
    async function uploadImage(b, file) {
      if (!file || !/^image\//.test(file.type)) return;
      const fd = new FormData(); fd.append('image', file);
      status.textContent = 'uploading…';
      try {
        const r = await (await fetch('/api/room/canvas/image', { method: 'POST', body: fd })).json();
        if (r.url) { b.data.src = r.url; b.data.srcUrl = ''; renderAll(); dirty(); if (sheetFor === b.id) openSheet(b.id); }
        else status.textContent = r.error || 'upload failed';
      } catch (_) { status.textContent = 'upload failed'; }
    }

    // ── events: sheet ──
    sheet.addEventListener('click', e => {
      const t = e.target.closest('[data-s]'); if (!t) return;
      const b = byId(sheetFor); if (!b) return;
      const s = t.dataset.s;
      if (s === 'close') closeSheet();
      else if (s === 'useurl') {
        const inp = sheet.querySelector('[data-s="url"]'); const url = (inp.value || '').trim(); if (!url) return;
        if (b.type === 'image') { b.data.src = url; b.data.srcUrl = url; renderAll(); dirty(); openSheet(b.id); }
        else { t.textContent = '…'; resolveLink(/^https?:\/\//.test(url) ? url : 'https://' + url).then(m => { Object.assign(b.data, { url: /^https?:\/\//.test(url) ? url : 'https://' + url }, m); if (b.type === 'mix' && b.data.start == null) { const s0 = startFromUrl(b.data.url); if (s0 > 0) b.data.start = s0; } renderAll(); dirty(); openSheet(b.id); }); }
      }
      else if (s === 'fit') { b.data.fit = t.dataset.v; renderAll(); dirty(); openSheet(b.id); }
      else if (s === 'embed') { b.data.embed = t.dataset.v === '1'; renderAll(); dirty(); openSheet(b.id); }
      else if (s === 'clear') { b.data.src = ''; b.data.srcUrl = ''; renderAll(); dirty(); openSheet(b.id); }
      else if (s === 'kind') { b.data.kind = t.dataset.v; sheet.querySelectorAll('[data-s="kind"]').forEach(x => x.classList.toggle('on', x === t)); renderPlaylistPane(b); }
    });
    sheet.addEventListener('change', e => { const f = e.target.closest('[data-s="file"]'); const b = byId(sheetFor); if (f && b && f.files[0]) uploadImage(b, f.files[0]); });
    const QUOTE_KEYS = { qtext: 'text', who: 'who', source: 'source', qurl: 'url' };
    sheet.addEventListener('input', e => {
      const b = byId(sheetFor); if (!b) return;
      const t = e.target.closest('[data-s="title"]');
      if (t) { b.data.title = t.value; renderAll(); dirty(); return; }
      const f = e.target.closest('[data-s]'); if (!f) return;
      const k = f.dataset.s;
      if (b.type === 'quote' && QUOTE_KEYS[k]) { b.data[QUOTE_KEYS[k]] = f.value; renderAll(); dirty(); }
      else if ((b.type === 'quote' && k === 'qat') || (b.type === 'mix' && k === 'start')) {
        const key = k === 'qat' ? 'at' : 'start', s = parseClock(f.value);
        if (s != null) b.data[key] = s; else if (!f.value.trim()) delete b.data[key];
        renderAll(); dirty();
      }
    });
    // a quote's link can say the time, and (if you have not named the source) what the page is called
    sheet.addEventListener('change', e => {
      const f = e.target.closest('[data-s="qurl"]'); const b = byId(sheetFor); if (!f || !b || b.type !== 'quote') return;
      const url = f.value.trim(); if (!/^https?:\/\//.test(url)) return;
      const s = startFromUrl(url);
      if (b.data.at == null && s != null) { b.data.at = s; const at = sheet.querySelector('[data-s="qat"]'); if (at) at.value = fmtClock(s); renderAll(); dirty(); }
      if (!(b.data.source || '').trim()) resolveLink(url).then(m => {
        const bb = byId(sheetFor); if (!bb || bb !== b || (b.data.source || '').trim() || !m.title || m.title === hostOf(url)) return;
        b.data.source = m.title; const src = sheet.querySelector('[data-s="source"]'); if (src) src.value = m.title; renderAll(); dirty();
      });
    });
    sheet.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('[data-s="url"]')) { e.preventDefault(); sheet.querySelector('[data-s="useurl"]').click(); } if (e.key === 'Escape') closeSheet(); });

    // ── events: add menu ──
    addBtn.onclick = e => { e.stopPropagation(); addMenu.classList.toggle('on'); };
    // One handler for the + menu: from the button the block lands where you are looking; from a right-click, centred on the click.
    // (Two handlers used to run here, and the right-click one moved the PREVIOUS block to the click before the new one was made.)
    addMenu.addEventListener('click', e => {
      const t = e.target.closest('[data-type]'); if (!t) return;
      const at = addMenu.classList.contains('at') ? { x: +addMenu.dataset.x, y: +addMenu.dataset.y } : null;
      addMenu.classList.remove('on', 'at'); addMenu.style.left = ''; addMenu.style.top = '';
      if (t.dataset.type === '__music') { if (window.WallMusic) window.WallMusic.openStart(at); return; }
      addBlock(t.dataset.type, at);
    });
    view.addEventListener('contextmenu', e => {
      if (e.target !== view && e.target !== stage) return; e.preventDefault();
      const w = worldAtPointer(e, 0);
      addMenu.classList.add('on'); addMenu.dataset.x = snap(w.x); addMenu.dataset.y = snap(w.y);
      addMenu.style.left = Math.min(e.clientX, innerWidth - 260) + 'px'; addMenu.style.top = Math.min(e.clientY, innerHeight - 320) + 'px'; addMenu.classList.add('at');
    });
    document.addEventListener('click', e => { if (!addMenu.contains(e.target) && e.target !== addBtn) { addMenu.classList.remove('on', 'at'); addMenu.style.left = ''; addMenu.style.top = ''; } });

    // ── events: blocks (select · tools · inline text · play) ──
    stage.addEventListener('click', e => {
      const el = e.target.closest('.rc-block');
      if (!el) return;
      const b = byId(el.dataset.id); if (!b) return;
      if (linkFrom) { const from = linkFrom; cancelLinking(); if (from !== b.id) addLink(from, b.id); else select(b.id); return; }
      if (dragMoved) { dragMoved = false; return; }                       // that click was the end of a drag
      select(b.id); if (!e.target.closest('.rc-tools, .rc-dot')) fire('click', b);
      const act = e.target.closest('[data-act]'); if (!act) return;
      const a = act.dataset.act;
      if (a === 'del') { if (confirm('Remove this block?')) removeBlock(b.id); }
      else if (a === 'up') bump(b.id, +1);
      else if (a === 'link') { linkFrom = b.id; host.classList.add('linking'); status.textContent = 'now click the block to join it to  ·  esc to cancel'; }
      else if (a === 'down') bump(b.id, -1);
      else if (a === 'far' || a === 'near') { b.d = clamp(depthOf(b) + (a === 'near' ? 1 : -1), -DEPTH_MAX, DEPTH_MAX); renderAll(); dirty(); }
      else if (a === 'fill') openSheet(b.id);
      else if (a === 'play') playNow(toItem(b.data.track));
      else if (a === 'cue') { cue([toItem(b.data.track)], false); flash(act, '✓'); }
      else if (a === 'playall') playAll((b.data.tracks || []).map(toItem));
      else if (a === 'cueall') { cue((b.data.tracks || []).map(toItem), false); flash(act, '✓ cued'); }
      else if (a === 'playrow') playNow(toItem((b.data.tracks || [])[+act.dataset.i]));
      else if (a === 'open') { const tid = +(act.dataset.tid || (b.data.track || {}).id); if (tid && opts.openTrack) { try { opts.openTrack(tid); } catch (_) { location.href = `/room?track=${tid}`; } } }
      else if (a === 'unpack') { if (window.WallMusic) window.WallMusic.unpack(b.id); }
      else if (a === 'openweb') { const u = String((b.data || {}).url || ''); if (/^\/connections\?/.test(u)) location.href = u; }
      else if (a === 'openurl') openUrl(b.type === 'quote' ? withStart(b.data.url, quoteAt(b.data)) : b.data.url);
    });
    function flash(btn, txt) { const o = btn.textContent; btn.textContent = txt; setTimeout(() => { btn.textContent = o; }, 1200); }
    stage.addEventListener('input', e => {
      const ed = e.target.closest('[contenteditable][data-k]'); if (!ed) return;
      const el = ed.closest('.rc-block'); const b = byId(el.dataset.id); if (!b) return;
      b.data[ed.dataset.k] = ed.innerText.replace(/\n+$/, '');
      if (ed.dataset.k === 'caption') ed.classList.toggle('has', !!b.data.caption.trim());
      el.dataset.sig = sig(b);              // typed here — don't re-render under the caret
      dirty();
    });
    stage.addEventListener('paste', e => {   // plain words only
      const ed = e.target.closest('[contenteditable]'); if (!ed) return;
      e.preventDefault(); document.execCommand('insertText', false, (e.clipboardData || window.clipboardData).getData('text/plain'));
    });
    stage.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.matches('[data-k="heading"], [data-k="caption"]')) { e.preventDefault(); e.target.blur(); }
      if (e.key === 'Escape' && e.target.matches('[contenteditable]')) e.target.blur();
    });
    // drop an image file onto an image block (or anywhere on an empty block)
    stage.addEventListener('dragover', e => { const el = e.target.closest('.rc-block--image'); if (el) { e.preventDefault(); el.classList.add('drop'); } });
    stage.addEventListener('dragleave', e => { const el = e.target.closest('.rc-block--image'); if (el) el.classList.remove('drop'); });
    stage.addEventListener('drop', e => { const el = e.target.closest('.rc-block--image'); if (!el) return; e.preventDefault(); el.classList.remove('drop'); const b = byId(el.dataset.id); const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (b && f) uploadImage(b, f); });

    // ── moving around: drag the background to pan, two fingers / scroll to pan, pinch to zoom ──
    let drag = null, panning = null, pinch = null, dragMoved = false;
    const fingers = new Map();
    // a block's own scroll (a playlist's rows, a long quote) keeps its wheel; everywhere else a wheel pans
    function scrollsUnder(e) {
      for (let n = e.target; n && n !== view; n = n.parentElement) {
        const o = getComputedStyle(n).overflowY;
        if ((o === 'auto' || o === 'scroll') && n.scrollHeight > n.clientHeight + 1) {
          const up = e.deltaY < 0; if ((up && n.scrollTop > 0) || (!up && n.scrollTop + n.clientHeight < n.scrollHeight - 1)) return true;
        }
      }
      return false;
    }
    view.addEventListener('wheel', e => {
      const v = viewSize();
      if (e.ctrlKey) {                                  // a trackpad pinch arrives as ctrl + wheel
        e.preventDefault(); Object.assign(cam, zoomAt(cam, Math.exp(-wheelPx(e.deltaY, e.deltaMode, v.H) * 0.012), e.clientX - v.l, e.clientY - v.t)); applyCam(); return;
      }
      if (scrollsUnder(e)) return;
      e.preventDefault(); cam.x -= wheelPx(e.deltaX, e.deltaMode, v.W); cam.y -= wheelPx(e.deltaY, e.deltaMode, v.H); applyCam();
    }, { passive: false });
    let gScale = 1;                                     // Safari and the app: pinch arrives as gesture events
    view.addEventListener('gesturestart', e => { e.preventDefault(); gScale = 1; });
    view.addEventListener('gesturechange', e => { e.preventDefault(); const v = viewSize(), f = e.scale / gScale; gScale = e.scale; Object.assign(cam, zoomAt(cam, f, e.clientX - v.l, e.clientY - v.t)); applyCam(); });
    view.addEventListener('gestureend', e => e.preventDefault());

    // the look-around: with depth on, the vanishing point drifts a little toward the pointer, so layers slide past each other
    function look(e) {
      if (!cam.depth || reduced || drag || panning || pinch) return;
      const a = document.activeElement; if (a && a.matches && a.matches('input, textarea, [contenteditable]')) return;
      const v = viewSize(); lookTo = { x: v.W / 2 + (e.clientX - v.l - v.W / 2) * 0.16, y: v.H / 2 + (e.clientY - v.t - v.H / 2) * 0.16 };
      if (!lookRaf) lookRaf = requestAnimationFrame(lookStep);
    }
    function lookStep() {
      lookRaf = 0; if (!lookTo || drag || panning || pinch) return;
      const dx = lookTo.x - cam.ox, dy = lookTo.y - cam.oy; if (Math.abs(dx) < 0.2 && Math.abs(dy) < 0.2) return;
      cam.ox += dx * 0.12; cam.oy += dy * 0.12; applyCam(); lookRaf = requestAnimationFrame(lookStep);
    }
    view.addEventListener('pointerleave', () => { if (!drag && !panning) { const v = viewSize(); lookTo = { x: v.W / 2, y: v.H / 2 }; if (!lookRaf) lookRaf = requestAnimationFrame(lookStep); } });

    view.addEventListener('pointerdown', e => {
      if (e.pointerType === 'touch') {
        fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (fingers.size === 2) { const [a, b] = [...fingers.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) }; drag = null; panning = null; view.classList.remove('pan'); return; }
      }
      if (e.target.closest('.rc-block') || e.target.closest('.rc-links g')) return;
      if (e.button !== 0 && e.button !== 1) return;
      lookTo = null;
      panning = { sx: e.clientX, sy: e.clientY, cx: cam.x, cy: cam.y, moved: false };
      view.classList.add('pan'); view.setPointerCapture(e.pointerId); e.preventDefault();
    });
    view.addEventListener('pointermove', e => {
      if (e.pointerType === 'touch' && fingers.has(e.pointerId)) {
        fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (pinch && fingers.size === 2) {
          const [a, b] = [...fingers.values()], d = Math.hypot(a.x - b.x, a.y - b.y), v = viewSize();
          Object.assign(cam, zoomAt(cam, d / pinch.d, (a.x + b.x) / 2 - v.l, (a.y + b.y) / 2 - v.t)); pinch.d = d; applyCam(); return;
        }
      }
      if (panning) {
        const dx = e.clientX - panning.sx, dy = e.clientY - panning.sy;
        if (!panning.moved && Math.hypot(dx, dy) < 4) return;
        panning.moved = true; cam.x = panning.cx + dx; cam.y = panning.cy + dy; applyCam();
      } else look(e);
    });
    const endPan = e => {
      fingers.delete(e.pointerId); if (fingers.size < 2) pinch = null;
      if (!panning) return; const p = panning; panning = null; view.classList.remove('pan');
      if (!p.moved) { cancelLinking(); select(null); selectLink(null); closeSheet(); }
    };
    view.addEventListener('pointerup', endPan); view.addEventListener('pointercancel', endPan);

    host.querySelector('#rc-hud').addEventListener('click', e => {
      const b = e.target.closest('[data-hud]'); if (!b) return; const v = viewSize(), h = b.dataset.hud;
      if (h === 'fit') fitNow(true);
      else if (h === 'in' || h === 'out') { Object.assign(cam, zoomAt(cam, h === 'in' ? 1.25 : 0.8, v.W / 2, v.H / 2)); applyCam(); }
      else if (h === 'depth') {
        cam.depth = !cam.depth; try { localStorage.setItem(depthKey, cam.depth ? '1' : '0'); } catch (_) {}
        centreOrigin(); renderAll(); applyCam();
      }
    });

    // ── drag + resize a block (pointer events; a click under 4px stays a click) ──
    // The pointer is turned into a place on the block's own layer, so a block that sits nearer or farther still follows the hand exactly.
    stage.addEventListener('pointerdown', e => {
      if (e.button !== 0 || pinch) return;
      const el = e.target.closest('.rc-block'); if (!el) return;
      const b = byId(el.dataset.id); if (!b) return;
      const dot = e.target.closest('.rc-dot');
      if (dot) { lookTo = null; linking = { from: b.id, x: e.clientX, y: e.clientY }; dot.setPointerCapture(e.pointerId); e.preventDefault(); drawLinks(); return; }
      const rz = e.target.closest('.rc-rz');
      if (!rz && e.target.closest('[contenteditable], input, button, a, iframe, select, textarea, img[data-act], .rc-pl__rows')) return;
      lookTo = null; dragMoved = false; const z = zOf(b, rankOf[b.id]) + 40;
      drag = { b, el, mode: rz ? rz.className.replace(/.*rc-rz--(\w+).*/, '$1') : 'move', z, w0: worldAtPointer(e, z), ox: b.x, oy: b.y, ow: b.w, oh: b.h, moved: false, z0: b.z };
      el.setPointerCapture(e.pointerId); el.classList.add('live'); b.z = 9999; place(b, el, true);
      e.preventDefault();
    });
    stage.addEventListener('pointermove', e => {
      if (linking) { linking.x = e.clientX; linking.y = e.clientY; drawLinks(); return; }
      if (!drag) return;
      const w = worldAtPointer(e, drag.z), dx = w.x - drag.w0.x, dy = w.y - drag.w0.y;
      if (!drag.moved && Math.hypot(dx * cam.k, dy * cam.k) < 4) return; drag.moved = true;
      const b = drag.b;
      if (drag.mode === 'move') { b.x = snap(drag.ox + dx); b.y = snap(drag.oy + dy); }
      else { if (drag.mode !== 's') b.w = Math.max(MIN_W, snap(drag.ow + dx)); if (drag.mode !== 'e') b.h = Math.max(MIN_H, snap(drag.oh + dy)); }
      place(b, drag.el, true); drawLinks(); if (sheetFor === b.id) positionSheet(b);
    });
    const endDrag = e => {
      if (!drag) return;
      const { b, el, moved, z0 } = drag; drag = null; dragMoved = moved; el.classList.remove('live');
      b.z = z0; if (moved) { b.z = Math.max(...blocks.map(x => x.z === 9999 ? 0 : x.z), 0) + 1; }   // what you touched comes to the front
      renderAll(); if (moved) dirty(); select(b.id);
    };
    stage.addEventListener('pointerup', endDrag); stage.addEventListener('pointercancel', endDrag);
    const endLinking = e => {
      if (!linking) return;
      const from = linking.from; linking = null;
      const under = document.elementFromPoint(e.clientX, e.clientY), el = under && under.closest && under.closest('.rc-block');
      drawLinks(); if (el && e.type === 'pointerup' && el.dataset.id !== from) addLink(from, el.dataset.id);
    };
    stage.addEventListener('pointerup', endLinking); stage.addEventListener('pointercancel', endLinking);

    // a line: click it to word or remove it
    svg.addEventListener('click', e => { const g = e.target.closest('g[data-k]'); if (g) selectLink(g.dataset.k); });
    linkBox.addEventListener('input', e => { const l = links.find(x => linkKey(x.a, x.b) === selLink); if (l && e.target.matches('[data-l="label"]')) { l.label = e.target.value.slice(0, 80); drawLinks(); dirty(); } });
    linkBox.addEventListener('click', e => { if (e.target.matches('[data-l="remove"]')) removeLink(selLink); });
    linkBox.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === 'Escape') { e.preventDefault(); selectLink(null); } });
    addEventListener('keydown', e => {
      if (!host.classList.contains('on')) return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && selLink && !e.target.matches('input, textarea, [contenteditable]')) { e.preventDefault(); removeLink(selLink); return; }
      if ((e.key === 'Delete' || e.key === 'Backspace') && sel && !e.target.matches('input, textarea, [contenteditable]')) { e.preventDefault(); if (confirm('Remove this block?')) removeBlock(sel); }
      if (e.key === 'Escape' && !e.target.matches('input, textarea, [contenteditable]')) { cancelLinking(); closeSheet(); select(null); selectLink(null); }
      if (!e.target.matches('input, textarea, [contenteditable]') && !e.metaKey && !e.ctrlKey) {
        const v = viewSize();
        if (e.key === '0') fitNow(true);
        else if (e.key === '+' || e.key === '=') { Object.assign(cam, zoomAt(cam, 1.25, v.W / 2, v.H / 2)); applyCam(); }
        else if (e.key === '-') { Object.assign(cam, zoomAt(cam, 0.8, v.W / 2, v.H / 2)); applyCam(); }
      }
    });
    addEventListener('resize', () => { const b = byId(sheetFor); if (b) positionSheet(b); if (camSet) { centreOrigin(); applyCam(); } });

    const ready = load();
    const api = {
      reload: load, addBlock, addLink, host, get blocks() { return blocks; }, get links() { return links; },
      on: (kind, fn) => { if (hooks[kind]) hooks[kind].push(fn); },
      // a block made by code (a music node): x, y are its middle, in the wall's own coordinates
      addNode(type, data, mid, d, size) {
        const T = TYPES[type], w = size ? size.w : T.w, h = size ? size.h : T.h;
        const b = { id: uid(), type, x: snap(mid.x - w / 2), y: snap(mid.y - h / 2), w, h, z: blocks.length + 1, data };
        if (d) b.d = d; blocks.push(b); return b;
      },
      removeBlocks(ids) { const set = new Set(ids); blocks = blocks.filter(b => !set.has(b.id)); links = links.filter(l => !set.has(l.a) && !set.has(l.b)); if (sel && set.has(sel)) sel = null; },
      setLinks(next) { links = next; },
      changed() { renderAll(); dirty(); },
      ready, select, viewCentreWorld, fit: fitNow, snap, TYPES,
      panTo(wx, wy) { const v = viewSize(); cam.x = v.W / 2 - wx * cam.k; cam.y = v.H / 2 - wy * cam.k; applyCam(); },
    };
    if (window.WallMusic) window.WallMusic.attach(api);
    return api;
  }

  return { mount, TYPES, embedFor, fmtClock, parseClock, startFromUrl, withStart, quoteHtml, webHtml, nodeHtml, LinkOps,
           camera: { PERSP, DEPTH_STEP, DEPTH_MAX, K_MIN, K_MAX, clamp, depthOf, zOf, scaleAt, toScreen, toWorld, zoomAt, fitCam, wheelPx } };
})();
