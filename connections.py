"""connections.py — the neighbourhood of a record: who is near it, and where to go next.

Starts from an album (or an artist) and returns a small web the page can draw and walk:

  album  -> its label(s), genres, the other records by the artist, the artists that sound like them,
            and anyone credited beside the main artist
  artist -> their records, and the artists that sound like them

Source: Deezer's free, no-key API (the same one the app already uses for matching). It is good for the
neighbourhood and works for obscure records. It does NOT hold producers, writers or samples; where it
has none, the web says so plainly. (MusicBrainz sample links and Discogs credits are the next step.)

Nothing is invented: a group with nothing in it is left out, and every failure is a note, not a crash.

Routes:
  GET /connections                              (sends you to the wall: the map lives there, see static/js/wall_music.js)
  GET /api/connections?kind=album&artist=&album=   (or &id=<deezer album id>)
  GET /api/connections?kind=artist&id=<deezer artist id>   (or &name=)
"""
import json
import re
import time
import urllib.parse
import urllib.request

API = "https://api.deezer.com"
USER_AGENT = "MusicRoom/1.0 (personal discography notebook)"
CACHE_S = 3600
MAX_RELATED = 12
MAX_ALBUMS = 10

_cache = {}


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class Deezer:
    """A tiny cached client. `get` is replaceable so tests never touch the network."""

    def __init__(self, get=None):
        self._get = get or _http_json
        self.errors = 0

    def j(self, path):
        url = API + path
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        time.sleep(0.12)                      # Deezer allows ~50 calls per 5 s; stay well inside
        try:
            data = self._get(url)
        except Exception:
            self.errors += 1
            return None
        if isinstance(data, dict) and data.get("error"):
            self.errors += 1
            return None
        _cache[url] = (time.time(), data)
        return data


# ── pure helpers ───────────────────────────────────────────────────────────

def split_labels(text):
    """'TVPCO, Superkasety' -> ['TVPCO', 'Superkasety'], dropping repeats and empties."""
    out, seen = [], set()
    for part in re.split(r"\s*(?:,|/|;|\s&\s|\s\+\s)\s*", text or ""):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


def year_of(date):
    m = re.match(r"^(\d{4})", date or "")
    return m.group(1) if m else ""


def spotify_artist_url(name):
    return "https://open.spotify.com/search/" + urllib.parse.quote(name or "") + "/artists"


def spotify_album_url(artist, album):
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{artist or ''} {album or ''}".strip()) + "/albums"


def label_search_url(name):
    return "https://www.discogs.com/search/?q=" + urllib.parse.quote(name or "") + "&type=label"


def _artist_node(a):
    return {"type": "artist", "id": a.get("id"), "name": a.get("name") or "", "image": a.get("picture_medium") or "",
            "fans": a.get("nb_fan"), "url": spotify_artist_url(a.get("name"))}


def _album_node(al, artist_name=""):
    return {"type": "album", "id": al.get("id"), "name": al.get("title") or "", "image": al.get("cover_medium") or "",
            "year": year_of(al.get("release_date")), "kind": al.get("record_type") or "",
            "url": spotify_album_url(artist_name, al.get("title"))}


def _group(key, title, nodes, note=""):
    return {"key": key, "title": title, "nodes": nodes, "note": note}


def _albums_sorted(raw, skip_id=None):
    """An artist's records, albums first and newest first; singles last. One entry per title."""
    seen, rows = set(), []
    for al in raw or []:
        if not isinstance(al, dict) or al.get("id") == skip_id or not al.get("title"):
            continue
        key = re.sub(r"[^a-z0-9]", "", al["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(al)
    rank = {"album": 0, "ep": 1, "single": 2}
    rows.sort(key=lambda a: (rank.get(a.get("record_type"), 3), -(int(year_of(a.get("release_date")) or 0))))
    return rows[:MAX_ALBUMS]


# ── the two webs ───────────────────────────────────────────────────────────

def find_album_id(dz, artist, album):
    """The Deezer album for 'artist' + 'album', or None. The plain search is used: the strict
    field query misses accents and odd punctuation."""
    q = urllib.parse.quote(f"{artist or ''} {album or ''}".strip())
    found = dz.j(f"/search/album?q={q}&limit=5") or {}
    cands = found.get("data") or []
    if not cands:
        return None
    want = re.sub(r"[^a-z0-9]", "", (album or "").lower())
    for c in cands:                           # prefer an exact title, else the first hit
        if re.sub(r"[^a-z0-9]", "", (c.get("title") or "").lower()) == want and want:
            return c.get("id")
    return cands[0].get("id")


def album_web(dz, album_id):
    al = dz.j(f"/album/{int(album_id)}")
    if not al or not al.get("title"):
        return None, "That album was not found."
    artist = al.get("artist") or {}
    labels = split_labels(al.get("label"))
    genres = [g["name"] for g in ((al.get("genres") or {}).get("data") or []) if g.get("name")]
    center = {"type": "album", "id": al.get("id"), "name": al["title"], "sub": artist.get("name") or "",
              "image": al.get("cover_medium") or "", "year": year_of(al.get("release_date")),
              "tracks": al.get("nb_tracks"), "fans": al.get("fans"), "url": spotify_album_url(artist.get("name"), al["title"]),
              "deezer": al.get("link") or ""}
    groups, notes = [], []
    if labels:
        groups.append(_group("labels", "On the label", [{"type": "label", "name": n, "url": label_search_url(n)} for n in labels]))
    if genres:
        groups.append(_group("genres", "Genres", [{"type": "genre", "name": g} for g in genres]))
    if artist.get("id"):
        others = _albums_sorted((dz.j(f"/artist/{int(artist['id'])}/albums?limit=50") or {}).get("data"), skip_id=al.get("id"))
        if others:
            groups.append(_group("albums", "More by " + (artist.get("name") or "them"), [_album_node(a, artist.get("name")) for a in others]))
        rel = (dz.j(f"/artist/{int(artist['id'])}/related") or {}).get("data") or []
        rel = [r for r in rel if isinstance(r, dict) and r.get("name")][:MAX_RELATED]
        if rel:
            groups.append(_group("related", "Sounds like", [_artist_node(r) for r in rel]))
    credited = [c for c in (al.get("contributors") or []) if isinstance(c, dict) and c.get("id") != artist.get("id") and c.get("name")]
    if credited:
        groups.append(_group("credits", "Also credited", [dict(_artist_node(c), role=c.get("role") or "") for c in credited][:8]))
    else:
        notes.append("No producers, writers or samples are listed for this record in the free sources.")
    if dz.errors:
        notes.append("Some of this could not be read just now; try again in a minute.")
    return {"center": center, "groups": groups, "notes": notes}, None


def artist_web(dz, artist_id):
    a = dz.j(f"/artist/{int(artist_id)}")
    if not a or not a.get("name"):
        return None, "That artist was not found."
    center = {"type": "artist", "id": a.get("id"), "name": a["name"], "sub": "", "image": a.get("picture_medium") or "",
              "fans": a.get("nb_fan"), "albums": a.get("nb_album"), "url": spotify_artist_url(a["name"]), "deezer": a.get("link") or ""}
    groups, notes = [], []
    recs = _albums_sorted((dz.j(f"/artist/{int(a['id'])}/albums?limit=50") or {}).get("data"))
    if recs:
        groups.append(_group("albums", "Records", [_album_node(r, a["name"]) for r in recs]))
    rel = [r for r in ((dz.j(f"/artist/{int(a['id'])}/related") or {}).get("data") or []) if isinstance(r, dict) and r.get("name")][:MAX_RELATED]
    if rel:
        groups.append(_group("related", "Sounds like", [_artist_node(r) for r in rel]))
    if not groups:
        notes.append("Nothing else is known about this artist in the free sources.")
    if dz.errors:
        notes.append("Some of this could not be read just now; try again in a minute.")
    return {"center": center, "groups": groups, "notes": notes}, None


def find_artist_id(dz, name):
    q = urllib.parse.quote((name or "").strip())
    found = (dz.j(f"/search/artist?q={q}&limit=3") or {}).get("data") or []
    want = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for c in found:
        if re.sub(r"[^a-z0-9]", "", (c.get("name") or "").lower()) == want and want:
            return c.get("id")
    return found[0].get("id") if found else None


def _fold(text):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or "")).lower()
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((feat|with|ft)[^)]*\)|\[[^\]]*\]|\s-\s.*(remaster|version|mix|edit).*$", "", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def first_artist(text):
    """'Kanye West, GLC, Consequence' -> 'Kanye West'; 'A & B', 'A feat. B', 'A x B' -> 'A'."""
    return re.split(r"\s*(?:,|&|;|\bfeat\.?|\bft\.?|\bwith\b|\sx\s)\s*", str(text or ""), maxsplit=1, flags=re.I)[0].strip()


def find_track(dz, artist, title, album=""):
    """The Deezer song for an artist + title, as (album_id, artist_id, song title) or None. A song only counts
    when its title matches AND one of its artists is the one named; an album title that matches wins a tie.
    Anything looser is a different song, so it is refused."""
    if not (artist or "").strip() or not (title or "").strip():
        return None
    q = urllib.parse.quote(f"{first_artist(artist)} {title}".strip())
    found = (dz.j(f"/search/track?q={q}&limit=10") or {}).get("data") or []
    want_t, want_a, want_al = _fold(title), _fold(first_artist(artist)), _fold(album)
    best, best_score = None, 0
    for c in found:
        if not isinstance(c, dict) or not (c.get("album") or {}).get("id"):
            continue
        if _fold(c.get("title")) != want_t or not want_t:
            continue
        names = [_fold((c.get("artist") or {}).get("name"))] + [_fold(x.get("name")) for x in (c.get("contributors") or []) if isinstance(x, dict)]
        if not want_a or not any(n and (n == want_a or want_a in n or n in want_a) for n in names):
            continue
        score = 2 + (1 if want_al and _fold((c.get("album") or {}).get("title")) == want_al else 0)
        if score > best_score:
            best, best_score = c, score
    if not best:
        return None
    return best["album"]["id"], (best.get("artist") or {}).get("id"), best.get("title") or title


def web_for(args, dz):
    """Query args -> (web, error, status)."""
    kind = (args.get("kind") or "album").strip()
    try:
        if kind == "track":                     # a song: open on its record, and remember which song it was
            title, who = (args.get("title") or "").strip(), (args.get("artist") or "").strip()
            hit = find_track(dz, who, title, args.get("album") or "")
            song = {"title": title, "artist": who}
            if hit:
                web, err = album_web(dz, int(hit[0]))
                if web:
                    web["song"] = song
                    return web, None, 200
            aid = find_artist_id(dz, first_artist(who)) if who else None       # no record found: the artist instead
            if not aid:
                return None, "Could not find that song.", 404
            web, err = artist_web(dz, int(aid))
            if err:
                return None, err, 404
            web["song"] = song
            web["notes"].insert(0, "That song's record was not found, so this is the artist.")
            return web, None, 200
        if kind == "artist":
            aid = args.get("id") or find_artist_id(dz, args.get("name") or "")
            if not aid:
                return None, "That artist was not found.", 404
            web, err = artist_web(dz, int(aid))
        else:
            aid = args.get("id") or find_album_id(dz, args.get("artist"), args.get("album"))
            if not aid:
                return None, "That album was not found on Deezer.", 404
            web, err = album_web(dz, int(aid))
    except (TypeError, ValueError):
        return None, "That is not a valid id.", 400
    if err:
        return None, err, 404
    return web, None, 200


# ── hearing a record before deciding ───────────────────────────────────────

def safe_preview(url):
    """A preview address only if it is https and on Deezer's own audio host; otherwise ''.
    The page plays whatever this returns, so it must never be someone else's address."""
    try:
        u = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if u.scheme == "https" and (host == "dzcdn.net" or host.endswith(".dzcdn.net")):
        return u.geturl()
    return ""


def _preview_row(t, sub=""):
    if not isinstance(t, dict) or not t.get("title"):
        return None
    return {"title": t["title"], "sub": sub or ((t.get("artist") or {}).get("name") or ""),
            "duration_sec": t.get("duration") or None, "preview": safe_preview(t.get("preview")),
            "url": t.get("link") or ""}


def preview_for(dz, kind, rid):
    """Thirty-second previews to help decide: an album's songs in order, or an artist's best-known songs.
    Returns (items, note, error). Songs with no preview are kept (so the list is the whole record)
    but cannot be played."""
    if kind == "artist":
        data = (dz.j(f"/artist/{int(rid)}/top?limit=8") or {}).get("data")
        rows = [_preview_row(t, (t.get("album") or {}).get("title") or "") for t in data or []]
    else:
        data = (dz.j(f"/album/{int(rid)}/tracks?limit=60") or {}).get("data")
        rows = [_preview_row(t) for t in data or []]
    rows = [r for r in rows if r]
    if not rows:
        return [], "", ("Deezer would not list any songs just now." if dz.errors else "No songs are listed for this.")
    note = "" if any(r["preview"] for r in rows) else "No previews are available for this one."
    return rows, note, None


def register(app):
    from flask import jsonify, redirect, request

    hooks = {"get": None}                     # tests replace this so nothing touches the network
    app.config["CONNECTIONS_HOOKS"] = hooks

    @app.route("/connections")
    def connections_page():
        """The map lives on the wall now. An old address (a bookmark, an old Web block's "explore", a link from Find it)
        lands there: with a record in the address it is put on the wall, otherwise the Music panel opens."""
        keep = {k: request.args[k] for k in ("kind", "id", "artist", "album", "title", "name") if request.args.get(k)}
        if keep:
            return redirect("/room?wall=1&seed=" + urllib.parse.quote(urllib.parse.urlencode(keep), safe=""))
        return redirect("/room?wall=1&music=1")

    @app.route("/api/connections/preview")
    def api_connections_preview():
        kind = "artist" if request.args.get("kind") == "artist" else "album"
        try:
            rid = int(request.args.get("id") or "")
        except ValueError:
            return jsonify({"error": "That is not a valid id."}), 400
        items, note, err = preview_for(Deezer(hooks["get"]), kind, rid)
        if err:
            return jsonify({"error": err}), 404
        return jsonify({"items": items, "note": note})

    @app.route("/api/connections")
    def api_connections():
        dz = Deezer(hooks["get"])
        web, err, status = web_for(request.args, dz)
        if err:
            return jsonify({"error": err}), status
        return jsonify(web)
