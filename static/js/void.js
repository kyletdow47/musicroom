/* void.js — the Void: your wall as a real 3D space, with time as depth.
 *
 * Everything you save is a thing in the Void. Its time decides how far away it is (void_layout.js): the newest things float
 * near you and older ones recede into the dark behind, and distance fades into the page's own colour, so the far past is a
 * haze. You scroll to pan and pinch to move through time, drag to look around, and click a thing to fly to it (Esc flies back out).
 * A date and a thin timeline say where in time you are.
 *
 * It offers the same small interface the old wall did (blocks, links, on, addNode, select …), so what was built on that
 * interface — music, the record page, the list and search — works here unchanged (wall_music.js, wall_index.js).
 *
 * Kept in its own store (void_store.py); the old wall is left as it was.
 */
(function (root) {
  'use strict';

  var VL = root.VoidLayout, S = VL.S;
  var TYPES = { node: { w: 148, h: 128 }, text: { w: 360, h: 200 }, quote: { w: 380, h: 240 }, image: { w: 320, h: 320 }, link: { w: 320, h: 200 }, mix: { w: 440, h: 280 } };
  var KIND_WORD = { node: 'Music', text: 'Words', quote: 'Quote', image: 'Picture', link: 'Link', mix: 'Video' };
  var PX = 200;                                           // canvas pixels per unit of space (a text thing is painted at this density)
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };
  var uid = function () { return 'k' + Math.random().toString(36).slice(2, 9); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); };
  var pad = function (n) { return n > 0 ? String(n).padStart(3, '0') : ''; };
  var hostOf = function (u) { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (_) { return ''; } };

  // a picture address the scene may use: this app's own, or the music services' pictures through the app's proxy
  function pictureUrl(u) {
    u = String(u || '');
    if (/^\/(static|api)\//.test(u)) return u;
    if (/^https:\/\//.test(u)) return '/api/void/image?u=' + encodeURIComponent(u);
    return '';
  }

  // wrap text to a width using a canvas context's own measuring
  function wrapLines(ctx, text, maxW, maxLines) {
    var out = [], line = '';
    var paras = String(text || '').replace(/\r/g, '').split('\n');
    paras.forEach(function (p) {
      line = '';
      p.split(/\s+/).filter(Boolean).forEach(function (w) {
        var t = line ? line + ' ' + w : w;
        if (ctx.measureText(t).width > maxW && line) { out.push(line); line = w; } else line = t;
      });
      out.push(line);
    });
    if (maxLines && out.length > maxLines) { out = out.slice(0, maxLines); out[maxLines - 1] = out[maxLines - 1].replace(/\s*\S*$/, '') + '…'; }
    return out;
  }

  // A published Void arrives inside someone else's room design, so it is rebuilt from only what is understood before it is shown
  function cleanSnapshot(s) {
    var out = { blocks: [], links: [], next: 0 }; if (!s || typeof s !== 'object') return out;
    var num = function (v) { return typeof v === 'number' && isFinite(v) ? v : null; }, seen = Object.create(null), ids = Object.create(null);
    (Array.isArray(s.blocks) ? s.blocks : []).slice(0, 2000).forEach(function (b) {
      if (!b || typeof b !== 'object' || typeof b.id !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(b.id) || seen[b.id] || !Object.prototype.hasOwnProperty.call(TYPES, b.type)) return;
      var x = num(b.x), y = num(b.y), w = num(b.w), h = num(b.h); if (x === null || y === null || w === null || h === null) return;
      seen[b.id] = 1; ids[b.id] = 1;
      out.blocks.push({ id: b.id, type: b.type, x: x, y: y, w: Math.max(20, Math.min(4000, w)), h: Math.max(20, Math.min(4000, h)), c: num(b.c) > 0 ? b.c : 1, n: num(b.n) > 0 ? Math.floor(b.n) : 0, d: Math.max(-3, Math.min(3, Math.round(num(b.d) || 0))), data: b.data && typeof b.data === 'object' && !Array.isArray(b.data) ? b.data : {} });
    });
    (Array.isArray(s.links) ? s.links : []).slice(0, 6000).forEach(function (l) {
      if (l && ids[l.a] && ids[l.b] && l.a !== l.b) out.links.push({ a: l.a, b: l.b, kind: l.kind === 'near' ? 'near' : 'mine', label: typeof l.label === 'string' ? l.label.slice(0, 80) : '' });
    });
    out.next = Math.max(num(s.next) || 0, 0); return out;
  }

  function mount(host, opts) {
    opts = opts || {};
    var placeId = opts.placeId, ro = !!opts.readOnly;                 // readOnly: a published Void, shown to visitors (nothing can be changed or saved)
    var blocks = [], links = [], counter = 0, sel = null, saveT = 0, loaded = false, hooks = { select: [], click: [], dblclick: [] };
    var depth = VL.depths([]), items = {}, THREE = null, renderer = null, scene = null, camera = null, lineMine = null, lineNear = null, meshes = [];
    var T = (root.MRTones && root.MRTones.read) ? root.MRTones.read() : { bg: '#161513', text: '#f2efe8', muted: '#9b968a', dark: true };
    var reduced = !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
    var fine = !!(root.matchMedia && root.matchMedia('(pointer: fine)').matches);

    host.classList.add('rc', 'vd'); if (ro) host.classList.add('vd-ro');
    host.innerHTML =
      '<canvas class="vd-canvas"></canvas><div class="vd-caps"></div><div class="vd-date" id="vd-date"></div>' +
      '<div class="vd-tl" id="vd-tl"><div class="vd-tl__line"></div><div class="vd-tl__ticks"></div><div class="vd-tl__thumb"></div></div>' +
      '<div class="rc-hud" id="rc-hud"><button data-hud="now" title="Fly back to the present">Now</button></div>' +
      '<div class="vd-sel" id="vd-sel"></div>' +
      '<div class="rc-empty" id="rc-empty"><div class="rc-intro" id="rc-intro"><p class="rc-intro__l"></p><p class="rc-intro__l"></p>' +
        '<div class="rc-intro__go" hidden><button data-intro="music">[ Add music ]</button><button data-intro="block">[ Add something ]</button></div>' +
        '<button class="rc-intro__skip" data-intro="skip">[ Skip ]</button></div></div>' +
      '<div class="rc-addwrap"><span class="rc-status" id="rc-status"></span><button class="rc-add" id="rc-add" title="Add something to the Void">+ Add</button><div class="rc-addmenu" id="rc-addmenu"></div></div>' +
      '<div class="vd-form" id="vd-form"></div><div class="rc-ring" id="rc-ring"></div>';
    var $ = function (sel) { return host.querySelector(sel); };
    var canvas = $('.vd-canvas'), capsEl = $('.vd-caps'), dateEl = $('#vd-date'), tlEl = $('#vd-tl'), selEl = $('#vd-sel'), emptyEl = $('#rc-empty'), statusEl = $('#rc-status');
    var addBtn = $('#rc-add'), addMenu = $('#rc-addmenu'), formEl = $('#vd-form'), ringEl = $('#rc-ring');
    var say = function (t) { statusEl.textContent = t || ''; };
    canvas.style.cursor = fine ? 'none' : 'default';

    // ── numbers: each thing gets the next one, once, and keeps it (the server keeps the highest ever given out) ──
    function numberAll() {
      blocks.forEach(function (b) { if (b.n > counter) counter = b.n | 0; });
      var any = false; blocks.forEach(function (b) { if (!(b.n > 0)) { b.n = ++counter; any = true; } }); return any;
    }

    // ── keeping it ──
    function dirty() { if (ro) return; say('…'); clearTimeout(saveT); saveT = setTimeout(save, 600); }
    async function save() {
      try {
        var r = await fetch('/api/room/void/' + placeId, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ blocks: blocks, links: links, next: counter }) });
        if (!r.ok) throw new Error('save failed');
        say('saved'); setTimeout(function () { if (statusEl.textContent === 'saved') say(''); }, 1400);
      } catch (_) { say('not saved'); }
    }

    // ── the pictures: text things are painted onto a canvas; covers come through the app's picture proxy ──
    var fontsReady = Promise.resolve();
    try {
      if (document.fonts && document.fonts.load) fontsReady = Promise.all(['40px "Instrument Serif"', 'italic 40px "Instrument Serif"', '24px "IBM Plex Mono"'].map(function (f) { return document.fonts.load(f).catch(function () {}); }));
    } catch (_) {}
    var SERIF = '"Instrument Serif", Georgia, serif', MONO = '"IBM Plex Mono", ui-monospace, Menlo, monospace';

    function paintThing(b) {
      var d = b.data || {}, W = Math.round(b.w * S * PX), pad = 22, c = document.createElement('canvas'), x = c.getContext('2d');
      var lines = [];   // [{ text, font, color, lh, gapAfter }]
      var addLines = function (text, font, color, lh, maxLines, gap, upper) {
        x.font = font; var t = upper ? String(text || '').toUpperCase() : text;
        wrapLines(x, t, W - pad * 2, maxLines).forEach(function (l, i, a) { lines.push({ text: l, font: font, color: color, lh: lh, gap: i === a.length - 1 ? (gap || 0) : 0 }); });
      };
      if (b.type === 'text') {
        if (d.heading) addLines(d.heading, '46px ' + SERIF, T.text, 54, 3, 10);
        if (d.body) addLines(d.body, '24px ' + MONO, T.text, 36, 12, 0);
        if (!lines.length) addLines('Words', '46px ' + SERIF, T.muted, 54, 1, 0);
      } else if (b.type === 'quote') {
        if (d.text) addLines('“' + d.text + '”', 'italic 52px ' + SERIF, T.text, 60, 8, 18); else addLines('A quote', 'italic 52px ' + SERIF, T.muted, 60, 1, 18);
        var by = [d.who, d.source].filter(Boolean).join(', ');
        addLines(by || 'no source yet', '21px ' + MONO, T.muted, 30, 2, 0, true);
      } else {   // link, video
        addLines(d.title || hostOf(d.url) || (b.type === 'mix' ? 'A video' : 'A link'), '46px ' + SERIF, T.text, 54, 3, 10);
        addLines((hostOf(d.url) || (b.type === 'mix' ? 'video' : 'link')) + '  ↗', '21px ' + MONO, T.muted, 30, 1, 0, true);
      }
      var H = pad * 2 + lines.reduce(function (s, l) { return s + l.lh + l.gap; }, 0);
      c.width = W; c.height = Math.max(120, H); x = c.getContext('2d'); x.textBaseline = 'alphabetic';
      var y = pad;
      lines.forEach(function (l) { x.font = l.font; x.fillStyle = l.color; y += l.lh; x.fillText(l.text, pad, y - l.lh * 0.28); y += l.gap; });
      return c;
    }
    function paintTag(b) {   // a label or a genre: its name in small capitals, underlined (dashed for a genre)
      var d = b.data || {}, c = document.createElement('canvas'), x = c.getContext('2d'), text = String(d.name || '').toUpperCase() + (d.kind === 'label' ? '  ↗' : '');
      x.font = '28px ' + MONO; var w = Math.ceil(x.measureText(text).width) + 40; c.width = Math.min(w, 900); c.height = 70; x = c.getContext('2d');
      x.font = '28px ' + MONO; x.fillStyle = T.text; x.textBaseline = 'alphabetic'; x.fillText(text, 20, 38);
      x.strokeStyle = T.muted; x.lineWidth = 2; if (d.kind === 'genre') x.setLineDash([8, 6]); x.beginPath(); x.moveTo(20, 52); x.lineTo(c.width - 20, 52); x.stroke();
      return c;
    }
    function paintPlaceholder(b) {   // a cover that has not arrived (or has none): its first letter on a square
      var d = b.data || {}, c = document.createElement('canvas'), x = c.getContext('2d'); c.width = c.height = 256;
      x.strokeStyle = T.muted; x.lineWidth = 2; x.strokeRect(1, 1, 254, 254); x.fillStyle = T.muted; x.font = '120px ' + SERIF; x.textAlign = 'center'; x.textBaseline = 'middle';
      x.fillText(String(d.name || b.data && b.data.alt || '?').charAt(0).toUpperCase(), 128, 140); return c;
    }

    // ── the things in the space ──
    function sizeUnits(b) {
      var d = b.data || {};
      if (b.type === 'node') { if (d.kind === 'label' || d.kind === 'genre') return null; var s = d.home ? 1.9 : 1.25; return { w: s, h: s }; }
      if (b.type === 'image') { var k = (b.w || 320) * S; return { w: k, h: k }; }
      return null;   // painted things take the size of their painting
    }
    function texFrom(canvasOrImage, isCanvas) {
      var t = isCanvas ? new THREE.CanvasTexture(canvasOrImage) : canvasOrImage;
      t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy()); t.needsUpdate = true; return t;
    }
    function apply(item, tex, w, h) {
      if (item.mesh.material.map && item.mesh.material.map !== tex) item.mesh.material.map.dispose();
      item.mesh.material.map = tex; item.mesh.material.needsUpdate = true; item.w = w; item.h = h; item.mesh.scale.set(w, h, 1);
    }
    function paintItem(item) {
      var b = item.b, d = b.data || {};
      if (b.type === 'node' && (d.kind === 'label' || d.kind === 'genre')) { var c = paintTag(b); apply(item, texFrom(c, true), c.width / PX, c.height / PX); b.h = Math.round(c.height / PX / S); b.w = Math.round(c.width / PX / S); return; }
      var su = sizeUnits(b), url = (b.type === 'node' || b.type === 'image') ? pictureUrl(b.type === 'node' ? d.image : d.src) : '';
      if (b.type === 'node' || b.type === 'image') {
        var ph = paintPlaceholder(b); apply(item, texFrom(ph, true), su.w, su.h);
        if (url) { var token = item.token = (item.token || 0) + 1;
          new THREE.TextureLoader().load(url, function (tex) { if (item.token !== token || !items[b.id]) { tex.dispose(); return; } apply(item, texFrom(tex, false), su.w, su.h); }, undefined, function () {}); }
        return;
      }
      var cv = paintThing(b); apply(item, texFrom(cv, true), cv.width / PX, cv.height / PX); b.h = Math.round(cv.height / PX / S);
    }
    function makeItem(b) {
      var mat = new THREE.MeshBasicMaterial({ transparent: true, fog: true, depthWrite: false }), mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mat);
      var cap = document.createElement('div'); cap.className = 'vd-cap'; capsEl.appendChild(cap);
      var item = { b: b, mesh: mesh, cap: cap, w: 1, h: 1, token: 0, z: 0, hover: 0 }; mesh.userData.id = b.id; scene.add(mesh); items[b.id] = item; paintItem(item); return item;
    }
    var centreOf = function (b) { return { x: (b.x + b.w / 2) * S, y: -(b.y + b.h / 2) * S }; };
    function place(item) {
      var b = item.b, c = centreOf(b), z = depth.z[b.id] != null ? depth.z[b.id] : 0; item.z = z; item.mesh.position.set(c.x, c.y, z);
    }
    function rebuildLines() {
      if (!THREE) return;
      var mk = function (kind) {
        var pts = []; links.forEach(function (l) { if ((l.kind === 'near') !== (kind === 'near')) return; var a = items[l.a], b = items[l.b]; if (!a || !b) return; pts.push(a.mesh.position.x, a.mesh.position.y, a.mesh.position.z, b.mesh.position.x, b.mesh.position.y, b.mesh.position.z); });
        return new Float32Array(pts);
      };
      [['mine', lineMine], ['near', lineNear]].forEach(function (p) { var arr = mk(p[0]), g = p[1].geometry; g.setAttribute('position', new THREE.BufferAttribute(arr, 3)); g.setDrawRange(0, arr.length / 3); g.computeBoundingSphere(); });
    }
    function sync() {   // make the space match the blocks (new things appear, deleted ones go), then depths, places, lines
      if (!THREE) return;
      depth = VL.depths(blocks);
      blocks.forEach(function (b) { if (!items[b.id]) makeItem(b); else if (items[b.id].b !== b) items[b.id].b = b; });
      Object.keys(items).forEach(function (id) {
        if (blocks.some(function (b) { return b.id === id; })) return; var it = items[id]; scene.remove(it.mesh); it.mesh.geometry.dispose(); if (it.mesh.material.map) it.mesh.material.map.dispose(); it.mesh.material.dispose(); it.cap.remove(); delete items[id];
      });
      meshes = Object.keys(items).map(function (id) { return items[id].mesh; });
      Object.keys(items).forEach(function (id) { place(items[id]); });
      rebuildLines(); drawTimeline(); intro();
    }

    // ── the camera: falls through time, looks around, flies ──
    var cam = { x: 0, y: 0, z: 8 }, tgt = { x: 0, y: 0, z: 8 }, look = { yaw: 0, pitch: 0, ty: 0, tp: 0 }, par = { x: 0, y: 0 }, fly = null, back = null, W = 1, H = 1;
    var FOCUS = 12;                                         // you look at things this far ahead of you, so the camera is always 12 units in front of the moment it shows
    var OUT = 90;                                           // and you can pull back this far past the present, to see the whole space from far away
    var front = function () { return depth.zMax + FOCUS; };  // where the present is
    var zRange = function () { return { max: front() + OUT, min: depth.zMin + FOCUS }; };
    var ease = function (t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
    function flyPose(to, ms) {
      if (reduced) { cam.x = tgt.x = to.x; cam.y = tgt.y = to.y; cam.z = tgt.z = to.z; fly = null; return; }
      fly = { t0: performance.now(), ms: ms || 1100, from: { x: cam.x, y: cam.y, z: cam.z }, to: to };
    }
    // How far back to stand so a box w by h units fills about `share` of the view (the camera sees 2·tan(fov/2) across, per unit of distance)
    function standOff(w, h, share) {
      var t = 2 * Math.tan((camera ? camera.fov : 52) * Math.PI / 360), asp = (W || 1) / (H || 1);
      return Math.max(6, h / (t * share), w / (t * asp * share));
    }
    function flyTo(b) {                                       // fly to a thing, remembering where we were (once, until we come back)
      var it = items[b.id]; if (!it) return;
      if (!back) back = { x: cam.x, y: cam.y, z: cam.z };
      var z = it.z + standOff(it.w, it.h, 0.25);          // stand back: the thing is clear, and its neighbours are still in view
      tgt.x = it.mesh.position.x; tgt.y = it.mesh.position.y; tgt.z = z; look.ty = look.tp = 0;
      flyPose({ x: tgt.x, y: tgt.y, z: z });
    }
    function flyToGroup(list) {                               // fly to where several things can all be seen at once (a record and what grew round it)
      var its = (list || []).map(function (b) { return items[b.id]; }).filter(Boolean); if (!its.length) return;
      if (!back) back = { x: cam.x, y: cam.y, z: cam.z };
      var x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9, zn = -1e9;
      its.forEach(function (it) { var p = it.mesh.position; x0 = Math.min(x0, p.x - it.w / 2); x1 = Math.max(x1, p.x + it.w / 2); y0 = Math.min(y0, p.y - it.h / 2); y1 = Math.max(y1, p.y + it.h / 2); zn = Math.max(zn, it.z); });
      var z = clamp(zn + standOff(x1 - x0, y1 - y0, 0.8), zRange().min, zRange().max);
      tgt.x = (x0 + x1) / 2; tgt.y = (y0 + y1) / 2; tgt.z = z; look.ty = look.tp = 0; flyPose({ x: tgt.x, y: tgt.y, z: z }, 1300);
    }
    function flyBack() { if (!back) return false; var p = back; back = null; tgt.x = p.x; tgt.y = p.y; tgt.z = p.z; flyPose({ x: p.x, y: p.y, z: p.z }); return true; }
    function flyNow() { back = null; var r = zRange(); tgt.x = 0; tgt.y = 0; tgt.z = front(); look.ty = look.tp = 0; flyPose({ x: 0, y: 0, z: front() }, 1400); }

    // ── the date and the timeline ──
    function drawTimeline() {
      var ticks = tlEl.querySelector('.vd-tl__ticks'), span = (depth.zMax - depth.zMin) || 1, st = depth.stops;
      tlEl.classList.toggle('on', st.length > 0);
      ticks.innerHTML = st.map(function (s, i) {
        var top = clamp((depth.zMax - s.z) / span, 0, 1) * 100, show = i === 0 || i === st.length - 1 || st.length < 9 || i % Math.ceil(st.length / 8) === 0;
        return '<button data-z="' + s.z.toFixed(2) + '" style="top:' + top.toFixed(2) + '%" title="' + esc(s.label) + '"><i></i>' + (show ? '<span>' + esc(s.label) + '</span>' : '') + '</button>';
      }).join('');
    }
    function updateTimeline() {
      var span = (depth.zMax - depth.zMin) || 1, th = tlEl.querySelector('.vd-tl__thumb'), look0 = cam.z - FOCUS;
      th.style.top = (clamp((depth.zMax - look0) / span, 0, 1) * 100).toFixed(2) + '%';
      var t = VL.timeAt(look0, depth.stops); dateEl.textContent = t ? '[ ' + VL.dateLabel(t) + ' ]' : '';
    }
    tlEl.addEventListener('click', function (e) { var b = e.target.closest('button[data-z]'); if (!b) return; back = null; tgt.z = clamp(+b.dataset.z + FOCUS, zRange().min, zRange().max); flyPose({ x: cam.x, y: cam.y, z: tgt.z }, 800); });

    // ── drawing a frame ──
    function frame(now) {
      requestAnimationFrame(frame);
      if (!renderer || !host.classList.contains('on')) return;
      var w = host.clientWidth, h = host.clientHeight; if (w !== W || h !== H) { W = w; H = h; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }
      if (fly) { var t = clamp((now - fly.t0) / fly.ms, 0, 1), e = ease(t); cam.x = fly.from.x + (fly.to.x - fly.from.x) * e; cam.y = fly.from.y + (fly.to.y - fly.from.y) * e; cam.z = fly.from.z + (fly.to.z - fly.from.z) * e; if (t >= 1) fly = null; }
      else { var k = reduced ? 1 : 0.12; cam.x += (tgt.x - cam.x) * k; cam.y += (tgt.y - cam.y) * k; cam.z += (tgt.z - cam.z) * k; }
      var lk = reduced ? 1 : 0.14; look.yaw += (look.ty - look.yaw) * lk; look.pitch += (look.tp - look.pitch) * lk;
      var out = Math.max(0, cam.z - front()); scene.fog.near = 16 + out; scene.fog.far = 78 + out;
      camera.position.set(cam.x + par.x * 0.35, cam.y + par.y * 0.25, cam.z); camera.rotation.set(look.pitch + par.y * 0.02, look.yaw - par.x * 0.025, 0, 'YXZ');
      // things seen from close by are sharpened by their own hover; a thing under the pointer grows a little
      Object.keys(items).forEach(function (id) { var it = items[id], want = (hover === id || sel === id) ? 1.05 : 1; it.hover += (want - it.hover) * 0.2; var s = it.hover; it.mesh.scale.set(it.w * s, it.h * s, 1); });
      renderer.render(scene, camera); captions(); updateTimeline();
    }
    var proj = null;
    function captions() {
      var list = [], v = proj || (proj = new THREE.Vector3());
      Object.keys(items).forEach(function (id) {
        var it = items[id], dz = cam.z - it.z, show = dz > 0.6 && dz < 46;
        if (show) { v.set(it.mesh.position.x, it.mesh.position.y + it.h / 2 + 0.1, it.z); v.project(camera); show = v.z < 1 && Math.abs(v.x) < 1.15 && Math.abs(v.y) < 1.15; if (show) list.push({ it: it, x: (v.x * 0.5 + 0.5) * W, y: (-v.y * 0.5 + 0.5) * H, dz: dz }); }
        else it.cap.style.display = 'none';
      });
      list.sort(function (a, b) { return a.dz - b.dz; });
      list.forEach(function (p, i) {
        var c = p.it.cap; if (i > 70) { c.style.display = 'none'; return; }
        var b = p.it.b, d = b.data || {}, name = b.type === 'node' && d.kind !== 'label' && d.kind !== 'genre' ? d.name : '';
        var html = '<i>' + pad(b.n) + '</i>' + (name ? '. ' + esc(name) : '');
        if (c._h !== html) { c.innerHTML = html; c._h = html; }
        c.style.display = ''; c.style.transform = 'translate(' + p.x.toFixed(1) + 'px,' + p.y.toFixed(1) + 'px) translate(-50%,-100%)'; var lit = sel === b.id || hover === b.id, far = clamp(1 - Math.max(0, cam.z - front()) / 35, 0, 1);   // seen from far away the names fade out, so only the shapes remain
        c.style.opacity = String(lit ? 1 : clamp(1 - p.dz / 46, 0.12, 1) * far); c.classList.toggle('on', sel === b.id || hover === b.id);
      });
    }

    // ── pointing: hover, click, drag a thing, drag to look ──
    var ray = null, ndc = null, hover = null, down = null, linkFrom = null, plane = null;
    function pick(e) {
      if (!THREE || !meshes.length) return null; var r = canvas.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(ndc, camera);
      var hit = ray.intersectObjects(meshes, false); return hit.length ? hit[0].object.userData.id : null;
    }
    var blockById = function (id) { return blocks.find(function (b) { return b.id === id; }); };
    function onDown(e) {
      if (e.button !== 0 && e.button !== 1) return;
      var id = pick(e); down = { x: e.clientX, y: e.clientY, id: id, moved: false, yaw: look.ty, pitch: look.tp, off: null };
      if (id && !ro) { var it = items[id]; plane.set(new THREE.Vector3(0, 0, 1), -it.z); var p = new THREE.Vector3(); if (ray.ray.intersectPlane(plane, p)) down.off = { x: p.x - it.mesh.position.x, y: p.y - it.mesh.position.y }; }
      canvas.setPointerCapture(e.pointerId); ringEl.classList.add('down'); fly = null;
    }
    function onMove(e) {
      if (fine && e.pointerType !== 'touch') { ringEl.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)'; }
      if (!down) {
        var r = canvas.getBoundingClientRect(); par.x = ((e.clientX - r.left) / r.width - 0.5) * 2; par.y = ((e.clientY - r.top) / r.height - 0.5) * 2; if (reduced) par.x = par.y = 0;
        var id = pick(e); if (id !== hover) { hover = id; canvas.style.cursor = id ? (linkFrom ? 'crosshair' : 'pointer') : (fine ? 'none' : 'default'); }
        ringEl.classList.toggle('on', fine && !id && e.pointerType !== 'touch'); return;
      }
      var dx = e.clientX - down.x, dy = e.clientY - down.y;
      if (!down.moved && Math.hypot(dx, dy) < 4) return; down.moved = true;
      if (down.id && down.off && !linkFrom) {                                          // drag a thing across its own depth
        var it = items[down.id], r2 = canvas.getBoundingClientRect(); ndc.set(((e.clientX - r2.left) / r2.width) * 2 - 1, -((e.clientY - r2.top) / r2.height) * 2 + 1); ray.setFromCamera(ndc, camera);
        plane.set(new THREE.Vector3(0, 0, 1), -it.z); var p = new THREE.Vector3();
        if (ray.ray.intersectPlane(plane, p)) { it.mesh.position.x = p.x - down.off.x; it.mesh.position.y = p.y - down.off.y; var b = it.b; b.x = (it.mesh.position.x / S) - b.w / 2; b.y = -(it.mesh.position.y / S) - b.h / 2; rebuildLines(); }
      } else {                                                                         // drag the empty void: look around
        look.ty = clamp(down.yaw + dx * 0.0042, -0.9, 0.9); look.tp = clamp(down.pitch + dy * 0.0042, -0.6, 0.6);
      }
    }
    function onUp(e) {
      ringEl.classList.remove('down'); var d = down; down = null; if (!d) return;
      if (d.moved) { if (d.id && !ro) { var b = blockById(d.id); if (b) { b.x = Math.round(b.x); b.y = Math.round(b.y); changed(); } } return; }
      if (d.id) {                                                                       // a click on a thing
        var blk = blockById(d.id); if (!blk) return;
        if (linkFrom) { var from = linkFrom; linkFrom = null; say(''); canvas.style.cursor = ''; if (from !== blk.id) addLink(from, blk.id); return; }
        select(blk.id); flyTo(blk); fire('click', blk);
      } else { if (linkFrom) { linkFrom = null; say(''); } select(null); flyBack(); }  // a click on nothing: let go, and fly back out
    }
    canvas.addEventListener('pointerdown', onDown); canvas.addEventListener('pointermove', onMove); canvas.addEventListener('pointerup', onUp); canvas.addEventListener('pointercancel', onUp);
    canvas.addEventListener('pointerleave', function () { ringEl.classList.remove('on'); par.x = par.y = 0; hover = null; });
    canvas.addEventListener('dblclick', function (e) { var id = pick(e); if (id) fire('dblclick', blockById(id)); });
    canvas.addEventListener('wheel', function (e) {                                    // two-finger scroll pans across the wall; pinch (a ctrl-wheel) moves slowly through depth
      e.preventDefault(); var r = zRange(); fly = null; back = null;
      var cap = function (v, m) { return Math.max(-m, Math.min(m, v)); };               // one flick of momentum must never throw the camera across the space
      if (e.ctrlKey) tgt.z = clamp(tgt.z + cap(e.deltaY, 12) * 0.05, r.min, r.max);
      else { var pz = Math.max(1, (cam.z - depth.zMax) / FOCUS); tgt.x += cap(e.deltaX, 60) * 0.004 * pz; tgt.y -= cap(e.deltaY, 60) * 0.004 * pz; }   // pan faster the further back you are
    }, { passive: false });
    var fire = function (kind, b) { hooks[kind].forEach(function (f) { try { f(b); } catch (_) {} }); };

    // ── choosing a thing ──
    function select(id) {
      sel = id; var b = id ? blockById(id) : null; showSel(b); fire('select', b);
    }
    function showSel(b) {
      if (!b) { selEl.classList.remove('on'); selEl.innerHTML = ''; return; }
      selEl.classList.add('on');
      if (ro) {                                                                         // visitors: what it is, and a way out to it
        var lab = root.WallIndex && root.WallIndex.pure ? root.WallIndex.pure.labelOf(b) : { title: '', sub: '' }, d0 = b.data || {};
        var href = [d0.url, d0.deezer].filter(function (u) { return /^https?:\/\//.test(String(u || '')); })[0];
        selEl.innerHTML = '<span class="vd-sel__n">' + pad(b.n) + '</span><span class="vd-sel__t">' + esc(lab.title) + (lab.sub ? ' <i>' + esc(lab.sub) + '</i>' : '') + '</span>' +
          (href ? '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">[ Open ↗ ]</a>' : ''); return;
      }
      selEl.innerHTML = '<span class="vd-sel__n">' + pad(b.n) + '</span>' + (b.type !== 'node' ? '<button data-v="edit">[ Edit ]</button>' : '') + '<button data-v="link">[ Link ]</button><button data-v="delete">[ Delete ]</button>';
    }
    selEl.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-v]'); if (!btn) return; var b = blockById(sel); if (!b) return;
      if (btn.dataset.v === 'edit') openForm(b);
      else if (btn.dataset.v === 'link') { linkFrom = b.id; say('now click the thing to join it to  ·  esc to cancel'); }
      else if (btn.dataset.v === 'delete') { if (confirm('Remove this from the Void?')) { removeBlocks([b.id]); select(null); changed(); flyBack(); } }
    });

    // ── lines between things ──
    function addLink(a, b) {
      var LinkOps = root.MRRoomCanvas && root.MRRoomCanvas.LinkOps; if (!LinkOps) return;
      var r = LinkOps.add(links, a, b, 'mine'); if (!r.added) return; links = r.links; changed(); say('joined');
      setTimeout(function () { if (statusEl.textContent === 'joined') say(''); }, 1200);
    }

    // ── adding ──
    addMenu.innerHTML = '<button data-type="__music"><b>Music</b><small>from what is playing, a liked song, or an album you type</small></button>' +
      [['text', 'Words', 'a heading and some words'], ['quote', 'Quote', 'words someone said, and where from'], ['link', 'Link or video', 'any web page or video, as a card'], ['image', 'Picture', 'a picture, from a link']]
        .map(function (t) { return '<button data-type="' + t[0] + '"><b>' + t[1] + '</b><small>' + t[2] + '</small></button>'; }).join('');
    addBtn.addEventListener('click', function (e) { e.stopPropagation(); addMenu.classList.toggle('on'); });
    document.addEventListener('click', function (e) { if (!addMenu.contains(e.target) && e.target !== addBtn) addMenu.classList.remove('on'); });
    addMenu.addEventListener('click', function (e) {
      var t = e.target.closest('[data-type]'); if (!t) return; addMenu.classList.remove('on');
      if (t.dataset.type === '__music') { if (root.WallMusic) root.WallMusic.openStart(null); return; }
      var T0 = TYPES[t.dataset.type] || TYPES.text, want = viewCentreWorld(), free = root.WallMusic && root.WallMusic.pure && root.WallMusic.pure.freeSpot;
      var spot = free ? free(api.nearBlocks(null).map(function (k) { return { x: k.x, y: k.y, w: k.w, h: k.h }; }), want, { w: T0.w, h: T0.h }, 40) : null;      // not on top of what is already there
      var b = addNode(t.dataset.type, {}, spot || want, 0); changed(); select(b.id); flyTo(b); openForm(b);
    });
    function addNode(type, data, mid, d, size, c) {
      var T0 = TYPES[type] || TYPES.text, w = size ? size.w : T0.w, h = size ? size.h : T0.h;
      var b = { id: uid(), type: type, x: Math.round(mid.x - w / 2), y: Math.round(mid.y - h / 2), w: w, h: h, c: c || Date.now(), n: 0, d: d || 0, data: data || {} };
      blocks.push(b); return b;
    }
    function removeBlocks(ids) { var set = {}; ids.forEach(function (i) { set[i] = 1; }); blocks = blocks.filter(function (b) { return !set[b.id]; }); links = links.filter(function (l) { return !set[l.a] && !set[l.b]; }); if (sel && set[sel]) sel = null; }
    var viewCentreWorld = function () { return { x: cam.x / S, y: -cam.y / S }; };
    function changed() { numberAll(); sync(); dirty(); }

    // ── filling in a thing (words, a quote, a link, a picture) ──
    var repaintT = 0;
    function repaint(b) { clearTimeout(repaintT); repaintT = setTimeout(function () { var it = items[b.id]; if (it) { paintItem(it); place(it); } dirty(); }, 180); }
    function openForm(b) {
      var d = b.data = b.data || {}, row = function (k, label, ph, area) { var v = esc(d[k] || ''); return '<label>' + label + (area ? '<textarea data-k="' + k + '" rows="4" placeholder="' + ph + '">' + v + '</textarea>' : '<input type="text" data-k="' + k + '" placeholder="' + ph + '" value="' + v + '">') + '</label>'; };
      var body = b.type === 'text' ? row('heading', 'Heading', 'a heading') + row('body', 'Words', 'what you want to say', true)
        : b.type === 'quote' ? row('text', 'The words', 'as they were said or written', true) + row('who', 'Who said it', 'a name') + row('source', 'Where from', 'a talk, a book, a film') + row('url', 'A link to it', 'https://… (optional)')
        : b.type === 'image' ? row('src', 'Picture address', 'https://… (a picture on the web)') + row('alt', 'What it is', 'a few words')
        : row('url', 'Address', 'https://…') + row('title', 'Title', 'what to call it');
      formEl.innerHTML = '<button class="vd-form__x" data-f="close" title="Close">×</button><h2>' + esc(KIND_WORD[b.type] || 'Thing') + ' ' + pad(b.n) + '</h2>' + body + '<div class="rc-info__row"><button class="go" data-f="close">Done</button></div>';
      formEl.classList.add('on'); formEl.dataset.id = b.id; var f = formEl.querySelector('input, textarea'); if (f) f.focus();
    }
    formEl.addEventListener('input', function (e) { var k = e.target.dataset && e.target.dataset.k, b = blockById(formEl.dataset.id); if (!k || !b) return; b.data[k] = e.target.value; repaint(b); });
    formEl.addEventListener('change', async function (e) {                                // a link names itself
      var k = e.target.dataset && e.target.dataset.k, b = blockById(formEl.dataset.id); if (!b || k !== 'url' || (b.type !== 'link' && b.type !== 'mix') || (b.data.title || '').trim() || !/^https?:\/\//.test(e.target.value)) return;
      try { var r = await fetch('/api/discover/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: e.target.value }) }), m = await r.json(); if (m && m.title && !(b.data.title || '').trim()) { b.data.title = m.title; var t = formEl.querySelector('[data-k=title]'); if (t) t.value = m.title; repaint(b); } } catch (_) {}
    });
    formEl.addEventListener('click', function (e) { if (e.target.closest('[data-f=close]')) { formEl.classList.remove('on'); formEl.innerHTML = ''; } });

    // ── the intro: an empty void says one quiet thing, typed out (once a visit), with a skip ──
    var INTRO = ['Everything you save is connected to something.', '   Start with a record, a line someone said, or a picture.'], introT = 0, introLine = 0, introChar = 0;
    var introSeen = function () { try { return sessionStorage.getItem('rc_intro') === '1'; } catch (_) { return false; } };
    function introEls() { var r = emptyEl.querySelector('#rc-intro'); return { lines: [].slice.call(r.querySelectorAll('.rc-intro__l')), go: r.querySelector('.rc-intro__go'), skip: r.querySelector('.rc-intro__skip') }; }
    function introFinish() { clearTimeout(introT); introT = 0; var e = introEls(); e.lines.forEach(function (l, i) { l.textContent = INTRO[i]; l.classList.remove('typing'); }); e.go.hidden = false; e.skip.hidden = true; introLine = 2; try { sessionStorage.setItem('rc_intro', '1'); } catch (_) {} }
    function intro() {
      var empty = loaded && !blocks.length; emptyEl.style.display = empty ? '' : 'none'; if (!empty) { clearTimeout(introT); introT = 0; introLine = 0; introChar = 0; return; }
      if (introT || introLine >= 2) return; if (reduced || introSeen()) { introFinish(); return; }
      var e = introEls(); e.go.hidden = true; e.skip.hidden = false;
      var step = function () { introT = 0; var text = INTRO[introLine]; introChar++; e.lines[introLine].textContent = text.slice(0, introChar); e.lines[introLine].classList.add('typing');
        if (introChar >= text.length) { e.lines[introLine].classList.remove('typing'); introLine++; introChar = 0; if (introLine >= 2) { introFinish(); return; } introT = setTimeout(step, 520); } else introT = setTimeout(step, 22 + Math.floor(Math.random() * 34)); };
      step();
    }
    emptyEl.addEventListener('click', function (e) {
      var b = e.target.closest('[data-intro]'); if (!b) return; e.stopPropagation(); var a = b.dataset.intro;
      if (a === 'skip') introFinish(); else if (a === 'music') { if (root.WallMusic) root.WallMusic.openStart(null); } else if (a === 'block') addBtn.click();
    });

    // ── the controls ──
    host.querySelector('#rc-hud').addEventListener('click', function (e) { var b = e.target.closest('[data-hud="now"]'); if (b) { select(null); flyNow(); } });
    var onKey = function (e) {
      if (!host.classList.contains('on')) return;
      var t = e.target, typing = !!(t && t.matches && t.matches('input, textarea, select, [contenteditable]')); if (typing) { if (e.key === 'Escape' && formEl.contains(t)) { formEl.classList.remove('on'); formEl.innerHTML = ''; } return; }
      var r = zRange();
      if (e.key === 'Escape') { if (linkFrom) { linkFrom = null; say(''); } else if (formEl.classList.contains('on')) { formEl.classList.remove('on'); formEl.innerHTML = ''; } else { select(null); flyBack(); } }
      else if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); fly = null; tgt.z = clamp(tgt.z - (e.key === 'PageDown' ? 18 : 4), r.min, r.max); }
      else if (e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); fly = null; tgt.z = clamp(tgt.z + (e.key === 'PageUp' ? 18 : 4), r.min, r.max); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); tgt.x -= 1.2; } else if (e.key === 'ArrowRight') { e.preventDefault(); tgt.x += 1.2; }
      else if (e.key === 'Home') { e.preventDefault(); select(null); flyNow(); }
    };

    // ── what the music, the record page and the list are built on ──
    var api = {
      host: host, TYPES: TYPES, get blocks() { return blocks; }, get links() { return links; },
      on: function (kind, fn) { if (hooks[kind]) hooks[kind].push(fn); },
      addNode: addNode, removeBlocks: removeBlocks, setLinks: function (next) { links = next; }, changed: changed, select: select, viewCentreWorld: viewCentreWorld,
      snap: function (v) { return Math.round(v); }, flyTo: flyTo, flyToGroup: flyToGroup, fit: flyNow,
      panTo: function (x, y) { var b = blocks.find(function (k) { return Math.abs(k.x + k.w / 2 - x) < 1.5 && Math.abs(k.y + k.h / 2 - y) < 1.5; }); if (b) flyTo(b); },
      // the things a new arrival must not land on: only those at about the depth it is arriving at (not a world away in time)
      nearBlocks: function (anchor) { var z = anchor && depth.z[anchor.id] != null ? depth.z[anchor.id] : depth.zMax; var ids = VL.nearIds(depth.z, z, 8); return blocks.filter(function (b) { return ids.indexOf(b.id) >= 0; }); },
      screenOf: function (id) { var it = items[id]; if (!it || !camera) return null; var v = new THREE.Vector3(it.mesh.position.x, it.mesh.position.y, it.z); v.project(camera); var r = canvas.getBoundingClientRect(); return { x: r.left + (v.x * 0.5 + 0.5) * r.width, y: r.top + (-v.y * 0.5 + 0.5) * r.height, behind: v.z > 1 }; },
      addLink: addLink, void: true, state: function () { return { cam: { x: cam.x, y: cam.y, z: cam.z }, depth: depth, items: Object.keys(items).length }; }
    };

    // ── start: the 3D engine, then the things ──
    api.ready = (async function () {
      var data = { blocks: [], links: [], next: 0 };
      if (ro) data = cleanSnapshot(opts.data); else try { data = await (await fetch('/api/room/void/' + placeId)).json(); } catch (_) {}
      blocks = Array.isArray(data.blocks) ? data.blocks : []; links = Array.isArray(data.links) ? data.links : []; counter = data.next | 0;
      try { THREE = await import('three'); } catch (e) { say('The Void needs the 3D engine, which did not load.'); loaded = true; intro(); return; }
      await fontsReady;
      try {
        renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false }); renderer.setPixelRatio(Math.min(root.devicePixelRatio || 1, 2));
      } catch (e) { say('The Void needs WebGL, which this browser does not have.'); loaded = true; intro(); return; }
      scene = new THREE.Scene(); var bg = new THREE.Color(T.bg); scene.background = bg; scene.fog = new THREE.Fog(bg, 16, 78);
      camera = new THREE.PerspectiveCamera(52, 1, 0.05, 300); ray = new THREE.Raycaster(); ndc = new THREE.Vector2(); plane = new THREE.Plane();
      var lm = function (color, op) { var g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(0), 3)); return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: new THREE.Color(color), transparent: true, opacity: op, fog: true, depthWrite: false })); };
      lineMine = lm(T.text, 0.6); lineNear = lm(T.muted, 0.28); lineMine.frustumCulled = lineNear.frustumCulled = false; scene.add(lineMine, lineNear);
      loaded = true; var added = numberAll(); sync(); cam.z = tgt.z = front() + 4; flyNow(); if (added && blocks.length) dirty();
      addEventListener('keydown', onKey);
      requestAnimationFrame(frame);
    })();

    host.__vd = api;                                       // (so a test, or the console, can ask where the camera is)

    // the music, the record page and the list join in (their own keys are registered first, so Esc closes a page before it leaves a thing)
    if (root.WallMusic && !ro) root.WallMusic.attach(api);
    if (root.WallIndex) root.WallIndex.attach(api);
    return api;
  }

  var api = { mount: mount, cleanSnapshot: cleanSnapshot, pictureUrl: pictureUrl, wrapLines: wrapLines, TYPES: TYPES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.MRVoid = api;
})(typeof window !== 'undefined' ? window : this);
