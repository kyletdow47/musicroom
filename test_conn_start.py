"""Proves where a web can start from (static/js/conn_start.js): the song playing now (the app's player,
Spotify elsewhere, or paused), a liked song, or a search hit.

Pure module, run under node.

Run:  venv/bin/python test_conn_start.py
"""
import os, sys, json, subprocess, shutil

PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)

JS = r'''
const S = require(process.argv[1]);
const q = (artist, title) => ({ artist, title, image_url: 'http://i/' + title });
const out = {};
const np = s => { const x = S.nowPlaying(s); return x && [x.artist, x.title, x.playing, x.where]; };
out.roomPlaying = np({ playing: true, idx: 1, queue: [q('A', 'one'), q('B', 'two')], guest: { title: 'g', artist: 'G', playing: true, device: 'iPhone' } });
out.guestPlaying = np({ playing: false, idx: 0, queue: [q('A', 'one')], guest: { title: 'eye of ra', artist: 'purc', playing: true, device: 'iPhone', image_url: 'http://i/g' } });
out.guestPaused = np({ playing: false, idx: 0, queue: [], guest: { title: 'eye of ra', artist: 'purc', playing: false, device: 'iPhone' } });
out.queuePaused = np({ playing: false, idx: 0, queue: [q('Pecker Dunne', 'Wexford')], guest: {}, remote: {} });
out.remoteOnly = np({ playing: false, queue: [], guest: {}, remote: { title: 'R', artist: 'RA', device: 'Desk' } });
out.pausedBeatsNothing = np({ playing: false, idx: 0, queue: [q('A', 'one')], guest: { title: 'g', artist: 'G', playing: false } });
out.nothing = [np({}), np(null), np(undefined), np('x'), np({ queue: [{ artist: 'A' }], idx: 0, playing: true }), np({ guest: { artist: 'only artist' } })];
out.badIdx = np({ playing: true, idx: 9, queue: [q('A', 'one')], guest: { title: 'g', artist: 'G', playing: true } });
out.image = S.nowPlaying({ playing: true, idx: 0, queue: [q('A', 'one')] }).image;
out.noThrowOnJunk = (() => { try { S.nowPlaying({ queue: 'nope', idx: 'x', guest: 5, remote: [] }); return true; } catch (e) { return false; } })();
out.liked = S.fromLiked({ title: ' Surrender ', artist: 'HEALER, Clemquicourt', album: 'Baekdudaegan', album_image_small: 'http://s', album_image_url: 'http://big' });
out.likedNoTitle = [S.fromLiked({ artist: 'x' }), S.fromLiked(null), S.fromLiked({ title: '  ' })];
out.search = S.fromSearch({ stream_type: 'track', title: 'T', subtitle: 'Art', album: 'Alb', thumb: 'http://t', image: 'http://i' });
out.searchNotTrack = [S.fromSearch({ stream_type: 'album', title: 'x' }), S.fromSearch(null)];
out.query = [S.query({ artist: 'A & B', title: 'Song?', album: 'Al' }).toString(), S.query({ artist: 'A', title: 'S' }).toString()];
console.log(JSON.stringify(out));
'''
r = subprocess.run([node, "-e", JS, os.path.join(PROJ, "static", "js", "conn_start.js")], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "conn_start.js runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

chk(o["roomPlaying"] == ["B", "two", True, "the player"], f"the app's player, when playing, wins ({o['roomPlaying']})")
chk(o["guestPlaying"] == ["purc", "eye of ra", True, "iPhone"], f"Spotify playing on a phone is found, with where ({o['guestPlaying']})")
chk(o["guestPaused"] == ["purc", "eye of ra", False, "iPhone"], "a paused Spotify song is still offered, marked not playing")
chk(o["queuePaused"] == ["Pecker Dunne", "Wexford", False, "the player"], "the player's loaded song, paused, is offered when nothing else is")
chk(o["remoteOnly"] == ["RA", "R", True, "Desk"], "the remote's song is used when nothing else is going")
chk(o["pausedBeatsNothing"][1] == "g", "a paused Spotify song beats a paused queued one")
chk(o["nothing"] == [None] * 6, "nothing playing, or only half a song, gives null")
chk(o["badIdx"] == ["G", "g", True, "Spotify"], "an out-of-range queue position falls through to Spotify")
chk(o["image"] == "http://i/one", "the cover comes along")
chk(o["noThrowOnJunk"], "junk state never throws")
chk(o["liked"] == {"artist": "HEALER, Clemquicourt", "title": "Surrender", "album": "Baekdudaegan", "image": "http://s"}, "a liked song is tidied (title trimmed, small cover)")
chk(o["likedNoTitle"] == [None, None, None], "a liked row with no title is dropped")
chk(o["search"] == {"artist": "Art", "title": "T", "album": "Alb", "image": "http://t"}, "a search hit becomes the same shape")
chk(o["searchNotTrack"] == [None, None], "only tracks start a web from search")
chk(o["query"][0] == "kind=track&artist=A+%26+B&title=Song%3F&album=Al" and o["query"][1] == "kind=track&artist=A&title=S", "the query is encoded, and album is left out when unknown")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
