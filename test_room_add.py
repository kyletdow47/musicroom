"""Proves "bring an album into My Room" (static/js/room_add.js): the right Spotify album is picked
(never another artist's), its songs are read, and they land in a new or an existing grúpa, with a
plain fallback when Spotify will not list the songs.

Pure module, run under node with a fake network.

Run:  venv/bin/python test_room_add.py
"""
import os, sys, json, subprocess, shutil

PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)

JS = r'''
const R = require(process.argv[1]);
const alb = (title, subtitle, id) => ({ stream_type: 'album', title, subtitle, url: 'https://open.spotify.com/album/' + id, image: 'http://i/' + id });
const out = {};
const P = (res, a, t) => { const x = R.pick(res, a, t); return x && x.title + '|' + x.subtitle; };
out.exact = P([alb('Kosmobotanika (Instrumentals)', 'ETNOBOTANIKA', 'AAAAAAAAAA'), alb('KOSMOBOTANIKA', 'ETNOBOTANIKA', 'BBBBBBBBBB')], 'ETNOBOTANIKA', 'Kosmobotanika');
out.sameTitleOtherArtist = P([alb('Discovery', 'Some Tribute Band', 'CCCCCCCCCC'), alb('Discovery', 'Daft Punk', 'DDDDDDDDDD')], 'Daft Punk', 'Discovery');
out.onlyOtherArtist = P([alb('Discovery', 'Some Tribute Band', 'CCCCCCCCCC')], 'Daft Punk', 'Discovery');
out.accents = P([alb('Leśne Duchy', 'ETNOBOTANIKA', 'EEEEEEEEEE')], 'Etnobotanika', 'Lesne duchy');
out.deluxe = P([alb('Arrow (Deluxe Version)', 'Pavel Dovgal', 'FFFFFFFFFF')], 'Pavel Dovgal', 'Arrow');
out.closeThenFirst = P([alb('Totally Other', 'Artist X', 'GGGGGGGGGG'), alb('Another One', 'Artist X', 'HHHHHHHHHH')], 'Artist X', 'Missing Album');
out.noArtistGiven = P([alb('Thing', 'Whoever', 'IIIIIIIIII')], '', 'Thing');
out.skipsNonAlbums = P([{ stream_type: 'track', title: 'KOSMOBOTANIKA', subtitle: 'ETNOBOTANIKA', url: 'https://open.spotify.com/track/x' }], 'ETNOBOTANIKA', 'KOSMOBOTANIKA');
out.empty = [R.pick([], 'a', 'b'), R.pick(null, 'a', 'b'), R.pick([null, {}, { stream_type: 'album' }], 'a', 'b')];
out.fold = [R.fold('Ł Café (Remastered 2020) [Bonus]'), R.fold(null)];

// the steps, against a fake app
function fakeApp(opts) {
  const calls = [];
  const f = async (url, o) => {
    calls.push([url, o && o.method, o && o.body ? JSON.parse(o.body) : null]);
    const r = (body, ok = true) => ({ ok, json: async () => body });
    if (url.startsWith('/api/spotify/search')) return r(opts.search);
    if (url.startsWith('/api/find/album-tracks')) return r(opts.tracks, !(opts.tracks && opts.tracks.error));
    if (url.startsWith('/api/grupa?')) return r({ grupa: opts.grupas || [] });
    if (url === '/api/grupa' && o && o.method === 'POST') return r({ ok: true, id: 77 }, true);
    if (/\/api\/grupa\/\d+\/streams/.test(url)) return r(opts.streams || { ok: true, added: o ? JSON.parse(o.body).items.length : 0, already: 0 });
    return r({}, false);
  };
  return { f, calls };
}
(async () => {
  const album = alb('KOSMOBOTANIKA', 'ETNOBOTANIKA', 'BBBBBBBBBB');
  let app = fakeApp({ search: { connected: true, results: [album] } });
  out.matchOk = (await R.create(app.f).match('ETNOBOTANIKA', 'Kosmobotanika')).album.title;
  out.matchQuery = app.calls[0][0];
  out.notConnected = await R.create(fakeApp({ search: { connected: false, results: [] } }).f).match('a', 'b');
  out.notFound = await R.create(fakeApp({ search: { connected: true, results: [] } }).f).match('a', 'b');
  out.searchError = await R.create(fakeApp({ search: { connected: true, results: [], error: "Spotify didn't answer" } }).f).match('a', 'b');

  app = fakeApp({ tracks: { items: [{ url: 'https://open.spotify.com/track/1', title: 'One' }, { url: 'https://open.spotify.com/track/2', title: 'Two' }], total: 2 } });
  let s = await R.create(app.f).songs(album);
  out.songs = { n: s.items.length, whole: s.whole, url: app.calls[0][0] };
  s = await R.create(fakeApp({ tracks: { error: 'Forbidden', status: 403 } }).f).songs(album);
  out.songsRefused = { n: s.items.length, whole: s.whole, title: s.items[0].title, why: s.why };
  s = await R.create(fakeApp({ tracks: { items: [] } }).f).songs(album);
  out.songsEmpty = { n: s.items.length, whole: s.whole };
  s = await R.create(fakeApp({}).f).songs({ url: 'https://example.com/no-id', title: 'X', subtitle: 'Y' });
  out.songsNoId = { n: s.items.length, whole: s.whole };

  app = fakeApp({ grupas: [{ id: 1, name: 'Hound Dog Studios', has: true }, { id: 2, name: 'Other', has: false }] });
  out.grupas = await R.create(app.f).grupas('https://open.spotify.com/track/1');
  out.grupasUrl = app.calls[0][0];

  app = fakeApp({});
  let a = await R.create(app.f).add({ items: [{ url: 'u1', title: 't' }, { url: 'u2', title: 't2' }], name: 'My New One', note: 'From Connections' });
  out.addNew = { res: a, calls: app.calls.map(c => [c[0], c[1]]), made: app.calls[0][2] };
  app = fakeApp({});
  a = await R.create(app.f).add({ items: [{ url: 'u1', title: 't' }], grupaId: 5 });
  out.addExisting = { res: a, calls: app.calls.map(c => [c[0], c[1]]) };
  app = fakeApp({ streams: { error: 'not found' } });
  out.addFails = await R.create(app.f).add({ items: [{ url: 'u1' }], grupaId: 5 });
  out.addAlready = await R.create(fakeApp({ streams: { ok: true, added: 0, already: 3 } }).f).add({ items: [{ url: 'u1' }], grupaId: 5 });
  console.log(JSON.stringify(out));
})();
'''
r = subprocess.run([node, "-e", JS, os.path.join(PROJ, "static", "js", "room_add.js")], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "room_add.js runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

chk(o["exact"] == "KOSMOBOTANIKA|ETNOBOTANIKA", f"the exact title beats a longer 'Instrumentals' title ({o['exact']})")
chk(o["sameTitleOtherArtist"] == "Discovery|Daft Punk", f"the same title by the right artist beats a tribute band ({o['sameTitleOtherArtist']})")
chk(o["onlyOtherArtist"] is None, "an album by a different artist is never chosen")
chk(o["accents"] == "Leśne Duchy|ETNOBOTANIKA", "accents and case do not matter")
chk(o["deluxe"] == "Arrow (Deluxe Version)|Pavel Dovgal", "a deluxe edition matches the plain title")
chk(o["closeThenFirst"] == "Totally Other|Artist X", "no title match: the artist's first album, so the person can judge")
chk(o["noArtistGiven"] == "Thing|Whoever", "with no artist given, the title decides")
chk(o["skipsNonAlbums"] is None, "a track is not an album")
chk(o["empty"] == [None, None, None], "empty or junk results give nothing, not an error")
chk(o["fold"] == ["cafe", ""], f"folding strips accents, edition notes and punctuation ({o['fold']})")
chk(o["matchOk"] == "KOSMOBOTANIKA" and "q=ETNOBOTANIKA%20Kosmobotanika" in o["matchQuery"], "match asks Spotify with artist + album")
chk(o["notConnected"].get("notConnected") is True, "match: Spotify not connected is its own answer")
chk("not found" in o["notFound"]["error"], "match: nothing found says so")
chk("didn't answer" in o["searchError"]["error"], "match: Spotify's error is passed on")
chk(o["songs"] == {"n": 2, "whole": False, "url": o["songs"]["url"]} and "id=BBBBBBBBBB" in o["songs"]["url"], "songs: the album's own songs, by its Spotify id")
chk(o["songsRefused"]["whole"] is True and o["songsRefused"]["n"] == 1 and o["songsRefused"]["title"] == "KOSMOBOTANIKA" and o["songsRefused"]["why"] == "Forbidden", "songs: refused -> the album as one element, with the reason")
chk(o["songsEmpty"] == {"n": 1, "whole": True}, "songs: an empty answer also falls back to the whole album")
chk(o["songsNoId"] == {"n": 1, "whole": True}, "songs: no Spotify id -> the whole album")
chk(o["grupas"] == [{"id": 1, "name": "Hound Dog Studios", "has": True}, {"id": 2, "name": "Other", "has": False}] and "url=https%3A" in o["grupasUrl"], "grúpas come back with which already hold it")
chk(o["addNew"]["res"] == {"grupaId": 77, "added": 2, "already": 0} and [c[0] for c in o["addNew"]["calls"]] == ["/api/grupa", "/api/grupa/77/streams"], "add: makes the grúpa, then adds the songs to it")
chk(o["addNew"]["made"]["name"] == "My New One" and o["addNew"]["made"]["description"] == "From Connections", "add: the new grúpa carries its name and where it came from")
chk(o["addExisting"]["calls"] == [["/api/grupa/5/streams", "POST"]], "add: into an existing grúpa makes no new one")
chk("error" in o["addFails"], "add: a refusal is reported, not swallowed")
chk(o["addAlready"] == {"grupaId": 5, "added": 0, "already": 3}, "add: songs already there are counted, not doubled")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
