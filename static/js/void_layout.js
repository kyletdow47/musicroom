/* void_layout.js — time as depth.
 *
 * In the Void everything you save has a time, and the time is how far away it is: the newest things float near you and
 * older ones recede into the dark behind. This is the arithmetic of that, with no page and no 3D needed (so it can be
 * tested):
 *
 *   VoidLayout.depths(blocks)          every block's depth (z), and one "stop" per day with its date
 *   VoidLayout.timeAt(z, stops)        what moment you are looking at when the camera is at z
 *   VoidLayout.zAtTime(ms, stops)      where in the void a moment is
 *   VoidLayout.nearIds(...)            what sits close to a depth (the things a new arrival must not land on)
 *   VoidLayout.dateLabel(ms)           "26 NOV 2026"
 *
 * Depth is in 3D units (one unit is a hundred of the wall's old pixels, so a record cover is about 1.2 units across).
 * Days with something in them are spaced by how long the gap was, but a long quiet stretch is squeezed so you do not fall
 * for ever through nothing: a day later is 9 units, four days or more is 36. Within one day, things are spread a little
 * (newest nearest) so they do not sit on one plane.
 */
(function (root) {
  'use strict';

  var S = 0.01;                          // 3D units per wall pixel
  var GAP = 9, GAP_MAX_DAYS = 4;         // units per day of gap, and the most days a gap is counted as
  var STEP = 0.7, SPREAD_MAX = 7;        // within a day: units between things, and the most a day may span
  var PUSH = 0.8;                        // a block's own push (its layer d, -3 … 3) nearer or farther than its time says
  var DAY = 86400000;

  var localDay = function (ms) { var d = new Date(ms); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); };
  var valid = function (b) { return b && typeof b.id === 'string' && isFinite(b.c) && b.c > 0; };
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };

  function dateLabel(ms) {
    if (!isFinite(ms) || ms <= 0) return '';
    var d = new Date(ms), mon = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'][d.getMonth()];
    return String(d.getDate()).padStart(2, '0') + ' ' + mon + ' ' + d.getFullYear();
  }

  // -> { z: { id: depth }, stops: [{ c, z, label, n }], zMax, zMin }.  `c` of a stop is the middle of its day.
  function depths(blocks) {
    var items = (Array.isArray(blocks) ? blocks : []).filter(valid).slice().sort(function (a, b) { return b.c - a.c; });   // newest first
    var out = { z: {}, stops: [], zMax: 0, zMin: 0 };
    if (!items.length) return out;
    var byDay = [], idx = {};
    items.forEach(function (b) { var k = localDay(b.c); if (idx[k] == null) { idx[k] = byDay.length; byDay.push({ day: k, items: [] }); } byDay[idx[k]].items.push(b); });
    var z = 0;
    byDay.forEach(function (g, i) {
      if (i > 0) z -= clamp(Math.round((byDay[i - 1].day - g.day) / DAY), 1, GAP_MAX_DAYS) * GAP;
      var step = Math.min(STEP, SPREAD_MAX / Math.max(1, g.items.length - 1));
      g.items.forEach(function (b, j) { out.z[b.id] = z - j * step + (clamp(Math.round(+b.d || 0), -3, 3)) * PUSH; });
      var newest = g.items[0].c, oldest = g.items[g.items.length - 1].c;
      out.stops.push({ c: Math.round((newest + oldest) / 2), z: z - (g.items.length - 1) * step / 2, label: dateLabel(g.day + DAY / 2), n: g.items.length });
    });
    var zs = Object.keys(out.z).map(function (k) { return out.z[k]; });
    out.zMax = Math.max.apply(null, zs); out.zMin = Math.min.apply(null, zs);
    return out;
  }

  // which moment is the camera looking at: linear between the two stops it is between, and the ends beyond them
  function timeAt(z, stops) {
    if (!stops || !stops.length || !isFinite(z)) return 0;
    if (z >= stops[0].z) return stops[0].c;
    var last = stops[stops.length - 1]; if (z <= last.z) return last.c;
    for (var i = 0; i < stops.length - 1; i++) {
      var a = stops[i], b = stops[i + 1];
      if (z <= a.z && z >= b.z) { var t = (a.z - z) / ((a.z - b.z) || 1); return Math.round(a.c + (b.c - a.c) * t); }
    }
    return last.c;
  }
  function zAtTime(ms, stops) {
    if (!stops || !stops.length || !isFinite(ms)) return 0;
    if (ms >= stops[0].c) return stops[0].z;
    var last = stops[stops.length - 1]; if (ms <= last.c) return last.z;
    for (var i = 0; i < stops.length - 1; i++) {
      var a = stops[i], b = stops[i + 1];
      if (ms <= a.c && ms >= b.c) { var t = (a.c - ms) / ((a.c - b.c) || 1); return a.z + (b.z - a.z) * t; }
    }
    return last.z;
  }

  // ids of the blocks whose depth is within `band` of z (for placing something new among what is at that depth, not what is a world away)
  function nearIds(zmap, z, band) {
    var out = []; band = band == null ? 6 : band;
    Object.keys(zmap || {}).forEach(function (id) { if (Math.abs(zmap[id] - z) <= band) out.push(id); });
    return out;
  }

  // where the camera may go: a little in front of the newest thing, to a little beyond the oldest
  function range(d, front, beyond) { return { max: d.zMax + (front == null ? 7 : front), min: d.zMin - (beyond == null ? 6 : beyond) }; }

  var api = { S: S, GAP: GAP, STEP: STEP, PUSH: PUSH, depths: depths, timeAt: timeAt, zAtTime: zAtTime, nearIds: nearIds, range: range, dateLabel: dateLabel, localDay: localDay };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.VoidLayout = api;
})(typeof window !== 'undefined' ? window : this);
