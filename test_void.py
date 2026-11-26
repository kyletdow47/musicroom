"""Proves the Void's own parts (static/js/void.js): which pictures it may show, how text is wrapped onto a thing, that it offers
everything the music, the record page and the list were built to call, and the choices that keep it safe and calm.

(How it looks and moves is checked in a real browser; here is what can be checked without one.)

Run:  venv/bin/python test_void.py
"""
import os, sys, json, subprocess, shutil, re
PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
JS = os.path.join(PROJ, "static", "js")
void_src = open(os.path.join(JS, "void.js"), encoding="utf-8").read()
node = shutil.which("node")

if node:
    H = r"""
    const fs = require('fs'), vm = require('vm');
    const win = {}; const ctx = { window: win, console, URL, Date };
    for (const f of ['void_layout.js', 'void.js']) vm.runInNewContext(fs.readFileSync(process.argv[1] + '/' + f, 'utf8'), ctx);
    const V = win.MRVoid, out = {};
    out.pic = ['/static/room_canvas/a.jpg', '/api/void/image?u=x', 'https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg', 'https://i.scdn.co/image/a b', 'http://cdn.dzcdn.net/x.jpg', 'javascript:alert(1)', 'data:image/png;base64,AA', '//cdn.dzcdn.net/x.jpg', '', null, undefined, 5, '/etc/passwd', 'file:///x', 'ftp://x/y'].map(u => V.pictureUrl(u));
    const ctxm = { font: '', measureText(t) { return { width: t.length * 10 }; } };
    const wl = (t, w, n) => V.wrapLines(ctxm, t, w, n);
    out.wrap = { basic: wl('one two three four five', 100), exact: wl('aaaaaaaaaa bbbbbbbbbb', 100), newline: wl('first\n\nsecond line here', 200), empty: wl('', 100), nil: wl(null, 100), long: wl('supercalifragilisticexpialidocious tiny', 100),
                 capped: wl('a b c d e f g h i j k l m n o p q r s t', 40, 2), cappedEnd: wl('alpha beta gamma delta epsilon zeta', 80, 2), spaces: wl('   lots    of   space   ', 200), one: wl('word', 10) };
    out.types = Object.keys(V.TYPES);
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", H, JS], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(r.stderr[-1500:]); chk(False, "void.js loads under node"); sys.exit(1)
    o = json.loads(r.stdout.strip().splitlines()[-1])
    p = o["pic"]
    chk(p[0] == "/static/room_canvas/a.jpg" and p[1] == "/api/void/image?u=x", "the app's own pictures are used as they are")
    chk(p[2] == "/api/void/image?u=https%3A%2F%2Fcdn-images.dzcdn.net%2Fimages%2Fcover%2Fx%2F250x250.jpg" and p[3].startswith("/api/void/image?u=https%3A%2F%2Fi.scdn.co%2Fimage%2Fa%20b"), "any other https picture goes through the app's picture proxy (which only fetches from the music services), encoded")
    chk(p[4:] == [""] * 11, f"http, javascript:, data:, protocol-relative, other paths, file, ftp, and junk give no picture at all ({p[4:]})")
    w = o["wrap"]
    chk(w["basic"] == ["one two", "three four", "five"], f"words wrap at the width ({w['basic']})")
    chk(w["exact"] == ["aaaaaaaaaa", "bbbbbbbbbb"] and w["one"] == ["word"], "a line that just fits is kept; a word alone is one line")
    chk(w["newline"] == ["first", "", "second line here"] or w["newline"] == ["first", "second line here"], f"new lines in the words are kept ({w['newline']})")
    chk(w["empty"] == [""] and w["nil"] == [""], "no words give one empty line, never a crash")
    chk(w["long"][0] == "supercalifragilisticexpialidocious" and w["long"][1] == "tiny", "a word too long for the width stays whole on its own line")
    chk(len(w["capped"]) == 2 and w["capped"][1].endswith("…") and len(w["cappedEnd"]) == 2 and w["cappedEnd"][1].endswith("…"), f"a cap on lines ends the last with an ellipsis ({w['capped']})")
    chk(w["spaces"] == ["lots of space"], "stray spaces are tidied")
    chk(o["types"] == ["node", "text", "quote", "image", "link", "mix"], "the Void's kinds match what the server keeps (void_store.TYPES)")
else:
    print("  SKIP  node is not installed: the pure parts are not checked")

import void_store
chk(list(void_store.TYPES) == ["node", "text", "quote", "image", "link", "mix"], "…the server's kinds are the same list")

# ── what the music, the record page and the list call must exist ──
wm = open(os.path.join(JS, "wall_music.js"), encoding="utf-8").read()
wi = open(os.path.join(JS, "wall_index.js"), encoding="utf-8").read()
used = set(re.findall(r"\bwall\.([A-Za-z]+)", wm + wi))
api_block = void_src[void_src.index("    var api = {"):void_src.index("    // ── start: the 3D engine")]
provided = set(re.findall(r"\b([A-Za-z]+)\s*:", api_block)) | set(re.findall(r"get\s+([A-Za-z]+)\s*\(", api_block)) | set(re.findall(r"\bapi\.([A-Za-z]+)\s*=", void_src))
optional = {"flyTo", "flyToGroup", "nearBlocks"}                       # the old wall has neither; they ask first (if (wall.flyTo) …)
missing = sorted(n for n in used if n not in provided)
chk(not missing, f"the Void offers everything the music, the record page and the list call on the wall: {missing or sorted(used)}")
chk(all(n in provided for n in optional), "…and the three things only the Void has (flyTo, flyToGroup, nearBlocks)")
chk("if (wall.flyTo)" in wm and "wall.nearBlocks ?" in wm and "if (wall.flyTo)" in wi and "if (wall.flyToGroup)" in wm and wm.count("wall.flyToGroup") >= 2, "…which the music and the list use only when they are there, so the old wall still works")

# ── the choices that keep it safe and calm ──
chk("/api/void/image?u=" in void_src and "pictureUrl(" in void_src and "new THREE.TextureLoader().load(url" in void_src, "every cover goes through the picture proxy")
chk("html = '<i>' + pad(b.n) + '</i>' + (name ? '. ' + esc(name) : '')" in void_src and "esc(s.label)" in void_src and "esc(d[k] || '')" in void_src and "esc(KIND_WORD[b.type] || 'Thing')" in void_src,
    "the places it writes into the page (a caption, a timeline date, a form's values and title) escape what they show")
chk("c.getContext('2d')" in void_src and "fillText" in void_src and "textContent" not in void_src.split("paintThing")[1].split("function paintTag")[0], "words are painted onto a canvas, never put into the page as markup")
chk("prefers-reduced-motion" in void_src and "par.x = par.y = 0" in void_src and "var k = reduced ? 1 : 0.12" in void_src, "reduced motion: no drift toward the pointer, and flights are instant")
chk("var FOCUS = 7;" in void_src and "depth.zMin + FOCUS" in void_src, "the camera stops 7 units in front of the oldest thing, not past it")
chk("tgt.z = clamp(tgt.z - e.deltaY * f, r.min, r.max)" in void_src and "e.preventDefault(); var r = zRange()" in void_src, "scroll falls through time and cannot leave the void's ends")
chk("flyBack()" in void_src and "if (!back) back = " in void_src and "function flyNow()" in void_src, "Esc and a click on nothing fly back out, to where you were; Now flies to the present")
chk("changed(); select(b.id); flyTo(b); openForm(b);" in void_src and "freeSpot" in void_src, "a new thing goes in a clear spot, the camera flies to it, and its form opens")
chk("'/api/room/void/' + placeId" in void_src and "next: counter" in void_src, "it keeps its things in its own store, with the number counter")
chk("host.__vd = api" in void_src and "WallMusic.attach(api)" in void_src and "WallIndex.attach(api)" in void_src, "the music and the list join in")
chk("The Void needs WebGL" in void_src and "intro();" in void_src, "without WebGL it says so (and the list still works)")
tpl = open(os.path.join(PROJ, "templates", "room3d.html"), encoding="utf-8").read()
chk("/static/js/void_layout.js" in tpl and "/static/js/void.js" in tpl and tpl.index("void_layout.js") < tpl.index("static/js/void.js"), "the page loads the layout before the Void")
chk("window.MR_WALL && window.MRVoid" in tpl and ">Void</button>" in tpl and 'data-to="wall">Void' in tpl, "in this fork the third tab is the Void (the full app keeps the old wall)")
css = open(os.path.join(PROJ, "static", "css", "room_canvas.css"), encoding="utf-8").read()
vc = css[css.index("/* ── the Void:"):]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", vc) and "box-shadow" not in vc and "border-radius:0" in vc.replace(" ", ""), "the Void's styles use theme tokens only, square, no shadows")
chk(all(c.startswith("vd") or c in ("rc-hud", "rc-ring") for c in set(re.findall(r"\.((?:vd|rc)-[a-z0-9_-]+)", vc))), "its new classes all start with vd (no clashes)")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
