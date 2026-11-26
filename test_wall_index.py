"""Proves the wall as a list (static/js/wall_index.js): every block in numbered order, a filter by kind, a search that
ignores capitals and accents, and that nothing odd on the wall can break it.

The rows, filters and search are pure, so they run under node.

Run:  venv/bin/python test_wall_index.py
"""
import os, sys, json, subprocess, shutil, re
PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)
JSF = os.path.join(PROJ, "static", "js", "wall_index.js")

JS = r"""
const fs = require('fs'), vm = require('vm');
const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, console, URL });
const P = win.WallIndex.pure, out = {};
const blocks = [
  { id: 'q', n: 2, type: 'quote', data: { text: 'Café con leche, siempre', who: 'Ana', source: 'a talk in Montevideo' } },
  { id: 'a', n: 1, type: 'node', data: { kind: 'album', name: 'KOSMOBOTANIKA', sub: 'ETNOBOTANIKA', year: '2025' } },
  { id: 't', n: 3, type: 'text', data: { heading: 'Heading', body: 'a long body '.repeat(20) } },
  { id: 't2', n: 4, type: 'text', data: { heading: '', body: 'only a body' } },
  { id: 'i', n: 5, type: 'image', data: { src: '/x.jpg', alt: 'a rocket', caption: 'from the sleeve' } },
  { id: 'm', n: 6, type: 'mix', data: { url: 'https://www.youtube.com/watch?v=abc', title: '' } },
  { id: 'l', n: 7, type: 'link', data: { url: 'https://example.com/page', title: 'An Article' } },
  { id: 'r', n: 8, type: 'record', data: { track: { title: 'Surrender', artist_name: 'HEALER' } } },
  { id: 'p', n: 9, type: 'playlist', data: {} }, { id: 'w', n: 10, type: 'web', data: { title: 'Old Album' } },
  { id: 'g', n: 11, type: 'node', data: { kind: 'genre', name: 'Électro' } },
  { id: 'unnumbered', type: 'text', data: { body: 'new one' } }, { id: 'weird', n: 12, type: 'who-knows' }];
const rows = P.rowsOf(blocks);
out.order = rows.map(r => r.id);
out.titles = Object.fromEntries(rows.map(r => [r.id, [r.title, r.sub]]));
out.counts = P.counts(rows);
const ids = (g, q) => P.filterRows(rows, g, q).map(r => r.id);
out.search = { cafe: ids('all', 'CAFE'), leche: ids('all', 'leche cafe'), none: ids('all', 'zzzz'), electro: ids('all', 'electro'), body: ids('all', 'only a body'), url: ids('all', 'youtube'), track: ids('all', 'healer surrender'),
               blank: ids('all', '   ').length, two: ids('all', 'cafe talk'), sub: ids('all', 'etnobotanika') };
out.group = { node: ids('node', ''), record: ids('record', ''), quote: ids('quote', 'ana'), groupAndSearch: ids('node', 'electro'), unknown: ids('nonsense', '').length };
out.junk = (() => { try { return [P.rowsOf(null).length, P.rowsOf('x').length, P.rowsOf([null, 5, 'x', {}, { id: 7 }, { id: 'ok' }]).map(r => r.id), P.filterRows(P.rowsOf([{ id: 'a', type: 'text', data: null }]), 'all', 'x').length, P.labelOf(null).title, P.labelOf({ type: 'node', data: 'str' }).title]; } catch (e) { return 'threw ' + e.message; } })();
out.long = rows.find(r => r.id === 't').title.length <= 70 && rows.find(r => r.id === 't').title.endsWith('Heading') ;
out.cut = P.rowsOf([{ id: 'x', n: 1, type: 'text', data: { body: 'word '.repeat(60) } }])[0].title;
out.fold = [P.fold('Électro ÉÑÍ'), P.fold(null), P.fold(undefined), P.fold(12)];
out.filters = P.FILTERS.map(f => f[0]);
const src = fs.readFileSync(process.argv[1], 'utf8');
out.src = { slash: src.includes("e.key === '/'") && src.includes("!host.querySelector('.rc-page.on')"), esc: src.includes("open && e.key === 'Escape'") && src.includes('stopImmediatePropagation'), enter: src.includes("t.id === 'rc-lq'"),
            go: src.includes('wall.select(blk.id); if (wall.flyTo) wall.flyTo(blk); else wall.panTo(blk.x + blk.w / 2, blk.y + blk.h / 2);'), esc2: src.includes('esc(r.title)') && src.includes('esc(r.sub)') && src.includes('esc(query)') };
console.log(JSON.stringify(out));
"""
r = subprocess.run([node, "-e", JS, JSF], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "the list logic runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

chk(o["order"] == ["a", "q", "t", "t2", "i", "m", "l", "r", "p", "w", "g", "weird", "unnumbered"], f"the list runs in numbered order, and a block with no number comes last ({o['order']})")
tt = o["titles"]
chk(tt["a"] == ["KOSMOBOTANIKA", "album · ETNOBOTANIKA · 2025"], "music: its name, and what it is")
chk(tt["q"] == ["“Café con leche, siempre”", "Ana, a talk in Montevideo"], "a quote: its words, and who and where")
chk(tt["t"][0] == "Heading" and tt["t"][1].endswith("…") and tt["t2"] == ["only a body", ""], "words: the heading (with a bit of the body) or else the body")
chk(tt["i"] == ["a rocket", "from the sleeve"] and tt["m"][0] == "youtube.com" and tt["l"] == ["An Article", "example.com"], "pictures, videos and links get a sensible name")
chk(tt["r"] == ["Surrender", "HEALER"] and tt["p"][0] == "a playlist" and tt["w"][0] == "Old Album" and tt["weird"][0] == "a who-knows", "records, playlists, old webs and unknown things are named, never blank")
chk(o["counts"]["all"] == 13 and o["counts"]["node"] == 2 and o["counts"]["record"] == 2 and o["counts"]["text"] == 3, f"the filters know how many of each ({o['counts']})")
s = o["search"]
chk(s["cafe"] == ["q"] and s["electro"] == ["g"], "search ignores capitals and accents: CAFE finds Café, electro finds Électro")
chk(s["leche"] == ["q"] and s["two"] == ["q"] and s["none"] == [] and s["blank"] == 13, "every word typed must be there, in any order; nothing typed means everything")
chk(s["body"] == ["t2"] and s["url"] == ["m"] and s["track"] == ["r"] and s["sub"] == ["a"], "it searches the words inside things too: a body, a link's address, a record's artist")
g = o["group"]
chk(g["node"] == ["a", "g"] and g["record"] == ["r", "p"] and g["quote"] == ["q"] and g["groupAndSearch"] == ["g"] and g["unknown"] == 0, "a filter and a search work together; records and playlists are one kind")
chk(o["junk"] == [0, 0, ["ok"], 0, "a block", "(untitled)"], f"junk on the wall never breaks the list ({o['junk']})")
chk(o["long"] and o["cut"].endswith("…") and len(o["cut"]) <= 70, "long words are shortened with an ellipsis")
chk(o["fold"] == ["electro eni", "", "", "12"], f"folding is safe for anything ({o['fold']})")
chk(o["filters"][0] == "all" and "node" in o["filters"] and "quote" in o["filters"], "the filters start with All")
sr = o["src"]
chk(sr["slash"] and sr["esc"] and sr["enter"], "/ opens the list ready to search (not while typing, or on a record page); Esc closes it; Enter goes to the first match")
chk(sr["go"] and sr["esc2"], "a row takes you to the thing on the wall and selects it; everything shown is escaped")
src = open(JSF, encoding="utf-8").read()
rc = open(os.path.join(PROJ, "static", "js", "room_canvas.js"), encoding="utf-8").read()
tpl = open(os.path.join(PROJ, "templates", "room3d.html"), encoding="utf-8").read()
chk("window.WallIndex.attach(api)" in rc and "/static/js/wall_index.js" in tpl, "the wall loads and attaches it")
css = open(os.path.join(PROJ, "static", "css", "room_canvas.css"), encoding="utf-8").read()
lc = css[css.index("/* ── the list:"):css.index("/* ── the typed intro")]
chk(not re.search(r"#[0-9a-fA-F]{3,8}\b", lc) and "box-shadow" not in lc.replace("box-shadow:none", "") and "border-radius:0" in lc.replace(" ", ""), "the list styles use theme tokens only, square, no shadows")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
