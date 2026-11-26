"""find_music.py — "Find it": paste a YouTube link, say what it is, and bring the music home.

Link-only: nothing is downloaded. This reads what a video's public page already says (title,
channel, length, description) and helps work out what it is:

  album  one artist's record uploaded whole          -> find the real album on Spotify
  mix    a DJ mix / compilation with many artists    -> find each track on Spotify
  talk   a lecture, talk, film or audiobook          -> not music; kept as moments for a wall

The page asks the person which of the three it is; `guess()` only pre-selects the likely one.
Spotify itself is searched by the page through the app's own /api/spotify/search, so a single
connection and one rate limiter serve everything.

Routes:
  GET  /find                 the page
  POST /api/find/read        {url, tracklist_text?} -> what the video says, a guess, a tracklist if found
"""
import json
import re
import urllib.parse
import urllib.request

USER_AGENT = "MusicRoom/1.0 (personal music notebook; link-only)"
MAX_ENTRIES = 300

# ── YouTube ids ────────────────────────────────────────────────────────────

_ID = r"[A-Za-z0-9_-]{11}"
_YT_PATTERNS = (
    re.compile(rf"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:[^#]*&)?v=({_ID})"),
    re.compile(rf"youtu\.be/({_ID})"),
    re.compile(rf"(?:youtube\.com|youtube-nocookie\.com)/(?:embed|shorts|live|v)/({_ID})"),
)


def parse_youtube_id(url):
    """The 11-character video id in a YouTube link, or None."""
    s = (url or "").strip()
    if not s:
        return None
    if re.fullmatch(_ID, s):
        return s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    host = urllib.parse.urlparse(s).hostname or ""
    if not re.search(r"(^|\.)(youtube\.com|youtube-nocookie\.com|youtu\.be)$", host):
        return None
    for pat in _YT_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


# ── times and tracklists ───────────────────────────────────────────────────

def parse_time(text):
    """'5:58' -> 358.0, '1:02:03' -> 3723.0, else None."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", (text or "").strip())
    if not m:
        return None
    a, b, c = m.groups()
    if c is None:
        return float(int(a) * 60 + int(b))
    return float(int(a) * 3600 + int(b) * 60 + int(c))


_TS = r"\(?\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\)?"
_LEAD = re.compile(rf"^\s*(?:[-*•\d]+[.)]\s+)?{_TS}\s*[-–—:|]?\s*(.+?)\s*$")
_TRAIL = re.compile(rf"^\s*(.+?)\s*[-–—:|]?\s*{_TS}\s*$")


def parse_timestamped_lines(text):
    """'0:00 Intro', '[05:58] - Disclaimers' or 'Intro 0:00' -> [{start_s, title}] in time order.
    Lines with no time are ignored. A title is never invented."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 300:
            continue
        m = _LEAD.match(line)
        if m:
            ts, title = m.group(1), m.group(2)
        else:
            m = _TRAIL.match(line)
            if not m:
                continue
            title, ts = m.group(1), m.group(2)
        start = parse_time(ts)
        title = title.strip(" -–—:|")
        if start is None or not title or (start, title) in seen:
            continue
        seen.add((start, title))
        out.append({"start_s": start, "title": title})
    out.sort(key=lambda e: e["start_s"])
    return out


_SPLIT = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")


def split_artist_title(label):
    """'Artist - Title' -> ('Artist', 'Title'). With no separator: ('', label)."""
    s = re.sub(r"^\s*\d{1,3}[.)]\s+", "", (label or "").strip())      # a leading track number
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)                           # a trailing [label]
    parts = _SPLIT.split(s, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", s.strip()


def parse_numbered_lines(text):
    """'1. Title' lines (no times) -> [titles], only when they run 1, 2, 3 … in order, so that a
    paragraph that happens to start with a number is not mistaken for a tracklist."""
    got = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d{1,3})[.)]\s+(.+?)\s*$", line)
        if m and len(m.group(2)) <= 200:
            got.append((int(m.group(1)), m.group(2)))
    if len(got) < 3 or [n for n, _ in got] != list(range(got[0][0], got[0][0] + len(got))) or got[0][0] > 1:
        return []
    return [t for _, t in got]


def entries_from_timestamped(items):
    """[{start_s, title}] -> entries. If most titles read 'Artist - Title' they are tracks, else chapters."""
    split = [split_artist_title(i["title"]) for i in items]
    track_like = sum(1 for a, _ in split if a)
    is_tracklist = bool(items) and track_like >= max(2, int(len(items) * 0.6))
    out = []
    for i, it in enumerate(items[:MAX_ENTRIES]):
        artist, title = split[i]
        out.append({"start_s": it["start_s"],
                    "end_s": items[i + 1]["start_s"] if i + 1 < len(items) else None,
                    "kind": "track" if is_tracklist else "chapter",
                    "title": title if is_tracklist else it["title"],
                    "artist": artist if is_tracklist else ""})
    return out


# ── what is it? ────────────────────────────────────────────────────────────

_ALBUM_WORDS = re.compile(r"\b(full album|full ep|full lp|album stream|entire album|complete album|\bep\b|\blp\b)\b", re.I)
_MIX_WORDS = re.compile(r"\b(mix|dj set|set|radio show|mixtape|boiler room|cercle|compilation|selection|selects|megamix|live set)\b", re.I)
_TALK_WORDS = re.compile(r"\b(lecture|talk|interview|documentary|essay|analysis|analy[sz]ing|audiobook|podcast|"
                         r"explained|keynote|seminar|conversation|film|movie|video essay|ted|masterclass|chapter)\b", re.I)


def album_guess(title, author):
    """'ARTIST - ALBUM (Full Album)' -> (artist, album). The channel stands in for a missing artist.
    Either may be '' — the person can correct it on the page."""
    t = (title or "").strip()
    t = re.sub(r"[\(\[][^\)\]]*(full|album|ep|lp|stream|official|\d{4})[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[|•].*$", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|")
    a, b = split_artist_title(t)
    if a and b:
        return a, b
    return (author or "").strip(), t


def guess(meta, entries=None, numbered=None):
    """What this video most likely is: {'kind': 'album'|'mix'|'talk', 'why': plain words}.
    A pre-selection only; the person decides."""
    title = meta.get("title") or ""
    desc = meta.get("description") or ""
    dur = meta.get("duration_s") or 0
    entries = entries or []
    artists = {e["artist"].lower() for e in entries if e.get("kind") == "track" and e.get("artist")}
    if len(artists) >= 3:
        return {"kind": "mix", "why": f"A tracklist with {len(artists)} different artists."}
    if _ALBUM_WORDS.search(title):
        return {"kind": "album", "why": "The title says it is a full album."}
    if _TALK_WORDS.search(title) and not _MIX_WORDS.search(title):
        return {"kind": "talk", "why": "The title reads like a talk, lecture or film."}
    if _MIX_WORDS.search(title):
        return {"kind": "mix", "why": "The title says it is a mix or a set."}
    if numbered and len(numbered) >= 3 and dur and 600 <= dur <= 5400:
        return {"kind": "album", "why": "A numbered track list and about the length of a record."}
    if entries and len(artists) <= 1 and not any(e.get("kind") == "track" for e in entries):
        return {"kind": "talk", "why": "Chapters, not tracks."}
    if dur and dur > 5400:
        return {"kind": "talk", "why": "Long, with no track list."}
    return {"kind": "album", "why": "Could not tell; a single record is the common case."}


# ── reading a video's public page ──────────────────────────────────────────

def _http_get(url, timeout=15, limit=3_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def fetch_meta(video_id, http_get=None):
    """What the video's public page says: title, channel, length, thumbnail, description.
    oEmbed first (the official door); the page's own data adds length and description.
    Anything unreachable is left out, never invented."""
    http_get = http_get or _http_get
    watch = f"https://www.youtube.com/watch?v={video_id}"
    meta = {"video_id": video_id, "url": f"https://youtu.be/{video_id}", "title": None, "author": None,
            "duration_s": None, "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "description": "", "notes": []}
    try:
        o = json.loads(http_get("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(watch, safe="")))
        meta["title"], meta["author"] = o.get("title"), o.get("author_name")
        meta["thumbnail_url"] = o.get("thumbnail_url") or meta["thumbnail_url"]
    except Exception:
        meta["notes"].append("Could not read the title from YouTube.")
    try:
        page = http_get(watch)
        m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*(?:var|</script>)", page, re.S)
        if m:
            vd = (json.loads(m.group(1)) or {}).get("videoDetails", {})
            meta["title"] = meta["title"] or vd.get("title")
            meta["author"] = meta["author"] or vd.get("author")
            if vd.get("lengthSeconds"):
                meta["duration_s"] = float(vd["lengthSeconds"])
            meta["description"] = vd.get("shortDescription") or ""
        else:
            meta["notes"].append("Could not read the description from the page.")
    except Exception:
        meta["notes"].append("Could not read the video page, so no description or length.")
    return meta


def read(url, pasted="", http_get=None):
    """Everything the page needs after a link is pasted. Returns (result, error)."""
    vid = parse_youtube_id(url)
    if not vid:
        return None, "That does not look like a YouTube link."
    meta = fetch_meta(vid, http_get)
    text = (pasted or "")[:100_000]
    source_text = text if text.strip() else (meta.get("description") or "")
    entries = entries_from_timestamped(parse_timestamped_lines(source_text))
    numbered = [] if entries else parse_numbered_lines(source_text)
    g = guess(meta, entries, numbered)
    artist, album = album_guess(meta.get("title"), meta.get("author"))
    return {"meta": meta, "guess": g, "entries": entries, "numbered": numbered,
            "from": "pasted" if text.strip() else "description",
            "album_guess": {"artist": artist, "album": album}}, None


_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{10,30}$")


def album_items(data, image=""):
    """A Spotify album's tracks answer ({items:[…]}) -> the rows a grúpa takes, in album order.
    Rows with no name or no link are left out; nothing is invented."""
    out = []
    for t in (data or {}).get("items") or []:
        if not isinstance(t, dict):
            continue
        name = (t.get("name") or "").strip()
        url = ((t.get("external_urls") or {}).get("spotify") or "").strip()
        if not url and t.get("id"):
            url = "https://open.spotify.com/track/" + str(t["id"])
        if not (name and url):
            continue
        artists = [(a.get("name") or "").strip() for a in (t.get("artists") or []) if isinstance(a, dict)]
        out.append({"url": url, "title": name, "subtitle": ", ".join(a for a in artists if a),
                    "image": image or "", "by_ids": ",".join(str(a.get("id")) for a in (t.get("artists") or [])
                                                             if isinstance(a, dict) and a.get("id")),
                    "duration_sec": round((t.get("duration_ms") or 0) / 1000.0, 1) or None})
    return out


def register(app, get_spotify=None):
    from flask import jsonify, render_template, request

    hooks = {"http_get": None}                # tests replace this so nothing touches the network
    app.config["FIND_MUSIC_HOOKS"] = hooks

    @app.route("/find")
    def find_page():
        return render_template("find.html")

    @app.route("/api/find/read", methods=["POST"])
    def api_find_read():
        d = request.get_json(silent=True) or {}
        res, err = read(str(d.get("url") or ""), str(d.get("tracklist_text") or ""), hooks["http_get"])
        if err:
            return jsonify({"error": err}), 400
        return jsonify(res)

    @app.route("/api/find/album-tracks")
    def api_find_album_tracks():
        """The songs on a Spotify album, ready for a grúpa. Spotify limits what apps in Development
        Mode may read, so a refusal is passed on in plain words and the page falls back to the whole album."""
        aid = (request.args.get("id") or "").strip()
        if not _SPOTIFY_ID.match(aid):
            return jsonify({"error": "That is not a Spotify album."}), 400
        spo = get_spotify() if get_spotify else None
        if spo is None or not spo.is_connected():
            return jsonify({"error": "Spotify is not connected."}), 401
        image = (request.args.get("image") or "")[:500]
        items, offset = [], 0
        while offset < 500:
            data = spo._api_get(f"/albums/{aid}/tracks", {"limit": 50, "offset": offset})
            if "error" in data:
                return jsonify({"error": data.get("error"), "status": data.get("status") or 502}), 200
            items += album_items(data, image)
            if not data.get("next"):
                break
            offset += 50
        return jsonify({"items": items, "total": len(items)})
