"""Proves the room wall's quote blocks and timed mixes: a time can be typed in several ways and is read
back the same way, a link's own time is found, a YouTube or Vimeo mix starts where it should, a quote's
markup escapes everything typed into it, a quote and a timed mix survive being saved and loaded with
the room's wall, and Explore's copy of the time reader agrees with the wall's.

Run:  venv/bin/python test_room_blocks.py
"""
import json, os, re, shutil, subprocess, sys, tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_blocks_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
PY = sys.executable
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

CANVAS = os.path.join(PROJ, "static", "js", "room_canvas.js")
EXPLORE = os.path.join(PROJ, "templates", "explore3d.html")

subprocess.run([PY, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([PY, "migrate.py"], env=os.environ, capture_output=True)

# ── the wall's helpers, under node ──
node = shutil.which("node")
if not node:
    print("  SKIP  no node here: the wall's helpers are not checked")
else:
    HARNESS = r"""
    const fs = require('fs'), vm = require('vm');
    const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, URL, console });
    const W = win.MRRoomCanvas;
    // Explore keeps its own copy of the time reader; pull it out of the page and run it here
    const html = fs.readFileSync(process.argv[2], 'utf8');
    const m = /function clockText\(sec\)\{[\s\S]*?\nfunction clockSecs\(txt\)\{[\s\S]*?\n\}\n/.exec(html);
    const ex = {}; vm.runInNewContext(m[0] + '; out.clockText = clockText; out.clockSecs = clockSecs;', { out: ex });
    const out = {};
    const clocks = ['9:19', '1:02:03', '559', '9m19s', '1h2m3s', '0:05', ' 12:00 ', '1:75', '1:02:75', 'abc', '', '1:2:3:4', '99:99:99', '-5', '100000h', '9:19.5', '5s', '2m', '1h', null, undefined];
    out.parse = clocks.map(c => W.parseClock(c));
    out.parseEx = clocks.map(c => ex.clockSecs(c));
    const secs = [0, 5, 59.9, 559, 3600, 3723, null, -1, NaN, Infinity, 'x'];
    out.fmt = secs.map(c => W.fmtClock(c));
    out.fmtEx = secs.map(c => (typeof c === 'number' ? ex.clockText(c) : null));
    const urls = ['https://youtu.be/abcdefghijk?t=559', 'https://www.youtube.com/watch?v=abcdefghijk&t=9m19s', 'https://youtu.be/abcdefghijk?start=90',
                  'https://vimeo.com/123456#t=30s', 'https://youtu.be/abcdefghijk', 'not a url', '', 'https://youtu.be/abcdefghijk?t=zzz'];
    out.from = urls.map(u => W.startFromUrl(u));
    out.with = [W.withStart('https://youtu.be/abcdefghijk', 559), W.withStart('https://youtu.be/abcdefghijk?t=10', 559), W.withStart('https://www.youtube.com/watch?v=abcdefghijk&start=5', 90),
                W.withStart('https://youtu.be/abcdefghijk', 0), W.withStart('https://example.com/x', 559), W.withStart('nope', 5), W.withStart('https://soundcloud.com/a/b', 30)];
    const e = u => W.embedFor.apply(null, [].concat(u));
    out.embed = [e(['https://youtu.be/abcdefghijk?t=559']), e(['https://youtu.be/abcdefghijk?t=559', 100]), e(['https://youtu.be/abcdefghijk']), e(['https://youtu.be/abcdefghijk', 0]),
                 e(['https://vimeo.com/123456', 45]), e(['https://vimeo.com/123456']), e(['https://open.spotify.com/track/abc123', 30]), e(['https://example.com/x']),
                 e(['https://soundcloud.com/a/b', 30])];
    const hostile = { text: '<script>alert(1)</script> "quoted" & <b>bold</b>', who: '<img src=x onerror=alert(2)>', source: '" onmouseover="alert(3)', url: 'https://youtu.be/abcdefghijk?t=559' };
    out.hostile = W.quoteHtml(hostile);
    out.empty = W.quoteHtml({});
    out.blank = W.quoteHtml({ text: '   ' });
    out.noSource = W.quoteHtml({ text: 'words' });
    out.timed = W.quoteHtml({ text: 'words', who: 'A', source: 'B', url: 'https://youtu.be/abcdefghijk?t=559' });
    out.typedAt = W.quoteHtml({ text: 'words', at: 75 });
    out.typedBeats = W.quoteHtml({ text: 'words', at: 75, url: 'https://youtu.be/abcdefghijk?t=559' });
    out.noTime = W.quoteHtml({ text: 'words', url: 'https://example.com/page' });
    out.types = Object.keys(W.TYPES);
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", HARNESS, CANVAS, EXPLORE], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        chk(False, "the helpers run under node: " + (r.stderr or "")[:300])
    else:
        o = json.loads(r.stdout)
        chk(o["parse"] == [559, 3723, 559, 559, 3723, 5, 720, None, None, None, None, None, None, None, None, None, 5, 120, 3600, None, None],
            "times are read in every way a person types them, and nonsense is refused: " + str(o["parse"]))
        chk(o["fmt"] == ["0:00", "0:05", "0:59", "9:19", "1:00:00", "1:02:03", "", "", "", "", ""], "times are shown as m:ss, or h:mm:ss from an hour: " + str(o["fmt"]))
        chk(o["parseEx"] == o["parse"], "Explore's copy of the time reader agrees with the wall's on every case")
        chk(o["fmtEx"][:6] == o["fmt"][:6] and o["fmtEx"][6] is None, "Explore's clock text agrees with the wall's")
        chk(o["from"] == [559, 559, 90, 30, None, None, None, None], "the time a link already carries is found, and a link without one gives None: " + str(o["from"]))
        w = o["with"]
        chk(w[0] == "https://youtu.be/abcdefghijk?t=559" and w[1] == "https://youtu.be/abcdefghijk?t=559", "a YouTube link opens at the time, and replaces one it had")
        chk("start=" not in w[2] and "t=90" in w[2], "an old start= is replaced by t=")
        chk(w[3] == "https://youtu.be/abcdefghijk" and w[4] == "https://example.com/x" and w[5] == "nope" and w[6] == "https://soundcloud.com/a/b",
            "no time, another site, or something that is not an address is left exactly as it was")
        em = o["embed"]
        chk(em[0]["src"].endswith("/embed/abcdefghijk?start=559") and em[0]["kind"] == "video", "a mix link's own time starts the player there")
        chk(em[1]["src"].endswith("?start=100"), "a time typed into the block beats the link's")
        chk(em[2]["src"].endswith("/embed/abcdefghijk") and em[3]["src"].endswith("/embed/abcdefghijk"), "with no time, or time 0, the player starts at the beginning")
        chk(em[4]["src"] == "https://player.vimeo.com/video/123456#t=45s" and em[5]["src"] == "https://player.vimeo.com/video/123456", "Vimeo starts at a time the same way")
        chk(em[6]["src"] == "https://open.spotify.com/embed/track/abc123" and em[6]["kind"] == "audio", "audio embeds are unchanged by a start time")
        chk(em[7] is None and em[8]["kind"] == "audio" and "?start" not in em[8]["src"], "an unknown site gives no player; SoundCloud ignores a start time")
        h = o["hostile"]
        chk("<script" not in h and "<img" not in h and "<b>" not in h and "&lt;script&gt;" in h, "a quote's words and names cannot inject markup")
        chk('" onmouseover' not in h and "&quot; onmouseover" in h, "quotes inside a quote's text cannot break out of an attribute")
        chk('<blockquote class="rc-quote__t">' in h and "▶ 9:19" in h, "a quote with a timed link shows its time as a link to it")
        chk("data-fill" not in o["empty"] and 'data-act="fill"' in o["empty"] and 'data-act="fill"' in o["blank"], "an empty quote is a button asking for one")
        chk("no source yet" in o["noSource"] and "openurl" not in o["noSource"], "a quote with no source says so, plainly")
        chk("— A, B" in o["timed"], "who and where from are shown together")
        chk('rc-quote__at">1:15' in o["typedAt"] and "openurl" not in o["typedAt"], "a typed time with no link is shown, not linked")
        chk("▶ 1:15" in o["typedBeats"], "a typed time beats the link's")
        chk("↗ source" in o["noTime"] and "▶" not in o["noTime"], "a link with no time is offered as the source")
        chk("quote" in o["types"] and o["types"].index("quote") == o["types"].index("text") + 1, "Quote is in the add menu, after Words")


# ── Explore's "put it on the wall": the real function, against a stand-in for the wall's saved layout ──
if node:
    WALL = r"""
    const fs = require('fs'), vm = require('vm');
    const html = fs.readFileSync(process.argv[1], 'utf8');
    const m = /async function putMomentOnWall\(pid, block\)\{[\s\S]*?\n\}\n/.exec(html);
    const store = { 2: { v: 1, blocks: [{ id: 'a', type: 'text', x: 40, y: 120, w: 360, h: 200, z: 1, data: {} }] } };
    let failNext = false;
    const fetch = async (url, opts) => {
      const pid = +url.split('/').pop();
      if (opts && opts.method === 'POST') { if (failNext) return { ok: false }; store[pid] = JSON.parse(opts.body); return { ok: true }; }
      return { json: async () => JSON.parse(JSON.stringify(store[pid] || { v: 1, blocks: [] })) };
    };
    const ctx = { fetch, Math, JSON, Array, out: {} };
    vm.runInNewContext(m[0] + '; out.put = putMomentOnWall;', ctx);
    (async () => {
      const mom = { url: 'https://youtu.be/abcdefghijk?t=559', title: 'T', embed: true, start: 559, caption: 'n' };
      const first = await ctx.out.put(2, mom);
      const again = await ctx.out.put(2, mom);
      const other = await ctx.out.put(2, Object.assign({}, mom, { url: 'https://youtu.be/abcdefghijk?t=600', start: 600 }));
      const empty = await ctx.out.put(9, mom);
      failNext = true; let threw = false; try { await ctx.out.put(2, Object.assign({}, mom, { url: 'https://youtu.be/zzzzzzzzzzz' })); } catch (_) { threw = true; }
      console.log(JSON.stringify({ first, again, other, empty, threw, blocks: store[2].blocks, fresh: store[9].blocks }));
    })();
    """
    r2 = subprocess.run([node, "-e", WALL, EXPLORE], capture_output=True, text=True, timeout=60)
    if r2.returncode != 0 or not r2.stdout.strip():
        chk(False, "the wall helper runs under node: " + (r2.stderr or r2.stdout)[:300])
    else:
        w = json.loads(r2.stdout)
        chk(w["first"] is True and w["again"] is False and w["other"] is True, "a moment is added once; saving it again adds nothing; another moment is added")
        mixes = [b for b in w["blocks"] if b["type"] == "mix"]
        chk(len(w["blocks"]) == 3 and len(mixes) == 2, "so the wall has the old block and exactly two mixes")
        chk(mixes[0]["y"] >= 120 + 200 + 24 and mixes[1]["y"] >= mixes[0]["y"] + mixes[0]["h"] + 24, "each lands below what is already on the wall, not on top of it")
        chk(mixes[0]["data"]["url"].endswith("?t=559") and mixes[0]["data"]["start"] == 559 and mixes[0]["data"]["embed"] is True and mixes[0]["data"]["caption"] == "n", "the block carries the link, the time and the person's note")
        chk(mixes[0]["z"] > 1 and len({b["id"] for b in w["blocks"]}) == 3, "it is in front, with its own id")
        chk(len(w["fresh"]) == 1 and w["fresh"][0]["y"] == 120, "an empty wall gets its first block near the top")
        chk(w["threw"] is True, "if the wall cannot be saved, the failure is reported, not hidden")

# ── the wall's layout keeps a quote and a timed mix ──
import app
import time as _t
app._cat_health_last_check[0] = _t.time() + 1e9           # keep upstream's weekly link check off the network
c = app.app.test_client()
blocks = [
    {"id": "k1", "type": "quote", "x": 40, "y": 120, "w": 380, "h": 240, "z": 1,
     "data": {"text": "Nothing here is original.", "who": "A speaker", "source": "A talk", "url": "https://youtu.be/abcdefghijk?t=559", "at": 559, "caption": "my own note"}},
    {"id": "k2", "type": "mix", "x": 440, "y": 120, "w": 440, "h": 280, "z": 2,
     "data": {"url": "https://youtu.be/abcdefghijk", "title": "The talk", "embed": True, "start": 559}},
]
r = c.post("/api/room/canvas/1", json={"v": 1, "blocks": blocks})
chk(r.status_code == 200 and r.get_json()["n"] == 2, "a wall with a quote and a timed mix is saved")
back = c.get("/api/room/canvas/1").get_json()["blocks"]
chk(back == blocks, "and comes back exactly as it was saved")

# ── the static pieces are in place ──
css = open(os.path.join(PROJ, "static", "css", "room_canvas.css"), encoding="utf-8").read()
chk(".rc-quote__t" in css and ".rc-quote__src" in css and ".rc-sheet textarea" in css, "the stylesheet styles quotes and the sheet's text box")
quote_css = css[css.index("/* a quote"):css.index("/* image */")]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", quote_css), "the quote styles use the theme's colours, never a fixed one")
html = open(EXPLORE, encoding="utf-8").read()
chk('id="ex-vsave"' in html and "saveVideoMoment" in html and "enablejsapi=1" in html, "Explore's player has its save-a-moment row")
chk("timestamp_sec: at" in html, "Explore saves the time with the moment")
chk(".ex-video__save [hidden]" in html, "the 'now' button really stays hidden until the player reports where it is (the button style would otherwise show it)")

# ── a web kept from Connections (the Web block) ──
if node:
    WEB_JS = r"""
    const fs = require('fs'), vm = require('vm');
    const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, URL, console });
    const W = win.MRRoomCanvas, out = {};
    const nd = (type, name, x, y, extra) => Object.assign({ type, name, x, y }, extra || {});
    const web = { nodes: [nd('album', 'Home <b>Album</b>', 0, 0, { image: 'https://cdn-images.dzcdn.net/a.jpg', year: '2025' }),
                          nd('artist', 'Photay', 200, -90, { image: 'https://i.scdn.co/b.jpg' }),
                          nd('artist', 'No Picture', -180, 100),
                          nd('label', 'TVPCO', -150, -120), nd('genre', 'Electro', 120, 150)], edges: [[0, 1], [0, 2], [0, 3], [0, 4]], home: 0 };
    out.good = W.webHtml({ web, title: 'KOSMOBOTANIKA', url: '/connections?kind=album&id=10' });
    out.hostile = W.webHtml({ web: { nodes: [nd('album', '<script>alert(1)</script>"><img src=x onerror=alert(2)>', 0, 0, { image: 'https://cdn.dzcdn.net/x.jpg" onload="alert(3)' }),
                                             nd('artist', "O'Brien & Sons", 100, 0, { image: 'javascript:alert(4)' })], edges: [[0, 1]], home: 0 }, title: '<i>t</i>' });
    out.empty = [W.webHtml({}), W.webHtml(null), W.webHtml({ web: { nodes: [] } }), W.webHtml({ web: 'x' })];
    out.junk = W.webHtml({ web: { nodes: [nd('album', 'A', NaN, Infinity), { type: 'artist' }, null, 5], edges: [[0, 99], [1], 'x', null, [0, 0]], home: 7 } });
    out.menu = Object.keys(W.TYPES).filter(t => !W.TYPES[t].keepOnly);
    out.webType = W.TYPES.web;
    const src = fs.readFileSync(process.argv[1], 'utf8');
    out.src = { filtersMenu: src.includes('.filter(t => !TYPES[t].keepOnly)'), noFill: src.includes("b.type !== 'text' && b.type !== 'web'"), openGuard: src.includes("/^\\/connections\\?/.test(u)") };
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", WEB_JS, CANVAS], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        chk(False, "the Web block runs under node: " + (r.stderr or "")[:300])
    else:
        o = json.loads(r.stdout)
        g = o["good"]
        chk('<svg viewBox="' in g and 'preserveAspectRatio="xMidYMid meet"' in g, "a kept web is one SVG that scales with its block")
        chk(g.count("<image ") == 2 and 'href="https://cdn-images.dzcdn.net/a.jpg"' in g, "records and artists with a picture show it; one with none does not get an <image>")
        chk(g.count("<line ") == 4 and g.count('class="soft"') == 2, "every join is drawn; label and genre joins are dashed")
        chk('class="fr home alb"' in g and "TVPCO" in g and "Electro" in g and 'class="tag dash"' in g, "the starting record is marked, labels are boxes, genres are dashed boxes")
        chk('class="ph"' in g, "an artist with no picture shows an initial")
        chk('data-act="openweb"' in g and "explore ↗" in g and "KOSMOBOTANIKA" in g, "the block has a title and a way back into the live web")
        h = o["hostile"]
        chk("<script" not in h and "<img" not in h and "<i>" not in h and "&lt;script&gt;" in h, "names and titles cannot inject markup")
        chk('onload="alert' not in h and "&quot; onload" in h, "a quote mark in a picture address cannot break out of the attribute")
        chk("javascript:" not in h and h.count("<image ") == 1, "only https pictures are drawn (a javascript: address is not)")
        chk("O&#39;Brien" in h and "&amp;" in h, "apostrophes and ampersands are escaped")
        chk(all("empty web" in x for x in o["empty"]), "no web, an empty one, or junk gives a plain message")
        chk("NaN" not in o["junk"] and "Infinity" not in o["junk"] and "undefined" not in o["junk"], "junk positions never reach the drawing (no NaN, Infinity or undefined)")
        chk(o["webType"]["keepOnly"] is True and "web" not in o["menu"] and "quote" in o["menu"], "Web is not in the + menu (it is only kept from Connections); the other blocks still are")
        chk(o["src"]["filtersMenu"] and o["src"]["noFill"] and o["src"]["openGuard"], "the wall hides 'fill' for a web, and only opens addresses inside Connections")
web_css = css[css.index("/* a web kept from Connections"):]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", web_css), "the Web block's CSS uses theme tokens only, no hex")
chk("border-radius:0" in web_css.replace(" ", "") and "box-shadow" not in web_css, "square, no shadows (the house look)")

# ── the camera: pan, zoom, depth ──
if node:
    CAM_JS = r"""
    const fs = require('fs'), vm = require('vm');
    const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, URL, console });
    const C = win.MRRoomCanvas.camera, out = {};
    const cam = (o) => Object.assign({ x: 120, y: 80, k: 1, ox: 720, oy: 480, depth: true }, o || {});
    // screen <-> world are exact inverses, on any layer, at any zoom, whatever the vanishing point
    let worst = 0;
    for (const k of [0.15, 0.5, 1, 1.7, 2.5]) for (const z of [-360, -120, 0, 120, 360]) for (const [wx, wy] of [[0, 0], [300, -200], [-1500, 900], [4000, 4000]]) {
      const c = cam({ k, x: -300 + k * 40, y: 55, ox: 400 + k * 100, oy: 300 });
      const s = C.toScreen(c, wx, wy, z), w = C.toWorld(c, s.x, s.y, z);
      worst = Math.max(worst, Math.abs(w.x - wx), Math.abs(w.y - wy));
    }
    out.roundTrip = worst;
    out.flatPlaneSame = (() => { const c = cam(), a = C.toScreen(c, 100, 50, 0), f = C.toScreen(cam({ depth: false }), 100, 50, 0); return [a.x === f.x, a.y === f.y]; })();
    out.depthOffIgnoresZ = (() => { const c = cam({ depth: false }), a = C.toScreen(c, 100, 50, 300), b = C.toScreen(c, 100, 50, -300); return a.x === b.x && a.y === b.y; })();
    // depth: nearer is bigger, farther is smaller, and a thing at the vanishing point does not move
    out.scales = [-360, -120, 0, 120, 360].map(z => +C.scaleAt(z, true).toFixed(4));
    out.scaleOff = C.scaleAt(300, false);
    out.atVanishing = (() => { const c = cam({ x: 0, y: 0, k: 1, ox: 100, oy: 100 }), p = C.toScreen(c, 100, 100, 300); return [p.x, p.y]; })();
    // parallax: shift the vanishing point and a near layer slides further than the page, a far layer the other way
    out.parallax = (() => { const w = [200, 200], at = (z, ox) => C.toScreen(cam({ x: 0, y: 0, k: 1, ox, oy: 480 }), w[0], w[1], z).x;
      return [at(-240, 760) - at(-240, 720), at(0, 760) - at(0, 720), at(240, 760) - at(240, 720)].map(v => +v.toFixed(3)); })();
    // zoom keeps the point under the cursor still
    out.zoomKeeps = (() => { let c = cam({ depth: false }), w0 = C.toWorld(c, 500, 400, 0); let m = 0;
      for (const f of [1.25, 1.25, 0.8, 3, 0.2, 1.1]) { c = C.zoomAt(c, f, 500, 400); const w = C.toWorld(c, 500, 400, 0); m = Math.max(m, Math.abs(w.x - w0.x), Math.abs(w.y - w0.y)); } return m; })();
    out.zoomBounds = [C.zoomAt(cam(), 1000, 0, 0).k, C.zoomAt(cam(), 0.0001, 0, 0).k, C.zoomAt(cam({ k: 1 }), 1.25, 0, 0).k];
    // depth of a block
    out.depthOf = [C.depthOf({ d: 2 }), C.depthOf({ d: 9 }), C.depthOf({ d: -9 }), C.depthOf({ d: 1.6 }), C.depthOf({}), C.depthOf({ d: 'x' }), C.depthOf({ d: NaN }), C.depthOf(null), C.depthOf({ d: Infinity })];
    out.zOf = [C.zOf({ d: 0 }, 0), C.zOf({ d: 1 }, 0), C.zOf({ d: -3 }, 0), C.zOf({ d: 0 }, 5), C.zOf({ d: 3 }, 10)];
    // fit: every block's corners, as drawn, land inside the area; with depth too
    const fits = (blocks, depth, W, H) => {
      const c = C.fitCam(blocks, W, H, cam({ depth, ox: W / 2, oy: H / 2 }));
      const rank = {}; blocks.slice().sort((a, b) => a.z - b.z).forEach((b, i) => { rank[b.id] = i; });
      let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      blocks.forEach(b => [[b.x, b.y], [b.x + b.w, b.y + b.h]].forEach(([wx, wy]) => { const p = C.toScreen(c, wx, wy, C.zOf(b, rank[b.id])); x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); }));
      return { x0, y0, x1, y1, k: c.k, ok: x0 >= 0 && x1 <= W && y0 >= 90 && y1 <= H };
    };
    const row = [{ id: 'a', x: 40, y: 160, w: 360, h: 200, z: 1, d: 0 }, { id: 'b', x: 480, y: 160, w: 360, h: 200, z: 2, d: -3 }, { id: 'c', x: 920, y: 160, w: 360, h: 200, z: 3, d: 3 }];
    out.fitFlat = fits(row, false, 1440, 1000); out.fitDepth = fits(row, true, 1440, 1000);
    out.fitNarrow = fits(row, true, 520, 800); out.fitTall = fits([{ id: 'a', x: 0, y: 0, w: 300, h: 4000, z: 1, d: 2 }], true, 1440, 1000);
    out.fitOne = fits([{ id: 'a', x: 5000, y: -3000, w: 200, h: 100, z: 1, d: 0 }], true, 1440, 1000);
    out.fitEmpty = (() => { const c = C.fitCam([], 1440, 1000, cam()); return [c.k, isFinite(c.x), isFinite(c.y)]; })();
    out.fitJunk = (() => { const c = C.fitCam([null, 5, { x: NaN, y: 0, w: 1, h: 1 }, { id: 'ok', x: 10, y: 10, w: 100, h: 100, z: 1 }], 1440, 1000, cam()); return [isFinite(c.k), isFinite(c.x), isFinite(c.y)]; })();
    out.fitKeepsOrigin = (() => { const c = C.fitCam(row, 1440, 1000, cam({ ox: 700, oy: 400 })); return [c.ox, c.oy]; })();
    out.wheel = [C.wheelPx(10, 0), C.wheelPx(3, 1), C.wheelPx(1, 2, 900), C.wheelPx(1, 2)];
    const src = fs.readFileSync(process.argv[1], 'utf8');
    out.src = { ctrl: src.includes('if (e.ctrlKey) {'), scrollKeeps: src.includes('function scrollsUnder(e)'), gesture: src.includes("view.addEventListener('gesturechange'"), reduced: src.includes('prefers-reduced-motion'),
                far: src.includes("a === 'far' || a === 'near'"), hud: src.includes('data-hud="fit"'), noScrollLeft: !src.includes('host.scrollLeft') && !src.includes('host.scrollTop') };
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", CAM_JS, CANVAS], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        chk(False, "the camera runs under node: " + (r.stderr or "")[:400])
    else:
        o = json.loads(r.stdout)
        chk(o["roundTrip"] < 1e-6, f"screen and world are exact inverses on every layer, zoom and vanishing point (worst error {o['roundTrip']})")
        chk(o["flatPlaneSame"] == [True, True], "on the page layer (z = 0) depth changes nothing, so zoom-around-the-cursor stays exact")
        chk(o["depthOffIgnoresZ"], "with depth off, a layer makes no difference at all")
        chk(o["scales"] == sorted(o["scales"]) and o["scales"][0] < 1 < o["scales"][-1] and o["scales"][2] == 1, f"nearer is bigger, farther is smaller, the page layer is 1:1 ({o['scales']})")
        chk(o["scaleOff"] == 1 and o["atVanishing"] == [100, 100], "no depth, no scaling; a thing at the vanishing point does not move whatever its layer")
        p = o["parallax"]
        chk(p[0] > 0 > p[2] and abs(p[1]) < 1e-9 and abs(p[2]) > abs(p[0]), f"moving the vanishing point right: far layers drift with it, near layers go against it (and further), the page layer not at all ({p})")
        chk(o["zoomKeeps"] < 1e-6, "zooming keeps the spot under the cursor still, however many times you do it")
        chk(o["zoomBounds"][0] == 2.5 and o["zoomBounds"][1] == 0.15 and o["zoomBounds"][2] == 1.25, f"zoom is held between 0.15 and 2.5 ({o['zoomBounds']})")
        chk(o["depthOf"] == [2, 3, -3, 2, 0, 0, 0, 0, 0], f"a block's layer is a whole number from -3 to 3, and anything odd is the page layer ({o['depthOf']})")
        chk(o["zOf"][0] == 0 and o["zOf"][1] == 120 and o["zOf"][2] == -360 and 0 < o["zOf"][3] < 5 and abs(o["zOf"][4] - 364) < 1e-9, f"each layer is 120px apart; blocks on one layer keep their stacking by a hair ({o['zOf']})")
        for name in ("fitFlat", "fitDepth", "fitNarrow", "fitTall", "fitOne"):
            chk(o[name]["ok"], f"Fit shows every block as it is drawn, {name[3:].lower()}: x {o[name]['x0']:.0f}-{o[name]['x1']:.0f}, y {o[name]['y0']:.0f}-{o[name]['y1']:.0f}")
        chk(o["fitDepth"]["k"] < o["fitFlat"]["k"], "with depth on, Fit zooms out a little more, because near blocks are drawn bigger")
        chk(o["fitEmpty"][0] == 1 and o["fitEmpty"][1] and o["fitEmpty"][2], "an empty wall gets a plain, finite view")
        chk(o["fitJunk"] == [True, True, True], "Fit ignores junk blocks instead of breaking")
        chk(o["fitKeepsOrigin"] == [700, 400], "Fit leaves the vanishing point where it was")
        chk(o["wheel"] == [10, 48, 900, 800], f"a wheel turn is read in pixels whatever unit the browser uses ({o['wheel']})")
        sr = o["src"]
        chk(sr["ctrl"] and sr["scrollKeeps"] and sr["gesture"], "pinch zooms (trackpad, Safari/app, touch); a plain scroll pans, except where a block scrolls itself")
        chk(sr["reduced"] and sr["far"] and sr["hud"], "the look-around respects reduced motion; blocks can be pushed far or pulled near; there are Fit / + / − / 3D buttons")
        chk(sr["noScrollLeft"], "nothing still depends on the old scroll bars")
        chk("addMenu.onclick" not in open(CANVAS, encoding="utf-8").read() and open(CANVAS, encoding="utf-8").read().count("addMenu.addEventListener('click'") == 1,
            "the + menu has ONE click handler (two used to run, and the right-click one moved the previous block to the click before the new one existed)")
        chk("snap(at.x - T.w / 2)" in open(CANVAS, encoding="utf-8").read(), "a right-click puts the new block's middle on the click")

# ── lines between blocks (client side; wall_links.py checks the same rules on save) ──
if node:
    LINK_JS = r"""
    const fs = require('fs'), vm = require('vm');
    const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, URL, console });
    const L = win.MRRoomCanvas.LinkOps, out = {};
    let r = L.add([], 'a', 'b'); out.addFirst = [r.added, r.links.length, r.link.kind, r.link.label];
    out.addSelf = L.add([], 'a', 'a').added; out.addEmpty = [L.add([], '', 'b').added, L.add([], 'a', null).added];
    r = L.add([{ a: 'a', b: 'b', kind: 'mine', label: '' }], 'b', 'a'); out.reversedSame = [r.added, r.links.length];
    r = L.add([{ a: 'a', b: 'b', kind: 'near', label: '' }], 'b', 'a'); out.nearBecomesMine = [r.added, r.links.length, r.links[0].kind];
    r = L.add([{ a: 'a', b: 'b', kind: 'near', label: '' }], 'a', 'b', 'near'); out.nearStaysNear = [r.added, r.links[0].kind];
    r = L.add([], 'a', 'b', 'near', 'x'.repeat(200)); out.nearKind = [r.link.kind, r.link.label.length];
    out.badKind = L.add([], 'a', 'b', 'evil').link.kind;
    const ls = [{ a: 'a', b: 'b' }, { a: 'b', b: 'c' }, { a: 'c', b: 'd' }];
    out.dropFor = L.dropFor(ls, 'b').map(l => l.a + l.b); out.dropNone = L.dropFor(ls, 'zzz').length;
    out.remove = L.remove(ls, 'c', 'b').map(l => l.a + l.b); out.has = [L.has(ls, 'b', 'a'), L.has(ls, 'a', 'c')];
    out.key = [L.key('a', 'b') === L.key('b', 'a'), L.key('a', 'b')];
    out.pure = (() => { const before = JSON.stringify(ls); L.add(ls, 'x', 'y'); L.remove(ls, 'a', 'b'); L.dropFor(ls, 'a'); return JSON.stringify(ls) === before; })();
    const src = fs.readFileSync(process.argv[1], 'utf8');
    out.src = { dot: src.includes('class="rc-dot"'), linkBtn: src.includes('data-act="link"'), esc: src.includes("cancelLinking(); closeSheet(); select(null); selectLink(null);"),
                del: src.includes("selLink && !e.target.matches('input, textarea, [contenteditable]')) { e.preventDefault(); removeLink(selLink)"), noPanOnLine: src.includes("e.target.closest('.rc-links g')"),
                saves: src.includes('JSON.stringify({ v: 1, blocks, links })'), dropsOnRemove: src.includes('links = LinkOps.dropFor(links, id)'), screenSpace: src.includes('const centreScreen = b => toScreen(cam') };
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", LINK_JS, CANVAS], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        chk(False, "the line operations run under node: " + (r.stderr or "")[:400])
    else:
        o = json.loads(r.stdout)
        chk(o["addFirst"] == [True, 1, "mine", ""], "joining two blocks makes one line, and it is the person's own")
        chk(o["addSelf"] is False and o["addEmpty"] == [False, False], "a block is not joined to itself or to nothing")
        chk(o["reversedSame"] == [False, 1] and o["key"] == [True, "a|b"], "a line has no direction: joining b to a when a is joined to b adds nothing")
        chk(o["nearBecomesMine"] == [True, 1, "mine"] and o["nearStaysNear"] == [False, "near"], "a suggestion you join yourself becomes your own line; a repeated suggestion stays a suggestion")
        chk(o["nearKind"] == ["near", 80] and o["badKind"] == "mine", "kinds are checked and words are cut to 80")
        chk(o["dropFor"] == ["cd"] and o["dropNone"] == 3, f"removing a block removes only the lines that ended on it ({o['dropFor']})")
        chk(o["remove"] == ["ab", "cd"] and o["has"] == [True, False], "a line can be removed from either end")
        chk(o["pure"], "none of it changes the lists it is given")
        sr = o["src"]
        chk(sr["dot"] and sr["linkBtn"], "every block has the drag-dot and a Link button")
        chk(sr["esc"] and sr["del"] and sr["noPanOnLine"], "Esc cancels, Delete removes a selected line, and a click on a line is not swallowed by panning")
        chk(sr["saves"] and sr["dropsOnRemove"] and sr["screenSpace"], "lines are saved with the blocks, go with a deleted block, and are drawn in screen space so they meet blocks on every layer")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
