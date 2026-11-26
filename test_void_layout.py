"""Proves time as depth (static/js/void_layout.js): the newest things are nearest, older things recede, a long quiet stretch is
squeezed, things made on one day are spread a little, and the camera can be turned into a moment and back.

Pure arithmetic, so it runs under node.

Run:  venv/bin/python test_void_layout.py
"""
import os, sys, json, subprocess, shutil
PROJ = os.path.dirname(os.path.abspath(__file__))
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))
node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed"); sys.exit(0)
JSF = os.path.join(PROJ, "static", "js", "void_layout.js")
JS = r"""
const fs = require('fs'), vm = require('vm');
const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), { window: win, console, Date });
const V = win.VoidLayout, out = {};
const noon = (y, m, d, h = 12) => new Date(y, m - 1, d, h, 0, 0).getTime();
const B = (id, c, extra) => Object.assign({ id, c }, extra || {});
// three days: two things today, one yesterday, one 20 days ago (a long gap)
const blocks = [B('old', noon(2026, 11, 6)), B('y1', noon(2026, 11, 25)), B('t2', noon(2026, 11, 26, 15)), B('t1', noon(2026, 11, 26, 9))];
const d = V.depths(blocks);
out.z = d.z; out.stops = d.stops.map(s => [s.label, s.n, +s.z.toFixed(2)]); out.zMax = d.zMax; out.zMin = d.zMin;
out.empty = [V.depths([]).stops.length, V.depths(null).zMax, V.depths('x').zMin, V.depths([null, 5, {}, { id: 'a' }, { id: 'b', c: -4 }, { id: 'c', c: 'x' }]).stops.length];
// newest nearest, always: order of z follows order of time
const many = Array.from({ length: 40 }, (_, i) => B('m' + i, noon(2026, 11, 26, 0) + i * 60000));       // 40 things on one day
const dm = V.depths(many); const zs = many.map(b => dm.z[b.id]);
out.sameDay = { sorted: zs.every((z, i) => i === 0 || z > zs[i - 1]), span: +(Math.max(...zs) - Math.min(...zs)).toFixed(2), stops: dm.stops.length };
const one = V.depths([B('only', noon(2026, 11, 26))]); out.single = [one.z.only, one.zMax, one.zMin, one.stops.length];
// the push: d moves a thing nearer (+) or farther (-) than its time says, by a fixed amount, within -3..3
out.push = [0, 2, -3, 99, 'x', -99, 1.6].map(dd => +V.depths([B('a', noon(2026, 11, 26), { d: dd })]).z.a.toFixed(2));
// gaps: 1 day = 9 units, 6+ days = 54 units, however long
const g = (days) => { const r = V.depths([B('n', noon(2026, 11, 26)), B('o', noon(2026, 11, 26) - days * 86400000)]); return +(r.z.n - r.z.o).toFixed(2); };
out.gaps = [g(1), g(2), g(3), g(4), g(7), g(30), g(400)];
// time <-> depth
const st = d.stops;
out.timeAt = [V.timeAt(100, st) === st[0].c, V.timeAt(-9999, st) === st[st.length - 1].c, V.timeAt(st[1].z, st) === st[1].c, V.timeAt(NaN, st), V.timeAt(0, []), V.timeAt(0, null)];
const mid = V.timeAt((st[0].z + st[1].z) / 2, st); out.midBetween = mid > st[1].c && mid < st[0].c;
out.roundTrip = [0, 1, 2].every(i => Math.abs(V.zAtTime(V.timeAt(st[i].z, st), st) - st[i].z) < 1e-6) && (() => { const z = st[0].z - 3; return Math.abs(V.zAtTime(V.timeAt(z, st), st) - z) < 0.01; })();
out.monotone = (() => { let prev = Infinity, ok = true; for (let z = 5; z > -90; z -= 1.5) { const t = V.timeAt(z, st); if (t > prev) ok = false; prev = t; } return ok; })();
out.zAtTimeEnds = [V.zAtTime(Date.now() * 2, st) === st[0].z, V.zAtTime(1, st) === st[st.length - 1].z, V.zAtTime(NaN, st), V.zAtTime(5, [])];
// near and range
out.near = [V.nearIds(d.z, 0, 6).sort(), V.nearIds(d.z, -30, 3).sort(), V.nearIds({}, 0).length, V.nearIds(null, 0).length];
out.range = [V.range(d), V.range({ zMax: 0, zMin: 0 }, 2, 3)];
out.labels = [V.dateLabel(noon(2026, 11, 26)), V.dateLabel(noon(2027, 1, 3)), V.dateLabel(0), V.dateLabel(NaN), V.dateLabel(-5)];
out.S = V.S;
// stability: the same things in a different order give the same depths; adding something old does not move what is newer
const shuffled = V.depths(blocks.slice().reverse());
out.stable = JSON.stringify(shuffled.z) === JSON.stringify(d.z);
const plus = V.depths(blocks.concat([B('older', noon(2026, 9, 1))]));
out.addOldFixed = ['t1', 't2', 'y1', 'old'].every(k => Math.abs(plus.z[k] - d.z[k]) < 1e-9);
const plusNew = V.depths(blocks.concat([B('newer', noon(2026, 11, 27))]));
out.addNewShifts = plusNew.z.newer === 0 && plusNew.z.t1 < d.z.t1;
console.log(JSON.stringify(out));
"""
r = subprocess.run([node, "-e", JS, JSF], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    print(r.stderr[-1500:]); chk(False, "the layout runs under node"); sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])
z = o["z"]
chk(z["t2"] > z["t1"] > z["y1"] > z["old"], f"newest nearest, oldest farthest ({ {k: round(v, 1) for k, v in z.items()} })")
chk(o["zMax"] == z["t2"] and o["zMin"] == z["old"] and o["zMax"] == 0, "the newest thing sits at depth 0")
chk(len(o["stops"]) == 3 and [s[0] for s in o["stops"]] == ["26 NOV 2026", "25 NOV 2026", "06 NOV 2026"] and [s[1] for s in o["stops"]] == [2, 1, 1], f"one stop per day, with its date and how many things ({o['stops']})")
chk(abs((z["t1"] - z["y1"]) - 9) < 1.0 and abs((z["y1"] - z["old"]) - 36) < 1.0, "a day apart is about 9 units; a gap of nearly three weeks is squeezed to 36")
chk(o["empty"] == [0, 0, 0, 0], "nothing, junk or things with no time give an empty void")
sd = o["sameDay"]
chk(sd["sorted"] and sd["span"] <= 7.001 and sd["stops"] == 1, f"40 things on one day are spread a little, newest nearest, within 7 units ({sd['span']})")
chk(o["single"] == [0, 0, 0, 1], "a single thing is at depth 0")
chk(o["push"] == [0, 1.6, -2.4, 2.4, 0, -2.4, 1.6], f"a block's own layer pushes it nearer or farther by 0.8 a step, from -3 to 3, and junk does nothing ({o['push']})")
chk(o["gaps"] == [9, 18, 27, 36, 36, 36, 36], f"gaps: 9 units a day, never more than 36 ({o['gaps']})")
tm = o["timeAt"]
chk(tm[:3] == [True, True, True] and tm[3] == 0 and tm[4] == 0 and tm[5] == 0, "past the ends the camera sees the newest or oldest moment; nothing to look at is 0")
chk(o["midBetween"] and o["monotone"], "between two stops the moment is in between, and going back in space never goes forward in time")
chk(o["roundTrip"], "turning a depth into a moment and back gives the same depth")
za = o["zAtTimeEnds"]
chk(za[0] and za[1] and za[2] == 0 and za[3] == 0, "a moment beyond the ends is the nearest end")
nr = o["near"]
chk(nr[0] == ["t1", "t2"] or set(nr[0]) == {"t1", "t2"}, f"what is near a depth is what is within the band ({nr[0]})")
chk(nr[1] == [] and nr[2] == 0 and nr[3] == 0, "a depth with nothing near it, or no things, gives none")
chk(o["range"][0]["max"] == 7 and o["range"][0]["min"] < z["old"] - 5 and o["range"][1] == {"max": 2, "min": -3}, f"the camera may go from a little in front of the newest to a little beyond the oldest ({o['range']})")
chk(o["labels"] == ["26 NOV 2026", "03 JAN 2027", "", "", ""], f"dates read like 26 NOV 2026 ({o['labels']})")
chk(o["S"] == 0.01, "a hundred of the wall's old pixels are one unit of space")
chk(o["stable"], "the same things in any order are at the same depths")
chk(o["addOldFixed"] and o["addNewShifts"], "adding something older moves nothing that is newer; adding something newer pushes the rest back")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
