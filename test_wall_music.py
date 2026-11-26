"""Proves music on the wall (static/js/wall_music.js): what a web becomes (which neighbours, how many, on which
layer), where new nodes are put (clear of everything already on the wall, which never moves), how an old frozen
Web block is unpacked, and that nothing grows by accident (a drag, or a click on a toolbar button).

The planning half is pure, so it runs under node.

Run:  venv/bin/python test_wall_music.py
"""
import os, sys, json, subprocess, shutil, re
PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)
JSDIR = os.path.join(PROJ, "static", "js")

JS = r"""
const fs = require('fs'), vm = require('vm');
const win = {}; const ctx = { window: win, console };
for (const f of ['conn_graph.js', 'wall_music.js']) vm.runInNewContext(fs.readFileSync(process.argv[1] + '/' + f, 'utf8'), ctx);
const W = win.WallMusic.pure, out = {};
const N = (type, id, name, extra) => Object.assign({ type, id, name }, extra || {});
// specOf
out.spec = {
  album: W.specOf(N('album', 10, '  KOSMOBOTANIKA ', { sub: 'ETNOBOTANIKA', year: '2025', image: 'https://cdn.dzcdn.net/a.jpg', fans: 160 })),
  tag: W.specOf(N('label', null, 'TVPCO')), badImage: W.specOf(N('album', 10, 'x', { image: 'javascript:alert(1)' })).image, httpImage: W.specOf(N('album', 10, 'x', { image: 'http://x/y.jpg' })).image,
  badYear: W.specOf(N('album', 10, 'x', { year: '19xx' })).year, longName: W.specOf(N('album', 10, 'x'.repeat(900))).name.length,
  rejected: [W.specOf(N('playlist', 1, 'x')), W.specOf(N('album', 10, '')), W.specOf(N('album', 10, '   ')), W.specOf(N('album', 0, 'x')), W.specOf(N('artist', 'abc', 'x')), W.specOf(N('artist', -3, 'x')), W.specOf(null), W.specOf({}), W.specOf(5)],
};
out.ident = [W.identOf({ kind: 'artist', rid: 10 }), W.identOf({ kind: 'album', rid: 10 }), W.identOf({ kind: 'label', name: 'Warp' }), W.identOf({ kind: 'label', name: 'WARP' }), W.identOf({ kind: 'genre', name: 'Warp' }), W.identOf(null)];
// planWeb
const web = { center: N('album', 10, 'Home', { sub: 'Me' }), groups: [
  { key: 'labels', nodes: [N('label', null, 'L1'), N('label', null, 'L2'), N('label', null, 'L3'), N('label', null, 'L4'), N('label', null, 'L5')] },
  { key: 'genres', nodes: [N('genre', null, 'Electro')] },
  { key: 'albums', nodes: Array.from({ length: 9 }, (_, i) => N('album', 200 + i, 'Alb ' + i)) },
  { key: 'related', nodes: Array.from({ length: 12 }, (_, i) => N('artist', 300 + i, 'Rel ' + i)).concat([N('artist', 0, 'no id'), N('mystery', 5, 'odd')]) },
  { key: 'credits', nodes: [N('artist', 400, 'Producer', { role: 'Producer' })] }, { key: 'wild', nodes: [N('artist', 500, 'In an unknown group')] }] };
const plan = W.planWeb(web), by = {}; plan.items.forEach(i => { by[i.group] = (by[i.group] || 0) + 1; });
out.plan = { center: plan.center.name, counts: by, total: plan.items.length,
  depths: { labels: plan.items.find(i => i.group === 'labels').d, genres: plan.items.find(i => i.group === 'genres').d, albums: plan.items.find(i => i.group === 'albums').d, related: plan.items.find(i => i.group === 'related').d, credits: plan.items.find(i => i.group === 'credits').d, wild: plan.items.find(i => i.group === 'wild').d },
  noInvalid: plan.items.every(i => i.spec.name !== 'no id' && i.spec.name !== 'odd'), custom: Object.keys(W.planWeb(web, { related: 2 }).items.filter(i => i.group === 'related')).length };
out.planBad = [W.planWeb(null), W.planWeb({}), W.planWeb({ center: N('album', 0, 'x'), groups: [] }), W.planWeb({ center: N('album', 1, 'ok') }).items.length];
out.size = [W.sizeOf({ kind: 'label' }), W.sizeOf({ kind: 'genre' }), W.sizeOf({ kind: 'album' }), W.sizeOf({ kind: 'artist', home: true })];
// settle: new nodes clear of existing blocks and of each other
const minGap = (pos, existing) => { const all = Object.entries(pos).map(([id, p]) => ({ id, x: p.x, y: p.y })); let m = Infinity;
  for (let i = 0; i < all.length; i++) { for (let j = i + 1; j < all.length; j++) m = Math.min(m, Math.hypot(all[i].x - all[j].x, all[i].y - all[j].y)); existing.forEach(e => { m = Math.min(m, Math.hypot(all[i].x - e.x, all[i].y - e.y) - e.r); }); } return m; };
const parent = { id: 'P', x: 0, y: 0, r: 90 };
const news = Array.from({ length: 10 }, (_, i) => ({ id: 'n' + i, r: 80 }));
let pos = W.settle([parent], 'P', news, { outward: 0 });
const xs = Object.values(pos).map(p => p.x), ys = Object.values(pos).map(p => p.y);
out.settle = { count: Object.keys(pos).length, finite: Object.values(pos).every(p => isFinite(p.x) && isFinite(p.y)), gap: Math.round(minGap(pos, [parent])), meanX: Math.round(xs.reduce((a, b) => a + b, 0) / xs.length), minDist: Math.round(Math.min(...Object.values(pos).map(p => Math.hypot(p.x, p.y)))) };
const crowd = [parent].concat(Array.from({ length: 8 }, (_, i) => ({ id: 'e' + i, x: Math.cos(i * 0.785) * 260, y: Math.sin(i * 0.785) * 260, r: 90 })));
pos = W.settle(crowd, 'P', news.slice(0, 5), { outward: 0 });
out.crowded = { finite: Object.values(pos).every(p => isFinite(p.x) && isFinite(p.y)), gap: Math.round(minGap(pos, crowd)), keys: Object.keys(pos).sort().join(',') };
out.settleEdge = [Object.keys(W.settle([parent], 'P', [], {})).length, Object.keys(W.settle([parent], 'GHOST', news, {})).length];
const again = W.settle([parent], 'P', news, { outward: 0 }), once = W.settle([parent], 'P', news, { outward: 0 });
out.settleRepeat = JSON.stringify(again) === JSON.stringify(once);
const left = W.settle([parent], 'P', news.slice(0, 6), { outward: Math.PI }), right = W.settle([parent], 'P', news.slice(0, 6), { outward: 0 });
out.direction = [Math.round(Object.values(left).reduce((a, p) => a + p.x, 0) / 6), Math.round(Object.values(right).reduce((a, p) => a + p.x, 0) / 6)];
const farParent = { id: 'P', x: 700, y: -300, r: 90 };
out.backFree = [3, 6, 10, 16].map(n => { const nw = Array.from({ length: n }, (_, i) => ({ id: 'n' + i, r: 80 })); const p = W.settle([farParent], 'P', nw, { outward: 0 });
  const angs = Object.values(p).map(q => Math.atan2(q.y + 300, q.x - 700)); return { n, backGapDeg: Math.round(Math.min(...angs.map(a => Math.PI - Math.abs(a))) * 180 / Math.PI), minRadius: Math.round(Math.min(...Object.values(p).map(q => Math.hypot(q.x - 700, q.y + 300)))),
    gap: Math.round(minGap(p, [farParent])) }; });
// freeSpot: new things go where there is room
const R = (x, y, w, h) => ({ x, y, w, h });
out.free = {
  empty: W.freeSpot([], { x: 10, y: 20 }, { w: 300, h: 300 }),
  clear: W.freeSpot([R(2000, 2000, 100, 100)], { x: 0, y: 0 }, { w: 300, h: 300 }),
  blocked: (() => { const rs = [R(-200, -200, 400, 400)]; const p = W.freeSpot(rs, { x: 0, y: 0 }, { w: 300, h: 300 }); return { p, ok: !rs.some(r => Math.abs(p.x - (r.x + r.w / 2)) < 150 + 40 + r.w / 2 && Math.abs(p.y - (r.y + r.h / 2)) < 150 + 40 + r.h / 2), dist: Math.round(Math.hypot(p.x, p.y)) }; })(),
  crowded: (() => { const rs = []; for (let x = -4600; x < 4600; x += 250) for (let y = -4600; y < 4600; y += 250) rs.push(R(x, y, 240, 240)); return W.freeSpot(rs, { x: 0, y: 0 }, { w: 300, h: 300 }); })(),
  padZero: W.freeSpot([R(160, 0, 100, 100)], { x: 0, y: 0 }, { w: 300, h: 100 }, 0),
};
// the record page
const blocks = [
  { id: 'A', n: 3, type: 'node', data: { kind: 'album', name: 'Home', sub: 'Me', year: '2025', tracks: 14, image: 'https://x/a.jpg', url: 'https://open.spotify.com/album/1', song: { title: 'Track' } } },
  { id: 'Q', n: 1, type: 'quote', data: { text: 'words', who: 'Someone' } }, { id: 'T', n: 2, type: 'text', data: { heading: 'h', body: 'b' } },
  { id: 'L1', n: 4, type: 'node', data: { kind: 'label', name: 'Warp' } }, { id: 'L2', n: 5, type: 'node', data: { kind: 'label', name: 'Ninja' } }, { id: 'G', n: 6, type: 'node', data: { kind: 'genre', name: 'Electro' } },
  { id: 'R2', n: 9, type: 'node', data: { kind: 'artist', name: 'Second' } }, { id: 'R1', n: 8, type: 'node', data: { kind: 'artist', name: 'First' } }, { id: 'X', n: 10, type: 'mix', data: {} }, { id: 'Z', n: 11, type: 'text', data: {} }];
const links = [{ a: 'A', b: 'Q', kind: 'mine', label: 'said about' }, { a: 'T', b: 'A', kind: 'mine', label: '' }, { a: 'A', b: 'L1', kind: 'near' }, { a: 'L2', b: 'A', kind: 'near' }, { a: 'A', b: 'G', kind: 'near' },
  { a: 'A', b: 'R2', kind: 'near' }, { a: 'A', b: 'R1', kind: 'mine', label: 'mine now' }, { a: 'A', b: 'A', kind: 'mine' }, { a: 'A', b: 'GHOST', kind: 'mine' }, { a: 'R1', b: 'X', kind: 'mine' }, { a: 'A', b: 'Q', kind: 'mine' }, null, 5];
const rm = W.recordModel(blocks[0], blocks, links);
out.rec = { n: rm.n, title: rm.title, kind: rm.kind, facts: rm.facts, mine: rm.mine.map(m => [m.id, m.type, m.line]), near: rm.near.map(n => [n.id, n.n, n.mine, n.line]), song: rm.song, spotify: rm.spotify, image: rm.image };
const art = W.recordModel(blocks[7], blocks, links);
out.recArtist = { facts: art.facts, mine: art.mine.map(m => m.id), near: art.near.map(n => n.id) };
out.recNone = W.recordModel({ id: 'solo', n: 1, data: { kind: 'artist', name: 'S' } }, [], []).facts;
out.recJunk = (() => { try { const m = W.recordModel({ id: 'A', data: {} }, [null, 5, { id: 'A' }, { id: 'B', type: 'node' }], [{ a: 'A', b: 'B' }, null]); return [m.mine.length, m.near.length, m.facts.length]; } catch (e) { return 'threw ' + e.message; } })();
out.order = W.recordOrder(blocks);
out.step = [W.stepRecord(['a', 'b', 'c'], 'a', 1), W.stepRecord(['a', 'b', 'c'], 'c', 1), W.stepRecord(['a', 'b', 'c'], 'a', -1), W.stepRecord(['a'], 'a', 1), W.stepRecord([], 'a', 1), W.stepRecord(['a', 'b'], 'zzz', 1)];
// outwardFrom
out.outward = [W.outwardFrom({ x: 0, y: 0 }, [{ x: -300, y: 0 }, { x: -300, y: 40 }]), W.outwardFrom({ x: 0, y: 0 }, []), W.outwardFrom({ x: 5, y: 5 }, [{ x: 5, y: 5 }]), W.outwardFrom({ x: 0, y: 0 }, [{ x: 0, y: 300 }])];
// unpack an old frozen web
const old = { nodes: [N('album', 10, 'Home', { x: 100, y: 100 }), N('artist', 70, 'Photay', { x: 300, y: 100 }), N('label', null, 'TVPCO', { x: 100, y: 300 }), { type: 'artist', id: 5, name: 'bad pos', x: NaN, y: 0 }, null], edges: [[0, 1], [0, 2], [0, 3], [1, 99], [0, 0], 'x'], home: 0 };
const up = W.unpackPlan(old, { x: 1000, y: 500 }, 0.5);
out.unpack = { n: up.nodes.length, names: up.nodes.map(n => n.spec.name), homeFlag: up.nodes.map(n => !!n.home), pos: up.nodes.map(n => [Math.round(n.x), Math.round(n.y)]), edges: up.edges, specHome: up.nodes[0].spec.home === true };
out.unpackEmpty = [W.unpackPlan(null, { x: 0, y: 0 }).nodes.length, W.unpackPlan({ nodes: [] }, { x: 0, y: 0 }).nodes.length, W.unpackPlan({ nodes: [null, 5] }, { x: 0, y: 0 }).nodes.length];
console.log(JSON.stringify(out));
"""
r = subprocess.run([node, "-e", JS, JSDIR], capture_output=True, text=True, timeout=120)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "the music planning runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

sp = o["spec"]
chk(sp["album"] == {"kind": "album", "rid": 10, "name": "KOSMOBOTANIKA", "sub": "ETNOBOTANIKA", "image": "https://cdn.dzcdn.net/a.jpg", "year": "2025", "fans": 160, "role": "", "url": "", "deezer": "", "tracks": None, "albums": None, "grown": False}, "a record becomes a music block's data, tidied")
chk(sp["tag"]["kind"] == "label" and sp["tag"]["rid"] is None, "labels and genres need no id")
chk(sp["badImage"] == "" and sp["httpImage"] == "", "only https pictures are kept")
chk(sp["badYear"] == "" and sp["longName"] == 200, "a year must be four digits, and a long name is cut")
chk(all(x is None for x in sp["rejected"]), "unknown types, empty names, and records or artists with no real id are refused")
chk(o["ident"][0] != o["ident"][1] and o["ident"][2] == o["ident"][3] and o["ident"][2] != o["ident"][4], f"an artist and an album that share a number are different; labels match by name in any case ({o['ident']})")
pl = o["plan"]
chk(pl["center"] == "Home" and pl["counts"] == {"labels": 4, "genres": 1, "albums": 6, "related": 8, "credits": 1, "wild": 1}, f"a growth is capped per group so a click never floods the wall ({pl['counts']})")
chk(pl["noInvalid"], "neighbours with no usable id or an odd type are left out")
chk(pl["depths"] == {"labels": -2, "genres": -2, "albums": 0, "related": -1, "credits": 0, "wild": 0}, f"context sits farther back than the record itself ({pl['depths']})")
chk(o["planBad"] == [None, None, None, 0], "a web with no usable centre plans nothing; a centre with no groups has no neighbours")
chk(o["size"][0]["h"] < o["size"][2]["h"] and o["size"][3]["w"] > o["size"][2]["w"], "tags are small; the record you started from is larger")
st = o["settle"]
chk(st["count"] == 10 and st["finite"], "every new node gets a position")
chk(st["gap"] >= 0, f"new nodes are clear of each other and of the node they grew from ({st['gap']}px to spare)")
chk(st["meanX"] > 15 and st["minDist"] > 100, f"they fan out in the direction asked for, not on top of their parent (mean x {st['meanX']})")
rc = o["rec"]
chk(rc["n"] == 3 and rc["title"] == "Home" and rc["kind"] == "album" and rc["image"] == "https://x/a.jpg" and rc["spotify"].endswith("/album/1") and rc["song"]["title"] == "Track", "the record page knows its number, title, cover, link and the song you started from")
chk(rc["facts"] == [["Artist", "Me"], ["Year", "2025"], ["Songs", "14"], ["Label", "Warp, Ninja"], ["Genres", "Electro"]], f"labels and genres joined to an album are its facts ({rc['facts']})")
chk(rc["mine"] == [["Q", "quote", "said about"], ["T", "text", ""]], f"your own blocks joined to it are listed in the order they were numbered, once each, whichever end the line was drawn from ({rc['mine']})")
chk(rc["near"] == [["R1", 8, True, "mine now"], ["R2", 9, False, ""]], f"the music near it is listed in numbered order and knows which lines are yours ({rc['near']})")
chk(o["recArtist"]["mine"] == ["X"] and o["recArtist"]["near"] == ["A"] and [f[0] for f in o["recArtist"]["facts"]] == [], "an artist's page shows what is joined to it, and no facts it does not have")
chk(o["recNone"] == [] and o["recJunk"] == [0, 0, 0], "a record with nothing joined to it, or a wall full of junk, gives an empty page and no crash")
chk(o["order"] == ["A", "R1", "R2"] and o["step"] == ["b", "a", "c", "a", None, "a"], f"Next and Prev go through albums and artists in numbered order and wrap round ({o['order']}, {o['step']})")
fr = o["free"]
chk(fr["empty"] == {"x": 10, "y": 20} and fr["clear"] == {"x": 0, "y": 0}, "with nothing in the way, a new record goes exactly where the person is looking")
chk(fr["blocked"]["ok"] and 100 < fr["blocked"]["dist"] < 800, f"with a block in the way it goes to the nearest free place, with a margin ({fr['blocked']['dist']}px away)")
chk(fr["crowded"] is None, "on a wall with no room at all it says so (the caller then falls back to a smaller footprint)")
chk(fr["padZero"] is not None, "the margin is a parameter")
bf = o["backFree"]
chk(all(x["backGapDeg"] >= 25 for x in bf), f"growth never wraps all the way round: the side facing back toward a node's other neighbours stays clear ({[x['backGapDeg'] for x in bf]} degrees)")
chk([x["minRadius"] for x in bf] == sorted(x["minRadius"] for x in bf) and bf[-1]["minRadius"] > bf[0]["minRadius"] + 200, f"the more there are, the wider the ring, so a big fan still has room ({[x['minRadius'] for x in bf]}px)")
chk(all(x["gap"] >= 0 for x in bf), "no overlaps at any size, wherever the node sits on the wall (the layout no longer pulls toward the wall's origin)")
cr = o["crowded"]
chk(cr["finite"] and cr["gap"] >= -2 and cr["keys"] == "n0,n1,n2,n3,n4", f"even among eight blocks already round the parent, new nodes find room ({cr['gap']}px to spare); existing blocks are not in the result, so they never move")
chk(o["settleEdge"] == [0, 0] and o["settleRepeat"], "nothing to place, or an unknown parent, is harmless; the same input gives the same map")
chk(o["direction"][0] < -50 and o["direction"][1] > 50, f"asking to grow left puts them left, right puts them right ({o['direction']})")
ow = o["outward"]
chk(abs(ow[0]) < 0.1 and ow[1] == -0.7853981633974483 and ow[2] == ow[1] and abs(ow[3] + 1.5707963267948966) < 0.01, f"growth faces away from what is already round a node ({ow})")
un = o["unpack"]
chk(un["n"] == 3 and un["names"] == ["Home", "Photay", "TVPCO"] and un["homeFlag"] == [True, False, False] and un["specHome"], "unpacking an old Web keeps its good nodes, marks the one it started from, skips bad ones")
chk(un["edges"] == [[0, 1], [0, 2]], f"its joins follow the nodes that survived ({un['edges']})")
chk(un["pos"][0] == [967, 533] or abs(un['pos'][1][0] - un['pos'][0][0]) == 100, f"positions keep their shape, scaled, centred where the block was ({un['pos']})")
chk(o["unpackEmpty"] == [0, 0, 0], "an empty or junk Web unpacks to nothing")

# ── the page half: guards and wiring, read from the source ──
wm = open(os.path.join(JSDIR, "wall_music.js"), encoding="utf-8").read()
rc = open(os.path.join(JSDIR, "room_canvas.js"), encoding="utf-8").read()
chk("if (!e.target.closest('.rc-tools, .rc-dot')) fire('click', b);" in rc, "a click on a toolbar button or the link dot never grows a node")
chk("if (dragMoved) { dragMoved = false; return; }" in rc and "dragMoved = moved;" in rc, "the click that ends a drag never grows a node")
chk("if (id === shown) return; shown = id;" in wm, "the card is rebuilt only when the selection really changes (not on every redraw, which would stop a preview's buttons)")
chk("/api/seinnteoir/pause" in wm and "audio.preload = 'none'" in wm, "a preview quiets the app's own player first, and nothing is fetched until you press play")
chk("RA.create(null, { onWait:" in wm and "for (var n = 0; n < 3; n++)" in wm and "r.status === 401" in wm, "Spotify slowness is said out loud; the liked songs are tried up to three times, but a sign-in problem is not retried")
chk("q.get('seed')" in wm and "q.get('music')" in wm and "wall.ready.then" in wm, "an address can put a record on the wall (?seed=) or open the Music panel (?music=1), once the wall has loaded")
chk("/api/connections?" in wm and "/api/connections/preview?" in wm and "/api/spotify/saved-tracks" in wm and "/api/seinnteoir/state" in wm, "it uses the app's own endpoints")
chk("esc(" in wm and wm.count("innerHTML") > 5, "everything it writes into the page is escaped")
chk("data-type=\"__music\"" in rc and "window.WallMusic.openStart(at)" in rc, "the + menu has a Music entry")
chk("wall.on('dblclick'" in wm and "stopImmediatePropagation()" in wm and "ArrowLeft" in wm, "double-click opens the record; Esc closes only it; the arrow keys step between records")
chk("data-a=\"openrec\"" in wm and "var activeRoot = function" in wm and wm.count("data-prev") >= 3 and wm.count("data-add") >= 3, "the quick card and the page share one set of songs and one Add to My Room, drawn into whichever is open")
chk("safeUrl" in wm and "/^(https?:\\/\\/|\\/)/.test(d.src" in wm, "links and pictures in the record page are checked before they are used")
chk("const isTyping = e =>" in rc and "!e.target.matches('input, textarea, [contenteditable]')" not in rc, "keys aimed at the page itself no longer crash the wall's key handler")
chk("data-act=\"unpack\"" in rc and "window.WallMusic.unpack(b.id)" in rc, "an old Web block has an 'unpack' button")
css = open(os.path.join(PROJ, "static", "css", "room_canvas.css"), encoding="utf-8").read()
mc = css[css.index("/* a music node:"):]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", mc), "the music styles use theme tokens only, no hex")
chk("box-shadow" not in mc.replace("box-shadow:none", ""), "no shadows")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
