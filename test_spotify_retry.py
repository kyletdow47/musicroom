"""A search that hits one of Spotify's brief hiccups is tried again, shortly, so a person does not
have to press twice.

On 26 Nov 2026 six of twenty searches failed on the first try: four with Spotify's HTTP 503 and two
with a dropped connection (each waiting out the 8-second timeout). None was a rate limit. The same
search a moment later worked. `SpotifyClient._player_call_retry` (which `search()` now uses) tries
those again, and nothing else: never a 429 (Spotify asking us to slow down), never a 4xx the person
has to fix. Spotify is a stand-in here; nothing is slept or sent.

Run:  venv/bin/python test_spotify_retry.py
"""
import os, sys
PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ); os.chdir(PROJ)
from spotify import SpotifyClient

fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

OK = (200, {"tracks": {"items": []}, "albums": {"items": [{"id": "A1", "name": "Kosmobotanika", "artists": [{"id": "x", "name": "ETNOBOTANIKA"}]}]}, "artists": {"items": []}})

def client(script):
    """A client whose Spotify answers come from `script`, in order (the last one repeats)."""
    c = SpotifyClient.__new__(SpotifyClient)
    c.calls = 0
    def call(method, path, params=None, body=None, timeout=8, _retry=True):
        r = script[min(c.calls, len(script) - 1)]; c.calls += 1
        return r
    c._player_call = call
    return c
pauses = []
def nap(s): pauses.append(s)

def run(script, **kw):
    pauses.clear()
    c = client(script)
    st, out = c._player_call_retry("GET", "/search", {"q": "x"}, sleep=nap, **kw)
    return st, c.calls, list(pauses)

st, n, p = run([OK])
chk((st, n, p) == (200, 1, []), "a good answer is returned at once: no retry, no waiting")
st, n, p = run([(503, {}), OK])
chk((st, n) == (200, 2) and p == [0.4], f"one 503, then fine: the person never sees it ({st}, {n} calls, paused {p})")
st, n, p = run([(503, {}), (503, {}), OK])
chk((st, n) == (200, 3) and p == [0.4, 1.0], f"two 503s, then fine ({n} calls, paused {p})")
st, n, p = run([(503, {})])
chk((st, n) == (503, 3) and p == [0.4, 1.0], f"503 every time: it gives up after three tries and says 503 ({n} calls)")
for code in (500, 502, 504):
    chk(run([(code, {}), OK])[0] == 200, f"{code} is a brief hiccup too: tried again")
st, n, p = run([(0, {"error": "network"}), OK])
chk((st, n) == (200, 2), "a dropped connection is tried once more")
st, n, p = run([(0, {"error": "network"})])
chk((st, n) == (0, 2), f"…but only once: a second timeout is not waited out again ({n} calls, so at most two 8-second waits)")
st, n, p = run([(503, {}), (0, {"error": "network"}), OK])
chk((st, n) == (200, 3), "a 503 and then a dropped connection can still recover")
for code, body in [(429, {"error": "rate-limited"}), (401, {"error": "not connected"}), (403, {}), (404, {}), (400, {}), (204, {})]:
    st, n, p = run([(code, body), OK])
    chk((st, n, p) == (code, 1, []), f"{code} is passed straight back, never retried (it needs time or the person, not a hammer)")
st, n, p = run([(503, {}), OK], pauses=())
chk((st, n) == (503, 1), "with no pauses given there are no retries")
st, n, p = run([(503, {}), (503, {}), (503, {}), OK], pauses=(0.1, 0.2, 0.3))
chk((st, n) == (200, 4) and p == [0.1, 0.2, 0.3], "the number of tries follows the pauses given")

# search() itself uses it, and still reads the answer
c = client([(503, {}), OK])
# (search() uses the real, short pauses: about a second and a half in all for the tests below)
status, found = c.search("etnobotanika kosmobotanika")
chk(status == 200 and c.calls == 2 and found["albums"][0]["title"] == "Kosmobotanika", "search(): a 503 first time, then the albums are read as normal")
c = client([(503, {})])
status, found = c.search("x")
chk(status == 503 and found == {"tracks": [], "albums": [], "artists": []} and c.calls == 3, "search(): if Spotify stays down, it ends as before: the status, and nothing found")
c = client([(429, {"error": "rate-limited"})])
status, found = c.search("x")
chk(status == 429 and c.calls == 1, "search(): a 429 is not hammered")
src = open(os.path.join(PROJ, "spotify.py"), encoding="utf-8").read()
chk('self._player_call_retry("GET", "/search"' in src and "never a 429" in src.lower(), "search() uses the retrying call, and the reason is written down")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
