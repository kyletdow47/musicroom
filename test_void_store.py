"""Proves what the Void keeps (void_store.py): its blocks are rebuilt from only what is understood, it has a store of its
own (the old wall is left exactly as it was), lines and the number counter behave, and the picture proxy fetches only from
the music services' own picture servers and nothing else.

Run:  venv/bin/python test_void_store.py
"""
import os, sys, json, tempfile, subprocess, time
PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_void_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
subprocess.run([sys.executable, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([sys.executable, "migrate.py"], env=os.environ, capture_output=True)

import void_store as vs
B = lambda id="k1", **kw: dict({"id": id, "type": "text", "x": 10, "y": 20, "w": 300, "h": 160, "c": 1_700_000_000_000, "n": 1, "d": 0, "data": {"heading": "h"}}, **kw)
NOW = 1_800_000_000_000

# ── clean_blocks ──
r = vs.clean_blocks([B()], now_ms=NOW)
chk(r == [{"id": "k1", "type": "text", "x": 10.0, "y": 20.0, "w": 300, "h": 160, "c": 1_700_000_000_000, "n": 1, "d": 0, "data": {"heading": "h"}}], "a good block is kept, rebuilt from only the fields it understands")
chk(vs.clean_blocks([B(foo="x", z=5, evil={"a": 1})], now_ms=NOW)[0].keys() == r[0].keys(), "unknown fields are dropped")
chk(vs.clean_blocks([B(type="web"), B("k2", type="record"), B("k3", type="<script>")], now_ms=NOW) == [], "only the Void's kinds are kept (the old wall's web, record and playlist blocks are not)")
chk([x["type"] for x in vs.clean_blocks([B("a%d" % i, type=t) for i, t in enumerate(vs.TYPES)], now_ms=NOW)] == list(vs.TYPES), "every kind it knows is kept")
for bad_id in ["", "a b", "a/b", "../x", "x" * 41, None, 5, "<b>", "a;b"]:
    chk(vs.clean_blocks([B(bad_id)], now_ms=NOW) == [], f"a block id that is not plain is dropped: {bad_id!r}")
chk(len(vs.clean_blocks([B("a"), B("a", x=99)], now_ms=NOW)) == 1 and vs.clean_blocks([B("a"), B("a", x=99)], now_ms=NOW)[0]["x"] == 10.0, "the same id twice keeps the first")
for bad in [float("nan"), float("inf"), 1e12, "x", None, True]:
    chk(vs.clean_blocks([B(x=bad)], now_ms=NOW) == [], f"a bad position drops the block: {bad!r}")
r = vs.clean_blocks([B(w=-5, h=1e9)], now_ms=NOW)[0]
chk((r["w"], r["h"]) == (360, 200), "a silly size becomes that kind's usual size (words: 360 by 200)")
r = vs.clean_blocks([B(d=9, n=-4, c="soon")], now_ms=NOW)[0]
chk(r["d"] == 0 and r["n"] == 0 and r["c"] == NOW, "a layer out of range, a bad number and a bad time are tamed (a block with no usable time lands in the present)")
chk(vs.clean_blocks([B(d=3), B("b", d=-3)], now_ms=NOW)[0]["d"] == 3 and vs.clean_blocks([B(d=-3)], now_ms=NOW)[0]["d"] == -3, "layers -3 to 3 are kept")
chk(vs.clean_blocks([B(data="string"), B("b", data=None), B("c", data=[1])], now_ms=NOW)[0]["data"] == {}, "data that is not an object becomes an empty one")
chk(vs.clean_blocks([B(data={"x": "y" * (vs.MAX_DATA + 10)})], now_ms=NOW) == [], "a block carrying a huge amount of data is dropped")
chk(len(vs.clean_blocks([B("k%d" % i) for i in range(vs.MAX_BLOCKS + 50)], now_ms=NOW)) == vs.MAX_BLOCKS, "the number of blocks is capped")
chk(vs.clean_blocks(None) == [] and vs.clean_blocks("x") == [] and vs.clean_blocks([None, 5, "x", [], {}]) == [], "junk is ignored")
chk(vs.clean_blocks([B()], now_ms=NOW) == vs.clean_blocks(vs.clean_blocks([B()], now_ms=NOW), now_ms=NOW + 5), "cleaning twice changes nothing")

# ── the store ──
import app
app._cat_health_last_check[0] = time.time() + 1e9
c = app.app.test_client()
chk(c.get("/api/room/void/3").get_json() == {"blocks": [], "links": [], "next": 0}, "a Void never saved is empty")
old = json.dumps({"v": 1, "blocks": [{"id": "old1", "type": "text", "x": 0, "y": 0, "w": 9, "h": 9, "z": 1, "data": {"heading": "Testing"}}], "links": [], "next": 4})
with app.app.app_context():
    app.set_setting("room_canvas:3", old)
blocks = [B("a", n=1), B("b", n=2, type="quote", data={"text": "words"}), B("c", n=3, type="node", data={"kind": "album", "name": "X"})]
r = c.post("/api/room/void/3", json={"blocks": blocks, "links": [{"a": "a", "b": "b", "kind": "mine", "label": "same night"}, {"a": "a", "b": "ghost"}], "next": 3})
chk(r.status_code == 200 and r.get_json() == {"ok": True, "n": 3, "links": 1, "next": 3}, "saving keeps the blocks and only the lines between real blocks")
g = c.get("/api/room/void/3").get_json()
chk([b["id"] for b in g["blocks"]] == ["a", "b", "c"] and g["links"][0]["label"] == "same night" and g["next"] == 3, "and loads them back")
with app.app.app_context():
    chk(app.get_setting("room_canvas:3", "") == old, "the old wall's own store is untouched, byte for byte")
c.post("/api/room/void/3", json={"blocks": blocks})
chk(len(c.get("/api/room/void/3").get_json()["links"]) == 1, "a save that does not mention lines keeps them")
c.post("/api/room/void/3", json={"blocks": blocks[:1], "links": []})
chk(c.get("/api/room/void/3").get_json()["links"] == [], "saving an empty list of lines clears them")
chk(c.get("/api/room/void/3").get_json()["next"] == 3, "the counter stays at its highest after blocks go (3 is never given out again)")
c.post("/api/room/void/3", json={"blocks": blocks[:1], "next": 1})
chk(c.get("/api/room/void/3").get_json()["next"] == 3, "…and a caller cannot lower it")
for bad in ({}, {"blocks": "x"}, {"blocks": None}, {"links": []}):
    chk(c.post("/api/room/void/3", json=bad).status_code == 400, f"a bad save is refused: {str(bad)[:30]}")
chk(c.post("/api/room/void/3", data="nope", content_type="text/plain").status_code == 400, "a non-JSON save is a 400, not a crash")
chk(c.post("/api/room/void/3", json={"blocks": [B("k%d" % i) for i in range(vs.MAX_BLOCKS * 2 + 1)]}).status_code == 400, "an absurd number of blocks is refused outright")
r = c.post("/api/room/void/4", json={"blocks": [B("x", type="web"), B("y", x="bad"), B("z")]})
chk(r.get_json()["n"] == 1, "what cannot be understood is dropped on the way in")
chk(c.get("/api/room/void/4").get_json()["blocks"][0]["id"] == "z" and c.get("/api/room/void/5").get_json()["blocks"] == [], "each room has its own Void")

# ── the picture proxy: only the music services' own picture servers ──
ok = ["https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg", "https://i.scdn.co/image/abc", "https://e-cdns-images.dzcdn.net/a.jpg", "https://i.ytimg.com/vi/abc/hqdefault.jpg", "https://dzcdn.net/x.jpg"]
for u in ok: chk(vs.image_url_ok(u), f"allowed: {u[:50]}")
for u in ["http://cdn-images.dzcdn.net/x.jpg", "https://evil.com/x.jpg", "https://dzcdn.net.evil.com/x.jpg", "https://evildzcdn.net/x.jpg", "https://cdn.dzcdn.net@evil.com/x.jpg",
          "https://user:pw@cdn.dzcdn.net/x.jpg", "https://cdn.dzcdn.net:8443/x.jpg", "https://127.0.0.1/x.jpg", "https://localhost/x.jpg", "https://10.0.0.1/x.jpg", "https://[::1]/x.jpg",
          "file:///etc/passwd", "data:image/png;base64,AA", "javascript:alert(1)", "//cdn.dzcdn.net/x.jpg", "", None, "https:///x", "ftp://i.scdn.co/x", "https://scdn.co.evil.com/x", "https://i.scdn.co.evil.com/x"]:
    chk(not vs.image_url_ok(u), f"refused: {str(u)[:50]!r}")

class Resp:
    def __init__(self, body, ctype="image/jpeg", code=200, headers=None):
        self._b, self.headers = body, dict({"Content-Type": ctype}, **(headers or {})); self.code = code
    def read(self, n=-1): return self._b if n < 0 else self._b[:n]
    def __enter__(self): return self
    def __exit__(self, *a): return False
class Opener:
    def __init__(self, script): self.script, self.urls = list(script), []
    def open(self, req, timeout=0):
        self.urls.append(req.full_url); r = self.script.pop(0)
        if isinstance(r, Exception): raise r
        return r
import urllib.error, io
def herr(code, loc=None): return urllib.error.HTTPError("u", code, "x", {"Location": loc} if loc else {}, io.BytesIO(b""))
u0 = ok[0]
chk(vs.fetch_image(u0, Opener([Resp(b"JPEGDATA")])) == ("image/jpeg", b"JPEGDATA"), "a good picture comes back with its type")
chk(vs.fetch_image(u0, Opener([Resp(b"<svg/>", "image/svg+xml")]))[0] is None, "an SVG is refused (it can carry script)")
chk(vs.fetch_image(u0, Opener([Resp(b"<html>", "text/html")]))[0] is None and vs.fetch_image(u0, Opener([Resp(b"x", "")]))[0] is None, "anything that is not a picture is refused")
chk(vs.fetch_image(u0, Opener([Resp(b"x" * (vs.MAX_IMAGE + 5))]))[0] is None, "a huge picture is refused")
chk(vs.fetch_image(u0, Opener([herr(404)]))[0] is None and vs.fetch_image(u0, Opener([OSError("down")]))[0] is None, "a failure is a plain refusal, not a crash")
op = Opener([herr(302, "https://i.scdn.co/image/final"), Resp(b"OK")])
chk(vs.fetch_image(u0, op) == ("image/jpeg", b"OK") and op.urls[-1] == "https://i.scdn.co/image/final", "a redirect to another allowed picture server is followed")
op = Opener([herr(302, "https://evil.com/steal"), Resp(b"NO")])
chk(vs.fetch_image(u0, op)[0] is None and op.urls == [u0], "a redirect to anywhere else is refused, and never fetched")
op = Opener([herr(302, "http://127.0.0.1:5177/api/secret")])
chk(vs.fetch_image(u0, op)[0] is None and op.urls == [u0], "a redirect to this very computer is refused")
op = Opener([herr(302, "/images/x.jpg"), Resp(b"REL")])
chk(vs.fetch_image(u0, op)[0] == "image/jpeg", "a relative redirect stays on the same picture server")
chk(vs.fetch_image(u0, Opener([herr(302, ok[0])] * 5))[0] is None, "a redirect loop gives up")

calls = []
def fake_fetch(url, **kw): calls.append(url); return "image/png", b"PNGBYTES"
vs.fetch_image = fake_fetch
vs._cache.clear()
r = c.get("/api/void/image", query_string={"u": ok[1]})
chk(r.status_code == 200 and r.data == b"PNGBYTES" and r.mimetype == "image/png", "the route serves a picture")
chk("max-age" in r.headers["Cache-Control"] and r.headers["X-Content-Type-Options"] == "nosniff", "…cacheable, and never guessed at")
c.get("/api/void/image", query_string={"u": ok[1]})
chk(calls == [ok[1]], "the same picture twice is fetched once")
chk(c.get("/api/void/image", query_string={"u": "https://evil.com/x.png"}).status_code == 400 and c.get("/api/void/image").status_code == 400 and len(calls) == 1, "a picture from anywhere else is refused before any fetch")
vs.fetch_image = lambda url, **kw: (None, "The picture server answered 404.")
vs._cache.clear()
r = c.get("/api/void/image", query_string={"u": ok[2]})
chk(r.status_code == 502 and "404" in r.get_json()["error"], "a failed fetch is a plain 502 with a reason")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
