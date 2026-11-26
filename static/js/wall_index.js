/* wall_index.js — the wall as a list.
 *
 * The wall is a map; this is its index. Everything on it, in the order it was numbered (001, 002 …), as a quiet centred
 * column, after the Listado of 197 Historias Ilustradas. A filter by kind of thing, a search that ignores capitals and
 * accents, and a click on any row that takes you there on the wall (and selects it).
 *
 *   WallIndex.pure   rows, filters and search with no page needed (this is what the tests run)
 *   WallIndex.attach the list view and its button, joined to a mounted wall (room_canvas.js calls it)
 */
(function (root) {
  'use strict';

  var FILTERS = [['all', 'All'], ['node', 'Music'], ['text', 'Words'], ['quote', 'Quotes'], ['image', 'Pictures'], ['mix', 'Videos'], ['link', 'Links'], ['record', 'Records']];
  var GROUP = { node: 'node', text: 'text', quote: 'quote', image: 'image', mix: 'mix', link: 'link', record: 'record', playlist: 'record', web: 'web' };
  var TYPE_WORD = { node: 'music', text: 'words', quote: 'quote', image: 'picture', mix: 'video', link: 'link', record: 'record', playlist: 'playlist', web: 'old web' };

  var str = function (v) { return typeof v === 'string' ? v.trim() : ''; };
  var cut = function (s, n) { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s; };
  function fold(s) { return String(s == null ? '' : s).normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase(); }
  function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (_) { return ''; } }

  // what a block is called in the list, and a muted line under that
  function labelOf(b) {
    var d = (b && b.data && typeof b.data === 'object') ? b.data : {}, t = b && b.type;
    if (t === 'node') return { title: str(d.name) || '(untitled)', sub: d.kind === 'album' ? ['album', str(d.sub), str(d.year)].filter(Boolean).join(' · ') : (d.kind || 'music') };
    if (t === 'text') { var h = str(d.heading), body = str(d.body); return { title: cut(h || body, 70) || '(empty words)', sub: h && body ? cut(body, 60) : '' }; }
    if (t === 'quote') return { title: str(d.text) ? '“' + cut(d.text, 70) + '”' : '(empty quote)', sub: [str(d.who), str(d.source)].filter(Boolean).join(', ') };
    if (t === 'image') return { title: str(d.alt) || str(d.caption) || 'a picture', sub: str(d.caption) && str(d.alt) ? cut(d.caption, 60) : '' };
    if (t === 'mix' || t === 'link') return { title: str(d.title) || hostOf(d.url) || str(d.url) || 'a ' + TYPE_WORD[t], sub: hostOf(d.url) };
    if (t === 'record') { var tr = d.track || {}; return { title: str(tr.title) || 'a record', sub: str(tr.artist_name || tr.artist) }; }
    if (t === 'playlist') return { title: str(d.title) || str(d.name) || 'a playlist', sub: '' };
    if (t === 'web') return { title: str(d.title) || 'a web', sub: 'a frozen web: unpack it' };
    return { title: 'a ' + String(t || 'block'), sub: '' };
  }
  // everything searchable about a block: its label plus any plain words in its data
  function haystack(b, lab) {
    var d = (b && b.data && typeof b.data === 'object') ? b.data : {}, out = [lab.title, lab.sub];
    Object.keys(d).forEach(function (k) { if (typeof d[k] === 'string') out.push(d[k]); });
    if (d.track && typeof d.track === 'object') { out.push(str(d.track.title), str(d.track.artist_name), str(d.track.album)); }
    return fold(out.join(' ')).slice(0, 4000);
  }
  // the whole wall as rows, in numbered order (a block with no number yet comes last, in the order it sits in the list)
  function rowsOf(blocks) {
    var rows = [];
    (Array.isArray(blocks) ? blocks : []).forEach(function (b, i) {
      if (!b || typeof b !== 'object' || typeof b.id !== 'string') return;
      var lab = labelOf(b);
      rows.push({ id: b.id, n: b.n > 0 ? b.n : 0, order: i, type: b.type, group: GROUP[b.type] || 'other', title: lab.title, sub: lab.sub, kind: TYPE_WORD[b.type] || 'block', hay: haystack(b, lab) });
    });
    rows.sort(function (x, y) { return (x.n || 1e9) - (y.n || 1e9) || x.order - y.order; });
    return rows;
  }
  // a kind of thing ('all' = everything) and/or a search: every word typed must be somewhere in the row
  function filterRows(rows, group, query) {
    var words = fold(query).split(/\s+/).filter(Boolean);
    return rows.filter(function (r) { return (!group || group === 'all' || r.group === group) && words.every(function (w) { return r.hay.indexOf(w) >= 0; }); });
  }
  function counts(rows) { var c = { all: rows.length }; rows.forEach(function (r) { c[r.group] = (c[r.group] || 0) + 1; }); return c; }
  var pad = function (n) { return n > 0 ? String(n).padStart(3, '0') : '   '; };

  var pure = { rowsOf: rowsOf, filterRows: filterRows, counts: counts, labelOf: labelOf, fold: fold, FILTERS: FILTERS };
  var api = { pure: pure, attach: function () {} };
  if (typeof document === 'undefined') { if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.WallIndex = api; return; }

  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; }); };

  api.attach = function (wall) {
    var host = wall.host, hud = host.querySelector('#rc-hud'), list = document.createElement('div');
    list.className = 'rc-ix'; host.appendChild(list);
    var group = 'all', query = '', open = false, filtersOpen = false;
    hud.insertAdjacentHTML('beforeend', '<button data-hud="list" id="rc-listbtn" title="Everything on the wall, as a list (press / to search)">List</button>');
    var btn = hud.querySelector('#rc-listbtn');

    function draw() {
      var all = rowsOf(wall.blocks), shown = filterRows(all, group, query), c = counts(all);
      list.innerHTML = '<div class="rc-ix__top"><button data-l="filters">[ Filters ' + (filtersOpen ? '−' : '+') + ' ]</button>' +
        '<span class="rc-ix__count">' + shown.length + (shown.length === all.length ? '' : ' of ' + all.length) + '</span>' +
        '<label class="rc-ix__search"><span>Search</span><input type="text" id="rc-lq" value="' + esc(query) + '" placeholder="a name, a word" autocomplete="off"></label></div>' +
        (filtersOpen ? '<div class="rc-ix__filters">' + FILTERS.filter(function (f) { return f[0] === 'all' || c[f[0]]; }).map(function (f) {
          return '<button data-l="group" data-g="' + f[0] + '"' + (f[0] === group ? ' class="on"' : '') + '>' + f[1] + ' <i>' + (c[f[0]] || 0) + '</i></button>'; }).join('') + '</div>' : '') +
        '<div class="rc-ix__col">' + (shown.length ? shown.map(function (r) {
          return '<button class="rc-ix__row" data-l="go" data-id="' + esc(r.id) + '"><span class="rc-ix__no">' + esc(pad(r.n)) + '</span><span class="rc-ix__t">' + esc(r.title) + '</span>' + (r.sub ? '<span class="rc-ix__s">' + esc(r.sub) + '</span>' : '') + '</button>'; }).join('')
          : '<p class="rc-ix__none">' + (all.length ? 'Nothing matches' + (query ? ' “' + esc(query) + '”' : '') + '.' : 'Nothing on the wall yet.') + '</p>') + '</div>';
      var q = list.querySelector('#rc-lq'); if (q && open && document.activeElement === document.body) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
    }
    function show(focusSearch) {
      open = true; list.classList.add('on'); host.classList.add('listing'); btn.textContent = 'Wall'; draw();
      if (focusSearch) { var q = list.querySelector('#rc-lq'); if (q) q.focus(); }
    }
    function hide() { open = false; list.classList.remove('on'); host.classList.remove('listing'); btn.textContent = 'List'; }
    btn.addEventListener('click', function (e) { e.stopPropagation(); open ? hide() : show(false); });
    list.addEventListener('input', function (e) {
      if (e.target.id !== 'rc-lq') return; query = e.target.value; var pos = e.target.selectionStart; draw();
      var q = list.querySelector('#rc-lq'); q.focus(); q.setSelectionRange(pos, pos);
    });
    list.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-l]'); if (!b) return; var a = b.dataset.l;
      if (a === 'filters') { filtersOpen = !filtersOpen; draw(); }
      else if (a === 'group') { group = b.dataset.g; draw(); }
      else if (a === 'go') {                                               // take me there: on the wall, selected
        var blk = wall.blocks.find(function (x) { return x.id === b.dataset.id; }); if (!blk) return;
        hide(); wall.panTo(blk.x + blk.w / 2, blk.y + blk.h / 2); wall.select(blk.id);
      }
    });
    // "/" opens the list ready to search; Esc closes it; Enter in the search goes to the first match
    window.addEventListener('keydown', function (e) {
      if (!host.classList.contains('on')) return;
      var t = e.target, typing = !!(t && t.matches && t.matches('input, textarea, select, [contenteditable]'));
      if (open && e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); hide(); return; }
      if (open && e.key === 'Enter' && t && t.id === 'rc-lq') { var first = list.querySelector('.rc-ix__row'); if (first) { e.preventDefault(); first.click(); } return; }
      if (!open && !typing && e.key === '/' && !e.metaKey && !e.ctrlKey && !host.querySelector('.rc-page.on')) { e.preventDefault(); show(true); }
    }, true);
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.WallIndex = api;
})(typeof window !== 'undefined' ? window : this);
