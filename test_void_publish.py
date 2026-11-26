"""Proves the published Void: a copy that arrives inside someone else's room design is rebuilt from only what is understood,
it is shown read-only (nothing to add, move or save), and the room editor and public page are wired to it.

Run:  venv/bin/python test_void_publish.py
"""
import os, sys, json, subprocess, shutil
PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
JS = os.path.join(PROJ, "static", "js")
rd = lambda *p: open(os.path.join(PROJ, *p), encoding="utf-8").read()
void_src, rb, pub, mine, app_src = rd("static", "js", "void.js"), rd("static", "js", "room_blocks.js"), rd("templates", "public_room.html"), rd("templates", "my_room.html"), rd("app.py")
node = shutil.which("node")

if node:
    H = r"""
    const fs = require('fs'), vm = require('vm'); const win = {}; const ctx = { window: win, console, URL, Date };
    for (const f of ['void_layout.js', 'void.js']) vm.runInNewContext(fs.readFileSync(process.argv[1] + '/' + f, 'utf8'), ctx);
    const C = win.MRVoid.cleanSnapshot, ok = (id, o) => Object.assign({ id, type: 'text', x: 1, y: 2, w: 100, h: 50, c: 5, n: 3, d: 0, data: { heading: 'hi' } }, o || {});
    const out = {};
    out.nothing = [C(null), C('x'), C(5), C({}), C({ blocks: 'no' })].map(s => s.blocks.length + '/' + s.links.length);
    const s = C({ blocks: [ok('a'), ok('a'), ok('b', { type: 'constructor' }), ok('c', { type: 'script' }), ok('d', { x: 'NaN' }), ok('e', { x: Infinity }), ok('bad id!'), ok('f', { w: 1e9, h: -4, d: 99, n: -2, c: 'x' }), ok('g', { data: [1] }), null, 7],
                 links: [{ a: 'a', b: 'f', kind: 'near', label: 'x'.repeat(200) }, { a: 'a', b: 'zzz' }, { a: 'a', b: 'a' }, { a: 'constructor', b: 'a' }, null], next: 12 });
    out.kept = s.blocks.map(b => b.id); out.f = s.blocks.find(b => b.id === 'f'); out.g = s.blocks.find(b => b.id === 'g').data; out.links = s.links.map(l => [l.a, l.b, l.kind, l.label.length]); out.next = s.next;
    out.many = C({ blocks: Array.from({ length: 5000 }, (_, i) => ok('k' + i)) }).blocks.length;
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", H, JS], capture_output=True, text=True, timeout=60)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        o = None; print(r.stdout, r.stderr)
    chk(o is not None, "the snapshot cleaner runs")
    if o:
        chk(o["nothing"] == ["0/0"] * 5, "nothing, or nonsense, becomes an empty Void")
        chk(o["kept"] == ["a", "f", "g"], f"only well-formed things survive (no repeats, unknown kinds, 'constructor', bad numbers, bad ids): {o['kept']}")
        chk(o["f"]["w"] == 4000 and o["f"]["h"] == 20 and o["f"]["d"] == 3 and o["f"]["n"] == 0 and o["f"]["c"] == 1, "sizes, push and numbers are held to their limits")
        chk(o["g"] == {}, "a thing's data must be an object")
        chk(o["links"] == [["a", "f", "near", 80]], f"only lines between two kept things survive, labels cut short: {o['links']}")
        chk(o["next"] == 12 and o["many"] == 2000, "the counter is kept and a huge Void is cut to 2000 things")

chk("readOnly: true" not in void_src and "ro = !!opts.readOnly" in void_src, "the Void has a read-only mode, off by default")
chk("function dirty() { if (ro) return;" in void_src and "if (id && !ro)" in void_src and "if (root.WallMusic && !ro)" in void_src, "read-only: nothing is saved, dragged or grown, and the music tools are not attached")
chk("if (ro) data = cleanSnapshot(opts.data)" in void_src and "'/api/room/void/' + placeId" in void_src, "read-only shows a cleaned copy and never reads the owner's store")
chk("register('void'" in rb and "data-void=" in rb and "snapshot" in rb, "the room editor has a Void block that holds a copy")
chk("MRVoid.mount(stage, { readOnly: true, data: b.snapshot })" in pub and 'type="importmap"' in pub and "void.js" in pub, "the public page opens it read-only, with the 3D engine")
chk("publishVoid" in mine and "'/api/void/mine'" in mine and "t === 'void' && !window.MR_WALL" in mine, "My Public Room can publish a copy (offered only in this fork)")
chk('"/api/void/mine"' in app_src, "the app says which place holds the Void")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
