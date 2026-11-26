/* conn_start.js — where a web can start from: the song playing now, or a song you like.
 *
 *   ConnStart.nowPlaying(state)   the app's player state (/api/seinnteoir/state) -> the song to start from, or null
 *   ConnStart.fromLiked(row)      a row of /api/spotify/saved-tracks -> { artist, title, album, image }
 *   ConnStart.fromSearch(row)     a track row of /api/spotify/search -> the same shape
 *   ConnStart.query(song)         -> the query string that opens the web: ?kind=track&artist=…&title=…
 *
 * Pure (no DOM), so it runs under node in the tests.
 */
(function (root) {
  'use strict';

  var str = function (v) { return typeof v === 'string' ? v.trim() : ''; };

  // What is playing? The app's own player wins when it is playing; then Spotify playing somewhere else
  // (a phone, another room); then whatever was last loaded and paused. Never a song with no title.
  function nowPlaying(state) {
    if (!state || typeof state !== 'object') return null;
    var q = Array.isArray(state.queue) ? state.queue : [], cur = q[state.idx | 0] || null;
    var g = state.guest && typeof state.guest === 'object' ? state.guest : null;
    var r = state.remote && typeof state.remote === 'object' ? state.remote : null;
    var fromQ = cur && str(cur.title) ? { artist: str(cur.artist), title: str(cur.title), album: '', image: str(cur.image_url), playing: !!state.playing, where: 'the player' } : null;
    var fromG = g && str(g.title) ? { artist: str(g.artist), title: str(g.title), album: '', image: str(g.image_url), playing: !!g.playing, where: str(g.device) || 'Spotify' } : null;
    var fromR = r && str(r.title) ? { artist: str(r.artist), title: str(r.title), album: '', image: '', playing: true, where: str(r.device) || 'Spotify' } : null;
    if (fromQ && fromQ.playing) return fromQ;
    if (fromG && fromG.playing) return fromG;
    if (fromR) return fromR;
    return fromG || fromQ || null;
  }

  function fromLiked(row) {
    if (!row || !str(row.title)) return null;
    return { artist: str(row.artist), title: str(row.title), album: str(row.album), image: str(row.album_image_small || row.album_image_url) };
  }
  function fromSearch(row) {
    if (!row || row.stream_type !== 'track' || !str(row.title)) return null;
    return { artist: str(row.subtitle), title: str(row.title), album: str(row.album), image: str(row.thumb || row.image) };
  }
  function query(song) {
    var p = new URLSearchParams({ kind: 'track', artist: song.artist || '', title: song.title || '' });
    if (song.album) p.set('album', song.album);
    return p;
  }

  var api = { nowPlaying: nowPlaying, fromLiked: fromLiked, fromSearch: fromSearch, query: query };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ConnStart = api;
})(typeof window !== 'undefined' ? window : this);
