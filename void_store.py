"""void_store.py — what the Void keeps, and the pictures it may show.

The Void is the wall rebuilt as a real 3D space: everything you save has a time, the newest things float near you and
older ones recede into the dark behind (see static/js/void.js). It keeps its things in its OWN store, so the old wall
and its blocks are left exactly as they were.

A thing in the Void is a block: { id, type, x, y, w, h, c, n, d, data }
  x, y   where it sits across the space (its depth is not stored: it comes from c)
  c      when it was made (ms since 1970): this is what makes time the depth
  n      its archive number, never reused (see wall_links.next_number)
  d      a small push nearer (+) or farther (-) than its time says, -3 … 3
  data   what it holds (a record's details, some words, a quote …)

Nothing from the browser is trusted: every block is checked and rebuilt here, and the picture proxy only ever fetches
from the music services' own picture servers.

Routes:
  GET  /api/room/void/<pid>      {blocks, links, next}
  POST /api/room/void/<pid>      {blocks, links?, next?}   (lines and the counter are kept when a caller does not send them)
  GET  /api/void/image?u=<url>   a picture from Deezer, Spotify or YouTube, served from here so the 3D scene may use it
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import wall_links

TYPES = ("node", "text", "quote", "image", "link", "mix")
MAX_BLOCKS = 2000
MAX_DATA = 20000                         # characters of JSON in one block's data
MAX_PAYLOAD = 4_000_000
DEFAULT_SIZE = {"node": (148, 128), "text": (360, 200), "quote": (380, 240), "image": (320, 320), "link": (320, 200), "mix": (440, 280)}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_BOUND = 1_000_000


def _num(v, lo, hi, default=None):
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f or f < lo or f > hi else f


def _int(v, lo, hi, default=0):
    f = _num(v, lo, hi, None)
    return default if f is None else int(f)


def clean_blocks(blocks, now_ms=None):
    """The blocks worth keeping, rebuilt from only what is understood. Always a list. A block with no usable time gets `now`
    (so it lands in the present), and the same id twice keeps the first."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out, seen = [], set()
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict) or not isinstance(b.get("id"), str) or not _ID.match(b["id"]) or b["id"] in seen:
            continue
        t = b.get("type")
        if t not in TYPES:
            continue
        x, y = _num(b.get("x"), -_BOUND, _BOUND), _num(b.get("y"), -_BOUND, _BOUND)
        if x is None or y is None:
            continue
        dw, dh = DEFAULT_SIZE[t]
        data = b.get("data") if isinstance(b.get("data"), dict) else {}
        try:
            if len(json.dumps(data)) > MAX_DATA:
                continue
        except (TypeError, ValueError):
            continue
        seen.add(b["id"])
        out.append({"id": b["id"], "type": t, "x": round(x, 1), "y": round(y, 1),
                    "w": _int(b.get("w"), 20, 4000, dw), "h": _int(b.get("h"), 20, 4000, dh),
                    "c": _int(b.get("c"), 1, 4_102_444_800_000, now_ms), "n": _int(b.get("n"), 1, 10**9, 0),
                    "d": _int(b.get("d"), -3, 3, 0), "data": data})
        if len(out) >= MAX_BLOCKS:
            break
    return out


# ── pictures ───────────────────────────────────────────────────────────────

IMAGE_HOSTS = ("dzcdn.net", "scdn.co", "ytimg.com")
MAX_IMAGE = 5_000_000
_cache = {}                              # url -> (type, bytes)
CACHE_MAX = 300


def image_url_ok(url):
    """True only for an https address on one of the music services' own picture servers (a host, or a subdomain of one)."""
    try:
        u = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or (u.port not in (None, 443)):
        return False
    return any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_image(url, opener=None, hops=2):
    """(content_type, bytes) or (None, reason). Redirects are followed by hand, and each hop is checked again."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(hops + 1):
        if not image_url_ok(url):
            return None, "That picture is not from a place this app fetches from."
        req = urllib.request.Request(url, headers={"User-Agent": "MusicRoom/1.0 (picture proxy)", "Accept": "image/*"})
        try:
            with opener.open(req, timeout=10) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/") or ctype == "image/svg+xml":
                    return None, "That is not a picture."
                data = r.read(MAX_IMAGE + 1)
                if len(data) > MAX_IMAGE:
                    return None, "That picture is too big."
                return ctype, data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            return None, f"The picture server answered {e.code}."
        except Exception:
            return None, "Could not reach the picture server."
    return None, "Too many redirects."


def register(app, get_setting, set_setting):
    from flask import jsonify, request, Response

    key = lambda pid: f"room_void:{pid}"

    def load(pid):
        try:
            d = json.loads(get_setting(key(pid), "") or "{}")
        except (TypeError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        blocks = clean_blocks(d.get("blocks"))
        return {"blocks": blocks, "links": wall_links.clean_links(d.get("links"), blocks), "next": wall_links.next_number(d.get("next"), blocks)}

    @app.route("/api/room/void/<int:pid>")
    def api_void_get(pid):
        return jsonify(load(pid))

    @app.route("/api/room/void/<int:pid>", methods=["POST"])
    def api_void_save(pid):
        d = request.get_json(silent=True)
        if not isinstance(d, dict) or not isinstance(d.get("blocks"), list):
            return jsonify({"error": "blocks must be a list"}), 400
        if len(d["blocks"]) > MAX_BLOCKS * 2:
            return jsonify({"error": "too many blocks"}), 400
        before = load(pid)
        blocks = clean_blocks(d["blocks"])
        links = wall_links.clean_links(d["links"] if "links" in d else before["links"], blocks)
        sent = d.get("next") if isinstance(d.get("next"), (int, float)) and not isinstance(d.get("next"), bool) else 0
        nxt = wall_links.next_number(max(int(sent), before["next"]), blocks)                # it only ever goes up
        payload = json.dumps({"v": 2, "blocks": blocks, "links": links, "next": nxt})
        if len(payload) > MAX_PAYLOAD:
            return jsonify({"error": "too big"}), 413
        set_setting(key(pid), payload)
        return jsonify({"ok": True, "n": len(blocks), "links": len(links), "next": nxt})

    @app.route("/api/void/image")
    def api_void_image():
        url = request.args.get("u", "")
        if not image_url_ok(url):
            return jsonify({"error": "not a picture this app fetches"}), 400
        hit = _cache.get(url)
        if not hit:
            ctype, data = fetch_image(url)
            if ctype is None:
                return jsonify({"error": data}), 502
            if len(_cache) >= CACHE_MAX:
                _cache.pop(next(iter(_cache)), None)
            hit = _cache[url] = (ctype, data)
        resp = Response(hit[1], mimetype=hit[0])
        resp.headers["Cache-Control"] = "public, max-age=604800"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
