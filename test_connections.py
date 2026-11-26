"""Proves the neighbourhood web (connections.py): an album or an artist becomes groups of
labels, genres, other records, artists that sound alike, and anyone credited beside the artist.

Deezer is replaced by a fake, so nothing here touches the network. Covers the pure helpers, both
webs, an obscure record with little data, failure handling, the route, and the page.

Run:  venv/bin/python test_connections.py
"""
import os, sys, tempfile, subprocess, re

PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_conn_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
PY = sys.executable
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

subprocess.run([PY, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([PY, "migrate.py"], env=os.environ, capture_output=True)

import connections as C
C.time.sleep = lambda s: None            # the real client paces itself; tests need not wait

# ── helpers ──
chk(C.split_labels("TVPCO, Superkasety") == ["TVPCO", "Superkasety"], "two labels split on a comma")
chk(C.split_labels("Warp Records") == ["Warp Records"], "one label stays whole")
chk(C.split_labels("Ninja Tune / Big Dada; ninja tune") == ["Ninja Tune", "Big Dada"], "slash and semicolon split; repeats dropped (any case)")
chk(C.split_labels("") == [] and C.split_labels(None) == [], "no label -> none")
chk(C.year_of("2025-06-27") == "2025" and C.year_of("") == "" and C.year_of(None) == "", "year from a date, or nothing")
chk(C.spotify_artist_url("Björk & Co").startswith("https://open.spotify.com/search/Bj%C3%B6rk%20%26%20Co/artists"), "artist search link is encoded")
chk("discogs.com/search/?q=Warp%20Records&type=label" in C.label_search_url("Warp Records"), "label search link")

rows = C._albums_sorted([
    {"id": 1, "title": "Old Album", "release_date": "2019-01-01", "record_type": "album"},
    {"id": 2, "title": "New Single", "release_date": "2025-05-01", "record_type": "single"},
    {"id": 3, "title": "New Album", "release_date": "2024-01-01", "record_type": "album"},
    {"id": 4, "title": "new  album", "release_date": "2024-01-01", "record_type": "album"},     # same title: dropped
    {"id": 5, "title": "This One", "release_date": "2023-01-01", "record_type": "album"},        # the centre: skipped
    {"id": 6, "title": "", "release_date": "2023-01-01"}, "junk", None], skip_id=5)
chk([r["title"] for r in rows] == ["New Album", "Old Album", "New Single"], f"albums first, newest first, singles last, no repeats, centre skipped ({[r['title'] for r in rows]})")
chk(len(C._albums_sorted([{"id": i, "title": f"t{i}", "record_type": "album"} for i in range(40)])) == C.MAX_ALBUMS, "capped")

# ── a fake Deezer ──
ALBUM = {"id": 10, "title": "KOSMOBOTANIKA", "label": "TVPCO, Superkasety", "release_date": "2025-06-27", "nb_tracks": 14, "fans": 160,
         "link": "https://www.deezer.com/album/10", "cover_medium": "http://x/c.jpg",
         "genres": {"data": [{"name": "Electro"}, {"name": "Dance"}]},
         "artist": {"id": 7, "name": "ETNOBOTANIKA", "picture_medium": "http://x/a.jpg"},
         "contributors": [{"id": 7, "name": "ETNOBOTANIKA", "role": "Main"}]}
PAGES = {
    "/search/album?q=ETNOBOTANIKA%20Kosmobotanika&limit=5": {"data": [{"id": 99, "title": "Something Else"}, {"id": 10, "title": "KOSMOBOTANIKA"}]},
    "/album/10": ALBUM,
    "/artist/7/albums?limit=50": {"data": [{"id": 10, "title": "KOSMOBOTANIKA", "record_type": "album", "release_date": "2025-06-27"},
                                           {"id": 11, "title": "Leśne Duchy", "record_type": "album", "release_date": "2023-06-23", "cover_medium": "http://x/l.jpg"},
                                           {"id": 12, "title": "Planeta Radio", "record_type": "single", "release_date": "2025-06-06"}]},
    "/artist/7/related": {"data": [{"id": 70, "name": "Photay", "picture_medium": "http://x/p.jpg", "nb_fan": 2959}, {"id": 71, "name": "Darkside", "nb_fan": 5}, {"id": 72}]},
    "/artist/70": {"id": 70, "name": "Photay", "nb_fan": 2959, "nb_album": 9, "picture_medium": "http://x/p.jpg", "link": "https://www.deezer.com/artist/70"},
    "/artist/70/albums?limit=50": {"data": [{"id": 700, "title": "Onism", "record_type": "album", "release_date": "2019-01-01"}]},
    "/artist/70/related": {"data": [{"id": 70000, "name": "Four Tet"}]},
    "/search/artist?q=photay&limit=3": {"data": [{"id": 5, "name": "Some Photay Fan"}, {"id": 70, "name": "Photay"}]},
}
calls = []
def fake(url):
    path = url[len(C.API):]
    calls.append(path)
    if path in PAGES: return PAGES[path]
    raise OSError("no such page " + path)
def dz(): C._cache.clear(); calls.clear(); return C.Deezer(fake)

# ── an album ──
d = dz()
aid = C.find_album_id(d, "ETNOBOTANIKA", "Kosmobotanika")
chk(aid == 10, "find_album_id prefers the exact title over the first hit")
web, err = C.album_web(d, 10)
keys = [g["key"] for g in web["groups"]]
chk(err is None and web["center"]["name"] == "KOSMOBOTANIKA" and web["center"]["sub"] == "ETNOBOTANIKA" and web["center"]["year"] == "2025", "centre: album, artist, year")
chk(keys == ["labels", "genres", "albums", "related"], f"groups in a fixed order, empty ones left out ({keys})")
g = {x["key"]: x for x in web["groups"]}
chk([n["name"] for n in g["labels"]["nodes"]] == ["TVPCO", "Superkasety"] and g["labels"]["nodes"][0]["url"].startswith("https://www.discogs.com/"), "two labels, each with a search link")
chk([n["name"] for n in g["genres"]["nodes"]] == ["Electro", "Dance"], "genres")
chk([n["name"] for n in g["albums"]["nodes"]] == ["Leśne Duchy", "Planeta Radio"], "the artist's other records, this one excluded")
chk([n["name"] for n in g["related"]["nodes"]] == ["Photay", "Darkside"], "related artists; one with no name dropped")
chk(g["related"]["nodes"][0]["id"] == 70 and g["related"]["nodes"][0]["url"].endswith("/artists"), "a related artist keeps its id (to walk to) and a Spotify link")
chk(any("no producers" in n.lower() for n in web["notes"]), "no credits -> says so plainly instead of showing an empty group")
chk("credits" not in keys, "no credits group when only the main artist is credited")

# credits when there are some
PAGES["/album/10"] = dict(ALBUM, contributors=ALBUM["contributors"] + [{"id": 80, "name": "A. Producer", "role": "Producer"}])
web2, _ = C.album_web(dz(), 10)
cr = [x for x in web2["groups"] if x["key"] == "credits"]
chk(cr and cr[0]["nodes"][0]["name"] == "A. Producer" and cr[0]["nodes"][0]["role"] == "Producer", "a credited producer shows with their role")
chk(not any("no producers" in n.lower() for n in web2["notes"]), "…and the 'none listed' note goes away")
PAGES["/album/10"] = ALBUM

# an obscure record: almost nothing
PAGES["/album/20"] = {"id": 20, "title": "Tiny", "artist": {"id": 8, "name": "Nobody"}}
PAGES["/artist/8/albums?limit=50"] = {"data": []}
PAGES["/artist/8/related"] = {"data": []}
web3, err3 = C.album_web(dz(), 20)
chk(err3 is None and web3["groups"] == [] and web3["center"]["name"] == "Tiny", "an obscure record gives a centre and no groups, not an error")
chk(web3["notes"], "…with a note")

# failures
d = dz(); PAGES["/album/30"] = {"id": 30, "title": "Half", "artist": {"id": 9, "name": "Half Artist"}, "label": "L"}
web4, _ = C.album_web(d, 30)         # /artist/9/... are missing -> the fake raises
chk(web4 and [g["key"] for g in web4["groups"]] == ["labels"] and any("could not be read" in n for n in web4["notes"]), "if the network fails part-way, what was read is kept and the page is told")
w, e = C.album_web(dz(), 404)
chk(w is None and "not found" in e, "unknown album -> a plain error")
PAGES["/album/31"] = {"error": {"type": "DataException", "code": 800}}
w, e = C.album_web(dz(), 31)
chk(w is None, "Deezer's own error body is treated as not found")

# caching
d = dz(); C.album_web(d, 10); n1 = len(calls); C.album_web(d, 10)
chk(len(calls) == n1, "a second look at the same record makes no new calls")

# ── an artist (walking to a neighbour) ──
web5, e5 = C.artist_web(dz(), 70)
chk(e5 is None and web5["center"]["type"] == "artist" and web5["center"]["name"] == "Photay", "artist web: centre")
chk([g["key"] for g in web5["groups"]] == ["albums", "related"] and web5["groups"][0]["nodes"][0]["name"] == "Onism", "artist web: records and neighbours")
chk(C.find_artist_id(dz(), "photay") == 70, "an artist is found by exact name, not the first fuzzy hit")

# ── the entry point ──
web6, err6, st6 = C.web_for({"kind": "album", "artist": "ETNOBOTANIKA", "album": "Kosmobotanika"}, dz())
chk(st6 == 200 and web6["center"]["id"] == 10, "web_for: album by name")
_, err7, st7 = C.web_for({"kind": "album", "id": "abc"}, dz())
chk(st7 == 400 and err7, "web_for: a bad id is 400, not a crash")
_, _, st8 = C.web_for({"kind": "album", "artist": "Zzz", "album": "Nope"}, dz())
chk(st8 == 404, "web_for: no such album -> 404")
web9, _, st9 = C.web_for({"kind": "artist", "id": "70"}, dz())
chk(st9 == 200 and web9["center"]["type"] == "artist", "web_for: artist by id")

# ── starting from a song ──
chk(C.first_artist("Kanye West, GLC, Consequence") == "Kanye West" and C.first_artist("A & B") == "A" and C.first_artist("A feat. B") == "A"
    and C.first_artist("Tyler, The Creator") == "Tyler" and C.first_artist("") == "" and C.first_artist(None) == "", "first_artist picks the lead name")
chk(C._fold("Surrender (feat. X)") == C._fold("Surrender") and C._fold("Space Oddity - 2015 Remaster") == C._fold("Space Oddity") and C._fold("Éclair") == "eclair", "song titles compare without feats, remaster tags, accents")
PAGES["/search/track?q=HEALER%20Surrender&limit=10"] = {"data": [
    {"title": "Surrender", "artist": {"id": 5, "name": "Someone Else"}, "album": {"id": 500, "title": "Wrong One"}},
    {"title": "Surrender (Remix)", "artist": {"id": 6, "name": "HEALER"}, "album": {"id": 600, "title": "Remixes"}},
    {"title": "Surrender", "artist": {"id": 6, "name": "HEALER"}, "contributors": [{"name": "HEALER"}, {"name": "Clemquicourt"}], "album": {"id": 601, "title": "Baekdudaegan"}},
    {"title": "Surrender", "artist": {"id": 6, "name": "HEALER"}, "album": {"id": 602, "title": "Compilation"}}]}
chk(C.find_track(dz(), "HEALER, Clemquicourt", "Surrender") == (601, 6, "Surrender"), "find_track: same title AND same artist; a Remix or someone else's song of that name is skipped")
chk(C.find_track(dz(), "HEALER", "Surrender", "Compilation")[0] == 602, "find_track: the matching album title wins a tie")
chk(C.find_track(dz(), "", "Surrender") is None and C.find_track(dz(), "HEALER", "") is None, "find_track: needs both an artist and a title")
PAGES["/search/track?q=Nobody%20Nothing&limit=10"] = {"data": [{"title": "Nothing", "artist": {"id": 1, "name": "Somebody Else"}, "album": {"id": 1}}]}
chk(C.find_track(dz(), "Nobody", "Nothing") is None, "find_track: a song by a different artist is refused, not guessed")
PAGES["/album/601"] = {"id": 601, "title": "Baekdudaegan", "artist": {"id": 6, "name": "HEALER"}, "label": "L"}
PAGES["/artist/6/albums?limit=50"] = {"data": []}; PAGES["/artist/6/related"] = {"data": []}
w, e, st = C.web_for({"kind": "track", "artist": "HEALER, Clemquicourt", "title": "Surrender"}, dz())
chk(st == 200 and w["center"]["type"] == "album" and w["center"]["name"] == "Baekdudaegan" and w["song"] == {"title": "Surrender", "artist": "HEALER, Clemquicourt"}, "web_for(track): opens on the song's record and remembers the song")
PAGES["/search/track?q=Solo%20Song&limit=10"] = {"data": []}
PAGES["/search/artist?q=Solo&limit=3"] = {"data": [{"id": 70, "name": "Solo"}]}
PAGES["/artist/70"] = {"id": 70, "name": "Solo", "nb_fan": 1}; PAGES["/artist/70/albums?limit=50"] = {"data": []}; PAGES["/artist/70/related"] = {"data": []}
w, e, st = C.web_for({"kind": "track", "artist": "Solo", "title": "Song"}, dz())
chk(st == 200 and w["center"]["type"] == "artist" and w["song"]["title"] == "Song" and "artist" in w["notes"][0].lower(), "web_for(track): no record found -> the artist, and it says so")
_, e, st = C.web_for({"kind": "track", "artist": "Zzz Nobody", "title": "Nothing Here"}, dz())
chk(st == 404 and e, "web_for(track): nothing at all -> a plain 404")

# ── previews ──
chk(C.safe_preview("https://cdnt-preview.dzcdn.net/api/1/1/a/b.mp3") == "https://cdnt-preview.dzcdn.net/api/1/1/a/b.mp3", "a Deezer preview address is allowed")
for bad in ["http://cdnt-preview.dzcdn.net/x.mp3", "https://evil.com/x.mp3", "https://dzcdn.net.evil.com/x.mp3", "https://notdzcdn.net/x.mp3",
            "javascript:alert(1)", "data:audio/mp3;base64,AAAA", "//cdn.dzcdn.net/x.mp3", "", None, "https:///x"]:
    chk(C.safe_preview(bad) == "", f"preview address refused: {bad!r}")
PAGES["/album/10/tracks?limit=60"] = {"data": [
    {"title": "Song A", "duration": 215, "preview": "https://cdnt-preview.dzcdn.net/a.mp3", "link": "https://www.deezer.com/track/1", "artist": {"name": "ETNOBOTANIKA"}},
    {"title": "Song B", "duration": 0, "preview": "https://evil.com/b.mp3", "artist": {"name": "ETNOBOTANIKA"}},
    {"title": "", "preview": "https://cdnt-preview.dzcdn.net/c.mp3"}, "junk"]}
rows, note, err = C.preview_for(dz(), "album", 10)
chk(err is None and [r["title"] for r in rows] == ["Song A", "Song B"], "album previews: the songs in order, junk rows dropped")
chk(rows[0]["preview"].endswith("/a.mp3") and rows[1]["preview"] == "", "…a foreign preview address is blanked, the song is kept")
chk(rows[0]["duration_sec"] == 215 and rows[1]["duration_sec"] is None and rows[0]["sub"] == "ETNOBOTANIKA", "length and artist carried")
PAGES["/artist/70/top?limit=8"] = {"data": [{"title": "Hit", "duration": 200, "preview": "https://cdnt-preview.dzcdn.net/h.mp3", "album": {"title": "Onism"}}]}
rows, _, err = C.preview_for(dz(), "artist", 70)
chk(err is None and rows[0]["sub"] == "Onism" and rows[0]["preview"], "artist previews: best-known songs, each with its record")
PAGES["/album/11/tracks?limit=60"] = {"data": [{"title": "Quiet", "duration": 10, "artist": {"name": "x"}}]}
rows, note, err = C.preview_for(dz(), "album", 11)
chk(err is None and rows and "No previews" in note, "songs with no previews say so plainly")
PAGES["/album/12/tracks?limit=60"] = {"data": []}
_, _, err = C.preview_for(dz(), "album", 12)
chk(err, "an empty record -> an error message, not an empty list")
_, _, err = C.preview_for(dz(), "album", 404)         # the fake raises -> network failure
chk(err and "just now" in err, "a network failure says to try again")

# ── keeping a web on the wall ──
def node(t, i, name, x=0, y=0, **kw): return dict({"type": t, "id": i, "name": name, "x": x, "y": y}, **kw)
GOOD = {"nodes": [node("album", 10, "KOSMOBOTANIKA", 0, 0, sub="ETNOBOTANIKA", year="2025", image="https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg"),
                  node("artist", 70, "Photay", 200.456, -90, image="https://i.scdn.co/image/abc"),
                  node("label", None, "TVPCO", -150, 60), node("genre", None, "Electro", 0, 180)],
        "edges": [[0, 1], [0, 2], [0, 3], [1, 0], [2, 2]], "home": 0}
w = C.clean_web(GOOD)
chk(w and [n["name"] for n in w["nodes"]] == ["KOSMOBOTANIKA", "Photay", "TVPCO", "Genre"[:0] + "Electro"] and w["home"] == 0, "clean_web keeps a good web")
chk(w["edges"] == [[0, 1], [0, 2], [0, 3]], f"edges: repeated and self-joined ones dropped, each pair in order ({w['edges']})")
chk(w["nodes"][1]["x"] == 200.5 and w["nodes"][0]["image"].startswith("https://cdn-images.dzcdn.net/") and w["nodes"][1]["image"].startswith("https://i.scdn.co/"), "positions rounded; Deezer and Spotify pictures kept")
chk(w["nodes"][2]["id"] is None and w["nodes"][0]["id"] == 10, "labels and genres carry no id; records and artists keep theirs")
import copy
def tweak(f):
    g = copy.deepcopy(GOOD); f(g); return C.clean_web(g)
for bad in ["http://cdn.dzcdn.net/x.jpg", "https://evil.com/x.jpg", "https://dzcdn.net.evil.com/x.jpg", "https://cdn.dzcdn.net@evil.com/x.jpg",
            "https://evildzcdn.net/x.jpg", "javascript:alert(1)", "data:image/png;base64,AA", "https://cdn.dzcdn.net/x.jpg\"onload=\"alert(1)", "https://cdn.dzcdn.net/" + "a" * 600]:
    r = tweak(lambda g: g["nodes"][0].update(image=bad))
    chk(r and r["nodes"][0]["image"] == "", f"picture address refused, node kept: {bad[:48]!r}")
r = tweak(lambda g: g["nodes"][0].update(name='<img src=x onerror=alert(1)>  \n  Title'))
chk(r and r["nodes"][0]["name"] == "<img src=x onerror=alert(1)> Title", "names are tidied to one line and length-capped (the page escapes them when drawing)")
chk(len(tweak(lambda g: g["nodes"][0].update(name="x" * 900))["nodes"][0]["name"]) == 200, "a very long name is cut")
chk(tweak(lambda g: g["nodes"][0].update(year="19xx"))["nodes"][0]["year"] == "", "a year must be four digits")
r = tweak(lambda g: g["nodes"].insert(2, {"type": "playlist", "id": 1, "name": "x", "x": 0, "y": 0}))
chk(r and [n["type"] for n in r["nodes"]] == ["album", "artist", "label", "genre"], "an unknown node type is dropped")
r = tweak(lambda g: (g["nodes"].insert(0, node("artist", "abc", "Bad id")), g.update(home=1)))
chk(r and r["nodes"][0]["name"] == "KOSMOBOTANIKA" and r["home"] == 0, "an artist with no usable id is dropped, and home still points at the right node")
r = tweak(lambda g: g.update(edges=[[0, 1], [0, 99], ["a", 1], [1], None, [-5, 0]]))
chk(r and r["edges"] == [[0, 1]], "edges that point nowhere are dropped")
for bad_xy in [float("nan"), float("inf"), 1e9, "x", None]:
    r = tweak(lambda g: g["nodes"][1].update(x=bad_xy))
    chk(r and [n["name"] for n in r["nodes"]] == ["KOSMOBOTANIKA", "TVPCO", "Electro"] and r["home"] == 0, f"a node with a bad position is dropped: {bad_xy!r}")
chk(tweak(lambda g: g.update(home=2)) is None, "home must be a record or an artist, not a label")
chk(tweak(lambda g: g.update(home=99)) is None and tweak(lambda g: g.update(home="x")) is None and tweak(lambda g: g.pop("home")) is None, "home must point at a real node")
chk(tweak(lambda g: g["nodes"][0].update(name="")) is None, "if home itself is unusable, the web is refused")
chk(C.clean_web({"nodes": [node("album", 1, "n%d" % i) for i in range(C.WEB_MAX_NODES + 1)], "edges": [], "home": 0}) is None, "too many nodes is refused, not silently cut")
chk(all(C.clean_web(x) is None for x in [None, [], "x", 5, {}, {"nodes": "x"}, {"nodes": [], "home": 0}]), "junk is refused")
big = {"nodes": [node("album", i + 1, "n%d" % i) for i in range(C.WEB_MAX_NODES)], "edges": [[0, j] for j in range(1, C.WEB_MAX_NODES)] + [[a, b] for a in range(1, 40) for b in range(a + 1, 40)], "home": 0}
chk(len(C.clean_web(big)["edges"]) == C.WEB_MAX_EDGES, "edges are capped")
chk(C.clean_web(GOOD) == C.clean_web(copy.deepcopy(GOOD)) and GOOD["nodes"][1]["x"] == 200.456, "cleaning is repeatable and does not change what it was given")

# putting it on the wall
wb, how = C.put_web_on_wall([], w, "kAAA")
chk(how == "added" and wb[0]["type"] == "web" and wb[0]["y"] == 120 and wb[0]["x"] == 32 and (wb[0]["w"], wb[0]["h"]) == (680, 500), "an empty wall: the web goes in at the top")
chk(wb[0]["data"]["url"] == "/connections?kind=album&id=10" and wb[0]["data"]["title"] == "KOSMOBOTANIKA" and wb[0]["data"]["kind"] == "album" and wb[0]["data"]["rid"] == 10, "the block knows how to open its live web (always inside the app)")
chk(wb[0]["id"] == "kAAA" and wb[0]["z"] == 1, "the block gets the id it was given")
wall = [{"id": "a", "type": "text", "x": 0, "y": 100, "w": 300, "h": 205, "z": 5, "data": {}}, "junk", None]
wb, how = C.put_web_on_wall(wall, w, "kBBB")
chk(how == "added" and len(wb) == 2 and wb[1]["y"] == 336 and wb[1]["y"] % 8 == 0 and wb[1]["z"] == 2, f"it lands below what is there, on the 8px grid ({wb[1]['y']}); junk entries are dropped")
chk(wall[0] == {"id": "a", "type": "text", "x": 0, "y": 100, "w": 300, "h": 205, "z": 5, "data": {}}, "existing blocks are untouched")
moved = copy.deepcopy(wb); moved[1].update(x=700, y=900, w=640); moved[1]["data"]["caption"] = "my note"
w2 = C.clean_web(dict(GOOD, nodes=GOOD["nodes"] + [node("artist", 71, "Darkside", 5, 5)]))
wb2, how2 = C.put_web_on_wall(moved, w2, "kCCC")
chk(how2 == "updated" and len(wb2) == 2 and len(wb2[1]["data"]["web"]["nodes"]) == 5, "keeping the same record again refreshes its web instead of adding a second block")
chk((wb2[1]["x"], wb2[1]["y"], wb2[1]["w"]) == (700, 900, 640) and wb2[1]["data"]["caption"] == "my note" and wb2[1]["id"] == wb[1]["id"], "…and its place, size, caption and id stay as the person left them")
other = C.clean_web({"nodes": [node("artist", 10, "Same id, different kind")], "edges": [], "home": 0})
_, how3 = C.put_web_on_wall(moved, other, "kDDD")
chk(how3 == "added", "an artist and an album that share a number are not the same record")

# the route, with a stand-in wall
from flask import Flask
store = {"blocks": [], "saved": 0}
fake_wall = {"load": lambda pid: {"v": 1, "blocks": list(store["blocks"])}, "save": lambda pid, b: (store.update(blocks=b, saved=store["saved"] + 1)),
             "well": lambda: 7}
def mini_app(wall):
    a = Flask(__name__); C.register(a, wall=wall); return a.test_client()
mc = mini_app(fake_wall)
r = mc.post("/api/connections/keep", json={"web": GOOD})
chk(r.status_code == 200 and r.get_json() == {"ok": True, "placed": "added", "pid": 7, "n": 1} and store["blocks"][0]["type"] == "web", "POST /api/connections/keep adds it to the wall")
r = mc.post("/api/connections/keep", json={"web": GOOD})
chk(r.get_json()["placed"] == "updated" and len(store["blocks"]) == 1 and store["saved"] == 2, "keeping it twice updates, never duplicates")
n0 = store["saved"]
chk(mc.post("/api/connections/keep", json={"web": {"nodes": [], "home": 0}}).status_code == 400 and store["saved"] == n0, "a bad web is refused and nothing is saved")
chk(mc.post("/api/connections/keep", data="nope", content_type="text/plain").status_code == 400, "a non-JSON body is a 400, not a crash")
chk(mc.post("/api/connections/keep", json={}).status_code == 400, "no web -> 400")
chk(mini_app(dict(fake_wall, well=lambda: None)).post("/api/connections/keep", json={"web": GOOD}).status_code == 404, "no room to keep it in -> 404")
chk(mini_app(None).post("/api/connections/keep", json={"web": GOOD}).status_code == 503, "no wall wired in -> 503, plainly")
store["blocks"] = [{"id": "x%d" % i, "type": "text", "x": 0, "y": i, "w": 8, "h": 8, "z": 1, "data": {}} for i in range(400)]
chk(mc.post("/api/connections/keep", json={"web": GOOD}).status_code == 400, "a full wall (400 blocks) refuses another")

# ── the route and the page ──
import app
app._cat_health_last_check[0] = __import__("time").time() + 1e9
app.app.config["CONNECTIONS_HOOKS"]["get"] = fake
C._cache.clear()
c = app.app.test_client()
r = c.get("/api/connections?kind=album&artist=ETNOBOTANIKA&album=Kosmobotanika")
chk(r.status_code == 200 and r.get_json()["center"]["name"] == "KOSMOBOTANIKA", "GET /api/connections works")
chk(c.get("/api/connections?kind=album&id=zzz").status_code == 400, "bad id over HTTP -> 400")
chk(c.get("/api/connections?kind=album&artist=Zzz&album=Nope").status_code == 404, "unknown album over HTTP -> 404")
rp = c.get("/api/connections/preview?kind=album&id=10")
chk(rp.status_code == 200 and rp.get_json()["items"][0]["title"] == "Song A", "GET /api/connections/preview works")
chk(c.get("/api/connections/preview?kind=album&id=zzz").status_code == 400, "preview: bad id -> 400")
chk(c.get("/api/connections/preview?kind=album&id=12").status_code == 404, "preview: nothing to hear -> 404")
p = c.get("/connections")
chk(p.status_code == 200 and b"cn-stage" in p.data, "GET /connections renders the page")
html = open(os.path.join(PROJ, "templates", "connections.html"), encoding="utf-8").read()
style = html[html.index("<style>"):html.index("</style>")]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", style), "page CSS uses theme tokens only, no hex")
chk("border-radius: 0" in style or "border-radius:0" in style, "square, no rounding (the house look)")
chk("box-shadow" not in style, "no shadows")
chk("esc(" in html and "innerHTML" in html, "page escapes what it prints")
chk("e.ctrlKey" in html and "if (!e.ctrlKey) return;" in html, "zoom: a wheel only zooms when it is a pinch (ctrlKey); plain scroll is left alone")
chk("deltaY < 0 ? 1.12" not in html, "zoom: the old scroll-to-zoom is gone")
chk("gesturechange" in html and "pointerType !== 'touch'" in html, "zoom: Safari/app pinch and two-finger touch pinch are handled")
chk("/static/js/room_add.js" in html and "/api/connections/preview" in html and "/api/seinnteoir/pause" in html, "page loads the add module, fetches previews, and quiets the app's own player first")
chk("/static/js/conn_start.js" in html and "cn-now" in html and "cn-liked" in html and "cn-type" in html, "page offers the three starts: now playing, a liked song, a typed album")
chk("/api/spotify/saved-tracks" in html and "/api/seinnteoir/state" in html, "page reads the liked songs and the player's state from the app's own endpoints")
chk("history.replaceState" in html and "p.get('title')" in html, "a song-started web has a link that reloads to the same web")
chk("cn-song mine" not in html and ".cn-song.mine" in html, "the song you started from is marked in the song list")
chk("RoomAdd.create(null, { onWait" in html and "Finding it on Spotify…" in html, "Connections tells the person while Spotify is slow (RoomAdd keeps trying)")
chk("tryN < 3" in html and "r.status === 401" in html, "liked songs: tried up to three times on a hiccup, but a sign-in problem is not retried")
chk("cn-keep" in html and "/api/connections/keep" in html and "/room?wall=1" in html, "page can keep a web and lead to the wall")
chk("edges: g.edges.filter" in html and "slice(0, 119)" in html, "a very large web is trimmed to the nearest 119 + the start before it is sent")
chk("Pinch to zoom" in html and "scroll to zoom" not in html.lower(), "the hint says pinch, not scroll")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
