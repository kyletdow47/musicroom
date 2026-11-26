/* room_add.js — bring an album you found into My Room.
 *
 * Given an artist and an album title (from anywhere), find the real album on Spotify, read its songs,
 * and put them in a grúpa. The steps use the app's own endpoints (/api/spotify/search,
 * /api/find/album-tracks, /api/grupa), so one Spotify connection and one rate limiter serve everything.
 *
 *   RoomAdd.pick(results, artist, album)   which Spotify album is the right one (pure, tested)
 *   RoomAdd.create(fetch)                  the steps below, with `fetch` replaceable for tests
 *       .match(artist, album)              -> { album } | { error, notConnected }
 *       .songs(album)                      -> { items, whole }   whole = true when only the album itself could be added
 *       .grupas(firstUrl)                  -> [{ id, name, has }]
 *       .add({ items, grupaId, name, note }) -> { grupaId, added, already }
 */
(function (root) {
  'use strict';

  var fold = function (s) {
    return String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
      .replace(/\((deluxe|expanded|remaster(ed)?|bonus)[^)]*\)|\[[^\]]*\]/g, '').replace(/[^a-z0-9]+/g, '');
  };

  // Which result is the album we mean? The same title by the same artist beats the same title by
  // anyone, which beats the first album by that artist. Never a different artist's album.
  function pick(results, artist, album) {
    var albums = (results || []).filter(function (r) { return r && r.stream_type === 'album' && r.url; });
    var a = fold(artist), t = fold(album);
    var byArtist = albums.filter(function (r) { var s = fold(r.subtitle); return !a || (s && (s.indexOf(a) >= 0 || a.indexOf(s) >= 0)); });
    var exact = byArtist.filter(function (r) { return t && fold(r.title) === t; });
    if (exact.length) return exact[0];
    var close = byArtist.filter(function (r) { var x = fold(r.title); return t && (x.indexOf(t) >= 0 || t.indexOf(x) >= 0); });
    if (close.length) return close[0];
    return byArtist[0] || null;
  }

  function create(f) {
    f = f || function (u, o) { return root.fetch(u, o); };
    var j = function (url, opt) {
      return f(url, opt).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok && !d.error) d.error = 'Something went wrong'; d._ok = r.ok; return d; });
      });
    };
    var post = function (url, body) { return j(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); };
    var enc = encodeURIComponent;

    return {
      match: function (artist, album) {
        return j('/api/spotify/search?q=' + enc((artist + ' ' + album).trim())).then(function (d) {
          if (d.connected === false) return { notConnected: true, error: 'Spotify is not connected.' };
          if (d.error) return { error: d.error };
          var m = pick(d.results, artist, album);
          return m ? { album: m } : { error: 'That album was not found on Spotify.' };
        });
      },
      songs: function (album) {
        var id = (String(album.url).match(/album\/([A-Za-z0-9]+)/) || [])[1];
        var whole = [{ url: album.url, title: album.title, subtitle: album.subtitle, image: album.image || album.thumb || '', by_ids: album.by_ids, duration_sec: album.duration_sec }];
        if (!id) return Promise.resolve({ items: whole, whole: true });
        return j('/api/find/album-tracks?id=' + id + '&image=' + enc(album.image || album.thumb || '')).then(function (d) {
          return (d.error || !(d.items || []).length) ? { items: whole, whole: true, why: d.error || '' } : { items: d.items, whole: false };
        });
      },
      grupas: function (firstUrl) {
        return j('/api/grupa?url=' + enc(firstUrl || '')).then(function (d) {
          return (d.grupa || []).map(function (g) { return { id: g.id, name: g.name, has: !!g.has }; });
        });
      },
      add: function (o) {
        var go = function (gid) {
          return post('/api/grupa/' + gid + '/streams', { items: o.items }).then(function (a) {
            return a.error ? { error: a.error } : { grupaId: gid, added: a.added || 0, already: a.already || 0 };
          });
        };
        if (o.grupaId) return go(o.grupaId);
        return post('/api/grupa', { name: (o.name || 'Found').slice(0, 200), description: o.note || '' }).then(function (c) {
          return c.error ? { error: c.error } : go(c.id);
        });
      }
    };
  }

  var api = { pick: pick, fold: fold, create: create };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.RoomAdd = api;
})(typeof window !== 'undefined' ? window : this);
