"""Proves "Find it" (find_music.py): paste a YouTube link, say album / mix / talk.

Covers the pure reading (video ids, times, tracklists, the album-or-mix-or-talk guess, the
artist/album read out of a title), the route with the network stubbed, and that the page
and its Tools link exist. Nothing here touches the network.

Run:  venv/bin/python test_find_music.py
"""
import os, sys, tempfile, subprocess, json

PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_find_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
PY = sys.executable
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

subprocess.run([PY, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([PY, "migrate.py"], env=os.environ, capture_output=True)

import find_music as f

# ── video ids ──
for u, want in [("https://youtu.be/gZUPDL3RBYs?si=abc", "gZUPDL3RBYs"),
                ("https://www.youtube.com/watch?v=nJx9POvQ894&t=5s", "nJx9POvQ894"),
                ("https://www.youtube.com/watch?feature=share&v=nJx9POvQ894", "nJx9POvQ894"),
                ("https://youtube.com/embed/gZUPDL3RBYs", "gZUPDL3RBYs"),
                ("gZUPDL3RBYs", "gZUPDL3RBYs"),
                ("youtu.be/gZUPDL3RBYs", "gZUPDL3RBYs"),
                ("https://evil.com/watch?v=gZUPDL3RBYs", None),
                ("https://youtube.com.evil.com/watch?v=gZUPDL3RBYs", None),
                ("", None), (None, None), ("https://youtu.be/short", None)]:
    chk(f.parse_youtube_id(u) == want, f"id of {u!r} -> {want!r}")

# ── times ──
chk(f.parse_time("5:58") == 358.0, "5:58 is 358s")
chk(f.parse_time("1:02:03") == 3723.0, "1:02:03 is 3723s")
chk(f.parse_time("nope") is None and f.parse_time("") is None and f.parse_time("5:5") is None, "bad times are None")

# ── timestamped tracklists ──
mix = "Intro text\n0:00 Aphex Twin - Xtal\n[04:32] Boards of Canada – Roygbiv\n1:02:03 - Burial - Archangel\nno time here\n04:32 Boards of Canada – Roygbiv"
items = f.parse_timestamped_lines(mix)
chk([i["start_s"] for i in items] == [0.0, 272.0, 3723.0], f"three timed lines, in order, duplicate dropped ({[i['start_s'] for i in items]})")
ents = f.entries_from_timestamped(items)
chk(all(e["kind"] == "track" for e in ents) and ents[0]["artist"] == "Aphex Twin" and ents[0]["title"] == "Xtal", "artist and title split")
chk(ents[0]["end_s"] == 272.0 and ents[-1]["end_s"] is None, "each ends where the next begins; the last is open")
chap = f.entries_from_timestamped(f.parse_timestamped_lines("0:00 Introduction\n3:10 The problem\n12:00 The answer"))
chk(all(e["kind"] == "chapter" for e in chap) and chap[1]["title"] == "The problem", "titles with no artist are chapters")
chk(f.parse_timestamped_lines("") == [] and f.parse_timestamped_lines(None) == [], "empty text -> nothing")
chk(f.parse_timestamped_lines("x" * 400 + " 0:00") == [], "an absurdly long line is skipped")

# ── numbered lists (the ETNOBOTANIKA case: no times, no artist) ──
good = "Tracklista:\n\n1. Mamy Jeszcze Jedno Miejsce  \n2. Trzy Doby Na Orbicie  \n3. Planeta Radio  \n4. Grzyby Na Marsie"
chk(f.parse_numbered_lines(good) == ["Mamy Jeszcze Jedno Miejsce", "Trzy Doby Na Orbicie", "Planeta Radio", "Grzyby Na Marsie"], "numbered list read")
chk(f.parse_numbered_lines("1. one\n3. three\n4. four") == [], "a list that skips numbers is not a tracklist")
chk(f.parse_numbered_lines("2. a\n3. b\n4. c") == [], "a list that does not start at 1 is not a tracklist")
chk(f.parse_numbered_lines("1. only\n2. two") == [], "fewer than three is not a tracklist")

# ── artist and album out of a title ──
for title, author, want in [
        ("ETNOBOTANIKA - KOSMOBOTANIKA (Full Album)", "ETNOBOTANIKA", ("ETNOBOTANIKA", "KOSMOBOTANIKA")),
        ("Boards of Canada - Music Has the Right to Children [Full Album]", "x", ("Boards of Canada", "Music Has the Right to Children")),
        ("Kosmobotanika", "ETNOBOTANIKA", ("ETNOBOTANIKA", "Kosmobotanika")),
        ("Artist – Record (2019) | Official", "c", ("Artist", "Record")),
        ("", "", ("", ""))]:
    chk(f.album_guess(title, author) == want, f"album_guess({title!r}) -> {want}")

# ── the guess ──
def g(title, dur=3000, entries=None, numbered=None, desc=""):
    return f.guess({"title": title, "duration_s": dur, "description": desc}, entries, numbered)["kind"]
tracks = [{"kind": "track", "artist": a, "title": "t"} for a in ("A", "B", "C", "D")]
chk(g("Some Boiler Room set", entries=tracks) == "mix", "four artists with times -> mix")
chk(g("ETNOBOTANIKA - KOSMOBOTANIKA (Full Album)") == "album", "'Full Album' -> album")
chk(g("Analyzing Divine Machinery as an Art Movement", 3600) == "talk", "an analysis -> talk")
chk(g("Joshua Bushman interview about film", 3600) == "talk", "an interview -> talk")
chk(g("Late night mix 2024") == "mix", "'mix' in the title -> mix")
chk(g("Untitled", 2400, numbered=["a", "b", "c"]) == "album", "numbered list at record length -> album")
chk(g("Untitled", 8000) == "talk", "very long, no tracklist -> talk")
chk(g("Untitled", 200) == "album", "unknown short thing -> album (the common case)")
chk(g("Deep House Mix for study", entries=tracks[:2]) == "mix", "'mix' beats a tiny tracklist")
chk(g("Great talk", entries=tracks) == "mix", "a real many-artist tracklist beats the title")
chk(bool(f.guess({"title": "", "duration_s": None, "description": ""})["why"]), "a guess always says why")

# ── reading a video (network stubbed) ──
PAGE = "<script>var ytInitialPlayerResponse = " + json.dumps({"videoDetails": {
    "title": "ETNOBOTANIKA - KOSMOBOTANIKA (Full Album)", "author": "ETNOBOTANIKA", "lengthSeconds": "3329",
    "shortDescription": good}}) + ";var x=1;</script>"
def fake_get(url, **kw):
    if "oembed" in url:
        return json.dumps({"title": "ETNOBOTANIKA - KOSMOBOTANIKA (Full Album)", "author_name": "ETNOBOTANIKA", "thumbnail_url": "https://i.ytimg.com/t.jpg"})
    return PAGE
res, err = f.read("https://youtu.be/gZUPDL3RBYs", http_get=fake_get)
chk(err is None and res["meta"]["duration_s"] == 3329.0 and res["meta"]["author"] == "ETNOBOTANIKA", "read: title, channel, length")
chk(res["guess"]["kind"] == "album", "read: the album upload is guessed as an album")
chk(res["album_guess"] == {"artist": "ETNOBOTANIKA", "album": "KOSMOBOTANIKA"}, "read: artist and album prefilled")
chk(len(res["numbered"]) == 4 and res["entries"] == [], "read: untimed numbered list kept, no fake times")
res2, _ = f.read("https://youtu.be/gZUPDL3RBYs", pasted="0:00 A - one\n3:00 B - two\n6:00 C - three", http_get=fake_get)
chk(res2["from"] == "pasted" and len(res2["entries"]) == 3 and res2["guess"]["kind"] == "mix", "read: a pasted tracklist wins over the description")
_, err = f.read("https://example.com/x", http_get=fake_get)
chk(err and "YouTube" in err, "read: a non-YouTube link is refused plainly")

def down(url, **kw): raise OSError("offline")
res3, err3 = f.read("https://youtu.be/gZUPDL3RBYs", http_get=down)
chk(err3 is None and res3["meta"]["notes"] and res3["meta"]["title"] is None, "read: offline gives notes, never an invented title")

# ── the routes ──
import app
hooks = app.app.config["FIND_MUSIC_HOOKS"]; hooks["http_get"] = fake_get
c = app.app.test_client()
app._cat_health_last_check[0] = __import__("time").time() + 1e9     # keep upstream's health thread quiet
r = c.post("/api/find/read", json={"url": "https://youtu.be/gZUPDL3RBYs"})
chk(r.status_code == 200 and r.get_json()["guess"]["kind"] == "album", "POST /api/find/read works")
chk(c.post("/api/find/read", json={"url": "nope"}).status_code == 400, "bad link -> 400")
chk(c.post("/api/find/read", data="not json", content_type="text/plain").status_code == 400, "non-JSON body -> 400, not 500")
page = c.get("/find")
chk(page.status_code == 200 and b"Find it" in page.data and b"fd-kind" in page.data, "GET /find renders the page")
chk(b"Find it" in c.get("/tools").data and b'href="/find"' in c.get("/tools").data, "Tools links to it")
html = open(os.path.join(PROJ, "templates", "find.html"), encoding="utf-8").read()
import re
style = html[html.index("<style>"):html.index("</style>")]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", style), "page CSS uses theme tokens only, no hex")
chk("border-radius: 0" in style and "box-shadow" not in style, "square, no shadows (the house look)")
chk("innerHTML" in html and "esc(" in html, "page escapes what it prints")

# ── an album's songs ──
alb = {"items": [
    {"id": "t1", "name": "Mamy Jeszcze Jedno Miejsce", "duration_ms": 215500, "artists": [{"id": "a1", "name": "ETNOBOTANIKA"}],
     "external_urls": {"spotify": "https://open.spotify.com/track/t1"}},
    {"id": "t2", "name": "Trzy Doby Na Orbicie", "duration_ms": 0, "artists": [{"id": "a1", "name": "ETNOBOTANIKA"}, {"id": "a2", "name": "Gość"}]},
    {"id": "t3", "name": "", "artists": []},                      # no name: left out
    "junk", {"name": "No link or id", "artists": []}]}            # not a dict / no way to link: left out
rows = f.album_items(alb, "https://i.scdn.co/cover.jpg")
chk([r["title"] for r in rows] == ["Mamy Jeszcze Jedno Miejsce", "Trzy Doby Na Orbicie"], "album_items keeps album order and drops unusable rows")
chk(rows[0]["duration_sec"] == 215.5 and rows[1]["duration_sec"] is None, "length in seconds; none when Spotify gives none")
chk(rows[1]["subtitle"] == "ETNOBOTANIKA, Gość" and rows[1]["by_ids"] == "a1,a2", "artists joined; ids kept")
chk(rows[1]["url"] == "https://open.spotify.com/track/t2" and rows[0]["image"] == "https://i.scdn.co/cover.jpg", "link built from the id when missing; cover carried")
chk(f.album_items(None) == [] and f.album_items({}) == [] and f.album_items({"items": None}) == [], "empty answers -> no rows")

from flask import Flask
class FakeSpo:
    def __init__(self, ok=True, pages=1): self.ok, self.pages, self.calls = ok, pages, []
    def is_connected(self): return self.ok
    def _api_get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path.endswith("/FORBIDDEN12345/tracks"): return {"error": "Forbidden", "status": 403}
        off = (params or {}).get("offset", 0)
        more = off // 50 + 1 < self.pages
        return {"items": [{"id": f"t{off}", "name": f"Song {off}", "artists": [], "duration_ms": 1000}], "next": "x" if more else None}
def mini(spo):
    a = Flask(__name__); f.register(a, get_spotify=lambda: spo); return a.test_client()
spo = FakeSpo(pages=3); mc = mini(spo)
r = mc.get("/api/find/album-tracks?id=ABCDEFGHIJ1234&image=http://x/y.jpg").get_json()
chk(r["total"] == 3 and spo.calls[0][0] == "/albums/ABCDEFGHIJ1234/tracks" and [c[1]["offset"] for c in spo.calls] == [0, 50, 100], "route walks every page of an album")
chk(mc.get("/api/find/album-tracks?id=../../me").status_code == 400, "route refuses an id that is not a Spotify id (no path tricks)")
chk(mc.get("/api/find/album-tracks").status_code == 400, "route needs an id")
chk(mini(FakeSpo(ok=False)).get("/api/find/album-tracks?id=ABCDEFGHIJ1234").status_code == 401, "route says so when Spotify is not connected")
e = mc.get("/api/find/album-tracks?id=FORBIDDEN12345").get_json()
chk(e["error"] == "Forbidden" and e["status"] == 403, "Spotify's refusal is passed on in words, not as a crash")
chk(mini(None).get("/api/find/album-tracks?id=ABCDEFGHIJ1234").status_code == 401, "no Spotify client at all -> 401")
# ── Spotify's brief hiccups on the album's songs ──
naps = []
class Flaky:
    def __init__(self, script): self.script, self.n = script, 0
    def is_connected(self): return True
    def _api_get(self, path, params=None):
        r = self.script[min(self.n, len(self.script) - 1)]; self.n += 1
        return r
GOODP = {"items": [{"id": "t1", "name": "One", "artists": []}], "next": None}
spo = Flaky([GOODP]); d = f.get_retry(spo, "/albums/X/tracks", {}, sleep=naps.append)
chk(d == GOODP and spo.n == 1 and naps == [], "get_retry: a good answer comes straight back")
naps.clear(); spo = Flaky([{"error": "Service Unavailable", "status": 503}, GOODP]); d = f.get_retry(spo, "/albums/X/tracks", {}, sleep=naps.append)
chk(d == GOODP and spo.n == 2 and naps == [0.5], "get_retry: a 503 then fine: the person never sees it")
naps.clear(); spo = Flaky([{"error": "Service Unavailable", "status": 503}]); d = f.get_retry(spo, "/albums/X/tracks", {}, sleep=naps.append)
chk(d["status"] == 503 and spo.n == 3 and naps == [0.5, 1.5], "get_retry: 503 every time ends after three tries, with the error")
for code in (429, 403, 404, 401):
    naps.clear(); spo = Flaky([{"error": "no", "status": code}, GOODP]); d = f.get_retry(spo, "/x", {}, sleep=naps.append)
    chk(d.get("status") == code and spo.n == 1 and naps == [], f"get_retry: {code} is returned as it is, never retried")
chk(f.get_retry(Flaky(["junk"]), "/x", {}, sleep=naps.append) == "junk", "get_retry: an odd answer is not mistaken for an error")
f._ALBUM_CACHE.clear()          # (an earlier test already fetched this album; the route would answer from memory)
sp3 = Flaky([{"error": "Service Unavailable", "status": 503}, GOODP]); f.time.sleep = lambda s: None
rr = mini(sp3).get("/api/find/album-tracks?id=ABCDEFGHIJ1234").get_json()
chk(rr.get("total") == 1 and sp3.n == 2, "the album-tracks route survives a 503 on its first try")
hp = open(os.path.join(PROJ, "templates", "find.html"), encoding="utf-8").read()
chk("async function patient(ask, say)" in hp and "d.status !== 401 && d.status !== 403" in hp and "[1200, 2500, 4500]" in hp, "page: keeps trying, out loud, on a Spotify hiccup, but never on a refusal")
chk(hp.count("await patient(") >= 3, "page: the album search, the album's songs and every mix lookup all go through it")
chk("t.state = 'failed'" in hp and "Spotify didn\\'t answer" in hp and "did not answer" in hp, "mix: a Spotify failure is shown as 'didn't answer', not as 'not found', and can be retried")
chk("t.state === 'done') continue" in hp, "mix: songs already answered are not asked again on a retry")
chk("/api/find/album-tracks" in hp and "albumSongs" in hp, "page asks for the songs and lands them")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
