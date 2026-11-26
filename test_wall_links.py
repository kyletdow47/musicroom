"""Proves the lines between things on the wall (wall_links.py, and the wall's save/load in app.py).

Lines are kept only when both blocks exist, never doubled (a line has no direction), cut to a sane size,
and survive a save by a caller that only knows about blocks (Explore's "save this moment", Keep).

Run:  venv/bin/python test_wall_links.py
"""
import os, sys, json, tempfile, subprocess
PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_links_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
subprocess.run([sys.executable, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([sys.executable, "migrate.py"], env=os.environ, capture_output=True)

import wall_links as wl
B = lambda *ids: [{"id": i, "type": "text"} for i in ids]
L = lambda a, b, **kw: dict({"a": a, "b": b}, **kw)

chk(wl.clean_links([L("a", "b")], B("a", "b")) == [{"a": "a", "b": "b", "kind": "mine", "label": ""}], "a line between two blocks is kept, and is the person's own by default")
chk(wl.clean_links([L("a", "zzz")], B("a", "b")) == [] and wl.clean_links([L("zzz", "a")], B("a", "b")) == [], "a line to a block that does not exist is dropped")
chk(wl.clean_links([L("a", "a")], B("a")) == [], "a block is not joined to itself")
r = wl.clean_links([L("a", "b", label="first"), L("b", "a", label="second"), L("a", "b")], B("a", "b"))
chk(len(r) == 1 and r[0]["label"] == "first", "a line has no direction: a→b and b→a are one line")
r = wl.clean_links([L("a", "b", kind="near"), L("b", "a", kind="mine", label="mine now")], B("a", "b"))
chk(len(r) == 1 and r[0]["kind"] == "mine" and r[0]["label"] == "mine now", "if a suggested line and the person's own line join the same pair, theirs wins")
r = wl.clean_links([L("a", "b", kind="near"), L("a", "b", kind="near")], B("a", "b"))
chk(r[0]["kind"] == "near", "two suggestions stay a suggestion")
chk(wl.clean_links([L("a", "b", kind="evil")], B("a", "b"))[0]["kind"] == "mine", "an unknown kind becomes the person's own")
chk(wl.clean_links([L("a", "b", label="  many   words \n here  ")], B("a", "b"))[0]["label"] == "many words here", "words are tidied to one line")
chk(len(wl.clean_links([L("a", "b", label="x" * 500)], B("a", "b"))[0]["label"]) == wl.MAX_LABEL, "very long words are cut")
chk(wl.clean_links([L("a", "b", label=None), L("a", "c", label=5)], B("a", "b", "c"))[1]["label"] == "5", "odd values do not crash")
for junk in (None, "x", 5, {}, [None, 5, "x", [], {"a": 1, "b": 2}, {"a": "a"}, {"b": "b"}]):
    chk(wl.clean_links(junk, B("a", "b")) == [], f"junk is ignored: {str(junk)[:30]}")
chk(wl.clean_links([L("a", "b")], None) == [] and wl.clean_links([L("a", "b")], [None, 5, {"x": 1}, {"id": 7}]) == [], "bad blocks mean no lines, not a crash")
many = [L("n%d" % i, "n%d" % (i + 1)) for i in range(wl.MAX_LINKS + 50)]
chk(len(wl.clean_links(many, B(*["n%d" % i for i in range(wl.MAX_LINKS + 60)]))) == wl.MAX_LINKS, "the number of lines is capped")
chk(wl.clean_links([L("a", "b", kind="near")], B("a", "b")) == wl.clean_links(wl.clean_links([L("a", "b", kind="near")], B("a", "b")), B("a", "b")), "cleaning twice changes nothing")

# the wall's save and load
import app
app._cat_health_last_check[0] = __import__("time").time() + 1e9
c = app.app.test_client()
blocks = [{"id": "k1", "type": "text", "x": 0, "y": 0, "w": 100, "h": 100, "z": 1, "data": {}}, {"id": "k2", "type": "text", "x": 200, "y": 0, "w": 100, "h": 100, "z": 2, "data": {}},
          {"id": "k3", "type": "text", "x": 400, "y": 0, "w": 100, "h": 100, "z": 3, "data": {}}]
r = c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks, "links": [L("k1", "k2", label="same night"), L("k2", "k3", kind="near"), L("k1", "ghost")]})
chk(r.status_code == 200 and r.get_json()["links"] == 2, "saving keeps real lines and drops the one to a block that is not there")
d = c.get("/api/room/canvas/9").get_json()
chk([(l["a"], l["b"], l["kind"], l["label"]) for l in d["links"]] == [("k1", "k2", "mine", "same night"), ("k2", "k3", "near", "")], "loading returns the lines")
r = c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks + [{"id": "k4", "type": "mix", "x": 0, "y": 300, "w": 100, "h": 100, "z": 4, "data": {}}]})
chk(r.status_code == 200 and len(c.get("/api/room/canvas/9").get_json()["links"]) == 2, "a save that does not mention lines (Explore's 'save this moment') leaves them alone")
r = c.post("/api/room/canvas/9", json={"v": 1, "blocks": [blocks[0], blocks[2]]})
chk([(l["a"], l["b"]) for l in c.get("/api/room/canvas/9").get_json()["links"]] == [], "removing a block removes the lines that ended on it")
c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks, "links": [L("k1", "k2")]})
c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks, "links": []})
chk(c.get("/api/room/canvas/9").get_json()["links"] == [], "saving an empty list of lines really clears them")
chk(c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks, "links": "nonsense"}).status_code == 200 and c.get("/api/room/canvas/9").get_json()["links"] == [], "nonsense for lines is cleared, not a crash")
chk(c.get("/api/room/canvas/777").get_json()["links"] == [], "a wall that was never saved has no lines")
# a wall saved before lines existed still loads
with app.app.app_context():
    app.set_setting("room_canvas:8", json.dumps({"v": 1, "blocks": blocks}))
chk(c.get("/api/room/canvas/8").get_json()["links"] == [] and len(c.get("/api/room/canvas/8").get_json()["blocks"]) == 3, "a wall saved before lines existed loads fine")
# connections.put_web_on_wall saves through the app's helper: lines must survive it
import connections
wall = {"load": lambda pid: app._room_canvas_load(pid), "well": lambda: 9}
c.post("/api/room/canvas/9", json={"v": 1, "blocks": blocks, "links": [L("k1", "k2", label="keep me")]})
from flask import Flask
a2 = Flask(__name__)
store = {}
def saver(pid, bl):
    app.set_setting(f"room_canvas:{pid}", json.dumps({"v": 1, "blocks": bl, "links": wl.clean_links(app._room_canvas_load(pid).get("links"), bl)}))
connections.register(a2, wall=dict(wall, save=saver))
web = {"nodes": [{"type": "album", "id": 1, "name": "A", "x": 0, "y": 0}], "edges": [], "home": 0}
rr = a2.test_client().post("/api/connections/keep", json={"web": web})
chk(rr.status_code == 200, "keeping a web still works")
after = c.get("/api/room/canvas/9").get_json()
chk(len(after["blocks"]) == 4 and [(l["a"], l["b"], l["label"]) for l in after["links"]] == [("k1", "k2", "keep me")], "…and does not wipe the lines already on the wall")
src = open(os.path.join(PROJ, "app.py"), encoding="utf-8").read()
chk('"links": __import__("wall_links").clean_links(_room_canvas_load(pid).get("links"), blocks)' in src, "app.py's own Keep helper preserves lines (it is what the test above imitates)")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
