"""A Spotify answer that worked is remembered, so a later hiccup does not show as a failure.

Search: a good answer is reused for ten minutes without asking Spotify; if Spotify then fails (503, a
dropped connection, a 429) the last good answer is shown, marked stale; with nothing remembered it is an
honest error. A sign-in problem (401) is never papered over. An album's songs are remembered the same way.
Spotify is a stand-in; no network.

Run:  venv/bin/python test_spotify_cache.py
"""
import os, sys, tempfile, subprocess, time
PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_scache_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
subprocess.run([sys.executable, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([sys.executable, "migrate.py"], env=os.environ, capture_output=True)

import app
app._cat_health_last_check[0] = time.time() + 1e9
FOUND = {"tracks": [], "albums": [{"spotify_id": "AAAAAAAAAA", "title": "Kosmobotanika", "artist": "ETNOBOTANIKA", "artist_ids": "x", "image": "", "thumb": "",
                                    "year": "2025", "tracks": 14, "album_type": "album", "spotify_url": "https://open.spotify.com/album/AAAAAAAAAA"}], "artists": []}
class Spo:
    def __init__(self, script): self.script, self.n = script, 0
    def is_connected(self): return True
    def search(self, q, limit=10):
        r = self.script[min(self.n, len(self.script) - 1)]; self.n += 1
        return r
def cached(spo, q, now): return app._spotify_search_cached(spo, q, now=now)
app._SPOTIFY_SEARCH_CACHE.clear()
BAD = (503, {"tracks": [], "albums": [], "artists": []})

spo = Spo([(200, FOUND)])
chk(cached(spo, "etnobotanika kosmobotanika", 1000) == (200, FOUND, False) and spo.n == 1, "first search asks Spotify and remembers the answer")
chk(cached(spo, "  ETNOBOTANIKA   Kosmobotanika ", 1100) == (200, FOUND, False) and spo.n == 1, "the same search again (any case or spacing) is answered from memory: Spotify is not asked")
spo = Spo([BAD, (200, FOUND)])
chk(cached(spo, "etnobotanika kosmobotanika", 1000 + 700) == (200, FOUND, True) and spo.n == 1, "after ten minutes Spotify is asked again; if it hiccups, the last good answer is shown, marked stale")
chk(cached(Spo([(200, FOUND)]), "etnobotanika kosmobotanika", 1000 + 700 + 5)[2] is False, "…and when it answers again the answer is fresh")
spo = Spo([BAD]); app._SPOTIFY_SEARCH_CACHE.clear()
chk(cached(spo, "never searched", 5000)[0] == 503, "nothing remembered and Spotify down: an honest error, not an invented answer")
app._SPOTIFY_SEARCH_CACHE.clear(); cached(Spo([(200, FOUND)]), "q", 1000)
for code in (429, 0, 500, 502, 504):
    chk(cached(Spo([(code, BAD[1])]), "q", 1000 + 700)[:1] == (200,) and cached(Spo([(code, BAD[1])]), "q", 1000 + 700)[2] is True, f"a {code} is covered by the last good answer")
chk(cached(Spo([(401, BAD[1])]), "q", 1000 + 700)[0] == 401, "a 401 (not signed in) is never covered up: the person has to know")
chk(cached(Spo([BAD]), "q", 1000 + 86400 + 10)[0] == 503, "an answer older than a day is not used")
app._SPOTIFY_SEARCH_CACHE.clear()
for i in range(app._SEARCH_MAX + 5):
    cached(Spo([(200, FOUND)]), f"query {i}", 1000 + i)
chk(len(app._SPOTIFY_SEARCH_CACHE) <= app._SEARCH_MAX, f"the memory is bounded ({len(app._SPOTIFY_SEARCH_CACHE)} entries)")
chk("query 0" not in app._SPOTIFY_SEARCH_CACHE and f"query {app._SEARCH_MAX + 4}" in app._SPOTIFY_SEARCH_CACHE, "…and forgets the oldest first")

# through the real route
app._SPOTIFY_SEARCH_CACHE.clear()
c = app.app.test_client()
spo = Spo([(200, FOUND), BAD, BAD])
app._get_spotify = lambda: spo
r = c.get("/api/spotify/search?q=etnobotanika%20kosmobotanika").get_json()
chk(r["connected"] and r["results"] and r["results"][0]["title"] == "Kosmobotanika" and "stale" not in r, "route: a normal answer")
app._SPOTIFY_SEARCH_CACHE["etnobotanika kosmobotanika"] = (time.time() - 3000, FOUND)
r = c.get("/api/spotify/search?q=etnobotanika%20kosmobotanika").get_json()
chk(r.get("stale") is True and r["results"] and "error" not in r, "route: Spotify down after ten minutes -> the results, marked stale, no error")
r = c.get("/api/spotify/search?q=something%20new").get_json()
chk(r["results"] == [] and r["error"] == "Spotify didn't answer" and r["connected"] is True, "route: nothing remembered -> the honest error, still 'connected'")

# an album's songs (find_music)
import find_music as fm
class Albums:
    def __init__(self, script): self.script, self.n = script, 0
    def is_connected(self): return True
    def _api_get(self, path, params=None):
        r = self.script[min(self.n, len(self.script) - 1)]; self.n += 1; return r
PAGE = {"items": [{"id": "t1", "name": "One", "artists": []}], "next": None}
fm.time.sleep = lambda s: None
from flask import Flask
def mini(spo):
    a = Flask(__name__); fm.register(a, get_spotify=lambda: spo); return a.test_client()
fm._ALBUM_CACHE.clear()
sp = Albums([PAGE]); mc = mini(sp)
chk(mc.get("/api/find/album-tracks?id=ABCDEFGHIJ1").get_json()["total"] == 1 and sp.n == 1, "songs: first look asks Spotify")
chk(mc.get("/api/find/album-tracks?id=ABCDEFGHIJ1").get_json()["total"] == 1 and sp.n == 1, "songs: a second look within the hour does not")
fm._ALBUM_CACHE["ABCDEFGHIJ1"] = (time.time() - 7200, fm._ALBUM_CACHE["ABCDEFGHIJ1"][1])
sp = Albums([{"error": "Service Unavailable", "status": 503}]); mc = mini(sp)
r = mc.get("/api/find/album-tracks?id=ABCDEFGHIJ1").get_json()
chk(r.get("stale") is True and r["total"] == 1 and "error" not in r, "songs: after an hour, a Spotify hiccup is covered by the last list (even after the retries)")
sp = Albums([{"error": "Forbidden", "status": 403}]); mc = mini(sp)
r = mc.get("/api/find/album-tracks?id=ABCDEFGHIJ1").get_json()
chk(r.get("error") == "Forbidden" and "items" not in r, "songs: a 403 is never covered up (Spotify is refusing, and the page must say so)")
sp = Albums([{"error": "Service Unavailable", "status": 503}]); mc = mini(sp)
chk(mc.get("/api/find/album-tracks?id=ZZZZZZZZZZ1").get_json().get("error") == "Service Unavailable", "songs: nothing remembered -> the honest error")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
