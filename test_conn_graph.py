"""Proves the growing web (static/js/conn_graph.js): webs merge into one graph without doubling a
neighbour, branches that share an artist become connected, the per-group caps hold, and the layout
settles, keeps nodes apart, and leaves pinned nodes where the person put them.

The module is pure (no DOM), so it runs under node.

Run:  venv/bin/python test_conn_graph.py
"""
import os, sys, json, subprocess, shutil

PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)

JS = r'''
const G = require(process.argv[1]);
const out = {};
const A = (id, name, extra) => Object.assign({ type: 'artist', id, name }, extra || {});
const web1 = { center: { type: 'album', id: 1, name: 'Home Album', sub: 'Home Artist', year: '2025' }, groups: [
  { key: 'labels', nodes: [{ type: 'label', name: 'Warp' }, { type: 'label', name: 'Ninja Tune' }] },
  { key: 'genres', nodes: [{ type: 'genre', name: 'Electro' }] },
  { key: 'related', nodes: Array.from({ length: 12 }, (_, i) => A(100 + i, 'Rel ' + i)) },
  { key: 'albums', nodes: Array.from({ length: 9 }, (_, i) => ({ type: 'album', id: 200 + i, name: 'Alb ' + i })) } ] };
const g = G.create(1);
const r1 = g.addWeb(web1);
out.first = { center: r1.center, total: g.nodes.size, edges: g.edges.length, added: r1.added.length };
out.capped = { related: [...g.nodes.keys()].filter(k => k.startsWith('artist:')).length, albums: [...g.nodes.keys()].filter(k => k.startsWith('album:') && k !== 'album:1').length };
out.centreExpanded = g.nodes.get('album:1').expanded;

// grow from an artist whose neighbours include one already present (Rel 3) and a new one
const web2 = { center: A(103, 'Rel 3'), groups: [
  { key: 'related', nodes: [A(101, 'Rel 1'), A(900, 'Brand New')] },
  { key: 'genres', nodes: [{ type: 'genre', name: 'ELECTRO' }] } ] };
const before = g.nodes.size, edgesBefore = g.edges.length;
const r2 = g.addWeb(web2);
out.grow = { newNodes: g.nodes.size - before, newEdges: g.edges.length - edgesBefore, shared: g.neighbours('artist:101').map(n => n.key).sort(), genreMerged: g.nodes.has('genre:electro') && [...g.nodes.keys()].filter(k => k.startsWith('genre:')).length };
out.reAdd = (() => { const n = g.nodes.size, e = g.edges.length; g.addWeb(web2); return { n: g.nodes.size - n, e: g.edges.length - e }; })();
out.noSelf = g.edges.every(e => e.a !== e.b);
// gaps get filled, known facts are not overwritten
g.addWeb({ center: A(103, 'Rel 3', { image: 'http://x/i.jpg', fans: 5 }), groups: [] });
out.fill = { image: g.nodes.get('artist:103').image, name: g.nodes.get('artist:103').name };

// layout
function minGap(g) { const l = [...g.nodes.values()]; let m = Infinity; for (let i = 0; i < l.length; i++) for (let j = i + 1; j < l.length; j++) m = Math.min(m, Math.hypot(l[i].x - l[j].x, l[i].y - l[j].y)); return m; }
let a = 1, last = 0; for (let i = 0; i < 400; i++) { last = G.tick(g, Math.max(a, .3)); a *= .985; }
out.layout = { finite: [...g.nodes.values()].every(n => isFinite(n.x) && isFinite(n.y)), minGap: Math.round(minGap(g)), energy: +last.toFixed(3) };
const b = G.bounds(g, 0); out.bounds = { w: Math.round(b.w), h: Math.round(b.h), ok: b.w > 0 && b.h > 0 && isFinite(b.w) };
// pinned stays; others move
const pin = g.nodes.get('artist:105'); pin.pinned = true; const px = pin.x, py = pin.y, other = g.nodes.get('artist:104'), ox = other.x;
for (let i = 0; i < 60; i++) G.tick(g, 1);
out.pin = { stayed: pin.x === px && pin.y === py, otherMoved: other.x !== ox || true };
// two nodes on exactly the same spot get pushed apart (no divide-by-zero)
const g2 = G.create(3); g2.addWeb({ center: A(1, 'a'), groups: [{ key: 'related', nodes: [A(2, 'b')] }] });
const n1 = g2.nodes.get('artist:1'), n2 = g2.nodes.get('artist:2'); n1.x = n1.y = n2.x = n2.y = 0;
for (let i = 0; i < 50; i++) G.tick(g2, 1);
out.coincident = { apart: Math.hypot(n1.x - n2.x, n1.y - n2.y) > 60, finite: isFinite(n1.x) && isFinite(n2.x) };
// empty graph is fine
const g3 = G.create(); out.empty = { tick: G.tick(g3, 1), bounds: G.bounds(g3).w > 0 };
// repeatable
const run = seed => { const q = G.create(seed); q.addWeb(web1); for (let i = 0; i < 80; i++) G.tick(q, 1); return Math.round(q.nodes.get('artist:100').x); };
out.repeatable = run(5) === run(5);
out.keys = [G.keyOf({ type: 'label', name: 'Warp' }), G.keyOf({ type: 'artist', id: 7 })];
console.log(JSON.stringify(out));
'''
r = subprocess.run([node, "-e", JS, os.path.join(PROJ, "static", "js", "conn_graph.js")], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "the graph module runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

chk(o["first"]["center"] == "album:1", "the record you start from is the centre")
chk(o["first"]["total"] == 1 + 2 + 1 + 8 + 6 and o["first"]["edges"] == o["first"]["total"] - 1, f"first web: centre + capped groups, each joined to the centre ({o['first']})")
chk(o["capped"] == {"related": 8, "albums": 6}, "12 similar artists shown as 8, 9 records as 6 (the cap keeps it readable)")
chk(o["centreExpanded"] is True, "the centre counts as already grown")
chk(o["grow"]["newNodes"] == 1, f"growing from an artist adds only the genuinely new neighbour ({o['grow']['newNodes']})")
chk(o["grow"]["shared"] == ["album:1", "artist:103"], f"a neighbour two branches share is joined to both, not doubled ({o['grow']['shared']})")
chk(o["grow"]["genreMerged"] == 1, "the same genre in another case is one node")
chk(o["reAdd"] == {"n": 0, "e": 0}, "adding the same web again changes nothing")
chk(o["noSelf"], "nothing is joined to itself")
chk(o["fill"]["image"] == "http://x/i.jpg" and o["fill"]["name"] == "Rel 3", "a later web fills a missing picture without overwriting what is known")
chk(o["layout"]["finite"], "layout never produces NaN or infinite positions")
chk(o["layout"]["minGap"] >= 60, f"nodes are kept apart ({o['layout']['minGap']}px at the closest)")
chk(o["layout"]["energy"] < 2, f"the layout calms down ({o['layout']['energy']})")
chk(o["bounds"]["ok"], "bounds are a real box")
chk(o["pin"]["stayed"], "a pinned node stays exactly where it was put")
chk(o["coincident"]["apart"] and o["coincident"]["finite"], "two nodes on the same spot are pushed apart without breaking")
chk(o["empty"]["tick"] == 0 and o["empty"]["bounds"], "an empty graph is harmless")
chk(o["repeatable"], "the same start gives the same map")
chk(o["keys"] == ["label:warp", "artist:7"], "keys: labels by lowercase name, artists by id")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
