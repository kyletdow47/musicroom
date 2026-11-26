"""
=============================================================
MUSIC ROOM — app.py
=============================================================
Routes:
    GET  /                          — the forest (home)
    GET  /place/<id>                — a specific place
    GET  /track/<id>                — track detail / file management
    GET  /sets                      — all sets
    GET  /set/<id>                  — set builder
    GET  /learn                     — learn page (quiz, stats, map)

API — Tracks, Files, Places, Sets, Tags, Analysis, Rekordbox
    See inline docstrings for each endpoint.

API — Learn
    GET  /api/learn/stats
    GET  /api/learn/artists
    GET  /api/learn/artist/<name>
    POST /api/learn/enrich_artist
    POST /api/learn/quiz/generate
    POST /api/learn/quiz/save
    GET  /api/learn/quiz/history
    GET  /api/learn/quiz/review/<session_id>

How to run:
    cd ~/Projects/music-room && source venv/bin/activate
    python3 migrate.py && python3 app.py
    Open http://127.0.0.1:5000
=============================================================
"""

import sqlite3
import os
import shutil
import mimetypes
# What kind of file a .js or .css is must not depend on the computer: on Windows Python takes
# it from the registry, and where that says ".js is plain text" the browser refuses the 3D
# engine's module and My Room stops at "Could not load the 3D engine".
for _ext, _kind in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                    (".json", "application/json"), (".svg", "image/svg+xml"), (".woff2", "font/woff2"),
                    (".woff", "font/woff"), (".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp")):
    mimetypes.add_type(_kind, _ext)
from pathlib import Path
import uuid
import subprocess
import threading
import xml.etree.ElementTree as ET
import json as _json
import re as _re
import time as _time
import urllib.request
import urllib.error
import random
from flask import (
    Flask, render_template, jsonify,
    request, abort, g, Response, send_file, send_from_directory, redirect
)
from tag_writer import write_tags_to_file
import mr_os            # opening, revealing and choosing — on a Mac or on Windows
import mr_text          # names as plain letters: accents off, the letter kept
import genre_suggest
import green_room
import recorder as mr_recorder

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))

# Writable user data (DB, uploaded media, exports, recordings) lives in the
# shared data dir — the project folder in development, or
# ~/Library/Application Support/Music Room when running as a bundled .app.
# Code, templates and static *assets* stay under BASE_DIR. See mr_paths.py.
import mr_paths
mr_paths.ensure_data_dirs()
DATA_DIR        = mr_paths.DATA_DIR
DB_PATH         = mr_paths.DB_PATH
VOICE_NOTES_DIR = mr_paths.VOICE_NOTES_DIR
MOODBOARD_DIR   = mr_paths.MOODBOARD_DIR
AMBIENT_DIR     = mr_paths.AMBIENT_DIR
PLACE_BG_DIR    = mr_paths.PLACE_BG_DIR
RECORDINGS_DIR  = mr_paths.RECORDINGS_DIR

# static_folder=None disables Flask's built-in /static rule so we can serve
# user media (which lives in the data dir) and code assets (which live in the
# bundle) from one overlay route below.
app = Flask(__name__, static_folder=None)
# A dev aid: MR_TEMPLATE_RELOAD=1 makes a copy of the app started without
# debug (the preview servers) re-read a template when its file changes, so a
# template edit doesn't need a restart. Off unless asked for.
if os.environ.get("MR_TEMPLATE_RELOAD") == "1":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

_CODE_STATIC = os.path.join(BASE_DIR, "static")
_DATA_STATIC = mr_paths.DATA_STATIC_DIR


# ── Only the app's own pages may use the app (2026-11-20, before opening the code) ──
# The app answers on 127.0.0.1 and asks for no password: it is yours, on your
# laptop. So any other web page you happen to open — or a plug-in, or the code
# in someone's public room — could otherwise send it requests ("delete this",
# "play that"). A browser says where a request comes from (Sec-Fetch-Site,
# Origin); anything from elsewhere is refused, except reading a picture or a
# file a plug-in is made of, and Spotify's answer when you sign in there.
_CROSS_OK_GET = ("/static/", "/api/gr/art/", "/api/gr/asset/", "/api/artwork/", "/plugins/files/",
                 "/api/spotify/callback", "/favicon")


@app.before_request
def _own_pages_only():
    site = (request.headers.get("Sec-Fetch-Site") or "").lower()
    origin = request.headers.get("Origin")
    own = request.host_url.rstrip("/")
    foreign = site in ("cross-site", "same-site") or origin == "null" or (
        origin is not None and origin.rstrip("/") != own and origin not in ("", "null"))
    if not foreign:
        return None
    if request.method in ("GET", "HEAD") and request.path.startswith(_CROSS_OK_GET):
        return None
    return jsonify({"error": "this app only answers its own pages"}), 403


# ── A public room's page: what it may load (2026-11-20) ──
# Someone else's room is shown inside YOUR app, so its own CSS and code are held
# to this: pictures from the app itself (the room's own uploads, record covers)
# or the streaming services' picture servers; no fonts, scripts, sound or
# frames from anywhere else — so a room can't play music from just anywhere
# (only from the Music Room and the licensed services' own players), can't
# load a tracker, and can't talk to anything but this app.
_ROOM_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://open.spotify.com https://open.spotifycdn.com https://w.soundcloud.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https://i.scdn.co https://*.scdn.co https://*.spotifycdn.com"
    " https://*.sndcdn.com https://i.ytimg.com https://*.bcbits.com",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "frame-src 'self' https://open.spotify.com https://w.soundcloud.com https://www.youtube.com"
    " https://www.youtube-nocookie.com https://bandcamp.com https://www.mixcloud.com https://player.vimeo.com",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
])


@app.after_request
def _room_page_rules(resp):
    if request.path.startswith("/rooms/"):
        resp.headers["Content-Security-Policy"] = _ROOM_CSP
    return resp


@app.route("/static/<path:filename>", endpoint="static")
def _static_overlay(filename):
    """Serve /static/* from the data dir first (uploaded moodboard / ambient /
    artwork / stickers / overlays) then fall back to the bundled code assets
    (css / js / fonts / textures). In dev both roots are the same folder, so
    behaviour is unchanged."""
    data_candidate = os.path.join(_DATA_STATIC, filename)
    if os.path.isfile(data_candidate):
        return send_from_directory(_DATA_STATIC, filename)
    return send_from_directory(_CODE_STATIC, filename)

# =============================================================
# CAMELOT WHEEL MAP
# Used in analysis display and Rekordbox export.
# =============================================================

CAMELOT_MAP = {
    "C major":  "8B",  "G major":  "9B",  "D major": "10B", "A major": "11B",
    "E major": "12B",  "B major":  "1B",  "F# major": "2B", "Db major": "3B",
    "Ab major": "4B",  "Eb major": "5B",  "Bb major": "6B", "F major":  "7B",
    "A minor":  "8A",  "E minor":  "9A",  "B minor": "10A", "F# minor": "11A",
    "C# minor": "12A", "G# minor": "1A",  "D# minor": "2A", "Bb minor": "3A",
    "F minor":  "4A",  "C minor":  "5A",  "G minor":  "6A", "D minor":  "7A",
}


# =============================================================
# ANALYSIS WORKER — runs librosa in a background thread
# =============================================================

_analysis_running = {}   # file_id -> status string


def _run_analysis_background(file_id, file_path):
    global _analysis_running
    _analysis_running[file_id] = "running"
    try:
        from importer import analyse_audio
        analysis = analyse_audio(file_path)
        a = analysis or {}
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE files SET
                bpm = :bpm, key_note = :key_note, key_scale = :key_scale,
                key_confidence = :key_confidence,
                loudness_rms = :loudness_rms, dynamic_range = :dynamic_range,
                spectral_centroid = :spectral_centroid,
                band_sub = :band_sub, band_bass = :band_bass, band_lowmid = :band_lowmid,
                band_body = :band_body, band_presence = :band_presence,
                band_brilliance = :band_brilliance, brightness_score = :brightness_score,
                has_analysis = :has_analysis,
                analysis_status = 'done', updated_at = datetime('now')
            WHERE id = :file_id
        """, {
            "bpm": a.get("bpm"), "key_note": a.get("key_note"),
            "key_scale": a.get("key_scale"), "key_confidence": a.get("key_confidence"),
            "loudness_rms": a.get("loudness_rms"),
            "dynamic_range": a.get("dynamic_range"),
            "spectral_centroid": a.get("spectral_centroid"),
            "band_sub": a.get("band_sub"), "band_bass": a.get("band_bass"),
            "band_lowmid": a.get("band_lowmid"), "band_body": a.get("band_body"),
            "band_presence": a.get("band_presence"), "band_brilliance": a.get("band_brilliance"),
            "brightness_score": a.get("brightness_score"),
            "has_analysis": a.get("has_analysis", 0), "file_id": file_id,
        })
        conn.commit()
        conn.close()
        _analysis_running[file_id] = "done"
    except Exception as e:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE files SET analysis_status = 'failed' WHERE id = ?", (file_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        _analysis_running[file_id] = "failed"
        print(f"  [analysis] failed for file {file_id}: {e}")


# =============================================================
# WHISPER — local model cache + workers
# 'base' model ~150MB. Loaded once, reused for all transcription.
# =============================================================

_whisper_model = None
_whisper_lock  = threading.Lock()

def _get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper as _w
            _whisper_model = _w.load_model("base")
        return _whisper_model


def _run_whisper_background(note_id, audio_path):
    """Transcribe a voice note with Whisper and save result to notes.whisper_transcript."""
    try:
        model  = _get_whisper_model()
        result = model.transcribe(audio_path, fp16=False)
        transcript = (result.get("text") or "").strip()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE notes SET whisper_transcript = ? WHERE id = ?",
            (transcript, note_id)
        )
        conn.commit()
        conn.close()
        print(f"  [whisper] note {note_id}: ok — \"{transcript[:60]}{'...' if len(transcript) > 60 else ''}\"")
    except ImportError:
        print(f"  [whisper] not installed — run: pip install openai-whisper")
    except Exception as e:
        print(f"  [whisper] note {note_id}: failed — {e}")


# =============================================================
# DATABASE
# =============================================================

def _apply_concurrency_pragmas(conn):
    """Idempotent. WAL mode + busy_timeout = good concurrent batch
    behaviour. The WAL switch requires an exclusive lock momentarily,
    so we tolerate failure (other connections might still be open) —
    once the DB file has been switched to WAL it sticks across opens.
    busy_timeout *always* applies and is the more important guard."""
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # Already-locked? Fine. WAL is per-file, will take effect later.
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.OperationalError:
        pass


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        # mrfold(x): x in plain lower-case letters, accents off — so a search for "roisin" finds
        # Róisín (the database's own LIKE and LOWER() only know A–Z)
        g.db.create_function("mrfold", 1, mr_text.fold, deterministic=True)
        g.db.execute("PRAGMA foreign_keys = ON")
        _apply_concurrency_pragmas(g.db)
    return g.db


def _connect_threadsafe(path=None):
    """Open a SQLite connection suitable for use from a background batch
    thread. WAL + busy_timeout means writes from this connection won't
    bounce off the journal lock when another batch is mid-write."""
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_concurrency_pragmas(conn)
    return conn


# One-shot at module load: switch the DB file to WAL the moment the app
# imports, so subsequent connections inherit it. Tolerates failure for
# the same reason as above (another process might still have a hold).
try:
    _wal_init = sqlite3.connect(DB_PATH, timeout=10)
    _apply_concurrency_pragmas(_wal_init)
    _wal_init.close()
    del _wal_init
except Exception:
    pass


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=()):
    return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    row = get_db().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql, params=()):
    db  = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def _table_exists(name):
    return query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def get_setting(key, default=''):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


# Defaults for the backup + "removed from Music Room" folders. These used to
# be hardcoded to the developer's external drive; now they default to the
# writable data dir so they work on any machine, and either can be overridden
# in Settings (backup_dir / removed_dir).
HD_BACKUP_DIR    = Path(DATA_DIR) / "backups"

def _removed_dir() -> Path:
    """Where 'Remove from Music Room' parks files. Settings override → default
    under the data dir. Created on demand."""
    configured = get_setting("removed_dir", "").strip()
    p = Path(configured) if configured else (Path(DATA_DIR) / "Removed from Music Room")
    return p

# Backup state (in-process, not persisted)
_backup_running  = False
_backup_lock     = threading.Lock()


# The database is never copied as a plain file while the app has it open. A copy made
# mid-write can be torn or out of date (what was saved last still sits in its -wal side
# file), and those side files come and go under rsync: on 24 Nov 2026 a launch backup was
# skipped whole ("library.db-wal: No such file"). Found by Kristian Vastveit
# (docs/RECOMMENDATIONS.md). SQLite makes the copy itself (_copy_database_to), and rsync
# is told to leave these names alone.
_DB_FILE = os.path.basename(DB_PATH)
_DB_SIDE = ("-wal", "-shm", "-journal")
_BACKUP_DB_EXCLUDES = [f"--exclude=/{_DB_FILE}", f"--exclude=/{_DB_FILE}-*", f"--exclude=/{_DB_FILE}.copying*"]


def _copy_database_to(dest_dir):
    """A whole copy of the database, as it stands at this moment, into dest_dir — made by
    SQLite (Connection.backup), so it is right even while the app is writing. It is
    written beside its place, checked, and only then put in place: stopped at any point,
    the copy from last time is still there. Returns (ok, err)."""
    dest = Path(dest_dir) / _DB_FILE
    part = dest.with_name(_DB_FILE + ".copying")
    try:
        if not os.path.isfile(DB_PATH):
            return True, None                           # no database yet: nothing to copy
        if dest.exists() and os.path.samefile(dest, DB_PATH):
            return True, None                           # the data folder itself: never onto itself
        for s in ("",) + _DB_SIDE:                      # what a copy that was cut short left
            Path(str(part) + s).unlink(missing_ok=True)
        src = sqlite3.connect(DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(str(part))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        look = sqlite3.connect(part.resolve().as_uri() + "?immutable=1", uri=True)
        try:
            good = look.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        finally:
            look.close()
        if not good:
            part.unlink(missing_ok=True)
            return False, "the copy of the database did not pass its check; the one from last time was kept"
        for s in _DB_SIDE:                              # side files from an older backup belong to an older
            Path(str(dest) + s).unlink(missing_ok=True) # database: they must not sit beside this one
            Path(str(part) + s).unlink(missing_ok=True)
        os.replace(part, dest)
        return True, None
    except Exception as e:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        return False, f"the database was not copied: {e}"


# DB + metadata only — never audio (music lives on the drive's Library), never
# the build / recovery snapshots / the backup folder itself. --delete ⇒ the
# destination is a single, most-recent mirror. (Not --delete-excluded: that would
# delete the database copy SQLite has just put there, since rsync is told to pass
# over it. Nothing on the list below is ever copied, so there is nothing of it to clear.)
_BACKUP_RSYNC_FLAGS = [
    "-a", "--delete",
    "--exclude=venv/", "--exclude=__pycache__/", "--exclude=*.pyc",
    "--exclude=.DS_Store", "--exclude=.git/", "--exclude=backups/",
    "--exclude=native/", "--exclude=*.before_*",
    "--exclude=*.mp3", "--exclude=*.flac", "--exclude=*.wav", "--exclude=*.aif",
    "--exclude=*.aiff", "--exclude=*.m4a", "--exclude=*.ogg", "--exclude=*.opus",
    "--exclude=*.aac",
]

def _rsync_data_to(dest_path):
    """Mirror DATA_DIR (DB + metadata) to one destination. Returns (ok, err)."""
    dest_path = Path(dest_path)
    parts = dest_path.parts
    if len(parts) >= 3 and parts[1] == "Volumes" and not (Path("/Volumes") / parts[2]).exists():
        return False, f"volume not mounted: /Volumes/{parts[2]}"
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
        db_ok, db_err = _copy_database_to(dest_path)    # the database first, by SQLite …
        src = str(DATA_DIR).rstrip("/") + "/"
        r = subprocess.run(["rsync", *_BACKUP_RSYNC_FLAGS, *_BACKUP_DB_EXCLUDES, src, str(dest_path) + "/"],
                           capture_output=True, text=True, timeout=600)   # … then everything else
        if db_ok and os.path.isfile(DB_PATH) and not (dest_path / _DB_FILE).exists():
            db_ok, db_err = _copy_database_to(dest_path)    # (an rsync that cleared it all the same)
        if not db_ok:
            return False, db_err
        return (r.returncode in (0, 24)), (None if r.returncode in (0, 24) else r.stderr[:300])
    except Exception as e:
        return False, str(e)

def _drive_appdata_dir():
    """Portable Music Room data folder on the LIBRARY's drive — so the drive
    carries a current DB + metadata right next to the music. None if the library
    isn't on a mounted /Volumes drive. (Thread-safe: no Flask context.)"""
    lib = (_get_setting_threadsafe("library_dest", "") or "").strip()
    if not lib:
        try:
            with sqlite3.connect(DB_PATH) as c:
                r = c.execute("SELECT file_path FROM files WHERE is_deleted=0 "
                              "AND file_path LIKE '%/Music Room Library/%' ORDER BY id LIMIT 1").fetchone()
            if r and r[0]:
                m = "/Music Room Library/"; i = r[0].find(m)
                if i != -1: lib = r[0][:i + len(m) - 1]
        except Exception:
            return None
    parts = Path(lib).parts
    if len(parts) >= 3 and parts[1] == "Volumes" and (Path("/Volumes") / parts[2]).exists():
        return str(Path("/Volumes") / parts[2] / mr_paths.DRIVE_APPDATA_FOLDER)
    return None

def _run_backup(dest_dir=None):
    """Back up DATA_DIR (DB + metadata) every launch to BOTH the laptop AND the
    library's drive (so the drive is a portable, self-contained Music Room) — or
    to one explicit dest_dir if given. Each destination is a single most-recent
    mirror, never accumulating."""
    global _backup_running
    with _backup_lock:
        if _backup_running:
            return
        _backup_running = True
    try:
        if dest_dir:
            targets = [str(dest_dir)]
        else:
            configured = (_get_setting_threadsafe("backup_dir", "") or "").strip()
            laptop = configured or str(HD_BACKUP_DIR)
            targets = [laptop]
            # Mirror to the drive only in "external drive" mode. Explicit 'local'
            # keeps everything on this computer; unset → infer (mirror if the
            # library actually lives on a drive), preserving prior behaviour.
            drive = _drive_appdata_dir()
            ml = (_get_setting_threadsafe("music_location", "") or "").strip()
            use_drive = (ml == "external") or (ml not in ("local", "external") and drive is not None)
            if use_drive and drive and os.path.normpath(drive) != os.path.normpath(laptop):
                targets.append(drive)
        ok_any = False
        for t in targets:
            ok, err = _rsync_data_to(t)
            if ok:
                ok_any = True
                print(f"[backup] done → {t}")
            else:
                print(f"[backup] skipped {t}: {err}")
        if ok_any:
            ts = _now_iso()
            with sqlite3.connect(DB_PATH) as _c:
                _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_backup_at', ?)", (ts,))
                _c.commit()
    except Exception as e:
        print(f"[backup] exception: {e}")
    finally:
        with _backup_lock:
            _backup_running = False


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_to_drive(dest_dir=None, block=False):
    """Kick off a background backup. If block=True, wait for it (for API use)."""
    t = threading.Thread(target=_run_backup, args=(dest_dir,), daemon=True)
    t.start()
    if block:
        t.join()


# ─── FULL backup: app data + ALL music files → a second external drive ──────
# This is the real backup Daithí runs: plug in a spare drive, press the button,
# and everything (database, metadata, AND the whole Music Room Library audio)
# is mirrored onto it. rsync --delete + skip-unchanged → fast on repeat runs.
_full_backup_state = {"running": False, "phase": "", "error": "", "at": ""}
_full_backup_lock = threading.Lock()

def _run_full_backup(dest_root):
    global _full_backup_state
    with _full_backup_lock:
        if _full_backup_state["running"]:
            return
        _full_backup_state = {"running": True, "phase": "starting", "error": "", "at": ""}
    try:
        dest_root = (dest_root or "").strip()
        if not dest_root:
            raise ValueError("No backup drive chosen")
        dest = Path(dest_root)
        # Must be a mounted external volume — never silently back up to the laptop.
        parts = dest.parts
        if not (len(parts) >= 3 and parts[1] == "Volumes" and (Path("/Volumes") / parts[2]).exists()):
            raise ValueError(f"Backup drive not mounted: {dest_root}")
        # get_library_dest() is request-bound (Flask g); resolve thread-safely here.
        library = (_get_setting_threadsafe("library_dest", "") or "").strip()
        if not library:
            # infer from a library file, same logic as get_library_dest()
            with sqlite3.connect(DB_PATH) as _c:
                r = _c.execute("SELECT file_path FROM files WHERE is_deleted=0 "
                               "AND file_path LIKE '%/Music Room Library/%' ORDER BY id LIMIT 1").fetchone()
            if r and r[0]:
                m = "/Music Room Library/"; i = r[0].find(m)
                if i != -1: library = r[0][: i + len(m) - 1]
        # Don't back a drive up onto itself.
        if library and os.path.normpath(library).startswith(os.path.normpath(str(dest)) + os.sep):
            raise ValueError("Backup drive is inside the music library — choose a different drive")

        def _rsync(src, sub, also=()):
            d = dest / sub
            d.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["rsync", "-a", "--delete", "--exclude=.DS_Store", "--exclude=.Trashes/",
                 "--exclude=venv/", "--exclude=__pycache__/", "--exclude=*.pyc",
                 "--exclude=.git/", "--exclude=backups/", "--exclude=*.before_*", *also,
                 str(src).rstrip("/") + "/", str(d) + "/"],
                capture_output=True, text=True, timeout=7200)
            if r.returncode not in (0, 24):   # 24 = some source files vanished mid-copy; tolerable
                raise RuntimeError(f"rsync failed ({r.returncode}): {r.stderr[:300]}")

        with _full_backup_lock:
            _full_backup_state["phase"] = "app data"
        # (the database by SQLite, the rest by rsync: see _copy_database_to)
        (dest / mr_paths.DRIVE_APPDATA_FOLDER).mkdir(parents=True, exist_ok=True)
        db_ok, db_err = _copy_database_to(dest / mr_paths.DRIVE_APPDATA_FOLDER)
        if not db_ok:
            raise RuntimeError(db_err)
        _rsync(DATA_DIR, mr_paths.DRIVE_APPDATA_FOLDER, also=_BACKUP_DB_EXCLUDES)

        if library and os.path.isdir(library):
            with _full_backup_lock:
                _full_backup_state["phase"] = "music files"
            _rsync(library, "Music Room Library")

            # Make the backup a self-contained portable rekordbox/Engine source:
            # the mirrored musicroom_library.xml still points at the SOURCE drive,
            # so rewrite its track paths to THIS backup drive's library root.
            # (Run "Whole library → DJ XML" before backing up to keep it current.)
            try:
                bxml = dest / "Music Room Library" / "musicroom_library.xml"
                if bxml.exists():
                    with _full_backup_lock:
                        _full_backup_state["phase"] = "playlist xml"
                    src_prefix = _rb_location(library)
                    dst_prefix = _rb_location(str(dest / "Music Room Library"))
                    if src_prefix and dst_prefix and src_prefix != dst_prefix:
                        txt = bxml.read_text(encoding="utf-8")
                        bxml.write_text(txt.replace(src_prefix, dst_prefix), encoding="utf-8")
            except Exception as _xe:
                print(f"[full-backup] xml path-rewrite skipped: {_xe}")

        ts = _now_iso()
        with sqlite3.connect(DB_PATH) as _c:
            _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                       ("last_full_backup_at", ts))
            _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                       ("last_full_backup_dest", str(dest)))
            _c.commit()
        with _full_backup_lock:
            _full_backup_state = {"running": False, "phase": "done", "error": "", "at": ts}
        print(f"[full-backup] done at {ts} → {dest}")
    except Exception as e:
        with _full_backup_lock:
            _full_backup_state = {"running": False, "phase": "error", "error": str(e), "at": ""}
        print(f"[full-backup] error: {e}")


def full_backup_to_drive(dest_root):
    t = threading.Thread(target=_run_full_backup, args=(dest_root,), daemon=True)
    t.start()

EXPORTS_DIR = Path(mr_paths.EXPORTS_DIR)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Rekordbox also watches this folder — symlink/copy destination
RB_XML_DIR = Path.home() / "Library" / "rekordbox" / "rekordbox" / "MusicRoom"
MASTER_XML  = RB_XML_DIR / "music_room.xml"


def get_export_dir():
    """Return the export directory Path.
    Primary: <project>/exports/  (inside project, always backed up).
    If user has configured a custom path, use that instead."""
    configured = get_setting("export_dir", "").strip()
    if configured:
        p = Path(configured)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    return EXPORTS_DIR


# =============================================================
# TRASH HELPER
# =============================================================

from trash_util import move_to_trash   # shared, verified-safe (never hard-deletes)


def move_to_removed(file_path: str, track_id: int):
    """
    Move an audio file to the 'Removed from Music Room' folder.
    The file is kept on disk but excluded from future imports.
    Returns (ok, error_msg).
    """
    src = Path(file_path)
    if not src.exists():
        return True, None   # Already gone — treat as success
    try:
        removed_dir = _removed_dir()
        removed_dir.mkdir(parents=True, exist_ok=True)
        # Prefix with track_id to avoid filename collisions
        dest = removed_dir / f"{track_id}_{src.name}"
        counter = 1
        while dest.exists():
            dest = removed_dir / f"{track_id}_{counter}_{src.name}"
            counter += 1
        shutil.move(str(src), str(dest))
        return True, None
    except Exception as e:
        return False, str(e)


# =============================================================
# AUDIO STREAMING
# =============================================================

#  Python's mimetypes returns legacy "audio/x-flac", "audio/x-wav" etc.
#  WebKit (Safari, pywebview) refuses to play those — needs the modern
#  registered types. Map every audio extension to what the browser expects.
AUDIO_MIME = {
    "mp3":  "audio/mpeg",
    "flac": "audio/flac",
    "wav":  "audio/wav",
    "aiff": "audio/aiff",
    "aif":  "audio/aiff",
    "m4a":  "audio/mp4",
    "mp4":  "audio/mp4",
    "ogg":  "audio/ogg",
    "opus": "audio/ogg",
    "webm": "audio/webm",
}

# Some files lie about their container: a YouTube/DASH MP4+AAC rip saved as
# ".mp3", or an MP3 saved as ".flac". Served by extension they get the wrong
# Content-Type (audio/mpeg for AAC bytes), and the browser <audio> element —
# the one in the track panel — plays from the start but breaks on SEEK: the
# duration collapses and the scrubber jumps to the end. Sniff the real
# container from the magic bytes and serve the honest MIME so seeking works.
def _sniff_audio_mime(path, fallback):
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except Exception:
        return fallback
    if head[4:8] == b"ftyp":                                            real = "audio/mp4"
    elif head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3",
                                            b"\xff\xf2", b"\xff\xfa"):  real = "audio/mpeg"
    elif head[:4] == b"fLaC":                                           real = "audio/flac"
    elif head[:4] == b"OggS":                                           real = "audio/ogg"
    elif head[:4] == b"RIFF":                                           real = "audio/wav"
    else:                                                               real = None
    return real or fallback

def _is_file_safe(p) -> bool:
    """os.path.isfile that never raises. A stale / permission-denied mount can
    make the underlying stat() throw — treat any failure as 'not a usable file'
    rather than letting it 500 the request."""
    try:
        return os.path.isfile(p)
    except OSError:
        return False


# A lazily-built {lowercase basename -> full path} index of the Music Room
# Library, so a file that was MOVED/REORGANISED inside the library still
# resolves by name instead of 404-ing. Cached for a few minutes (rebuilds are
# cheap — a few thousand entries — but we don't want to walk the drive per play).
_lib_index = {"map": None, "built_at": 0.0}
_AUDIO_EXTS = (".mp3", ".flac", ".wav", ".aif", ".aiff", ".m4a", ".ogg", ".opus", ".aac")

def _library_basename_index(force: bool = False):
    import time as _t
    now = _t.time()
    if not force and _lib_index["map"] is not None and (now - _lib_index["built_at"]) < 300:
        return _lib_index["map"]
    idx = {}
    try:
        lib = get_library_dest()
        if lib and os.path.isdir(lib):
            for dp, _dirs, files in os.walk(lib):
                for fn in files:
                    if fn.lower().endswith(_AUDIO_EXTS):
                        idx.setdefault(fn.lower(), os.path.join(dp, fn))
    except Exception:
        pass
    _lib_index["map"] = idx
    _lib_index["built_at"] = now
    return idx


def _resolve_audio_path(stored_path: str) -> str | None:
    """Return a valid path for a stored file path, or None if it can't be found
    (e.g. the external drive is unplugged).
    Resolution order: (1) the exact path; (2) the same relative path under any
    currently-mounted /Volumes/* (drive remounted under a new name); (3) by
    filename anywhere in the Music Room Library (file moved/reorganised). Only
    returns None when the file genuinely isn't on disk."""
    if not stored_path:
        return None
    if _is_file_safe(stored_path):           # isfile, not exists: reject dirs / dead symlinks
        return stored_path
    # (2) Try to remap to a different mounted volume.
    parts = Path(stored_path).parts
    try:
        vol_idx = parts.index("Volumes")
        rel_parts = parts[vol_idx + 2:]      # skip "Volumes" and the <drivename>
        if rel_parts:
            rel = Path(*rel_parts)
            try:
                for vol in Path("/Volumes").iterdir():
                    candidate = vol / rel
                    if _is_file_safe(candidate):
                        return str(candidate)
            except OSError:
                pass
    except ValueError:
        pass
    # (3) Self-heal: find the file by name in the Music Room Library.
    base = os.path.basename(stored_path).lower()
    if base:
        hit = _library_basename_index().get(base)
        if hit and _is_file_safe(hit):
            return hit
        # one rebuild in case the file moved since the index was last built
        hit = _library_basename_index(force=True).get(base)
        if hit and _is_file_safe(hit):
            return hit
    return None


def _stream_file(path, mime):
    """Stream a local file with HTTP Range support (for <audio>/<video> seeking).
    Honours a `Range` request header → 206 partial; otherwise 200 full. The file
    is read in 64 KB chunks so large tracks never load fully into memory.
    Single source of truth for byte-range streaming — used by both the track
    audio route and the show-recording route."""
    size = os.path.getsize(path)
    rng  = request.headers.get("Range")
    if rng:
        s, e = 0, size - 1
        try:
            parts = rng.replace("bytes=", "").split("-")
            s = int(parts[0]) if parts[0] else 0
            e = int(parts[1]) if parts[1] else size - 1
        except Exception:
            pass
        s = max(0, s)
        e = min(e, size - 1)
        clen = max(0, e - s + 1)
        def chunk():
            with open(path, "rb") as fh:
                fh.seek(s)
                rem = clen
                while rem > 0:
                    d = fh.read(min(65536, rem))
                    if not d: break
                    yield d
                    rem -= len(d)
        resp = Response(chunk(), 206, mimetype=mime, direct_passthrough=True)
        resp.headers.update({"Content-Range": f"bytes {s}-{e}/{size}",
                             "Accept-Ranges": "bytes", "Content-Length": str(clen)})
        return resp
    def full():
        with open(path, "rb") as fh:
            while True:
                d = fh.read(65536)
                if not d: break
                yield d
    resp = Response(full(), 200, mimetype=mime)
    resp.headers.update({"Accept-Ranges": "bytes", "Content-Length": str(size)})
    return resp


@app.route("/audio/<int:file_id>")
def serve_audio(file_id):
    f = query_one("SELECT * FROM files WHERE id = ? AND is_deleted = 0", (file_id,))
    if not f: abort(404)
    if (f.get("analysis_status") or "") == "locked":
        # a Green Room borrow: never served plain to a browser — it plays
        # through the Seinnteoir, which decrypts in memory
        return jsonify({"error": "borrowed record — plays through the Player"}), 403
    fp = _resolve_audio_path(f["file_path"])
    if not fp:
        # This file is gone from disk — fall back to any OTHER surviving copy of
        # the same track so the track still plays (e.g. primary lost, alt remains).
        for alt in query("SELECT file_path FROM files WHERE track_id=? AND is_deleted=0 AND id!=? "
                          "ORDER BY is_primary DESC, id", (f["track_id"], file_id)):
            fp = _resolve_audio_path(alt["file_path"])
            if fp: break
    if not fp: abort(404)
    ext  = os.path.splitext(fp)[1].lower().lstrip(".")
    mime = AUDIO_MIME.get(ext) or mimetypes.guess_type(fp)[0] or "audio/mpeg"
    mime = _sniff_audio_mime(fp, mime)   # honest Content-Type so seeking works
    return _stream_file(fp, mime)


@app.route("/api/files/missing")
def api_files_missing():
    """Diagnostic: which tracks' primary audio file can't be found on disk
    (after exact path, volume-remap, and library self-heal). Helps spot files
    lost to a move/cleanup so they can be restored from the Trash / a backup."""
    rows = query(
        "SELECT t.id AS track_id, t.title, t.artist_name, f.file_path "
        "FROM tracks t JOIN files f ON f.track_id = t.id AND f.is_deleted=0 "
        "WHERE t.is_deleted=0"
    )
    by_track = {}
    for r in rows:
        by_track.setdefault((r["track_id"], r["title"], r["artist_name"]), []).append(r["file_path"])
    missing = []
    for (tid, title, artist), paths in by_track.items():
        if not any(_resolve_audio_path(p) for p in paths):   # no copy plays → truly gone
            missing.append({"track_id": tid, "title": title, "artist": artist,
                            "paths": paths})
    missing.sort(key=lambda m: ((m["artist"] or "").lower(), (m["title"] or "").lower()))
    return jsonify({"total_tracks": len(by_track), "missing_count": len(missing),
                    "missing": missing})


# Live library-health readout — recomputed on demand (cached briefly) so opening
# Music Room instantly shows "X% of your tracks can't be read" and flags a
# wrong/unplugged drive. present/missing is computed FRESH (it changes with what's
# mounted); the slow decode result (integrity) is read from the DB cache.
_health_cache = {"data": None, "at": 0.0}

def _expected_volume(path):
    """The /Volumes/<name> a stored path expects, or None (e.g. a Mac path)."""
    parts = Path(path or "").parts
    try:
        i = parts.index("Volumes")
        return parts[i + 1]
    except (ValueError, IndexError):
        return None

@app.route("/api/library/health")
def api_library_health():
    import time as _t
    from collections import Counter
    now = _t.time()
    if request.args.get("refresh") != "1" and _health_cache["data"] and (now - _health_cache["at"]) < 20:
        return jsonify(_health_cache["data"])

    rows = query("SELECT t.id AS tid, f.file_path AS fp, f.integrity AS intg "
                 "FROM tracks t JOIN files f ON f.track_id=t.id AND f.is_deleted=0 "
                 "WHERE t.is_deleted=0")
    by_track = {}
    for r in rows:
        by_track.setdefault(r["tid"], []).append(r)

    mounted = set()
    try: mounted = {p.name for p in Path("/Volumes").iterdir()}
    except OSError: pass

    # Fast present-check for a live readout: exact path, else basename in the
    # (cached) Library index. Skips _resolve_audio_path's per-file /Volumes scan,
    # which made this ~29s; this is a couple of seconds over the whole library.
    _idx = _library_basename_index()
    def _present(p):
        if not p: return False
        if _is_file_safe(p): return True
        hit = _idx.get(os.path.basename(p).lower())
        return bool(hit and _is_file_safe(hit))

    playable = corrupt = missing = 0
    expected = Counter()       # volume -> tracks expecting it
    unmounted_expected = Counter()
    for tid, frows in by_track.items():
        good = bad = present = 0
        for fr in frows:
            rp = _present(fr["fp"])
            if rp:
                present += 1
                if (fr["intg"] or "") in ("corrupt", "zero"): bad += 1
                else: good += 1
        if good > 0:
            playable += 1
        elif present > 0 and bad > 0:
            corrupt += 1            # present but every present copy is damaged
        else:
            missing += 1
            for fr in frows:        # which drive did these expect?
                v = _expected_volume(fr["fp"])
                if v:
                    expected[v] += 1
                    if v not in mounted: unmounted_expected[v] += 1
                break

    total = len(by_track)
    pct = lambda n: round(100.0 * n / total, 1) if total else 0.0
    # "Wrong drive" heuristic: a lot missing AND most of it expects a drive
    # that isn't currently mounted.
    warning = None
    if total and missing / total >= 0.30 and unmounted_expected:
        top_vol, n = unmounted_expected.most_common(1)[0]
        if n >= 0.5 * missing:
            warning = {"kind": "drive_unmounted", "volume": top_vol, "tracks": n}

    data = {
        "total": total, "playable": playable, "corrupt": corrupt, "missing": missing,
        "playable_pct": pct(playable), "corrupt_pct": pct(corrupt), "missing_pct": pct(missing),
        "drives": [{"volume": v, "tracks": n, "mounted": v in mounted}
                   for v, n in expected.most_common()],
        "warning": warning,
        "last_integrity_scan": get_setting("last_integrity_scan", ""),
    }
    _health_cache["data"] = data
    _health_cache["at"] = now
    return jsonify(data)


def _threadsafe_library_index():
    """Build {basename->path} for the Music Room Library WITHOUT Flask context
    (get_library_dest/query are request-bound). For background workers."""
    idx = {}
    try:
        with sqlite3.connect(DB_PATH) as c:
            lib = (c.execute("SELECT value FROM settings WHERE key='library_dest'").fetchone() or [""])[0] or ""
            if not lib:
                r = c.execute("SELECT file_path FROM files WHERE is_deleted=0 "
                              "AND file_path LIKE '%/Music Room Library/%' ORDER BY id LIMIT 1").fetchone()
                if r and r[0]:
                    m = "/Music Room Library/"; i = r[0].find(m)
                    if i != -1: lib = r[0][: i + len(m) - 1]
        if lib and os.path.isdir(lib):
            for dp, _d, files in os.walk(lib):
                for fn in files:
                    if fn.lower().endswith(_AUDIO_EXTS):
                        idx.setdefault(fn.lower(), os.path.join(dp, fn))
    except Exception:
        pass
    return idx


_integrity_state = {"running": False, "done": 0, "total": 0, "corrupt": 0, "at": "", "error": ""}
_integrity_lock = threading.Lock()

def _decode_status(p):
    """'ok' / 'corrupt' / 'zero' / None — actually decode the audio."""
    import shutil as _sh
    from mr_ffmpeg import ffmpeg_path
    flac = _sh.which("flac"); ff = ffmpeg_path()
    try:
        if os.path.getsize(p) == 0: return "zero"
    except OSError: return None
    try:
        if p.lower().endswith(".flac") and flac:
            return "ok" if subprocess.run([flac, "-t", "-s", p], capture_output=True, timeout=120).returncode == 0 else "corrupt"
        if ff:
            r = subprocess.run([ff, "-v", "error", "-i", p, "-f", "null", "-"], capture_output=True, text=True, timeout=120)
            return "ok" if (r.returncode == 0 and not r.stderr.strip()) else "corrupt"
    except Exception:
        return None
    return None

def _run_integrity_scan():
    global _integrity_state
    with _integrity_lock:
        if _integrity_state["running"]: return
        _integrity_state = {"running": True, "done": 0, "total": 0, "corrupt": 0, "at": "", "error": ""}
    try:
        idx = _threadsafe_library_index()
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, file_path FROM files WHERE is_deleted=0"
                           " AND COALESCE(analysis_status,'') != 'locked'").fetchall()   # borrows can't decode — skip
        with _integrity_lock: _integrity_state["total"] = len(rows)
        done = corrupt = 0
        for r in rows:
            p = r["file_path"]
            if not (p and os.path.isfile(p)):
                hit = idx.get(os.path.basename(p or "").lower())
                p = hit if hit and os.path.isfile(hit) else None
            if p:
                st = _decode_status(p)
                if st:
                    con.execute("UPDATE files SET integrity=?, integrity_checked_at=datetime('now') WHERE id=?", (st, r["id"]))
                    if st in ("corrupt", "zero"): corrupt += 1
            done += 1
            if done % 25 == 0:
                con.commit()
                with _integrity_lock: _integrity_state.update(done=done, corrupt=corrupt)
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('last_integrity_scan',?)", (_now_iso(),))
        con.commit(); con.close()
        with _integrity_lock:
            _integrity_state = {"running": False, "done": done, "total": len(rows), "corrupt": corrupt, "at": _now_iso(), "error": ""}
    except Exception as e:
        with _integrity_lock:
            _integrity_state.update(running=False, error=str(e))

@app.route("/api/library/integrity-scan", methods=["POST"])
def api_integrity_scan():
    if _integrity_state["running"]:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409
    threading.Thread(target=_run_integrity_scan, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/library/integrity-scan/status")
def api_integrity_scan_status():
    return jsonify(dict(_integrity_state))

@app.route("/api/files/corrupt")
def api_files_corrupt():
    """Tracks whose present audio is damaged (from the last decode scan)."""
    rows = query("SELECT t.id tid, t.title, t.artist_name, f.file_path fp, f.integrity intg "
                 "FROM tracks t JOIN files f ON f.track_id=t.id AND f.is_deleted=0 "
                 "WHERE t.is_deleted=0 AND f.integrity IN ('corrupt','zero')")
    out = [{"track_id": r["tid"], "title": r["title"], "artist": r["artist_name"],
            "path": r["fp"], "status": r["intg"]} for r in rows
           if _resolve_audio_path(r["fp"])]
    out.sort(key=lambda m: ((m["artist"] or "").lower(), (m["title"] or "").lower()))
    return jsonify({"count": len(out), "corrupt": out})


@app.route("/api/eject-drive", methods=["POST"])
def api_eject_drive():
    """Safely unmount an external drive so the cable can be pulled. Stops the
    in-process player first (it holds an open audio file handle that would
    otherwise block the eject)."""
    d = request.get_json() or {}
    target = (d.get("path") or "").strip()
    if not target:
        target = get_library_dest() or ""
    parts = Path(target).parts
    if not (len(parts) >= 3 and parts[1] == "Volumes"):
        return jsonify({"ok": False, "error": "Not an external drive path"}), 400
    vol = "/Volumes/" + parts[2]
    if not os.path.exists(vol):
        return jsonify({"ok": False, "error": "That drive isn't mounted"}), 400
    # Release the player's file handle so the volume isn't 'in use'.
    try:
        if _seinnteoir_player is not None:
            _seinnteoir_player.clear()      # empties queue + stops playback
            try:
                import pygame; pygame.mixer.music.unload()
            except Exception:
                pass
    except Exception:
        pass
    r = subprocess.run(["diskutil", "eject", vol], capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        return jsonify({"ok": True, "message": f"{parts[2]} ejected — safe to unplug"})
    return jsonify({"ok": False, "error": (r.stderr or r.stdout or "Eject failed").strip()[:200],
                    "hint": "Close anything else using the drive, then try again"}), 500


# =============================================================
# VOICE NOTES
# =============================================================

@app.route("/voice_note/<int:note_id>")
def serve_voice_note(note_id):
    n = query_one("SELECT * FROM notes WHERE id = ?", (note_id,))
    if not n or not n.get("voice_note_path"): abort(404)
    p = n["voice_note_path"]
    if not os.path.exists(p): abort(404)
    mime = "audio/webm"
    if p.endswith(".ogg"): mime = "audio/ogg"
    elif p.endswith(".mp4"): mime = "audio/mp4"
    return send_file(p, mimetype=mime)


# =============================================================
# PAGE ROUTES
# =============================================================

# ── 3D-ONLY MODE ─────────────────────────────────────────────────────
# MUSICROOM_3D=1 runs the app as the Prototype-1 3D Music Room: every
# surface with a 3D equivalent serves the 3D version (/ → the 3D forest,
# /place/<id> → the place's 3D room), and the room opens non-3D pages as
# overlays over itself instead of navigating away. The 2D app is
# untouched — same code, same data, a different front door.
IS_3D = os.environ.get("MUSICROOM_3D") == "1"

# The server runs on the same machine as the window, so it knows the chip.
# Older Intel Macs strain under the 3D surfaces at full retina resolution —
# the pages cap their render resolution when this is true (tester report,
# 2026-07-14: the forest was laggy on an old Intel laptop).
import platform as _platform
IS_LOW_POWER = _platform.machine() == "x86_64"


# ── GREEN ROOM MODE ──────────────────────────────────────────────────
# MR_GREEN_ROOM=1 marks a GR build (the residency releases, "Music Room
# GRx"): the nav shows the Green Room and the recorder is hidden. The
# /greenroom page itself works in any mode so it can be developed and
# reviewed without the flag.
IS_GR = os.environ.get("MR_GREEN_ROOM") == "1"
# Hound Dog fork: the trial's room is two tabs, with the wall of blocks switched off. On this fork the wall comes
# back as a third tab ("Wall"), where kept webs, quotes and mixes live. Set MR_FORK_WALL=0 for the plain trial.
FORK_WALL = os.environ.get("MR_FORK_WALL", "1") != "0"


@app.context_processor
def _inject_3d():
    return {"is_3d": IS_3D, "is_low_power": IS_LOW_POWER, "is_gr": IS_GR, "fork_wall": FORK_WALL,
            "theme_head": _theme_head()}


# ── THEMES: the app's skin, as colour tokens (themes.py) ──
import themes as mr_themes


def _custom_themes():
    try:
        return [t for t in _json.loads(get_setting("ui_themes_custom", "[]") or "[]") if isinstance(t, dict)]
    except Exception:
        return []


def _all_themes():
    return mr_themes.BUILT_IN + _custom_themes()


def _theme_head():
    """The <style> that applies the chosen theme — rendered server-side so a
    page never flashes dark before going light. 'system' renders dark; the
    client flips it if the OS is light."""
    try:
        cur = get_setting("ui_theme", "dark") or "dark"
    except Exception:
        return ""
    # One page can be looked at in another theme without changing the setting:
    # add ?theme=dark (or light, or a custom theme's id). For the both-themes
    # checks — the setting is shared with whatever copy the person is using.
    try:
        ask = (request.args.get("theme") or "").strip()
        if ask and (ask == "dark" or any(x.get("id") == ask for x in _all_themes())):
            cur = ask
    except Exception:
        pass
    marker = ("<script>document.documentElement.classList.add('is-gr');window.MR_GR=true;</script>"
              if IS_GR else "")
    mine = _user_css_tag()
    if cur in ("dark", "system"):
        return marker + mine
    t = next((x for x in _all_themes() if x.get("id") == cur), None)
    return (mr_themes.css_for(t) if t else "") + marker + mine


USER_CSS_MAX = 60_000


def _user_css_tag():
    """Your own look: CSS you wrote (Settings → Your own look), laid over the
    app on this laptop only. It changes nothing for anyone else."""
    try:
        css = get_setting("user_css", "") or ""
    except Exception:
        return ""
    if not css.strip() or (request and request.args.get("plain") == "1"):    # ?plain=1: the app without it
        return ""
    css = css[:USER_CSS_MAX].replace("</", "<\\/")
    # ⌘⇧0 / Ctrl+Shift+0 opens Settings WITHOUT it: a stylesheet that hides the page would
    # otherwise lock its author out for good (the desktop window has no address bar)
    way_back = ("<script>addEventListener('keydown',function(e){if((e.metaKey||e.ctrlKey)&&e.shiftKey&&(e.key==='0'||e.code==='Digit0'))"
                "location.href='/settings?plain=1#look';});</script>")
    return f'<style id="mr-user-css">{css}</style>{way_back}'


@app.route("/api/themes")
def api_themes():
    return jsonify({"themes": _all_themes(), "current": get_setting("ui_theme", "dark") or "dark",
                    "tokens": mr_themes.TOKENS})


@app.route("/api/themes/current", methods=["POST"])
def api_themes_current():
    d = request.get_json(silent=True) or {}
    tid = (d.get("id") or "dark").strip()
    if tid != "system" and not any(t["id"] == tid for t in _all_themes()):
        return jsonify({"error": "no such theme"}), 404
    set_setting("ui_theme", tid)
    green_room.track("theme", {"id": tid})
    return jsonify({"current": tid})


@app.route("/api/themes/import", methods=["POST"])
def api_themes_import():
    """A theme file someone made (or exported and changed). Same name → replaced."""
    theme, err = mr_themes.validate(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    mine = [t for t in _custom_themes() if t.get("id") != theme["id"]]
    mine.append(theme)
    set_setting("ui_themes_custom", _json.dumps(mine))
    green_room.track("theme_import", {"name": theme["name"]})
    return jsonify({"theme": theme, "themes": _all_themes()})


@app.route("/api/themes/<tid>", methods=["DELETE"])
def api_themes_delete(tid):
    mine = [t for t in _custom_themes() if t.get("id") != tid]
    set_setting("ui_themes_custom", _json.dumps(mine))
    if get_setting("ui_theme", "dark") == tid:
        set_setting("ui_theme", "dark")
    return jsonify({"themes": _all_themes()})


def _needs_onboarding():
    """Should the welcome wizard take over? Yes whenever the app can't do
    anything useful yet — no music in the room AND nowhere to bring it in from.

    The 'setup_complete' flag suppresses the wizard once the walkthrough is
    finished, BUT a done-flag must never be able to strand someone in a dead
    empty room: if the library is still empty and no music-library folder has
    been chosen, we show the intro regardless (that state is otherwise
    unrecoverable — no tracks, no folder, no way back to the walkthrough).
    Once a library folder IS chosen the empty room guides the user itself
    ('Bring your music in →'), so we don't force the wizard then."""
    # The trial never sends anyone to the welcome wizard: it is the summer's fourteen steps (a
    # forest, places, sets — none of them in the trial), and it came straight after the opening
    # tutorial. Most fellows have no music of their own; the trial's room is never dead when
    # empty (Grúpaí, "Borrowed"), and it says "Bring your music in" itself.
    if IS_GR:
        return False
    track_count = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"]
    if track_count > 0:
        return False
    if get_setting("setup_complete", "") != "1":
        return True
    # Marked complete but empty → only truly set up if a library folder exists.
    return not (get_library_dest() or "").strip()


@app.route("/")
def forest():
    # Fresh, empty room (or a stuck one with no music + no folder) → walk the
    # arrival through setup. See _needs_onboarding for the exact rule.
    if _needs_onboarding():
        return redirect("/welcome")
    if IS_GR:
        return redirect("/room")              # the trial has no forest: home is My Room
    if IS_3D:
        return redirect("/forest3d")          # the forest IS 3D here
    places = query("""
        SELECT p.*, COUNT(pt.track_id) as track_count
        FROM places p LEFT JOIN place_tracks pt ON pt.place_id = p.id
        GROUP BY p.id ORDER BY p.is_well DESC, p.name ASC
    """)
    return render_template("forest.html", places=places)


@app.route("/place/<int:place_id>/map")
def place_map(place_id):
    p = query_one("SELECT * FROM places WHERE id = ?", (place_id,))
    if not p: abort(404)
    return render_template("place_map.html", place=p)


@app.route("/api/place/<int:pid>/tracks/map")
def api_place_map_data(pid):
    """Bulk endpoint for Spéaclaí — returns all track data in one query."""
    rows = query("""
        SELECT
            t.id, t.title, t.artist_name, t.album, t.track_number, t.year, t.colour_code, t.art_colour, t.label,
            t.vinyl_owned, t.needs_review,
            COALESCE(t.is_oneshot, 0) as is_oneshot,
            COALESCE(t.is_set_recording, 0) as is_set_recording,
            t.source_type, t.discovered_via, t.artwork_path, t.date_added,
            t.gr_borrow_id, t.gr_track_id,
            f.id as file_id,
            f.bpm, f.key_note, f.key_scale,
            f.loudness_rms, f.dynamic_range, f.spectral_centroid,
            f.band_sub, f.band_bass, f.band_lowmid,
            f.band_body, f.band_presence, f.band_brilliance,
            f.brightness_score,
            f.has_analysis, f.duration_sec, f.file_format, f.bitrate_kbps,
            f.file_path, f.integrity,
            pt.moment,
            ap.country, ap.city, ap.scene as artist_scene,
            ap.latitude as artist_lat, ap.longitude as artist_lng,
            ap.gender as artist_gender, ap.artist_type,
            lp.country as label_country, lp.city as label_city,
            lp.latitude as label_lat, lp.longitude as label_lng,
            lp.founded_year as label_founded,
            (SELECT COUNT(*) FROM notes n WHERE n.track_id = t.id) as note_count,
            (SELECT COUNT(*) FROM notes n WHERE n.track_id = t.id
             AND n.note_type = 'cue_point') as cue_count,
            (SELECT COUNT(*) FROM place_tracks pt2 WHERE pt2.track_id = t.id) as place_count
        FROM tracks t
        JOIN place_tracks pt ON pt.track_id = t.id AND pt.place_id = ?
        LEFT JOIN files f ON f.track_id = t.id AND f.is_primary = 1
        LEFT JOIN artist_profiles ap ON ap.artist_name = t.artist_name
        LEFT JOIN label_profiles lp ON lp.label_name = t.label
        WHERE t.is_deleted = 0""" + (
        "" if request.args.get("include_sets") == "1"
        else " AND COALESCE(t.is_set_recording, 0) = 0") + """
        ORDER BY t.artist_name NULLS LAST, t.title NULLS LAST
    """, (pid,))

    if not rows:
        return jsonify({"tracks": []})

    # the marks the curator made on records show EVERYWHERE in this place:
    # same scoping as the 2D tracks API (place-specific overlays beat all-places)
    _tids = [r["id"] for r in rows]
    _ov_by_track = {}
    _ph = ",".join("?" * len(_tids))
    for o in query(f"""SELECT track_id, id, overlay_type, content,
                              pos_x_pct, pos_y_pct, width_pct, height_pct,
                              rotation, z_index, color, place_id
                       FROM track_overlays
                       WHERE track_id IN ({_ph}) AND (place_id = ? OR place_id IS NULL)
                       ORDER BY z_index ASC, id ASC""", _tids + [pid]):
        _ov_by_track.setdefault(o["track_id"], []).append(dict(o))
    for tid_k, ovs in _ov_by_track.items():
        if any(o["place_id"] == pid for o in ovs):
            _ov_by_track[tid_k] = [o for o in ovs if o["place_id"] == pid]

    track_ids = [r["id"] for r in rows]

    # Fetch tags + genres for all tracks in one query each — much faster than per-track
    placeholders = ",".join("?" * len(track_ids))
    tag_rows = query(
        f"""SELECT tt.track_id, tg.id, tg.name, tg.colour
            FROM track_tags tt JOIN tags tg ON tg.id = tt.tag_id
            WHERE tt.track_id IN ({placeholders})""",
        track_ids,
    )
    tags_by_track = {}
    for tr in tag_rows:
        tags_by_track.setdefault(tr["track_id"], []).append(
            {"id": tr["id"], "name": tr["name"], "colour": tr["colour"]}
        )
    genre_rows = query(
        f"""SELECT tg.track_id, g.id, g.name
            FROM track_genres tg JOIN genres g ON g.id = tg.genre_id
            WHERE tg.track_id IN ({placeholders})""",
        track_ids,
    )
    genres_by_track = {}
    for tr in genre_rows:
        genres_by_track.setdefault(tr["track_id"], []).append(
            {"id": tr["id"], "name": tr["name"]}
        )

    # per-track audio health (same logic as the place-tracks API) so the room
    # can tuck away records that can't actually play
    _idx = _library_basename_index()
    def _present(p):
        if not p: return False
        if _is_file_safe(p): return True
        hit = _idx.get(os.path.basename(p).lower())
        return bool(hit and _is_file_safe(hit))

    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = tags_by_track.get(r["id"], [])
        d["genres"] = genres_by_track.get(r["id"], [])
        d["overlays"] = _ov_by_track.get(r["id"], [])
        d["art_v"] = _artwork_version(d.get("artwork_path"))   # cover cache token
        if d.get("gr_borrow_id"):
            # a borrowed record: 'offloaded' (kept, not on this laptop) is a
            # visible state, not a hidden-missing one
            d["borrowed"] = "offloaded" if not _is_file_safe(d.get("file_path") or "") else "ok"
            d["audio_status"] = d["borrowed"]
        elif not _present(d.get("file_path")):
            d["audio_status"] = "missing"
        elif (d.get("integrity") or "") in ("corrupt", "zero"):
            d["audio_status"] = "corrupt"
        else:
            d["audio_status"] = "ok"
        d.pop("file_path", None)   # keep the bulk payload lean
        d.pop("integrity", None)
        result.append(d)

    return jsonify({"tracks": result})


# ── drive hot-plug: the room watches the mounted-volume set and re-checks
# availability when it changes, so records "wake up" the moment a drive
# returns — and the room can SAY when a chunk of the library is resting.
@app.route("/api/volumes/state")
def api_volumes_state():
    """Mounted /Volumes names (cheap — polled). ?full=1 adds which volumes the
    library's files actually reference and how many files are stranded on
    unmounted ones (fetched on change, not per-poll)."""
    try:
        vols = sorted(v for v in os.listdir("/Volumes") if not v.startswith("."))
    except OSError:
        vols = []
    out = {"volumes": vols}
    # A drive that has just appeared: start keeping its covers on the laptop on
    # the next request, not up to ten minutes on (Daithí, 2026-11-14: covers
    # looked worse — 1,535 of 1,550 had no full-quality laptop copy, because
    # the pass that makes them needs the drive and had never met it).
    st = _artwork_warm_state
    if st["vols"] is not None and set(vols) - set(st["vols"]):
        st["last"] = 0.0
    st["vols"] = vols
    if st["running"] and st["total"]:
        out["covers"] = {"done": st["done"], "total": st["total"]}
    if request.args.get("full") == "1":
        refs = {}
        for r in query("SELECT file_path FROM files WHERE is_primary=1 AND is_deleted=0 "
                       "AND file_path LIKE '/Volumes/%'"):
            parts = (r["file_path"] or "").split("/", 3)
            if len(parts) > 2:
                refs[parts[2]] = refs.get(parts[2], 0) + 1
        out["referenced"] = refs
        out["unmounted"] = {v: n for v, n in refs.items() if v not in vols}
    return jsonify(out)


@app.route("/api/tracks/presence", methods=["POST"])
def api_tracks_presence():
    """{ids:[…]} → {track_id: file-on-disk?} for those tracks' primary files —
    same resolution as the map API (direct path, then the library basename
    index, rebuilt fresh since this is only called when a volume just changed)."""
    ids = [int(i) for i in ((request.get_json() or {}).get("ids") or [])][:30000]
    if not ids:
        return jsonify({})
    ph = ",".join("?" * len(ids))
    rows = query(f"SELECT track_id, file_path FROM files WHERE is_primary=1 "
                 f"AND is_deleted=0 AND track_id IN ({ph})", ids)
    idx = _library_basename_index(force=True)
    def _present(p):
        if not p:
            return False
        if _is_file_safe(p):
            return True
        hit = idx.get(os.path.basename(p).lower())
        return bool(hit and _is_file_safe(hit))
    return jsonify({str(r["track_id"]): _present(r["file_path"]) for r in rows})


@app.route("/place/<int:place_id>")
def place(place_id):
    p = query_one("SELECT * FROM places WHERE id = ?", (place_id,))
    if not p: abort(404)
    # 3D mode: a bare place visit lands in the place's 3D ROOM. The 2D page
    # still serves its embedded roles (the room's track panel, chrome-less
    # Mála tools, the lightbox overlay) — those carry a marker param.
    if IS_3D and not any(request.args.get(k) for k in ("panelonly", "mala", "overlay", "toolonly", "list")):
        t = request.args.get("track")
        if IS_GR:                             # no places in the trial — a record opens in My Room
            return redirect("/room" + (f"?track={t}" if t else ""))
        return redirect(f"/room/{place_id}" + (f"?track={t}" if t else ""))
    return render_template("place.html", place=p)


@app.route("/track/<int:track_id>")
def track_detail(track_id):
    t = query_one("SELECT * FROM tracks WHERE id = ? AND is_deleted = 0", (track_id,))
    if not t: abort(404)
    return render_template("track.html", track=t)


@app.route("/practice")
def practice():
    return render_template("practice.html")


@app.route("/study")
def study():
    return render_template("study.html")


@app.route("/tools")
def tools():
    return render_template("tools.html")


@app.route("/usb")
def usb_page():
    return render_template("usb.html")


@app.route("/room")
@app.route("/room/<int:place_id>")
def room3d(place_id=None):
    """A place as a 3D room — records floating, rearranging on filter. /room is
    the Music Room itself (the Well: every record); /room/<id> opens any place
    as its own room, carrying the place's name, colour and ambient sound. The
    2D list view stays at /place/<id>."""
    # Fresh/stuck empty install → onboarding first (mirrors the forest route).
    if _needs_onboarding():
        return redirect("/welcome")
    if place_id is None:
        w = query_one("SELECT id FROM places WHERE is_well=1")
        place_id = w["id"] if w else 1
    p = query_one("""SELECT id, name, is_well, colour_primary, theme_bg, theme_mood,
                            ambient_audio_path, ambient_audio_volume, room_bg_path
                     FROM places WHERE id=?""", (place_id,))
    if not p:
        return redirect("/room")
    if IS_GR and not p["is_well"]:            # places are out of the trial: there is one room, My Room
        t = request.args.get("track")
        return redirect("/room" + (f"?track={t}" if t else ""))
    return render_template("room3d.html", well_id=p["id"],
                           place_json=_json.dumps(dict(p)),
                           record_font=get_setting("record_font", "auto"),
                           room_bg_dim=get_setting("room_bg_dim", "45"),
                           ambient_volume_default=get_setting("ambient_volume_default", "35"))


@app.route("/forest3d")
def forest3d():
    """The forest as a 3D wood — places stand in clearings among procedural
    Irish trees, with the same add/edit/drag-to-position flow as /."""
    if IS_GR:                                 # Daithí, 2026-11-14: "remove the entire forest element" from the trial
        return redirect("/room")
    return render_template("forest3d.html")


@app.route("/audio-health")
def audio_health_page():
    return render_template("audio_health.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/import")
def import_page():
    return render_template("import.html")


@app.route("/seinnteoir/popout")
def seinnteoir_popout():
    return render_template("seinnteoir_popout.html")


# =============================================================
# SEINNTEOIR — server-side audio player. Audio lives in the Flask
# process so it keeps playing while the browser navigates.
# =============================================================
_seinnteoir_player = None
_seinnteoir_player_lock = threading.Lock()


def _get_seinnteoir_player():
    """Lazy singleton — pygame mixer is initialised on first use."""
    global _seinnteoir_player
    if _seinnteoir_player is not None:
        return _seinnteoir_player
    with _seinnteoir_player_lock:
        if _seinnteoir_player is None:
            from seinnteoir_player import SeinnteoirPlayer
            pl = SeinnteoirPlayer(resolve_path=_resolve_audio_path)
            # Spotify records in the cue play in the listener's OWN Spotify when
            # they are signed in with Premium (spotify_remote.py); otherwise in
            # Spotify's player on the page, as before.
            pl.set_remote(_get_spotify_remote())
            _seinnteoir_player = pl
    return _seinnteoir_player


_spotify_remote = None


def _get_spotify_remote():
    global _spotify_remote
    if _spotify_remote is None:
        from spotify_remote import SpotifyRemote
        _spotify_remote = SpotifyRemote(lambda: _get_spotify())
    return _spotify_remote


def _items_with_paths(items):
    """Given JS-shape items {track_id, file_id, title, artist}, look up the
    real file path for each via the files table so the player can load them."""
    out = []
    for it in (items or []):
        # A streamed item (Spotify / SoundCloud): the service itself plays it —
        # the queue only keeps its place.
        if it.get("kind") == "embed":
            if str(it.get("url") or "").startswith(("http://", "https://")):
                out.append({
                    "kind":         "embed",
                    "qid":          it.get("qid") or "",
                    "url":          it["url"],
                    "source":       it.get("source") or "",
                    "stream_type":  it.get("stream_type") or "",
                    "title":        it.get("title") or "",
                    "artist":       it.get("artist") or "",
                    "duration_sec": it.get("duration_sec") or 0,
                    "image_url":    it.get("image_url") or "",
                    "once":         bool(it.get("once")),     # record shopping's one play: leaves the cue after
                })
            continue
        # Remote stream items (Explore mixes/podcasts) carry an audio_url, not a
        # library file_id. Pass them through so the player can download+play
        # them through the same persistent queue as library tracks.
        if it.get("audio_url") and not it.get("file_id"):
            out.append({
                "kind":         "stream",
                "track_id":     None,
                "file_id":      None,
                "audio_url":    it["audio_url"],
                "title":        it.get("title") or "",
                "artist":       it.get("artist") or it.get("source") or "",
                "duration_sec": it.get("duration_sec") or 0,
                "image_url":    it.get("image_url") or "",
                "url":          it.get("url") or "",          # the page to open
                "feed_item_id": it.get("feed_item_id"),
            })
            continue
        # Green Room borrows: an encrypted local file + the key to play it.
        # The key rides the queue item so the player (its own thread, its own
        # DB habits) can decrypt in memory without a lookup at play time.
        if it.get("gr_borrow_id") and not it.get("file_id"):
            row = green_room.borrow_row(it["gr_borrow_id"])
            if not row or not row.get("file_path"): continue
            out.append({
                "kind":         "borrow",
                "track_id":     None,
                "file_id":      None,
                "gr_borrow_id": row["id"],
                "gr_path":      row["file_path"],
                "gr_key":       row["key_hex"],
                "gr_nonce":     row["nonce_hex"],
                "gr_format":    row["format"] or "",
                "title":        row["title"] or "",
                "artist":       row["artist"] or "",
                "duration_sec": row["duration_s"] or 0,
                "image_url":    f"/api/gr/art/{row['server_track_id']}",
            })
            continue
        fid = it.get("file_id")
        if not fid: continue
        # Pull the track's own title/artist alongside the path: some cue paths
        # hand us a record object missing them (JSON drops undefined fields), so
        # the cassette would read "Unknown" until the track played. The DB is the
        # source of truth — fall back to it whenever the browser left them blank.
        f = query_one(
            "SELECT f.file_path, f.duration_sec, t.title AS t_title, "
            "       t.artist_name AS t_artist, t.gr_borrow_id, t.gr_track_id "
            "FROM files f LEFT JOIN tracks t ON t.id = f.track_id "
            "WHERE f.id=?", (fid,))
        if not f: continue
        if f.get("gr_borrow_id"):
            # a borrowed record cued like any other: resolve to its locked
            # file + key so the player decrypts in memory
            row = green_room.borrow_row(f["gr_borrow_id"])
            if not row or not row.get("file_path"): continue
            out.append({
                "kind":         "borrow",
                "track_id":     it.get("track_id"),
                "file_id":      fid,
                "gr_borrow_id": row["id"],
                "gr_path":      row["file_path"],
                "gr_key":       row["key_hex"],
                "gr_nonce":     row["nonce_hex"],
                "gr_format":    row["format"] or "",
                "title":        it.get("title")  or f.get("t_title")  or "",
                "artist":       it.get("artist") or f.get("t_artist") or "",
                "duration_sec": f.get("duration_sec") or row["duration_s"] or 0,
                "image_url":    f"/api/gr/art/{row['server_track_id']}",
                "place_id":     it.get("place_id"),
                "place_name":   it.get("place_name") or "",
            })
            continue
        out.append({
            "track_id":     it.get("track_id"),
            "file_id":      fid,
            "file_path":    f["file_path"],
            "title":        it.get("title")  or f.get("t_title")  or "",
            "artist":       it.get("artist") or f.get("t_artist") or "",
            "duration_sec": f.get("duration_sec") or 0,
            # where the cue came from — the queue shows it, and offers the
            # jump back to that place
            "place_id":     it.get("place_id"),
            "place_name":   it.get("place_name") or "",
        })
    return out


@app.route("/api/seinnteoir/state")
def api_seinnteoir_state():
    return jsonify(_get_seinnteoir_player().get_state())


@app.route("/api/seinnteoir/play", methods=["POST"])
def api_seinnteoir_play():
    _get_seinnteoir_player().play()
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/pause", methods=["POST"])
def api_seinnteoir_pause():
    d = request.get_json(silent=True) or {}
    _get_seinnteoir_player().pause(fade_ms=int(d.get("fade_ms") or 0))
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/toggle", methods=["POST"])
def api_seinnteoir_toggle():
    _get_seinnteoir_player().toggle()
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/next", methods=["POST"])
def api_seinnteoir_next():
    _get_seinnteoir_player().next()
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/prev", methods=["POST"])
def api_seinnteoir_prev():
    _get_seinnteoir_player().prev()
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/seek", methods=["POST"])
def api_seinnteoir_seek():
    d = request.get_json() or {}
    sec = float(d.get("sec", 0))
    _get_seinnteoir_player().seek(sec)
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/volume", methods=["POST"])
def api_seinnteoir_volume():
    d = request.get_json() or {}
    v = float(d.get("volume", 0.8))
    _get_seinnteoir_player().set_volume(v)
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/ui_mode", methods=["POST"])
def api_seinnteoir_ui_mode():
    # the cassette's open/min/closed, remembered server-side — the desktop
    # shell wipes localStorage on relaunch, which reopened a put-away player
    d = request.get_json() or {}
    _get_seinnteoir_player().set_ui_mode(d.get("mode"))
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/queue", methods=["POST"])
def api_seinnteoir_set_queue():
    d = request.get_json() or {}
    items = _items_with_paths(d.get("items") or [])
    idx   = int(d.get("idx", 0))
    auto  = bool(d.get("autoplay", True))
    _get_seinnteoir_player().set_queue(items, idx=idx, autoplay=auto)
    return jsonify({"ok": True, "count": len(items)})


@app.route("/api/seinnteoir/add", methods=["POST"])
def api_seinnteoir_add():
    d = request.get_json() or {}
    items = _items_with_paths(d.get("items") or [])
    # autostart defaults True (existing 2D behaviour); the 3D room cues with
    # autostart=False so adding to an empty queue never starts playback.
    n = _get_seinnteoir_player().add(items, autostart=bool(d.get("autostart", True)),
                                     play_now=bool(d.get("play_now")))
    return jsonify({"ok": True, "added": n})


@app.route("/api/seinnteoir/play_at", methods=["POST"])
def api_seinnteoir_play_at():
    d = request.get_json() or {}
    _get_seinnteoir_player().play_at(int(d.get("idx", 0)))
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/remove", methods=["POST"])
def api_seinnteoir_remove():
    d = request.get_json() or {}
    _get_seinnteoir_player().remove(int(d.get("idx", -1)))
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/reorder", methods=["POST"])
def api_seinnteoir_reorder():
    d = request.get_json() or {}
    order = d.get("order") or []
    _get_seinnteoir_player().reorder(order)
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/move", methods=["POST"])
def api_seinnteoir_move():
    """Move one cue row to another position (by index — any mix of items)."""
    d = request.get_json() or {}
    try:
        _get_seinnteoir_player().move(int(d.get("from")), int(d.get("to")))
    except (TypeError, ValueError):
        return jsonify({"error": "from and to are positions"}), 400
    return jsonify({"ok": True})


@app.route("/api/seinnteoir/embed", methods=["POST"])
def api_seinnteoir_embed():
    """The page's streaming player reporting on the current streamed item:
    where it is, whether it was paused there, and when it has ended."""
    d = request.get_json() or {}
    def _num(k):
        try:    return float(d[k]) if d.get(k) is not None else None
        except (TypeError, ValueError): return None
    ok = _get_seinnteoir_player().embed_report(
        str(d.get("qid") or ""), position=_num("position"), duration=_num("duration"),
        paused=(bool(d["paused"]) if d.get("paused") is not None else None),
        ended=bool(d.get("ended")))
    return jsonify({"ok": ok})


@app.route("/api/seinnteoir/clear", methods=["POST"])
def api_seinnteoir_clear():
    _get_seinnteoir_player().clear()
    return jsonify({"ok": True})


# =============================================================
# SOURCES — curated outside-world music links (Explore)
# Magazines / stations / blogs / labels Daithí keeps to hand.
# =============================================================

@app.route("/api/sources")
def api_sources():
    rows = query("SELECT * FROM sources ORDER BY sort_order, name")
    return jsonify(rows)


@app.route("/api/sources", methods=["POST"])
def api_create_source():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    url  = (d.get("url")  or "").strip()
    kind = (d.get("kind") or "magazine").strip() or "magazine"
    desc = (d.get("description") or "").strip() or None
    feed = (d.get("feed_url") or "").strip() or None
    if feed and not feed.startswith(("http://", "https://")):
        feed = "https://" + feed
    if not name or not url:
        return jsonify({"error": "name and url required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    next_order_row = query_one("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM sources")
    nxt = next_order_row["n"] if next_order_row else 0
    sid = execute(
        "INSERT INTO sources (name,url,kind,description,feed_url,sort_order) VALUES (?,?,?,?,?,?)",
        (name, url, kind, desc, feed, nxt)
    )
    # If they didn't supply a feed, look one up in the background so new posts
    # start flowing into Latest without the user needing to know what RSS is.
    if not feed:
        threading.Thread(target=_discover_one_source_feed, args=(sid, url, name), daemon=True).start()
    return jsonify(query_one("SELECT * FROM sources WHERE id=?", (sid,))), 201


def _discover_one_source_feed(sid, url, name=""):
    """Best-effort: find a source's RSS/Atom feed and pull its recent items.
    Runs in a background thread on source creation (own DB connection).
    Consults the curated feed registry FIRST (known sites whose feed isn't
    auto-discoverable), then falls back to live discovery."""
    try:
        import feeds, feed_registry, datetime as _dt
        feed_url = ""
        try:
            feed_url = feed_registry.lookup(name, url) or feeds.discover_feed(url) or ""
        except Exception:
            feed_url = ""
        conn = _connect_threadsafe(); cur = conn.cursor()
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        cur.execute("UPDATE sources SET feed_url=?, feed_checked_at=? WHERE id=?",
                    (feed_url or None, now_iso, sid))
        if feed_url:
            try:
                for it in feeds.fetch_feed(feed_url, limit=40):
                    cur.execute(
                        "INSERT OR IGNORE INTO feed_items "
                        "(source_id, guid, title, url, summary, image_url, published_at, "
                        " audio_url, audio_duration_sec) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (sid, it["guid"], it["title"], it["url"],
                         it["summary"], it["image_url"], it["published_at"],
                         it.get("audio_url"), it.get("audio_duration_sec")))
            except Exception:
                pass
        conn.commit(); conn.close()
    except Exception:
        pass


@app.route("/api/source/find-feed", methods=["POST"])
def api_find_feed():
    """Given a website URL, try to find its RSS/Atom feed — so the user can add
    one without knowing what a feed is. Returns the feed URL + a quick sanity
    count, or ok:false when the site doesn't publish one."""
    import feeds, feed_registry
    d = request.get_json() or {}
    url  = (d.get("url") or "").strip()
    name = (d.get("name") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    feed_url = None
    try:
        feed_url = feed_registry.lookup(name, url)        # a known site → instant, no fetch
        if not feed_url:
            if feeds._looks_like_feed(url):
                feed_url = url
            else:
                feed_url = feeds.discover_feed(url)
    except Exception:
        feed_url = None
    if not feed_url:
        return jsonify({"ok": False})
    count, title = 0, None
    try:
        items = feeds.fetch_feed(feed_url, limit=5)
        count = len(items)
        if items:
            title = items[0].get("title")
    except Exception:
        pass
    return jsonify({"ok": True, "feed_url": feed_url, "count": count, "title": title})


@app.route("/api/source/<int:sid>", methods=["PATCH"])
def api_patch_source(sid):
    d = request.get_json() or {}
    fields = {}
    for k in ("name", "url", "kind", "description"):
        if k in d:
            v = (d[k] or "").strip()
            if k in ("name", "url") and not v:
                return jsonify({"error": f"{k} cannot be empty"}), 400
            if k == "url" and v and not v.startswith(("http://", "https://")):
                v = "https://" + v
            fields[k] = v or None
    if "feed_url" in d:
        fv = (d["feed_url"] or "").strip()
        if fv and not fv.startswith(("http://", "https://")):
            fv = "https://" + fv
        fields["feed_url"] = fv or None
    if not fields:
        return jsonify({"error": "no fields"}), 400
    set_c = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE sources SET {set_c} WHERE id=?", list(fields.values()) + [sid])
    return jsonify(query_one("SELECT * FROM sources WHERE id=?", (sid,)))


@app.route("/api/source/<int:sid>", methods=["DELETE"])
def api_delete_source(sid):
    execute("DELETE FROM sources WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/source/<int:sid>/visit", methods=["POST"])
def api_visit_source(sid):
    execute("UPDATE sources SET last_visited_at=datetime('now') WHERE id=?", (sid,))
    return jsonify({"ok": True})


# =============================================================
# ARTICLES — saved-for-later articles / inspo / mixes / write-ups
# =============================================================

def _parse_tags(s):
    if not s:
        return []
    return [t.strip() for t in str(s).split(",") if t.strip()]


@app.route("/api/articles")
def api_articles():
    tag      = (request.args.get("tag") or "").strip()
    place_id = request.args.get("place_id", type=int)
    is_read  = request.args.get("is_read")
    where, params = [], []
    if tag:
        # crude substring match — comma-separated tag list
        where.append("(',' || COALESCE(tags,'') || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if place_id is not None:
        where.append("place_id = ?"); params.append(place_id)
    if is_read in ("0", "1"):
        where.append("is_read = ?"); params.append(int(is_read))
    sql = "SELECT a.*, p.name as place_name FROM articles a LEFT JOIN places p ON p.id=a.place_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.created_at DESC"
    rows = query(sql, params)
    # Decorate with parsed tags
    for r in rows:
        r["tag_list"] = _parse_tags(r.get("tags"))
    return jsonify(rows)


@app.route("/api/articles", methods=["POST"])
def api_create_article():
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    title       = (d.get("title") or "").strip() or None
    description = (d.get("description") or "").strip() or None
    source_name = (d.get("source_name") or "").strip() or None
    place_id    = d.get("place_id") if isinstance(d.get("place_id"), int) else None
    tags        = ",".join(_parse_tags(d.get("tags"))) or None
    aid = execute(
        "INSERT INTO articles (url,title,description,source_name,place_id,tags) VALUES (?,?,?,?,?,?)",
        (url, title, description, source_name, place_id, tags)
    )
    row = query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row), 201


@app.route("/api/article/<int:aid>", methods=["PATCH"])
def api_patch_article(aid):
    a = query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if not a:
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    updates = {}
    if "url" in d:
        url = (d["url"] or "").strip()
        if not url:
            return jsonify({"error": "url cannot be empty"}), 400
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        updates["url"] = url
    for k in ("title", "description", "source_name"):
        if k in d:
            updates[k] = ((d[k] or "").strip() or None)
    if "place_id" in d:
        updates["place_id"] = d["place_id"] if isinstance(d["place_id"], int) else None
    if "tags" in d:
        updates["tags"] = ",".join(_parse_tags(d.get("tags"))) or None
    if "is_read" in d:
        updates["is_read"] = 1 if d["is_read"] else 0
    if not updates:
        return jsonify({"error": "no fields"}), 400
    set_c = ", ".join(f"{k}=?" for k in updates)
    execute(f"UPDATE articles SET {set_c} WHERE id=?", list(updates.values()) + [aid])
    row = query_one("SELECT * FROM articles WHERE id=?", (aid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


@app.route("/api/article/<int:aid>", methods=["DELETE"])
def api_delete_article(aid):
    execute("DELETE FROM articles WHERE id=?", (aid,))
    return jsonify({"ok": True})


@app.route("/api/article/<int:aid>/visit", methods=["POST"])
def api_visit_article(aid):
    execute("UPDATE articles SET last_visited_at=datetime('now'), is_read=1 WHERE id=?", (aid,))
    return jsonify({"ok": True})


@app.route("/api/article/tags")
def api_article_tags():
    """Distinct tags across all articles — for the filter UI."""
    rows = query("SELECT tags FROM articles WHERE tags IS NOT NULL AND tags != ''")
    counts = {}
    for r in rows:
        for t in _parse_tags(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    items = [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return jsonify(items)


# =============================================================
# WISHLIST — tracks/records to buy (bandcamp / spotify / youtube etc.)
# =============================================================

WISHLIST_STATUSES = ("wishing", "supported", "passed")
# Accept the legacy 'bought' value on input and silently normalise to
# 'supported' so anything cached client-side or in old links keeps working.
WISHLIST_STATUS_ALIASES = {"bought": "supported"}

# The three boxes — a FEELING axis orthogonal to status (the buy-funnel).
# A wish lives in exactly one box; the play game re-files cards between them.
WISHLIST_BUCKETS = ("another_listen", "need_now", "have_a_think")
WISHLIST_BUCKET_DEFAULT = "another_listen"

# Why a record gets wanted/bought — a small, optional self-log the user can tap
# in the play game. Kept as clean categories so the "why you buy" report can
# count them; 'other' carries free text in buy_reason_note. NOT pushed on the user.
WISHLIST_BUY_REASONS = ("sentimental", "djing", "research", "support", "collection", "other")


def _detect_source(url):
    if not url:
        return None
    u = url.lower()
    if "bandcamp.com"   in u: return "bandcamp"
    if "spotify.com"    in u or "spotify:" in u: return "spotify"
    if "youtube.com"    in u or "youtu.be" in u: return "youtube"
    if "soundcloud.com" in u: return "soundcloud"
    if "discogs.com"    in u: return "discogs"
    if "beatport.com"   in u: return "beatport"
    if "music.apple"    in u or "itunes.apple" in u: return "apple"
    if "tidal.com"      in u: return "tidal"
    return "other"


def _split_artist_title(s, author="", source=""):
    """Best-effort artist/title from a page/oembed title — every platform writes
    it differently, so try the common shapes and fall back to the raw string."""
    import re as _re
    s = (s or "").strip()
    if not s:
        return (author or "", "")
    m = _re.search(r"^(.*?),\s*by\s+(.+)$", s, _re.I)          # "Title, by Artist"  (Bandcamp)
    if m:
        return (m.group(2).strip(), m.group(1).strip())
    if source == "soundcloud":
        m = _re.search(r"^(.*?)\s+by\s+(.+)$", s, _re.I)        # "Title by Artist"  (SoundCloud oembed)
        if m:
            return (m.group(2).strip(), m.group(1).strip())
    if " - " in s:                                              # "Artist - Title"  (YouTube, generic)
        a, t = s.split(" - ", 1)
        return (a.strip(), t.strip())
    return (author or "", s)


def _resolve_link(url):
    """Paste a song link → best-effort {artist, title, source, image}. Free only:
    oEmbed where it's reliable (SoundCloud, YouTube), Open Graph tags otherwise
    (Spotify / Apple / Bandcamp / Discogs / anything). Always returns the raw
    title so the user can fix it. No API keys; aimed at the rare-record hunt."""
    import urllib.request, urllib.parse as _up, json as _json, html as _html, re as _re
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    source = _detect_source(url)
    UA = {"User-Agent": "MusicRoom/1.0 (personal DJ library)"}
    artist = title = image = raw = ""
    oembed = None
    if source == "soundcloud":
        oembed = "https://soundcloud.com/oembed?format=json&url=" + _up.quote(url, safe="")
    elif source == "youtube":
        oembed = "https://www.youtube.com/oembed?format=json&url=" + _up.quote(url, safe="")
    if oembed:
        try:
            d = _json.loads(urllib.request.urlopen(urllib.request.Request(oembed, headers=UA), timeout=10)
                            .read().decode("utf-8", "ignore"))
            raw = (d.get("title") or "").strip()
            image = d.get("thumbnail_url") or ""
            artist, title = _split_artist_title(raw, (d.get("author_name") or "").strip(), source)
        except Exception:
            pass
    if not title:                                              # Open Graph fallback
        try:
            page = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12) \
                   .read().decode("utf-8", "ignore")
            def _meta(prop):
                m = (_re.search(r'<meta[^>]+(?:property|name)=["\']' + _re.escape(prop) + r'["\'][^>]+content=["\']([^"\']*)', page, _re.I)
                     or _re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']' + _re.escape(prop) + r'["\']', page, _re.I))
                return _html.unescape(m.group(1)).strip() if m else ""
            ogt = _meta("og:title") or _meta("twitter:title")
            ogd = _meta("og:description") or _meta("twitter:description")
            image = image or _meta("og:image")
            raw = raw or ogt
            if ogt:
                artist, title = _split_artist_title(ogt, "", source)
            if source == "spotify" and ogd and "·" in ogd:   # "Artist · Song · Year"
                parts = [p.strip() for p in ogd.split("·")]
                if len(parts) >= 2:
                    artist = artist or parts[0]
                    title = title or parts[1]
        except Exception:
            pass
    return {"ok": bool(title or raw), "url": url, "source": source,
            "artist": artist, "title": title or raw, "image": image, "raw_title": raw}


@app.route("/api/discover/resolve", methods=["POST"])
def api_discover_resolve():
    """Resolve a single pasted link to metadata, ready to drop on the wishlist."""
    url = ((request.get_json() or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "paste a link"}), 400
    try:
        return jsonify(_resolve_link(url))
    except Exception:
        return jsonify({"ok": False, "url": url, "source": _detect_source(url),
                        "artist": "", "title": "", "image": "", "raw_title": ""})


def _search_meta(text, kind="track"):
    """Free-text → best online match (iTunes Search; either word order works, so
    'Burial Untrue' and 'Untrue Burial' both find it). kind 'album' searches
    collections, 'track' searches songs. Returns one best candidate
    {ok,artist,title,artwork_url,album,release_year,genre,kind,alternates} or {}.
    No API key."""
    import urllib.request, urllib.parse as _up, json as _json
    term = (text or "").strip()
    if not term:
        return {}
    UA = {"User-Agent": "MusicRoom/1.0 (personal DJ library)"}
    entity = "album" if kind == "album" else "song"
    try:
        url = "https://itunes.apple.com/search?" + _up.urlencode({"term": term, "entity": entity, "limit": 6})
        d = _json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10)
                        .read().decode("utf-8", "ignore"))
        results = d.get("results") or []
        if not results:
            return {}

        def shape(r):
            art = (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
            ttl = (r.get("collectionName") if kind == "album" else r.get("trackName")) or r.get("collectionName") or ""
            return {"artist": r.get("artistName") or "", "title": ttl, "artwork_url": art,
                    "album": r.get("collectionName") or "", "release_year": (r.get("releaseDate") or "")[:4],
                    "genre": r.get("primaryGenreName") or ""}
        best = shape(results[0]); best["ok"] = True; best["kind"] = kind
        best["alternates"] = [shape(r) for r in results[1:4]]
        return best
    except Exception:
        return {}


@app.route("/api/discover/search", methods=["POST"])
def api_discover_search():
    """Free-text album/track search → metadata, for the wishlist 'new entry' when
    the user types 'artist + title' (in any order) rather than pasting a link."""
    d = request.get_json() or {}
    return jsonify(_search_meta(d.get("text") or "", (d.get("kind") or "track").strip().lower()))


@app.route("/api/extract-article", methods=["POST"])
def api_extract_article():
    """Reader mode: fetch an article and return a clean, allowlisted reading view
    (title/byline/source/image + the words) to render IN-APP — the same thing
    Safari Reader / Pocket do. Cached per-url; a miss (e.g. a JS-only page) falls
    back to opening the original in the browser, client-side."""
    import reader
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "valid url required"}), 400
    if not d.get("refresh"):
        c = query_one("SELECT * FROM article_cache WHERE url=?", (url,))
        if c:
            c = dict(c)
            c["ok"] = bool(c.get("ok"))
            c["cached"] = True
            return jsonify(c)
    try:
        r = reader.extract(url)
    except Exception:
        r = {"ok": False, "url": url, "transient": True, "error": "extraction failed"}
    # cache successes + genuine "thin page" misses (so a JS-only page isn't
    # re-hammered), but NOT a transient fetch failure (network blip) — that would
    # condemn the article to the fallback forever.
    if not r.get("transient"):
        execute(
            "INSERT OR REPLACE INTO article_cache (url,ok,title,byline,image_url,published,source,body_html,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (url, 1 if r.get("ok") else 0, r.get("title"), r.get("byline"), r.get("image_url"),
             r.get("published"), r.get("source"), r.get("body_html")))
    return jsonify(r)


def _enrich_meta(artist, title):
    """Free metadata for a wish — one iTunes Search call gives artwork + album +
    year + genre; MusicBrainz adds the label. No API key. Returns {} on a miss."""
    import urllib.request, urllib.parse as _up, json as _json
    out, term = {}, (f"{artist} {title}").strip()
    if not term:
        return out
    UA = {"User-Agent": "MusicRoom/1.0 (personal DJ library)"}
    try:
        url = "https://itunes.apple.com/search?" + _up.urlencode({"term": term, "entity": "song", "limit": 1})
        d = _json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10)
                        .read().decode("utf-8", "ignore"))
        r = (d.get("results") or [None])[0]
        if r:
            art = (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
            if art:                       out["artwork_url"]  = art
            if r.get("collectionName"):   out["album"]        = r["collectionName"]
            if r.get("releaseDate"):      out["release_year"] = r["releaseDate"][:4]
            if r.get("primaryGenreName"): out["genre"]        = r["primaryGenreName"]
    except Exception:
        pass
    try:                                  # label (and a year fallback) from MusicBrainz — best effort
        from artwork_fetcher import _mb_request
        q = _up.quote(f'recording:"{title}" AND artist:"{artist}"')
        mb = _mb_request(f"https://musicbrainz.org/ws/2/recording?query={q}&fmt=json&limit=1") or {}
        rel = ((mb.get("recordings") or [{}])[0].get("releases") or [{}])[0]
        lbl = (rel.get("label-info") or [{}])[0].get("label") or {}
        if lbl.get("name"):                                   out["label"] = lbl["name"]
        if not out.get("release_year") and rel.get("date"):   out["release_year"] = rel["date"][:4]
    except Exception:
        pass
    return out


@app.route("/api/wishlist/<int:wid>/enrich", methods=["POST"])
def api_wishlist_enrich(wid):
    """Fill in cover art + album / year / genre / label for a wish, from free
    sources. Only fills BLANK fields — never clobbers a cover a link already gave."""
    w = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if not w:
        return jsonify({"error": "not found"}), 404
    meta = _enrich_meta(w.get("artist") or "", w.get("title") or "")
    sets, vals = [], []
    for k in ("artwork_url", "album", "release_year", "genre", "label"):
        if meta.get(k) and not (w.get(k) or "").strip():
            sets.append(f"{k}=?"); vals.append(meta[k])
    if sets:
        execute(f"UPDATE wishlist SET {', '.join(sets)} WHERE id=?", vals + [wid])
    row = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


# ── Discovery → wishlist: import tracks, route to listen + buy-direct ─────────
# The Music Room is a discovery hub, NOT a store: it imports tracks you've found
# (Shazam, a mix tracklist, an article), remembers WHY/where you found them, and
# generates outbound links so you can listen and BUY DIRECT FROM THE ARTIST. No
# money ever touches the app — every buy link routes out (Bandcamp first).

def _norm_at(artist, title):
    """A loose key for de-duping 'artist – title' against the wishlist/library.
    Folds accents (Flügel == Flugel) so the same track imported with/without
    diacritics doesn't slip in twice."""
    import re, unicodedata
    def n(s):
        s = unicodedata.normalize('NFKD', (s or '').lower())
        s = ''.join(c for c in s if not unicodedata.combining(c))   # strip diacritics
        return re.sub(r'[^a-z0-9]', '', s)
    return n(artist) + '|' + n(title)

def _discovery_links(artist, title):
    """Deterministic listen + buy-direct deep-links for a track — zero API, ToS-clean
    (the same searches a human would type). Bandcamp leads both lists (artist-direct).
    The UI renders these as 'listen at…' / 'buy at…' buttons; bought_via logs the choice."""
    from urllib.parse import quote_plus
    q = quote_plus(f"{(artist or '').strip()} {(title or '').strip()}".strip())
    return {
        "listen": [
            {"store": "bandcamp", "label": "Bandcamp", "url": f"https://bandcamp.com/search?q={q}"},
            {"store": "youtube",  "label": "YouTube",  "url": f"https://www.youtube.com/results?search_query={q}"},
            {"store": "spotify",  "label": "Spotify",  "url": f"https://open.spotify.com/search/{q}"},
        ],
        "buy": [
            {"store": "bandcamp", "label": "Bandcamp", "url": f"https://bandcamp.com/search?q={q}"},
            {"store": "discogs",  "label": "Discogs",  "url": f"https://www.discogs.com/search/?q={q}&type=release"},
            {"store": "boomkat",  "label": "Boomkat",  "url": f"https://boomkat.com/search?q={q}"},
            {"store": "beatport", "label": "Beatport", "url": f"https://www.beatport.com/search?q={q}"},
        ],   # (Juno Download shut down 2026-06-01; removed)
    }

def _parse_tracklist_text(text):
    """Best-effort parse of a pasted tracklist / song list into (artist, title) pairs.
    Handles '1. Artist - Title [Label]', '[00:00] Artist – Title', plain 'Artist - Title'.
    Artist-first is the norm (1001tracklists / Shazam / mix descriptions). Unparseable
    lines are skipped — false negatives are fine."""
    import re
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r'\s{2,}', ' ', line)   # collapse whitespace runs FIRST — bounds the O(n^2) split/subs below
        if len(line) > 2000:                   # cap per-line length before any regex (a pathological line can't wedge it)
            continue
        prev = None                                                              # strip any leading index / timestamp
        while prev != line:                                                      # bullets, in any order, until stable
            prev = line
            line = re.sub(r'^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*', '', line)   # timestamp
            line = re.sub(r'^\s*\d{1,3}[\.\)]\s*', '', line)                          # index "12." / "12)"
            line = re.sub(r'^\s*[-•*]\s*', '', line)                                  # bullet
        parts = re.split(r'\s+[-–—]\s+', line, maxsplit=1)                        # first ' - ' / ' – '
        if len(parts) != 2:
            continue
        artist = re.sub(r'\s*[\[(].*?[\])]\s*$', '', parts[0]).strip()
        title  = re.sub(r'\s*[\[(].*?[\])]\s*$', '', parts[1]).strip()
        title  = re.sub(r'\s+w/\s+.*$', '', title).strip()                       # 'w/ …' collab/vocal marker
        if artist and title and len(artist) < 200 and len(title) < 200:
            out.append({"artist": artist, "title": title, "raw": raw.strip()})
    return out

def _parse_shazam_csv(file_storage):
    """Parse a Shazam library export (myshazam 'Download CSV' or the GDPR
    SyncedShazams.csv). Header names aren't documented + have changed, so detect
    title/artist columns case-insensitively; skip a preamble line if present.
    Falls back to plain 'artist - title' parsing if it isn't really a CSV."""
    import csv, io
    raw = file_storage.read(2_000_000)        # cap: a Shazam library CSV is tiny; refuse to slurp a huge upload
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8-sig', errors='replace')
    lines = raw.splitlines()
    reader = None
    for start in range(min(6, len(lines))):
        try:
            rdr = csv.DictReader(io.StringIO("\n".join(lines[start:])))
            fns = [(f or '').strip().lower() for f in (rdr.fieldnames or [])]
            if any(k in fns for k in ('title', 'track', 'song', 'name')) and any('artist' in f for f in fns):
                reader = rdr
                break
        except Exception:
            continue
    if not reader:
        return _parse_tracklist_text(raw)
    out = []
    for row in reader:
        # a stray unquoted comma gives DictReader a ragged row: the overflow lands under
        # restkey (None) as a LIST. Coerce non-str values + skip bad rows rather than 500.
        try:
            norm = {(k or '').strip().lower():
                    (v if isinstance(v, str) else (v[0] if v else '')).strip()
                    for k, v in row.items() if k is not None}
            title  = next((norm[k] for k in ('title', 'track', 'song', 'name') if norm.get(k)), '')
            artist = next((norm[k] for k in norm if 'artist' in k and norm[k]), '')
            if artist and title:
                out.append({"artist": artist, "title": title, "raw": f"{artist} - {title}"})
        except Exception:
            continue
    return out

def _annotate_candidates(cands):
    """Flag each parsed candidate: already in the wishlist? already in the library?
    De-dupe within the paste itself. So the review screen shows what's new."""
    wkeys = set(_norm_at(r["artist"], r["title"]) for r in query("SELECT artist, title FROM wishlist"))
    lkeys = set(_norm_at(r["artist_name"], r["title"]) for r in query(
        "SELECT artist_name, title FROM tracks WHERE is_deleted=0"))
    seen, out = set(), []
    for c in cands:
        k = _norm_at(c["artist"], c["title"])
        if k in seen:
            continue
        seen.add(k)
        c["in_wishlist"] = k in wkeys
        c["in_library"]  = k in lkeys
        out.append(c)
    return {"candidates": out, "count": len(out)}


def _wish_full():
    """The trial's wishlist holds 100 (record_shop.WISH_MAX): a refusal to send
    back when it is full, else None. Bought ("supported") wishes don't count."""
    if not IS_GR:
        return None
    import record_shop
    room = record_shop.wish_room()
    if room["left"] > 0:
        return None
    return jsonify({"error": record_shop.WISH_FULL.format(n=room["max"]), "full": True}), 409


@app.route("/api/wishlist")
def api_wishlist():
    tag      = (request.args.get("tag") or "").strip()
    place_id = request.args.get("place_id", type=int)
    raw_status = (request.args.get("status") or "").strip()
    status   = WISHLIST_STATUS_ALIASES.get(raw_status, raw_status)
    bucket   = (request.args.get("bucket") or "").strip()
    where, params = [], []
    if tag:
        where.append("(',' || COALESCE(w.tags,'') || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if place_id is not None:
        where.append("w.place_id = ?"); params.append(place_id)
    if status in WISHLIST_STATUSES:
        where.append("w.status = ?"); params.append(status)
    if bucket in WISHLIST_BUCKETS:
        where.append("w.bucket = ?"); params.append(bucket)
    sql = (
        "SELECT w.*, p.name AS place_name, "
        "       t.title AS fulfilled_title, t.artist_name AS fulfilled_artist, "
        "       (SELECT COUNT(*) FROM wishlist_research wr WHERE wr.wishlist_id=w.id) AS research_count "
        "FROM wishlist w "
        "LEFT JOIN places p ON p.id = w.place_id "
        "LEFT JOIN tracks t ON t.id = w.fulfilled_track_id AND t.is_deleted = 0"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY w.created_at DESC"
    rows = query(sql, params)
    for r in rows:
        r["tag_list"] = _parse_tags(r.get("tags"))
        r["links"] = _discovery_links(r.get("artist"), r.get("title"))   # listen + buy-direct deep-links
    return jsonify(rows)


@app.route("/api/wishlist", methods=["POST"])
def api_create_wishlist():
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    title    = (d.get("title")  or "").strip() or None
    # a wish needs a link OR a name — manual entries ("44 mins into Set A", a
    # typed album/track) carry no link, only a title.
    if not url and not title:
        return jsonify({"error": "a link or a name is required"}), 400
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    artist   = (d.get("artist") or "").strip() or None
    notes    = (d.get("notes")  or "").strip() or None
    source   = (d.get("source") or "").strip() or (_detect_source(url) if url else "manual")
    place_id = d.get("place_id") if isinstance(d.get("place_id"), int) else None
    tags     = ",".join(_parse_tags(d.get("tags"))) or None
    status_in = (d.get("status") or "wishing").strip()
    status    = WISHLIST_STATUS_ALIASES.get(status_in, status_in)
    if status not in WISHLIST_STATUSES:
        status = "wishing"
    bucket_in = (d.get("bucket") or "").strip()
    bucket    = bucket_in if bucket_in in WISHLIST_BUCKETS else WISHLIST_BUCKET_DEFAULT
    artwork_url = (d.get("artwork_url") or d.get("image") or "").strip() or None
    provenance  = (d.get("provenance") or "").strip() or None   # carried so undo can restore "↳ from …"
    if status != "supported":
        full = _wish_full()
        if full:
            return full
    wid = execute(
        "INSERT INTO wishlist (url,title,artist,source,notes,place_id,tags,status,bucket,artwork_url,provenance) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (url or "", title, artist, source, notes, place_id, tags, status, bucket, artwork_url, provenance)
    )
    row = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row), 201


@app.route("/api/wishlist/<int:wid>", methods=["PATCH"])
def api_patch_wishlist(wid):
    w = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if not w:
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    updates = {}
    if "url" in d:
        url = (d["url"] or "").strip()
        if not url:
            return jsonify({"error": "url cannot be empty"}), 400
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        updates["url"]    = url
        # Re-detect source when URL changes, unless caller also supplies one
        if "source" not in d:
            updates["source"] = _detect_source(url)
    for k in ("title", "artist", "notes", "source"):
        if k in d:
            updates[k] = ((d[k] or "").strip() or None)
    if "place_id" in d:
        updates["place_id"] = d["place_id"] if isinstance(d["place_id"], int) else None
    if "tags" in d:
        updates["tags"] = ",".join(_parse_tags(d.get("tags"))) or None
    if "status" in d:
        s = (d["status"] or "wishing").strip()
        s = WISHLIST_STATUS_ALIASES.get(s, s)
        updates["status"] = s if s in WISHLIST_STATUSES else "wishing"
        # leaving 'supported' clears how it was bought (unless caller resets it)
        if updates["status"] != "supported" and "bought_via" not in d:
            updates["bought_via"] = None
    if "bought_via" in d:
        bv = (d["bought_via"] or "").strip().lower()
        updates["bought_via"] = bv if bv in ("bandcamp", "other") else None
    if "bucket" in d:
        b = (d["bucket"] or "").strip()
        updates["bucket"] = b if b in WISHLIST_BUCKETS else WISHLIST_BUCKET_DEFAULT
    if "buy_reason" in d:
        r = (d["buy_reason"] or "").strip().lower()
        updates["buy_reason"] = r if r in WISHLIST_BUY_REASONS else None
    if "buy_reason_note" in d:
        updates["buy_reason_note"] = ((d["buy_reason_note"] or "").strip() or None)
    if "fulfilled_track_id" in d:
        v = d["fulfilled_track_id"]
        updates["fulfilled_track_id"] = v if isinstance(v, int) else None
        # Stamp fulfilled_at iff we are setting (not clearing).
        if updates["fulfilled_track_id"]:
            updates["fulfilled_at"] = "datetime('now')"
        else:
            updates["fulfilled_at"] = None
    if not updates:
        return jsonify({"error": "no fields"}), 400
    # Handle the one expression value (datetime('now')) separately from params.
    cols     = list(updates.keys())
    set_bits = []
    vals     = []
    for k in cols:
        v = updates[k]
        if v == "datetime('now')":
            set_bits.append(f"{k}=datetime('now')")
        else:
            set_bits.append(f"{k}=?")
            vals.append(v)
    execute(f"UPDATE wishlist SET {', '.join(set_bits)} WHERE id=?", vals + [wid])
    row = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


@app.route("/api/wishlist/<int:wid>", methods=["DELETE"])
def api_delete_wishlist(wid):
    execute("DELETE FROM wishlist_research WHERE wishlist_id=?", (wid,))
    execute("DELETE FROM wishlist WHERE id=?", (wid,))
    return jsonify({"ok": True})


# ── THE HUNT: research attached to a wish (notes, forum links, comment shots) ──
@app.route("/api/wishlist/<int:wid>/research")
def api_wishlist_research(wid):
    return jsonify(query("SELECT * FROM wishlist_research WHERE wishlist_id=? ORDER BY created_at", (wid,)))


@app.route("/api/wishlist/<int:wid>/research", methods=["POST"])
def api_wishlist_research_add(wid):
    if not query_one("SELECT 1 FROM wishlist WHERE id=?", (wid,)):
        return jsonify({"error": "wishlist item not found"}), 404
    f = request.files.get("file")
    if f and f.filename:                          # a screenshot — comment section, flyer, listing
        import uuid, re
        base = os.path.basename(f.filename)
        ext = ("." + re.sub(r"[^A-Za-z0-9]", "", base.rsplit(".", 1)[1].lower())[:8]) if "." in base else ""
        if ext.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return jsonify({"error": "image files only (png/jpg/jpeg/gif/webp)"}), 400
        fname = "hunt_" + uuid.uuid4().hex + ext
        hd = os.path.join(_DATA_STATIC, "hunt"); os.makedirs(hd, exist_ok=True)
        f.save(os.path.join(hd, fname))
        text = (request.form.get("text") or "").strip() or None
        rid = execute("INSERT INTO wishlist_research(wishlist_id,kind,text,image_path) VALUES(?,?,?,?)",
                      (wid, "image", text, f"/static/hunt/{fname}"))
    else:
        d = request.get_json() or {}
        url  = (d.get("url") or "").strip()
        text = (d.get("text") or "").strip()
        if url:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            rid = execute("INSERT INTO wishlist_research(wishlist_id,kind,text,url) VALUES(?,?,?,?)",
                          (wid, "link", text or None, url))
        elif text:
            rid = execute("INSERT INTO wishlist_research(wishlist_id,kind,text) VALUES(?,?,?)",
                          (wid, "note", text))
        else:
            return jsonify({"error": "add a note, a link, or a screenshot"}), 400
    return jsonify(query_one("SELECT * FROM wishlist_research WHERE id=?", (rid,))), 201


@app.route("/api/wishlist/research/<int:rid>", methods=["DELETE"])
def api_wishlist_research_del(rid):
    row = query_one("SELECT * FROM wishlist_research WHERE id=?", (rid,))
    if row and row.get("image_path"):
        try:
            p = os.path.join(_DATA_STATIC, row["image_path"].replace("/static/", "", 1))
            if os.path.isfile(p):
                move_to_trash(p)                   # recoverable, never hard-deleted
        except Exception:
            pass
    execute("DELETE FROM wishlist_research WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.route("/api/wishlist/<int:wid>/visit", methods=["POST"])
def api_visit_wishlist(wid):
    execute("UPDATE wishlist SET last_visited_at=datetime('now') WHERE id=?", (wid,))
    return jsonify({"ok": True})


@app.route("/api/wishlist/<int:wid>/fulfill", methods=["POST"])
def api_fulfil_wishlist(wid):
    """Manual link: 'I bought this and added it as track X.'
    Sets status='supported', fulfilled_track_id, fulfilled_at."""
    d = request.get_json() or {}
    tid = d.get("track_id")
    if not isinstance(tid, int):
        return jsonify({"error": "track_id (int) required"}), 400
    if not query_one("SELECT id FROM tracks WHERE id=? AND is_deleted=0", (tid,)):
        return jsonify({"error": "track not found"}), 404
    if not query_one("SELECT id FROM wishlist WHERE id=?", (wid,)):
        return jsonify({"error": "wishlist row not found"}), 404
    execute(
        "UPDATE wishlist SET status='supported', fulfilled_track_id=?, "
        "fulfilled_at=datetime('now'), last_visited_at=datetime('now') WHERE id=?",
        (tid, wid)
    )
    row = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


@app.route("/api/wishlist/<int:wid>/unfulfill", methods=["POST"])
def api_unfulfil_wishlist(wid):
    """Undo a manual fulfilment — clear link + reopen to 'wishing'."""
    if not query_one("SELECT id FROM wishlist WHERE id=?", (wid,)):
        return jsonify({"error": "wishlist row not found"}), 404
    execute(
        "UPDATE wishlist SET status='wishing', fulfilled_track_id=NULL, fulfilled_at=NULL WHERE id=?",
        (wid,)
    )
    row = query_one("SELECT * FROM wishlist WHERE id=?", (wid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


@app.route("/api/discover/parse", methods=["POST"])
def api_discover_parse():
    """Parse a pasted tracklist / song list OR an uploaded Shazam CSV into reviewable
    candidates, each flagged for whether it's already in the wishlist or library.
    No external calls — just text parsing, so it works for any source you can paste."""
    f = request.files.get("file")
    if f and f.filename:
        cands = _parse_shazam_csv(f)
    else:
        d = request.get_json(silent=True) or {}
        cands = _parse_tracklist_text((d.get("text") or "")[:300_000])   # cap pasted text
    return jsonify(_annotate_candidates(cands))

@app.route("/api/discover/mixcloud", methods=["POST"])
def api_discover_mixcloud():
    """Pull a tracklist from a Mixcloud show URL via the free, no-auth Mixcloud API
    (only populated when the uploader typed one). A clean structured source; falls
    back to 'paste it yourself' when the show has no tracklist."""
    import re, json as _json, urllib.request
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    m = re.search(r'mixcloud\.com/([^/]+/[^/?#]+)', url)
    if not m:
        return jsonify({"error": "Not a Mixcloud show URL.", "candidates": [], "count": 0}), 400
    api = "https://api.mixcloud.com/" + m.group(1) + "/"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "MusicRoom/1.0 (personal)"})
        data = _json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace"))
    except Exception:
        return jsonify({"error": "Couldn't reach Mixcloud for that URL.", "candidates": [], "count": 0}), 502
    cands = []
    for s in (data.get("sections") or []):
        a = ((s.get("artist") or {}).get("name") or "").strip()
        t = ((s.get("track") or {}).get("name") or "").strip()
        if a and t:
            cands.append({"artist": a, "title": t, "raw": f"{a} - {t}"})
    res = _annotate_candidates(cands)
    res["mix_name"] = data.get("name")
    if not cands:
        res["note"] = "Mixcloud has no typed tracklist for this show — paste it instead."
    return jsonify(res)

@app.route("/api/discover/add", methods=["POST"])
def api_discover_add():
    """Add reviewed (ticked) candidates to the wishlist, with where-you-found-them
    provenance. Skips ones already in the wishlist. Status starts at 'wishing'."""
    d = request.get_json() or {}
    provenance = (d.get("provenance") or "").strip() or None
    source     = (d.get("source") or "import").strip() or "import"
    existing = set(_norm_at(r["artist"], r["title"]) for r in query("SELECT artist, title FROM wishlist"))
    added, skipped, no_room = 0, 0, 0
    for it in (d.get("items") or []):
        artist = (it.get("artist") or "").strip()
        title  = (it.get("title") or "").strip()
        if not (artist and title):
            continue
        if _norm_at(artist, title) in existing:
            skipped += 1
            continue
        if _wish_full():                       # the trial's wishlist holds 100
            no_room += 1
            continue
        # default the primary link to a Bandcamp search (artist-direct) so every
        # imported row is immediately clickable; a specific url overrides it.
        url = (it.get("url") or "").strip() or _discovery_links(artist, title)["buy"][0]["url"]
        execute("INSERT INTO wishlist (url, title, artist, source, provenance, notes, status) "
                "VALUES (?,?,?,?,?,?, 'wishing')",
                (url, title, artist, source, provenance, (it.get("notes") or "").strip() or None))
        existing.add(_norm_at(artist, title))
        added += 1
    return jsonify({"added": added, "skipped": skipped, "no_room": no_room})


@app.route("/api/wishlist/tags")
def api_wishlist_tags():
    rows = query("SELECT tags FROM wishlist WHERE tags IS NOT NULL AND tags != ''")
    counts = {}
    for r in rows:
        for t in _parse_tags(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    items = [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return jsonify(items)


@app.route("/api/place/<int:pid>/wishlist")
def api_place_wishlist(pid):
    rows = query(
        "SELECT * FROM wishlist WHERE place_id=? ORDER BY status='wishing' DESC, created_at DESC",
        (pid,)
    )
    for r in rows:
        r["tag_list"] = _parse_tags(r.get("tags"))
    return jsonify(rows)


# =============================================================
# RESOURCES — Learn → Resources tab.
# Personal helpers for DJing: genre references, theory, history,
# technique, scene notes. Free-text `kind` so the same table can
# hold links, PDFs (referenced by path), in-app notes, videos,
# books — and whatever future kind we don't know about yet.
# =============================================================

RESOURCE_KINDS_HINT = ("link", "pdf", "note", "video", "book", "audio", "image")


def _detect_resource_kind(url, file_path):
    if file_path:
        ext = (os.path.splitext(file_path)[1] or "").lower()
        if ext == ".pdf":   return "pdf"
        if ext in (".mp4", ".mov", ".m4v", ".webm"): return "video"
        if ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a"): return "audio"
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"): return "image"
        return "file"
    if url:
        u = url.lower()
        if "youtube.com" in u or "youtu.be" in u or "vimeo.com" in u: return "video"
        return "link"
    return "note"


@app.route("/api/resources")
def api_resources():
    kind     = (request.args.get("kind")     or "").strip()
    category = (request.args.get("category") or "").strip()
    tag      = (request.args.get("tag")      or "").strip()
    place_id = request.args.get("place_id", type=int)
    search   = (request.args.get("search")   or "").strip()
    where, params = [], []
    if kind:
        where.append("r.kind = ?"); params.append(kind)
    if category:
        where.append("r.category = ?"); params.append(category)
    if tag:
        where.append("(',' || COALESCE(r.tags,'') || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if place_id is not None:
        where.append("r.place_id = ?"); params.append(place_id)
    if search:
        like = f"%{search}%"
        where.append("(r.title LIKE ? OR r.description LIKE ? OR r.body LIKE ? OR r.url LIKE ? OR r.tags LIKE ? OR r.category LIKE ?)")
        params.extend([like, like, like, like, like, like])
    sql = "SELECT r.*, p.name as place_name FROM resources r LEFT JOIN places p ON p.id=r.place_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.is_pinned DESC, r.created_at DESC"
    rows = query(sql, params)
    for r in rows:
        r["tag_list"] = _parse_tags(r.get("tags"))
    return jsonify(rows)


@app.route("/api/resources", methods=["POST"])
def api_create_resource():
    d = request.get_json() or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    url       = (d.get("url") or "").strip() or None
    file_path = (d.get("file_path") or "").strip() or None
    body      = (d.get("body") or "").strip() or None
    description = (d.get("description") or "").strip() or None
    category    = (d.get("category") or "").strip() or None
    if url and not url.startswith(("http://", "https://", "file://", "spotify:", "music:", "tidal:")):
        url = "https://" + url
    kind = (d.get("kind") or "").strip() or _detect_resource_kind(url, file_path)
    place_id = d.get("place_id") if isinstance(d.get("place_id"), int) else None
    tags = ",".join(_parse_tags(d.get("tags"))) or None
    is_pinned = 1 if d.get("is_pinned") else 0
    rid = execute(
        "INSERT INTO resources (kind,title,url,file_path,body,description,category,tags,place_id,is_pinned) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, title, url, file_path, body, description, category, tags, place_id, is_pinned)
    )
    row = query_one("SELECT * FROM resources WHERE id=?", (rid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row), 201


@app.route("/api/resource/<int:rid>", methods=["PATCH"])
def api_patch_resource(rid):
    r = query_one("SELECT * FROM resources WHERE id=?", (rid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    updates = {}
    if "title" in d:
        val = (d["title"] or "").strip()
        if not val:
            return jsonify({"error": "title cannot be empty"}), 400
        updates["title"] = val
    if "url" in d:
        url = (d["url"] or "").strip()
        if url and not url.startswith(("http://", "https://", "file://", "spotify:", "music:", "tidal:")):
            url = "https://" + url
        updates["url"] = url or None
    for k in ("file_path", "body", "description", "category", "kind"):
        if k in d:
            updates[k] = ((d[k] or "").strip() or None)
    if "place_id" in d:
        updates["place_id"] = d["place_id"] if isinstance(d["place_id"], int) else None
    if "tags" in d:
        updates["tags"] = ",".join(_parse_tags(d.get("tags"))) or None
    if "is_pinned" in d:
        updates["is_pinned"] = 1 if d["is_pinned"] else 0
    if not updates:
        return jsonify({"error": "no fields"}), 400
    cols  = list(updates.keys())
    set_c = ", ".join(f"{k}=?" for k in cols) + ", updated_at=datetime('now')"
    execute(f"UPDATE resources SET {set_c} WHERE id=?", [updates[k] for k in cols] + [rid])
    row = query_one("SELECT * FROM resources WHERE id=?", (rid,))
    if row:
        row["tag_list"] = _parse_tags(row.get("tags"))
    return jsonify(row)


@app.route("/api/resource/<int:rid>", methods=["DELETE"])
def api_delete_resource(rid):
    execute("DELETE FROM resources WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.route("/api/resource/<int:rid>/open", methods=["POST"])
def api_open_resource(rid):
    r = query_one("SELECT * FROM resources WHERE id=?", (rid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    execute("UPDATE resources SET last_visited_at=datetime('now') WHERE id=?", (rid,))
    target = r.get("url") or r.get("file_path") or ""
    if not target:
        # Note kind — nothing to open externally. Caller should handle inline.
        return jsonify({"ok": True, "inline": True})
    try:
        if target.startswith(("http://", "https://", "spotify:", "music:", "tidal:")):
            native = _rewrite_to_native_uri(target)
            is_native = native != target
            browser = (get_setting("preferred_browser", "") or "").strip()
            mr_os.open_target(native if is_native else target, app=None if is_native else (browser or None))
        else:
            # a file: the computer's own app for it (Preview for PDFs, etc.)
            if not os.path.exists(target):
                return jsonify({"error": f"file not found: {target}"}), 404
            mr_os.open_target(target)
        return jsonify({"ok": True, "opened": target})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/resource/tags")
def api_resource_tags():
    rows = query("SELECT tags FROM resources WHERE tags IS NOT NULL AND tags != ''")
    counts = {}
    for r in rows:
        for t in _parse_tags(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    items = [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return jsonify(items)


@app.route("/api/resource/categories")
def api_resource_categories():
    rows = query(
        "SELECT category, COUNT(*) as count FROM resources "
        "WHERE category IS NOT NULL AND category != '' "
        "GROUP BY category ORDER BY count DESC, category ASC"
    )
    return jsonify(rows)


@app.route("/api/resource/kinds")
def api_resource_kinds():
    rows = query(
        "SELECT kind, COUNT(*) as count FROM resources "
        "GROUP BY kind ORDER BY count DESC, kind ASC"
    )
    # Always surface the standard hint kinds even if zero-count, so the
    # add-modal dropdown has sane defaults on a fresh install.
    existing = {r["kind"] for r in rows}
    for k in RESOURCE_KINDS_HINT:
        if k not in existing:
            rows.append({"kind": k, "count": 0})
    return jsonify(rows)


# =============================================================
# SPOTIFY BRIDGE — see music you have on a DSP, recognise what
# you already own locally, find the rest on Bandcamp / Discogs.
# This is *not* a player. Music Room never plays Spotify audio —
# the goal is migration toward ownership.
# (2026-11-13: streamed records can now sit in a grúpa and in the deck's cue,
# but they are still played by the service's OWN player beside the deck —
# see streams.py + static/js/snt_embed.js. No stream audio reaches our player.)
# =============================================================

def _get_setting_threadsafe(key, default=""):
    """Read a setting using a fresh SQLite connection — safe to call from
    background threads where Flask's `g` request context isn't available.
    The regular get_setting() needs the request context."""
    try:
        conn = _connect_threadsafe()
        cur  = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row  = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def _set_setting_threadsafe(key, value):
    try:
        conn = _connect_threadsafe()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value) if value is not None else ""),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


class _SpotifySettingsIO:
    """Adapter so spotify.py can use get_setting / set_setting.

    Uses the thread-safe variants so the SpotifyClient can be driven from
    a background batch thread without a Flask request context. Both code
    paths share the same `settings` table so token refreshes done from
    one path are visible to the other.
    """
    def get(self, key, default=""):
        return _get_setting_threadsafe(key, default)
    def set(self, key, value):
        _set_setting_threadsafe(key, value)

_SPOTIFY_SETTINGS_IO = _SpotifySettingsIO()


def _get_spotify():
    from spotify import SpotifyClient
    return SpotifyClient(_SPOTIFY_SETTINGS_IO)


def _norm_match(s):
    """Loose normalisation for artist/title match (case, punctuation,
    bracketed remix/feat tags)."""
    if not s: return ""
    out = s.lower()
    out = _re.sub(r"\(.*?\)|\[.*?\]", " ", out)
    out = _re.sub(r"[^\w\s]", " ", out)
    out = _re.sub(r"\s+", " ", out).strip()
    return out


def _match_library_batch(items):
    """items = [{'spotify_id','artist','title'}, …] → {spotify_id: local_track_id_or_None}"""
    rows = query(
        "SELECT id, title, artist_name FROM tracks "
        "WHERE is_deleted=0 AND title IS NOT NULL AND artist_name IS NOT NULL"
    )
    lookup = {}
    for r in rows:
        key = (_norm_match(r["artist_name"]), _norm_match(r["title"]))
        if key[0] and key[1]:
            lookup.setdefault(key, r["id"])
    out = {}
    for it in items or []:
        sid = it.get("spotify_id")
        if not sid: continue
        # Spotify joins multi-artist with ", " — try the full string first,
        # then fall back to just the first artist.
        artist = it.get("artist") or ""
        title  = it.get("title")  or ""
        key = (_norm_match(artist), _norm_match(title))
        match = lookup.get(key)
        if not match and "," in artist:
            first = artist.split(",", 1)[0]
            match = lookup.get((_norm_match(first), _norm_match(title)))
        out[sid] = match
    return out


def _upsert_spotify_track(t):
    """Cache a flattened Spotify track for cross-reference."""
    if not t or not t.get("spotify_id"): return
    existing = query_one("SELECT id, in_playlist_ids FROM spotify_tracks WHERE spotify_id=?", (t["spotify_id"],))
    if existing:
        execute(
            "UPDATE spotify_tracks SET title=?, artist=?, album=?, album_image_url=?, "
            "duration_ms=?, last_seen_at=datetime('now') WHERE spotify_id=?",
            (t.get("title"), t.get("artist"), t.get("album"), t.get("album_image_url"),
             t.get("duration_ms"), t["spotify_id"])
        )
    else:
        execute(
            "INSERT INTO spotify_tracks (spotify_id,title,artist,album,album_image_url,"
            "duration_ms,added_at,is_saved) VALUES (?,?,?,?,?,?,?,0)",
            (t["spotify_id"], t.get("title"), t.get("artist"), t.get("album"),
             t.get("album_image_url"), t.get("duration_ms"), t.get("added_at"))
        )


@app.route("/spotify")
def spotify_page():
    return render_template("spotify.html")


def _spotify_redirect_uri():
    """Where Spotify sends the listener back after sign-in: the ONE address
    registered on their Spotify app (spotify.DEFAULT_REDIRECT_URI, port 5177,
    unless one was set by hand in settings). It does NOT follow this copy's
    own port — Spotify's dashboard only reliably accepts an address with an
    explicit port, so there is one, and _spotify_callback_listener makes sure
    something is listening on it."""
    return _get_spotify().redirect_uri


def _own_port():
    host = request.host or ""
    try:
        return int(host.rsplit(":", 1)[1]) if ":" in host else int(os.environ.get("MUSICROOM_PORT", "5177"))
    except ValueError:
        return 5177


_spotify_cb = {"server": None}


def _spotify_callback_listener(port):
    """This copy runs on another port than the registered address names (the
    trial's dev copy on 5181, a native build that found 5177 taken): open
    that port just for Spotify's answer, finish the sign-in, close it again.
    True when the port is ours to listen on (or we already are); False when
    something else holds it."""
    import http.server
    if _spotify_cb["server"] is not None:
        return True

    def _stop():
        srv = _spotify_cb["server"]
        _spotify_cb["server"] = None
        if srv is not None:
            try:
                srv.shutdown(); srv.server_close()
            except Exception:
                pass

    class _Answer(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/api/spotify/callback":
                self.send_response(404); self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            page = _spotify_finish((q.get("code") or [""])[0], (q.get("state") or [""])[0],
                                   (q.get("error") or [None])[0], link=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            threading.Thread(target=_stop, daemon=True).start()       # one answer, then the port is let go

    try:
        srv = http.server.HTTPServer(("127.0.0.1", int(port)), _Answer)
    except OSError:
        return False
    _spotify_cb["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True, name="spotify-callback").start()
    t = threading.Timer(600, _stop)                                   # nobody answered in ten minutes: let it go
    t.daemon = True
    t.start()
    return True


def _spotify_finish(code, state, err, link=True):
    """Spotify's answer → the tokens kept, and the page the listener sees."""
    if err:
        return _spotify_callback_page(f"Spotify returned an error: {err}", ok=False, link=link)
    if not code or not state:
        return _spotify_callback_page("Missing code/state in callback.", ok=False, link=link)
    ok, info = _get_spotify().complete_auth(code, state)
    if not ok:
        if "PKCE" in str(info):
            info = "this sign-in was started by another copy of Music Room. Start it again from the copy you are using."
        return _spotify_callback_page(f"Could not connect: {info}", ok=False, link=link)
    _get_spotify_remote().forget()          # the deck may drive Spotify from now
    return _spotify_callback_page("Connected. You can close this tab.", ok=True, link=link)


@app.route("/api/spotify/status")
def api_spotify_status():
    return jsonify(_get_spotify().status())


@app.route("/api/spotify/checkup")
def api_spotify_checkup():
    """Why does it only play previews? Asked of Spotify itself, answered in words
    (spotify.SpotifyClient.checkup): listed on the key? Premium? a Spotify app open?"""
    import spotify as _sp
    import spotify_remote as _spr
    out = _get_spotify().checkup(app_there=_spr.spotify_app_here())
    what, do = _sp.CHECKUP_WORDS.get(out["step"], ("", ""))
    name = out.get("name") or "the same account"
    try:
        p = _get_seinnteoir_player()
        r = getattr(p, "_remote", None)
        if r is not None:
            r.forget()                       # (whatever changed, the deck asks afresh on its next record)
    except Exception:
        pass
    return jsonify(dict(out, what=what, do=do.replace("{name}", name)))


@app.route("/api/spotify/auth/start", methods=["POST"])
def api_spotify_auth_start():
    """Begin PKCE OAuth. Returns the Spotify authorize URL — the front
    end opens it in the preferred external browser. After login, Spotify
    redirects to /api/spotify/callback (this server) and we finish there."""
    spo = _get_spotify()
    if not spo.client_id:
        return jsonify({"error": "Spotify Client ID not set in Settings."}), 400
    ru = _spotify_redirect_uri()
    try:
        rp = urllib.parse.urlsplit(ru)
        want = rp.port or (443 if rp.scheme == "https" else 80)
        local = (rp.hostname or "") in ("127.0.0.1", "::1", "localhost")
    except ValueError:
        want, local = None, False
    if local and want != _own_port():
        # Spotify will answer on the registered port, not ours: listen there
        if not _spotify_callback_listener(want):
            # the port is held — fine if it is another copy of Music Room (it
            # takes the answer itself), a dead end if it is anything else
            try:
                other = _json.loads(urllib.request.urlopen(f"http://127.0.0.1:{want}/api/spotify/status", timeout=1.5).read())
                is_mr = isinstance(other, dict) and "client_id_set" in other
            except Exception:
                is_mr = False
            if not is_mr:
                return jsonify({"error": f"Another program is using port {want}, which Spotify's sign-in comes back on.",
                                "hint": f"Close whatever is using port {want} on this computer and try again."}), 409
    url, _state = spo.begin_auth(ru)
    if not url:
        return jsonify({"error": "could not start auth"}), 500
    return jsonify({"auth_url": url, "redirect_uri": ru})


@app.route("/api/spotify/callback")
def api_spotify_callback():
    """Spotify redirects here (GET) after the user authorises us.
    We finish the token exchange and bounce the user back to /spotify."""
    return _spotify_finish(request.args.get("code", ""), request.args.get("state", ""), request.args.get("error"))


def _spotify_callback_page(msg, ok=True, link=True):
    """Tiny self-contained HTML so this page can stand on its own in the
    user's browser tab without dragging in the rest of the app. `link` False
    = served by the one-answer listener, whose port closes behind it: no way
    back to offer, the listener just returns to the app."""
    colour = "#7dba9a" if ok else "#c08070"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Spotify · Music Room</title>
<style>
  body {{ background:#0a0e0c; color:#c2ddc8; font-family:system-ui, -apple-system, sans-serif;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; padding:24px; text-align:center; }}
  .card {{ max-width:420px; padding:36px 28px; border:1px solid #253d28; border-radius:10px;
          background:rgba(20,28,22,0.6); }}
  h1 {{ font-family:Georgia, serif; font-style:italic; font-weight:400; font-size:1.4rem;
       color:{colour}; margin:0 0 14px; }}
  p {{ font-size:0.94rem; line-height:1.6; color:#a8c4ad; }}
  a {{ color:#f5e6b0; }}
</style></head><body>
<div class="card">
  <h1>{msg}</h1>
  <p>{'<a href="/spotify">Return to Music Room</a>' if link else 'Go back to the Music Room.'}</p>
  <script>setTimeout(() => {{ try {{ window.close(); }} catch(_){{}} }}, 2500);</script>
</div></body></html>"""


@app.route("/api/spotify/disconnect", methods=["POST"])
def api_spotify_disconnect():
    _get_spotify().disconnect()
    _get_spotify_remote().forget()
    return jsonify({"ok": True})


@app.route("/api/spotify/open", methods=["POST"])
def api_spotify_open():
    """The Spotify mark on the deck: bring Spotify to the front. Nothing is
    played or paused. {url} = the record's page, opened in the browser only
    when this computer has no Spotify app."""
    import spotify_remote
    via = spotify_remote.show_app((request.get_json(silent=True) or {}).get("url") or "")
    return jsonify({"ok": bool(via), "via": via}), (200 if via else 404)


@app.route("/api/spotify/control", methods=["POST"])
def api_spotify_control():
    """The deck's keys while it shows the listener's own Spotify (the 'guest'
    in the deck's state): play / pause / toggle / next / prev / seek."""
    d = request.get_json(silent=True) or {}
    if not _get_spotify().can_control():
        return jsonify({"error": "Spotify can't be driven from here",
                        "hint": "Sign in to Spotify on the Spotify page. Playing through it needs Spotify Premium."}), 409
    ok = _get_seinnteoir_player().guest_control((d.get("action") or "").strip(), d.get("value"))
    return jsonify({"ok": bool(ok)}), (200 if ok else 400)


# A search that worked is remembered, so asking again costs Spotify nothing, and if Spotify then has one of its
# brief hiccups the last good answer is shown instead of an error. (Search results move slowly; ten minutes
# counts as fresh, and a day-old answer is still better than none.)
_SPOTIFY_SEARCH_CACHE = {}
_SEARCH_FRESH_S, _SEARCH_KEEP_S, _SEARCH_MAX = 600, 86400, 300


def _spotify_search_cached(spo, q, now=None):
    """-> (status, found, stale). `stale` is True when Spotify did not answer and an older good answer was used."""
    now = _time.time() if now is None else now
    key = " ".join(q.lower().split())
    hit = _SPOTIFY_SEARCH_CACHE.get(key)
    if hit and now - hit[0] < _SEARCH_FRESH_S:
        return 200, hit[1], False
    status, found = spo.search(q)
    if status == 200:
        _SPOTIFY_SEARCH_CACHE[key] = (now, found)
        if len(_SPOTIFY_SEARCH_CACHE) > _SEARCH_MAX:                       # drop the oldest few
            for k in sorted(_SPOTIFY_SEARCH_CACHE, key=lambda k: _SPOTIFY_SEARCH_CACHE[k][0])[:_SEARCH_MAX // 5]:
                _SPOTIFY_SEARCH_CACHE.pop(k, None)
        return 200, found, False
    if status != 401 and hit and now - hit[0] < _SEARCH_KEEP_S:
        return 200, hit[1], True
    return status, found, False


@app.route("/api/spotify/search")
def api_spotify_search():
    """What the listener typed, found on Spotify — tracks, albums, artists —
    to add to a grúpa. Loose matches are dropped unless nothing matches
    (spotify.tidy_search). Each row comes back ready to keep: link, title,
    who by, the cover at two sizes (`thumb` for the list, `image` to keep),
    length, and whether it is marked AI-made."""
    q = (request.args.get("q") or "").strip()
    spo = _get_spotify()
    if len(q) < 2 or not spo.is_connected():
        return jsonify({"results": [], "connected": spo.is_connected()})
    status, found, stale = _spotify_search_cached(spo, q)
    if status != 200:
        return jsonify({"results": [], "connected": status != 401, "error": "Spotify didn't answer"}), 200
    from spotify import tidy_search
    marked, cleared = _ai_marks()
    out = []
    for f in tidy_search(q, found):
        kind = f["type"]
        url = f.get("spotify_url") or f"https://open.spotify.com/{kind}/{f['spotify_id']}"
        by = f["spotify_id"] if kind == "artist" else (f.get("artist_ids") or "")
        row = {"kind": "stream", "source": "spotify", "stream_type": kind, "url": url,
               "title": f.get("title") or "", "by_ids": by, "loose": bool(f.get("loose")),
               "ai": ai_flags.verdict("spotify", by, url, marked, cleared)}
        if kind == "track":
            row.update(subtitle=f.get("artist") or "", album=f.get("album") or "", year=f.get("album_year") or "",
                       image=f.get("album_image_mid") or f.get("album_image_url") or "",
                       thumb=f.get("album_image_small") or f.get("album_image_url") or "",
                       duration_sec=round((f.get("duration_ms") or 0) / 1000.0, 1))
        elif kind == "album":
            row.update(subtitle=f.get("artist") or "", year=f.get("year") or "", tracks=f.get("tracks"),
                       album_type=f.get("album_type") or "album", image=f.get("image") or "", thumb=f.get("thumb") or "")
        else:
            row.update(subtitle="Artist", image=f.get("image") or "", thumb=f.get("thumb") or "")
        out.append(row)
    return jsonify(dict({"results": out, "connected": True}, **({"stale": True} if stale else {})))


@app.route("/api/spotify/saved-tracks")
def api_spotify_saved_tracks():
    limit  = max(1, min(50, request.args.get("limit",  type=int) or 50))
    offset = max(0,        request.args.get("offset", type=int) or 0)
    spo = _get_spotify()
    if not spo.is_connected():
        return jsonify({"error": "not connected"}), 401
    data = spo.saved_tracks(limit=limit, offset=offset)
    if "error" in data:
        return jsonify(data), 502
    from spotify import flatten_track, buy_links
    items_raw = data.get("items") or []
    items = []
    for it in items_raw:
        flat = flatten_track(it)
        if not flat: continue
        flat["buy_links"] = buy_links(flat.get("artist"), flat.get("title"))
        items.append(flat)
        _upsert_spotify_track(flat)
    # Match against local library + existing wishlist entries in one go.
    matches  = _match_library_batch(items)
    wished   = {r["spotify_id"]: r["id"] for r in query(
        "SELECT id, spotify_id FROM wishlist WHERE spotify_id IS NOT NULL AND spotify_id != ''"
    )}
    for it in items:
        sid = it["spotify_id"]
        it["owned_track_id"]   = matches.get(sid)
        it["wishlist_id"]      = wished.get(sid)
    import datetime as _dt
    set_setting("spotify_last_synced_at", _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z")
    return jsonify({
        "items":  items,
        "total":  data.get("total"),
        "limit":  limit,
        "offset": offset,
        "has_more": bool(data.get("next")),
    })


@app.route("/api/spotify/playlists")
def api_spotify_playlists():
    limit  = max(1, min(50, request.args.get("limit",  type=int) or 50))
    offset = max(0,        request.args.get("offset", type=int) or 0)
    spo = _get_spotify()
    if not spo.is_connected():
        return jsonify({"error": "not connected"}), 401
    data = spo.playlists(limit=limit, offset=offset)
    if "error" in data:
        return jsonify(data), 502
    me_id = (get_setting("spotify_user_id", "") or "").strip()
    items = []
    for p in (data.get("items") or []):
        if not isinstance(p, dict): continue
        owner_obj = p.get("owner") or {}
        owner_id  = owner_obj.get("id")
        images    = p.get("images") or []
        is_spotify_owned = (owner_id == "spotify")
        # Feb 2026 Dev Mode renamed `playlist.tracks` → `playlist.items`.
        # Try the new key first, fall back to the legacy one for Extended-
        # Quota / older apps.
        count = None
        for k in ("items", "tracks"):
            v = p.get(k)
            if isinstance(v, dict) and v.get("total") is not None:
                count = v.get("total"); break
        items.append({
            "id":          p.get("id"),
            "name":        p.get("name"),
            "owner":       owner_obj.get("display_name"),
            "owner_id":    owner_id,
            "is_mine":     bool(me_id and owner_id == me_id),
            "is_spotify_owned": is_spotify_owned,
            "track_count": count,
            "image_url":   images[0].get("url") if images else None,
            "url":         (p.get("external_urls") or {}).get("spotify"),
            "public":      p.get("public"),
        })
    return jsonify({
        "items":   items,
        "total":   data.get("total"),
        "has_more": bool(data.get("next")),
        "me_id":   me_id,
    })


@app.route("/api/spotify/playlist/<pid>/tracks")
def api_spotify_playlist_tracks(pid):
    spo = _get_spotify()
    if not spo.is_connected():
        return jsonify({"error": "not connected"}), 401
    # Walk all pages so the caller gets the full playlist.
    from spotify import flatten_track, buy_links
    items = []
    offset = 0
    while True:
        data = spo.playlist_tracks(pid, limit=100, offset=offset)
        if "error" in data:
            return jsonify(_annotate_spotify_error(data)), (data.get("status") or 502)
        for it in (data.get("items") or []):
            flat = flatten_track(it)
            if not flat: continue
            flat["buy_links"] = buy_links(flat.get("artist"), flat.get("title"))
            items.append(flat)
            _upsert_spotify_track(flat)
        if not data.get("next"): break
        offset += 100
        if offset > 5000:  # safety
            break
    matches = _match_library_batch(items)
    wished  = {r["spotify_id"]: r["id"] for r in query(
        "SELECT id, spotify_id FROM wishlist WHERE spotify_id IS NOT NULL AND spotify_id != ''"
    )}
    for it in items:
        it["owned_track_id"] = matches.get(it["spotify_id"])
        it["wishlist_id"]    = wished.get(it["spotify_id"])
    return jsonify({"items": items, "total": len(items)})


def _annotate_spotify_error(data):
    """Add a plain-English hint to a Spotify error payload when we
    recognise the failure mode."""
    status = data.get("status")
    hint = None
    if status == 403:
        hint = (
            "Spotify returned 403 (forbidden). Things to try, in order:\n\n"
            "1. Disconnect + reconnect. If you added yourself to User Management "
            "*after* connecting the first time, your access token still has the "
            "old permissions baked in. A fresh authorize gives you a new one.\n\n"
            "2. Confirm User Management. developer.spotify.com → your app → "
            "User Management → the same Spotify email you sign in with must be "
            "in the list.\n\n"
            "3. If it's still 403 after both: this endpoint likely requires "
            "Extended Quota Mode. Spotify restricts /playlists/{id}/tracks for "
            "new dev apps in Development Mode. Request Extended Quota from your "
            "app's Settings page (manual review by Spotify; not always granted).\n\n"
            "4. Spotify-curated playlists (Discover Weekly, Daily Mix, Release "
            "Radar, Made-For-You) are *permanently* unreadable for any app "
            "created after Nov 2024 — no workaround."
        )
    elif status == 404:
        hint = ("Spotify returned 404 (not found). The playlist may have been deleted, "
                "made private by another user, or restricted to its owner.")
    elif status == 401:
        hint = "Token expired or revoked. Disconnect and reconnect."
    out = dict(data)
    if hint:
        out["hint"] = hint
    return out


@app.route("/api/spotify/track-to-wishlist", methods=["POST"])
def api_spotify_track_to_wishlist():
    """Add a Spotify track to the Music Room wishlist (idempotent by spotify_id)."""
    d = request.get_json() or {}
    sid    = (d.get("spotify_id") or "").strip()
    title  = (d.get("title")      or "").strip() or None
    artist = (d.get("artist")     or "").strip() or None
    url    = (d.get("spotify_url") or "").strip() or (f"https://open.spotify.com/track/{sid}" if sid else None)
    if not sid and not url:
        return jsonify({"error": "spotify_id or url required"}), 400
    existing = query_one("SELECT * FROM wishlist WHERE spotify_id=?", (sid,)) if sid else None
    if existing:
        return jsonify({"ok": True, "wishlist_id": existing["id"], "already": True})
    place_id = d.get("place_id") if isinstance(d.get("place_id"), int) else None
    tags     = ",".join(_parse_tags(d.get("tags"))) or None
    notes    = (d.get("notes") or "").strip() or None
    full = _wish_full()
    if full:
        return full
    wid = execute(
        "INSERT INTO wishlist (url,title,artist,source,notes,place_id,tags,status,spotify_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (url, title, artist, "spotify", notes, place_id, tags, "wishing", sid)
    )
    return jsonify({"ok": True, "wishlist_id": wid, "already": False}), 201


@app.route("/api/spotify/import-playlist", methods=["POST"])
def api_spotify_import_playlist():
    """Import every track in a Spotify playlist into the wishlist."""
    d = request.get_json() or {}
    pid = (d.get("playlist_id") or "").strip()
    if not pid:
        return jsonify({"error": "playlist_id required"}), 400
    spo = _get_spotify()
    if not spo.is_connected():
        return jsonify({"error": "not connected"}), 401
    place_id = d.get("place_id") if isinstance(d.get("place_id"), int) else None
    tag_in   = (d.get("tag") or "").strip() or None
    status   = (d.get("status") or "wishing").strip()
    if status not in ("wishing", "bought", "passed"):
        status = "wishing"
    from spotify import flatten_track
    added = skipped = owned_skipped = 0
    offset = 0
    while True:
        data = spo.playlist_tracks(pid, limit=100, offset=offset)
        if "error" in data:
            return jsonify(_annotate_spotify_error(data)), (data.get("status") or 502)
        for it in (data.get("items") or []):
            flat = flatten_track(it)
            if not flat: continue
            _upsert_spotify_track(flat)
            sid = flat["spotify_id"]
            # Skip if already in wishlist
            existing = query_one("SELECT id FROM wishlist WHERE spotify_id=?", (sid,))
            if existing:
                skipped += 1
                continue
            # Skip if user already owns it locally — no point wishing for it
            owned = _match_library_batch([flat]).get(sid)
            if owned:
                owned_skipped += 1
                continue
            if _wish_full():                   # the trial's wishlist holds 100: the rest don't fit
                skipped += 1
                continue
            url = flat.get("spotify_url") or f"https://open.spotify.com/track/{sid}"
            execute(
                "INSERT INTO wishlist (url,title,artist,source,notes,place_id,tags,status,spotify_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (url, flat.get("title"), flat.get("artist"), "spotify", None,
                 place_id, tag_in, status, sid)
            )
            added += 1
        if not data.get("next"): break
        offset += 100
        if offset > 5000: break
    return jsonify({
        "ok": True,
        "added":         added,
        "skipped":       skipped,         # already in wishlist
        "owned_skipped": owned_skipped,   # already owned locally
    })


@app.route("/api/spotify/match-library")
def api_spotify_match_library():
    artist = (request.args.get("artist") or "").strip()
    title  = (request.args.get("title")  or "").strip()
    matches = _match_library_batch([{"spotify_id": "x", "artist": artist, "title": title}])
    return jsonify({"track_id": matches.get("x")})


@app.route("/api/spotify/stats")
def api_spotify_stats():
    """Cheap, DB-only stats for the /spotify page header. Uses what we
    already know — no Spotify API calls. The Spotify-side count of
    saved tracks gets populated organically as the user pages through."""
    counts = {r["dsp_check_state"] or "untested": r["n"] for r in query(
        "SELECT COALESCE(dsp_check_state,'untested') AS dsp_check_state, COUNT(*) AS n "
        "FROM tracks WHERE is_deleted=0 GROUP BY COALESCE(dsp_check_state,'untested')"
    )}
    library_total       = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"] or 0
    on_dsp_n            = counts.get("on_dsp", 0)
    not_on_dsp_n        = counts.get("not_on_dsp", 0)
    untested_n          = counts.get("untested", 0)
    skipped_n           = counts.get("skipped", 0)
    errors_n            = counts.get("error", 0)
    wishlist_from_spo   = query_one(
        "SELECT COUNT(*) AS n FROM wishlist WHERE source='spotify'"
    )["n"] or 0
    wishlist_total      = query_one("SELECT COUNT(*) AS n FROM wishlist")["n"] or 0
    cached_spotify_tracks = query_one("SELECT COUNT(*) AS n FROM spotify_tracks")["n"] or 0
    return jsonify({
        "library": {
            "total":           library_total,
            "on_dsp":          on_dsp_n,
            "not_on_dsp":      not_on_dsp_n,
            "untested":        untested_n,
            "skipped":         skipped_n,
            "errors":          errors_n,
        },
        "wishlist": {
            "total":        wishlist_total,
            "from_spotify": wishlist_from_spo,
        },
        "spotify": {
            "cached_tracks":   cached_spotify_tracks,
            "connected":       _get_spotify().is_connected(),
        },
    })


@app.route("/api/dsp/only-here")
def api_dsp_only_here():
    """Music Room tracks confirmed not on any checked DSP at last check.
    Paginated; supports an optional ?search= over title/artist/album/label."""
    limit  = max(1, min(200, request.args.get("limit",  type=int) or 50))
    offset = max(0,         request.args.get("offset", type=int) or 0)
    search = (request.args.get("search") or "").strip()
    where, params = ["t.is_deleted = 0", "t.dsp_check_state = 'not_on_dsp'"], []
    if search:
        like = f"%{search}%"
        where.append("(t.title LIKE ? OR t.artist_name LIKE ? OR t.album LIKE ? OR t.label LIKE ?)")
        params.extend([like, like, like, like])
    total = query_one(
        f"SELECT COUNT(*) AS n FROM tracks t WHERE {' AND '.join(where)}",
        params
    )["n"] or 0
    rows = query(
        f"""
        SELECT t.id, t.title, t.artist_name, t.album, t.year, t.label,
               t.artwork_path, t.dsp_checked_at,
               f.file_format, f.bpm, f.key_note, f.key_scale
        FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE {' AND '.join(where)}
        ORDER BY t.artist_name ASC, t.title ASC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset]
    )
    well = query_one("SELECT id FROM places WHERE is_well=1")
    well_id = well["id"] if well else None
    return jsonify({
        "items":   rows,
        "total":   total,
        "limit":   limit,
        "offset":  offset,
        "well_id": well_id,
    })


@app.route("/sets")
def sets_page():
    return render_template("sets.html")


@app.route("/set/<int:set_id>")
def set_page(set_id):
    s = query_one("SELECT * FROM sets WHERE id = ?", (set_id,))
    if not s: abort(404)
    return render_template("set.html", set=s)


@app.route("/shows")
@app.route("/longwave")
def shows_page():
    return render_template("shows.html")


@app.route("/show/<int:show_id>")
def show_page(show_id):
    s = query_one("SELECT * FROM shows WHERE id = ?", (show_id,))
    if not s: abort(404)
    return render_template("show.html", show=s)


@app.route("/learn")
def learn_page():
    return render_template("learn.html")


@app.route("/fix")
def fix_page():
    return render_template("fix.html")


@app.route("/explore")
def explore_page():
    return render_template("explore.html")


# ── Explore feed (living magazine rack) ─────────────────────────────
# Pulls RSS/Atom from the user's sources into feed_items so Explore opens
# to fresh content. No algorithm — just the user's chosen sources. The
# heavier "self-algorithm" (curation + challenge-me) is a later layer on
# top of this.
_feed_progress = {
    "running": False, "done": 0, "total": 0, "current": "",
    "new_items": 0, "errors": 0,
    "started": None, "finished": None, "stop_reason": None,
}


def _run_feed_refresh():
    import datetime as _dt
    import feeds
    global _feed_progress
    _feed_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "new_items": 0, "errors": 0,
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None, "stop_reason": None,
    }
    try:
        conn = _connect_threadsafe()
        cur = conn.cursor()
        srcs = cur.execute(
            "SELECT id, name, url, feed_url FROM sources ORDER BY name"
        ).fetchall()
        _feed_progress["total"] = len(srcs)
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        new_total = 0
        for i, s in enumerate(srcs, 1):
            if not _feed_progress["running"]:
                _feed_progress["stop_reason"] = "Cancelled."
                break
            _feed_progress["current"] = s["name"]
            feed_url = (s["feed_url"] or "").strip()
            # Discover (and remember) a feed URL the first time.
            if not feed_url:
                try:
                    feed_url = feeds.discover_feed(s["url"]) or ""
                except Exception:
                    feed_url = ""
                cur.execute(
                    "UPDATE sources SET feed_url=?, feed_checked_at=? WHERE id=?",
                    (feed_url or None, now_iso, s["id"])
                )
                conn.commit()
            if not feed_url:
                _feed_progress["done"] = i
                continue
            try:
                items = feeds.fetch_feed(feed_url, limit=40)
            except Exception:
                _feed_progress["errors"] += 1
                cur.execute("UPDATE sources SET feed_checked_at=? WHERE id=?", (now_iso, s["id"]))
                conn.commit()
                _feed_progress["done"] = i
                continue
            for it in items:
                cur.execute(
                    "INSERT OR IGNORE INTO feed_items "
                    "(source_id, guid, title, url, summary, image_url, published_at, "
                    " audio_url, audio_duration_sec) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], it["guid"], it["title"], it["url"],
                     it["summary"], it["image_url"], it["published_at"],
                     it.get("audio_url"), it.get("audio_duration_sec"))
                )
                if cur.rowcount > 0:
                    new_total += 1
                elif it.get("audio_url"):
                    # row predates audio mining — backfill the enclosure once so
                    # episodes already in Explore become playable after a refresh
                    cur.execute(
                        "UPDATE feed_items SET audio_url=?, audio_duration_sec=? "
                        "WHERE source_id=? AND guid=? AND audio_url IS NULL",
                        (it["audio_url"], it.get("audio_duration_sec"), s["id"], it["guid"]))
            cur.execute("UPDATE sources SET feed_checked_at=? WHERE id=?", (now_iso, s["id"]))
            conn.commit()
            _feed_progress["new_items"] = new_total
            _feed_progress["done"] = i
        # Remember when we last refreshed (manual upsert — thread-safe).
        cur.execute("UPDATE settings SET value=? WHERE key='feed_last_refresh_at'", (now_iso,))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO settings (key, value) VALUES ('feed_last_refresh_at', ?)", (now_iso,))
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _feed_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _feed_progress["running"] = False
        import datetime as _dt2
        _feed_progress["finished"] = _dt2.datetime.utcnow().isoformat()


# ── Explore curation — score feed items against the user's interests ──
# Transparent, no black box: each interest is a set of keywords + a weight
# the user controls. An item's score = sum of the weights of every interest
# whose keywords appear in its title/summary/source/topics. "For you" ranks
# by score; a serendipity quota reserves slots for unmatched items so the
# user still meets things outside their stated interests (anti-bubble).

def _interest_regex(keywords):
    import re
    parts = [k.strip() for k in re.split(r"[,\n;]+", keywords or "") if k.strip()]
    if not parts:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in parts) + r")\b", re.I)


def _feed_haystack(item):
    return " ".join(str(item.get(k) or "") for k in
                    ("title", "summary", "source_name", "source_topics"))


# the content types the Explore filters offer, in display order
FEED_TYPES = ["article", "mix", "video", "podcast", "newsletter", "website"]

def _feed_content_type(item):
    """What KIND of thing a feed item is (for the type filters). URL leads;
    source kind + a few title hints fill the gaps. Cheap + deterministic, so
    it's computed per request — no column, nothing to migrate or backfill."""
    u     = (item.get("url") or "").lower()
    title = (item.get("title") or "").lower()
    summ  = (item.get("summary") or "").lower()
    kind  = (item.get("source_kind") or "").lower()
    if any(d in u for d in ("youtube.com", "youtu.be", "vimeo.com")):
        return "video"
    if any(d in u for d in ("mixcloud.com", "soundcloud.com", "hearthis.at", "audius.co")):
        return "mix"
    # carries playable audio → it's a podcast (episode hints) or a mix
    if item.get("audio_url"):
        if "podcast" in u or "/episode" in u or "podcast" in title or "podcast" in summ \
           or kind == "podcast":
            return "podcast"
        return "mix"
    if "podcast" in u or "/episode" in u or "podcast" in title or "podcast" in summ:
        return "podcast"
    if kind == "mailing_list":
        return "newsletter"
    if any(p in title for p in ("dj mix", " mix ", "mixtape", "boiler room",
                                "nts ", "residency", "live set", "in session")):
        return "mix"
    return "article"


@app.route("/api/feed")
def api_feed():
    """Feed items for Explore.
      order=foryou  → ranked by interest score, serendipity woven in
      order=newest  → chronological
      mode=ritual   → a small calm handful (mostly matched + 1 wildcard)
      interest=<id> → only items matching that interest
    Every item carries `matched` (which interests hit) + `serendipity`.
    """
    import random as _random
    import datetime as _dt
    mode   = (request.args.get("mode") or "river").lower()
    order  = (request.args.get("order") or "foryou").lower()
    limit  = max(1, min(100, request.args.get("limit", type=int) or 30))
    offset = max(0,         request.args.get("offset", type=int) or 0)
    source_id   = request.args.get("source_id", type=int)
    interest_id = request.args.get("interest", type=int)

    where, params = ["fi.is_hidden = 0"], []
    if source_id:
        where.append("fi.source_id = ?")
        params.append(source_id)
    cols = ("fi.id, fi.source_id, fi.title, fi.url, fi.summary, fi.image_url, "
            "fi.published_at, fi.audio_url, fi.audio_duration_sec, "
            "s.name AS source_name, s.kind AS source_kind, "
            "s.colour AS source_colour, s.topics AS source_topics, "
            "s.created_at AS source_created_at")
    # Working window of recent items, scored in Python (cheap at this scale).
    rows = query(
        f"SELECT {cols} FROM feed_items fi JOIN sources s ON s.id = fi.source_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY (fi.published_at IS NULL), fi.published_at DESC, fi.fetched_at DESC "
        "LIMIT 600", params)
    items = [dict(r) for r in rows]

    interests = query(
        "SELECT id, name, keywords, weight, is_wildcard FROM interests WHERE is_active = 1"
    )
    compiled = [(i["name"], i["weight"], _interest_regex(i["keywords"]))
                for i in interests if not i["is_wildcard"]]
    for it in items:
        hay = _feed_haystack(it)
        matched, score = [], 0
        for name, wt, rx in compiled:
            if rx and rx.search(hay):
                matched.append(name)
                score += wt
        # A source you JUST added deserves to be seen before the Tuner has any
        # reason to rank it: adding it WAS the expression of interest. For its
        # first fortnight its items get a score floor, so they surface among
        # the ranked instead of falling into the serendipity lottery — that
        # burial read as "I added a source and it never appears".
        it["from_new_source"] = False
        if it.get("source_created_at"):
            try:
                _age = (_dt.datetime.utcnow()
                        - _dt.datetime.fromisoformat(str(it["source_created_at"]).replace("Z", ""))).days
                if 0 <= _age <= 14:
                    it["from_new_source"] = True
                    if not matched:
                        matched.append("new source")
                        score += 1
            except Exception:
                pass
        it["score"], it["matched"], it["serendipity"] = score, matched, False
        it["content_type"] = _feed_content_type(it)

    # Single-interest filter (click a chip).
    if interest_id:
        sel = next((i for i in interests if i["id"] == interest_id), None)
        rx = _interest_regex(sel["keywords"]) if sel else None
        items = [it for it in items if rx and rx.search(_feed_haystack(it))]

    # Date-range filter (?since=YYYY-MM-DD&before=YYYY-MM-DD) on published_at.
    # Applied BEFORE the type counts so the chips reflect the chosen window.
    since  = (request.args.get("since")  or "").strip()
    before = (request.args.get("before") or "").strip()
    if since or before:
        def _in_date_range(it):
            pa = it.get("published_at")
            if not pa:
                return False                       # undated items can't sit in a window
            d = str(pa)[:10]
            if since and d < since:
                return False
            if before and d > before:
                return False
            return True
        items = [it for it in items if _in_date_range(it)]

    # ── websites: the catalogue's discovery sites drift through the feed as
    # their own kind of card. They're places, not posts — no date, so the
    # range filter never touches them; their chip filters them like any type.
    web_items = []
    if not source_id and not interest_id:
        try:
            import source_catalogue as _sc
            for _e in _sc.all_entries():
                if _e.get("type") != "website":
                    continue
                web_items.append({
                    "id": None, "website": True, "content_type": "website",
                    "source_id": None, "source_name": _e.get("name") or "",
                    "source_kind": "website", "source_colour": None,
                    "source_topics": ", ".join(_e.get("tags") or []),
                    "title": _e.get("name") or "", "url": _e.get("url") or "",
                    "summary": _e.get("blurb") or "", "image_url": None,
                    "published_at": None, "audio_url": None,
                    "audio_duration_sec": None, "score": 0, "matched": [],
                    "serendipity": False, "from_new_source": False,
                })
        except Exception:
            pass

    # Type counts over the current scope (source/interest) BEFORE the type
    # filter, so the chips always show how much of each kind is available.
    type_counts = {t: 0 for t in FEED_TYPES}
    for it in items:
        type_counts[it["content_type"]] = type_counts.get(it["content_type"], 0) + 1
    if web_items:
        type_counts["website"] = len(web_items)

    # Content-type filter (?types=video,mix) — one or more kinds.
    types = [t.strip().lower() for t in (request.args.get("types") or "").split(",") if t.strip()]
    if types:
        items = [it for it in items if it["content_type"] in types]
        if "website" not in types:
            web_items = []

    try:
        serendipity_pct = int(get_setting("feed_serendipity_pct", "12") or 12)
    except Exception:
        serendipity_pct = 12

    # Calm "ritual" handful: a few of your matches + one wildcard.
    if mode == "ritual":
        n = max(1, min(20, request.args.get("count", type=int) or 6))
        rng = _random.Random()
        pool = sorted([it for it in items if it["score"] > 0],
                      key=lambda x: -x["score"])[:30]
        pick = rng.sample(pool, min(len(pool), max(1, n - 1))) if pool else []
        unmatched = [it for it in items if it["score"] == 0]
        if unmatched and serendipity_pct > 0:
            s = rng.choice(unmatched)
            s["serendipity"] = True
            pick.append(s)
        rng.shuffle(pick)
        return jsonify({"mode": mode, "order": order, "items": pick, "total": len(pick),
                        "type_counts": type_counts})

    if order == "newest":
        seq = items
    else:
        matched_items = sorted([it for it in items if it["score"] > 0],
                               key=lambda x: (x["score"], x["published_at"] or ""),
                               reverse=True)
        unmatched = [it for it in items if it["score"] == 0]
        # Stable through the day so pagination doesn't reshuffle under you.
        _random.Random(int(_dt.date.today().strftime("%Y%m%d"))).shuffle(unmatched)
        if serendipity_pct > 0 and unmatched:
            step = max(2, round(100 / serendipity_pct))   # ~1 wildcard every `step`
            seq, ui = [], 0
            for idx, m in enumerate(matched_items):
                seq.append(m)
                if (idx + 1) % (step - 1) == 0 and ui < len(unmatched):
                    unmatched[ui]["serendipity"] = True
                    seq.append(unmatched[ui])
                    ui += 1
            seq += unmatched[ui:]
        else:
            seq = matched_items + unmatched

        # A source added in the last fortnight must VISIBLY flow on page one —
        # a score floor alone drowns under a deep pile of high-scoring matches,
        # which read as "I added a source and it never appears". Weave its
        # freshest items (2 per source, 6 total) into the top of the ranking.
        if offset == 0 and not source_id:
            by_src = {}
            for it in items:
                if it.get("from_new_source"):
                    by_src.setdefault(it["source_id"], []).append(it)
            weave = []
            for lst in by_src.values():
                lst.sort(key=lambda x: x.get("published_at") or "", reverse=True)
                weave += lst[:2]
            weave.sort(key=lambda x: x.get("published_at") or "", reverse=True)
            weave = weave[:6]
            if weave:
                wids = {id(w) for w in weave}
                seq = [it for it in seq if id(it) not in wids]
                pos = 2
                for w in weave:
                    seq.insert(min(pos, len(seq)), w)
                    pos += 4

    # websites drift in — spread through the stream on a daily-stable shuffle
    if web_items:
        _wr = _random.Random(int(_dt.date.today().strftime("%Y%m%d")) ^ 0x5EB)
        _wr.shuffle(web_items)
        _pos = 4
        for _w in web_items:
            seq.insert(min(_pos, len(seq)), _w)
            _pos += 9

    page = seq[offset:offset + limit]
    # the reading view is honest from the first render: pages the reader has
    # already failed on (cached miss) never offer "▤ read" at all
    _urls = [it["url"] for it in page if it.get("url") and not it.get("website")]
    if _urls:
        _ph = ",".join("?" * len(_urls))
        _misses = {r["url"] for r in query(
            f"SELECT url FROM article_cache WHERE ok=0 AND url IN ({_ph})", _urls)}
        for it in page:
            it["no_reader"] = bool(it.get("url") in _misses)
    return jsonify({"mode": mode, "order": order, "items": page,
                    "total": len(seq), "offset": offset, "limit": limit,
                    "type_counts": type_counts})


@app.route("/api/feed/refresh", methods=["POST"])
def api_feed_refresh():
    if _feed_progress.get("running"):
        return jsonify({"ok": False, "error": "already running",
                        "progress": _feed_progress}), 409
    threading.Thread(target=_run_feed_refresh, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/feed/maybe-refresh", methods=["POST"])
def api_feed_maybe_refresh():
    """Throttled refresh for app/Explore open. Kicks a background refresh only if
    the feed is STALE (last refresh older than FEED_STALE_MIN) and nothing is
    already running — so opening the app surfaces the freshest items without
    hammering sources on every navigation. Returns whether it started."""
    import datetime as _dt
    FEED_STALE_MIN = 20
    if _feed_progress.get("running"):
        return jsonify({"ok": True, "started": False, "reason": "already running"})
    last = (get_setting("feed_last_refresh_at", "") or "").strip()
    stale = True
    if last:
        try:
            ts = _dt.datetime.fromisoformat(last.replace("Z", ""))
            stale = (_dt.datetime.utcnow() - ts) > _dt.timedelta(minutes=FEED_STALE_MIN)
        except Exception:
            stale = True
    if not stale:
        return jsonify({"ok": True, "started": False, "reason": "fresh"})
    threading.Thread(target=_run_feed_refresh, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/feed/refresh-status")
def api_feed_refresh_status():
    total_items = query_one("SELECT COUNT(*) AS n FROM feed_items WHERE is_hidden=0")["n"] or 0
    with_feed = query_one(
        "SELECT COUNT(*) AS n FROM sources WHERE feed_url IS NOT NULL AND TRIM(feed_url) != ''"
    )["n"] or 0
    total_sources = query_one("SELECT COUNT(*) AS n FROM sources")["n"] or 0
    return jsonify({
        **_feed_progress,
        "total_items":       total_items,
        "sources_with_feed": with_feed,
        "total_sources":     total_sources,
        "last_refresh_at":   get_setting("feed_last_refresh_at", "") or None,
    })


# ── Source catalogue — search a vibe, not a name ────────────────────────────
# A ship-with, tagged catalogue (source_catalogue.py + static/data/source_catalogue.json)
# the user searches when ADDING a source: "photo discovery" → are.na, "world map"
# → Radio Garden. Adding reuses POST /api/sources. Link-health + user reports keep
# it honest. See VISION §4.7.
def _bare_host(u):
    from urllib.parse import urlsplit
    try:
        netloc = (urlsplit(u if "//" in (u or "") else "//" + (u or "")).netloc or "").lower()
    except Exception:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


@app.route("/api/catalogue")
def api_catalogue():
    import source_catalogue
    q = request.args.get("q", "") or ""
    type_filter = request.args.get("type") or None
    limit = max(1, min(100, request.args.get("limit", type=int) or 40))
    results = source_catalogue.search(q, type_filter, limit)
    # flag entries the user already subscribes to (host match) so the UI can show "added"
    have = set()
    try:
        for s in query("SELECT url FROM sources"):
            h = _bare_host(s["url"])
            if h:
                have.add(h)
    except Exception:
        pass
    for r in results:
        r["already_added"] = _bare_host(r.get("url")) in have
    return jsonify({
        "entries": results,
        "tags": source_catalogue.all_tags(),
        "health_checked_at": source_catalogue.health_last_run(),
    })


@app.route("/api/catalogue/<entry_id>/report", methods=["POST"])
def api_catalogue_report(entry_id):
    import source_catalogue
    reason = (request.get_json(silent=True) or {}).get("reason", "")
    n = source_catalogue.report(entry_id, reason)
    if n is None:
        return jsonify({"ok": False, "error": "unknown entry"}), 404
    return jsonify({"ok": True, "report_count": n})


@app.route("/api/catalogue/health-check", methods=["POST"])
def api_catalogue_health_check():
    import source_catalogue
    threading.Thread(target=source_catalogue.run_health_check, daemon=True).start()
    return jsonify({"ok": True, "started": True})


_cat_health_last_check = [0.0]
@app.before_request
def _catalogue_health_hook():
    """Weekly automated link-success test for the catalogue — off the request
    thread, gated in-memory to at most hourly and stored so it only really runs
    once every 7 days. Keeps dead/abandoned entries flagged without any cron."""
    import time as _t
    now = _t.time()
    if now - _cat_health_last_check[0] < 3600:
        return
    _cat_health_last_check[0] = now
    try:
        import source_catalogue, datetime as _dt
        last = source_catalogue.health_last_run()
        due = True
        if last:
            try:
                ts = _dt.datetime.fromisoformat(last.replace("Z", ""))
                due = (_dt.datetime.utcnow() - ts) > _dt.timedelta(days=7)
            except Exception:
                due = True
        if due:
            threading.Thread(target=source_catalogue.run_health_check, daemon=True).start()
    except Exception:
        pass


# ── The place map (VISION §4.15) — the collection laid on a world map ────────
# Reads the geo culture.py already enriched into artist_profiles; geo_map.py
# normalises the messy country values to a canonical {code, name, lat, lon}. The
# equal-area, true-size country shapes are drawn on the front end.
def _map_filter_sql():
    """Shared WHERE + JOIN for map queries — optional text search + genre, so the
    map filters in parity with the room. Returns (join, where_list, params)."""
    q = (request.args.get("q") or "").strip().lower()
    genre = (request.args.get("genre") or "").strip().lower()
    join, where, params = "", ["t.is_deleted=0"], []
    if q:
        where.append("(LOWER(t.title) LIKE ? OR LOWER(t.artist_name) LIKE ?)")
        params += ["%" + q + "%", "%" + q + "%"]
    if genre:
        join = "JOIN track_genres tg ON tg.track_id=t.id JOIN genres g ON g.id=tg.genre_id"
        where.append("LOWER(g.name)=?")
        params.append(genre)
    return join, where, params


@app.route("/api/map/countries")
def api_map_countries():
    import geo_map
    from collections import defaultdict
    join, where, params = _map_filter_sql()
    total = query_one(
        f"SELECT COUNT(DISTINCT t.id) n FROM tracks t {join} WHERE {' AND '.join(where)}",
        params)["n"] or 0
    rows = query(
        f"""SELECT ap.country AS country, COUNT(DISTINCT t.id) AS n
            FROM tracks t {join}
            JOIN artist_profiles ap ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE {' AND '.join(where)} AND COALESCE(ap.country,'')!=''
            GROUP BY ap.country""", params)
    agg = defaultdict(int)
    for r in rows:
        code = geo_map.normalize(r["country"])
        if code:
            agg[code] += r["n"]
    countries = []
    for code, n in agg.items():
        info = geo_map.country_info(code)
        if not info:
            continue
        name, lat, lon = info
        countries.append({"code": code, "name": name, "lat": lat, "lon": lon, "count": n})
    countries.sort(key=lambda c: -c["count"])
    placed = sum(c["count"] for c in countries)
    return jsonify({
        "countries": countries,
        "total_tracks": total,        # tracks matching the current filter
        "total_placed": placed,       # of those, how many sit on the map
    })


@app.route("/api/map/country/<code>/tracks")
def api_map_country_tracks(code):
    import geo_map
    code = (code or "").upper()
    join, where, params = _map_filter_sql()
    rows = query(
        f"""SELECT DISTINCT t.id AS id, t.title AS title, t.artist_name AS artist, ap.country AS country
            FROM tracks t {join}
            JOIN artist_profiles ap ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE {' AND '.join(where)} AND COALESCE(ap.country,'')!=''
            ORDER BY t.artist_name, t.title""", params)
    out = [{"id": r["id"], "title": r["title"], "artist": r["artist"]}
           for r in rows if geo_map.normalize(r["country"]) == code]
    info = geo_map.country_info(code)
    return jsonify({"code": code, "name": info[0] if info else code,
                    "count": len(out), "tracks": out[:500]})


@app.route("/api/map/points")
def api_map_points():
    """Town/village markers — artists placed to their birthplace lat/lon (Wikidata,
    via culture.py). One marker per artist; size by track count. Same q+genre filter
    parity as /api/map/countries, so a vibe filter re-weights the markers too. This
    is the §4.15 zoom-in layer: countries (raised by count) zoomed out → these glowing
    points zoomed in, down to the town an artist is from."""
    join, where, params = _map_filter_sql()
    rows = query(
        f"""SELECT ap.artist_name AS artist, ap.city AS city, ap.country AS country,
                   ap.latitude AS lat, ap.longitude AS lon,
                   COUNT(DISTINCT t.id) AS n
            FROM tracks t {join}
            JOIN artist_profiles ap ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE {' AND '.join(where)}
                  AND ap.latitude IS NOT NULL AND ap.longitude IS NOT NULL
            GROUP BY ap.artist_name
            ORDER BY n DESC""", params)
    points = [{
        "artist":  r["artist"],
        "city":    r["city"] or None,
        "country": r["country"] or None,
        "lat":     r["lat"],
        "lon":     r["lon"],
        "count":   r["n"],
    } for r in rows]
    placed_tracks = query_one(
        f"""SELECT COUNT(DISTINCT t.id) n FROM tracks t {join}
            JOIN artist_profiles ap ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE {' AND '.join(where)}
                  AND ap.latitude IS NOT NULL AND ap.longitude IS NOT NULL""",
        params)["n"] or 0
    return jsonify({
        "points": points,
        "artist_count": len(points),
        "placed_tracks": placed_tracks,
    })


@app.route("/api/map/artist/<path:artist>/tracks")
def api_map_artist_tracks(artist):
    """Tracks for one artist marker — the click-through for a town/village point."""
    join, where, params = _map_filter_sql()
    rows = query(
        f"""SELECT DISTINCT t.id AS id, t.title AS title
            FROM tracks t {join}
            JOIN artist_profiles ap ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE {' AND '.join(where)} AND LOWER(ap.artist_name)=LOWER(?)
            ORDER BY t.title""", params + [artist])
    prof = query_one(
        "SELECT city, country, latitude, longitude FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?)",
        (artist,))
    return jsonify({
        "artist":  artist,
        "city":    (prof["city"] if prof else None) or None,
        "country": (prof["country"] if prof else None) or None,
        "count":   len(rows),
        "tracks":  [{"id": r["id"], "title": r["title"]} for r in rows[:500]],
    })


@app.route("/api/map/artist-country", methods=["POST"])
def api_map_set_artist_country():
    """Manual override — the user fixes/confirms where an artist is from. Upserts
    artist_profiles.country; culture's batch enrich is only_missing by default, so
    this won't be clobbered. (geo is a suggestion the user confirms — §4.15.)"""
    d = request.get_json() or {}
    artist = (d.get("artist_name") or "").strip()
    country = (d.get("country") or "").strip()
    if not artist:
        return jsonify({"ok": False, "error": "artist_name required"}), 400
    execute("""INSERT INTO artist_profiles (artist_name, country, enriched_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(artist_name) DO UPDATE SET
                   country = excluded.country, updated_at = datetime('now')""",
            (artist, country or None))
    return jsonify({"ok": True})


@app.route("/api/feed/item/<int:item_id>/hide", methods=["POST"])
def api_feed_item_hide(item_id):
    execute("UPDATE feed_items SET is_hidden=1 WHERE id=?", (item_id,))
    return jsonify({"ok": True})


@app.route("/api/feed/item/<int:item_id>/unhide", methods=["POST"])
def api_feed_item_unhide(item_id):
    """Undo a 'let it go' — bring a dismissed feed item back."""
    execute("UPDATE feed_items SET is_hidden=0 WHERE id=?", (item_id,))
    return jsonify({"ok": True})


# ── Interests ("your own algorithm" — user-controlled channels) ──────

@app.route("/api/interests")
def api_interests():
    rows = query(
        "SELECT id, name, keywords, weight, is_active, is_wildcard, sort_order "
        "FROM interests ORDER BY sort_order, id"
    )
    try:
        ser = int(get_setting("feed_serendipity_pct", "12") or 12)
    except Exception:
        ser = 12
    return jsonify({"interests": [dict(r) for r in rows], "serendipity_pct": ser})


@app.route("/api/interests", methods=["POST"])
def api_interests_create():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    nxt = query_one("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM interests")["n"]
    execute(
        "INSERT INTO interests (name, keywords, weight, sort_order) VALUES (?, ?, ?, ?)",
        (name, (d.get("keywords") or "").strip(),
         max(1, min(5, int(d.get("weight") or 3))), nxt)
    )
    return jsonify({"ok": True})


@app.route("/api/interest/<int:iid>", methods=["PATCH"])
def api_interest_update(iid):
    d = request.get_json() or {}
    fields, vals = [], []
    for k in ("name", "keywords"):
        if k in d:
            fields.append(f"{k} = ?")
            vals.append((d.get(k) or "").strip())
    if "weight" in d:
        fields.append("weight = ?")
        vals.append(max(1, min(5, int(d.get("weight") or 3))))
    if "is_active" in d:
        fields.append("is_active = ?")
        vals.append(1 if d.get("is_active") else 0)
    if not fields:
        return jsonify({"ok": True})
    vals.append(iid)
    execute(f"UPDATE interests SET {', '.join(fields)} WHERE id = ?", vals)
    return jsonify({"ok": True})


@app.route("/api/interest/<int:iid>", methods=["DELETE"])
def api_interest_delete(iid):
    execute("DELETE FROM interests WHERE id = ?", (iid,))
    return jsonify({"ok": True})


# =============================================================
# PLAY TRACKING — raw events + the headline listening analytics.
# The browser players (track panel / set player / canvas) log here;
# the Seinnteoir logs server-side with precise listened-time.
# =============================================================

@app.route("/api/play-event", methods=["POST"])
def api_play_event():
    d = request.get_json() or {}
    tid = d.get("track_id")
    if not tid:
        return jsonify({"error": "track_id required"}), 400
    src = (d.get("source") or "unknown")[:24]
    eid = execute("""INSERT INTO play_events (track_id, file_id, source, place_id)
                     VALUES (?,?,?,?)""",
                  (tid, d.get("file_id"), src, d.get("place_id")))
    execute("""UPDATE tracks SET play_count=COALESCE(play_count,0)+1,
               last_played_at=datetime('now') WHERE id=?""", (tid,))
    return jsonify({"ok": True, "id": eid}), 201


@app.route("/api/play-event/<int:eid>/progress", methods=["POST"])
def api_play_event_progress(eid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "duration_listened" in d:
        try:
            ups.append("duration_listened=?"); vals.append(round(float(d["duration_listened"]), 1))
        except (TypeError, ValueError):
            pass
    if "completed" in d:
        ups.append("completed=?"); vals.append(1 if d["completed"] else 0)
    if ups:
        vals.append(eid)
        execute(f"UPDATE play_events SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/stats/listening")
def api_stats_listening():
    """The numbers a listening-data team would reach for: volume, recency,
    completion, repeat behaviour, when you listen, what leads."""
    one = lambda q, p=(): (query_one(q, p) or {})
    days = request.args.get("days", type=int) or 30
    win = f"-{days} days"
    out = {
        "window_days": days,
        "plays_all_time": one("SELECT COUNT(*) n FROM play_events")["n"],
        "plays_window":   one("SELECT COUNT(*) n FROM play_events WHERE started_at >= datetime('now', ?)", (win,))["n"],
        "hours_window":   round((one("""SELECT COALESCE(SUM(duration_listened),0) s FROM play_events
                                        WHERE started_at >= datetime('now', ?)""", (win,))["s"] or 0) / 3600, 1),
        "distinct_tracks_window": one("""SELECT COUNT(DISTINCT track_id) n FROM play_events
                                         WHERE started_at >= datetime('now', ?)""", (win,))["n"],
        "completion_rate_window": None,
        "repeat_rate_window": None,
        "never_played": one("""SELECT COUNT(*) n FROM tracks
                               WHERE is_deleted=0 AND COALESCE(play_count,0)=0""")["n"],
    }
    cw = one("""SELECT COUNT(*) total, SUM(completed) done FROM play_events
                WHERE started_at >= datetime('now', ?) AND duration_listened IS NOT NULL""", (win,))
    if cw.get("total"):
        out["completion_rate_window"] = round((cw["done"] or 0) * 100 / cw["total"])
    rw = one("""SELECT COUNT(*) played,
                       SUM(CASE WHEN c >= 2 THEN 1 ELSE 0 END) repeated
                FROM (SELECT track_id, COUNT(*) c FROM play_events
                      WHERE started_at >= datetime('now', ?) GROUP BY track_id)""", (win,))
    if rw.get("played"):
        out["repeat_rate_window"] = round((rw["repeated"] or 0) * 100 / rw["played"])
    out["top_tracks"] = [dict(r) for r in query("""
        SELECT pe.track_id, t.title, t.artist_name, COUNT(*) plays,
               ROUND(COALESCE(SUM(pe.duration_listened),0)/60, 1) minutes
        FROM play_events pe JOIN tracks t ON t.id = pe.track_id
        WHERE pe.started_at >= datetime('now', ?)
        GROUP BY pe.track_id ORDER BY plays DESC, minutes DESC LIMIT 10""", (win,))]
    out["by_hour"] = {r["h"]: r["n"] for r in query("""
        SELECT CAST(strftime('%H', started_at, 'localtime') AS INTEGER) h, COUNT(*) n
        FROM play_events WHERE started_at >= datetime('now', ?) GROUP BY h""", (win,))}
    out["by_source"] = {r["source"]: r["n"] for r in query("""
        SELECT source, COUNT(*) n FROM play_events
        WHERE started_at >= datetime('now', ?) GROUP BY source""", (win,))}
    out["by_day"] = [dict(r) for r in query("""
        SELECT date(started_at, 'localtime') d, COUNT(*) n FROM play_events
        WHERE started_at >= datetime('now', ?) GROUP BY d ORDER BY d""", (win,))]
    return jsonify(out)


@app.route("/insights")
def insights_page():
    """The local insights dashboard (VISION §4.14 Phase 1) — measure the app's
    impact off the data it already generates. LOCAL ONLY; nothing here is uploaded."""
    return render_template("insights.html")


@app.route("/api/insights")
def api_insights():
    """Aggregated data for the local dashboard: the artist-support purchase funnel
    (the whole point), listening, library + discovery. LOCAL ONLY — never uploaded;
    Phase-2 telemetry sends bucketed ROLLUPS of a small allowlisted subset instead."""
    one = lambda q, p=(): (query_one(q, p) or {})
    days = request.args.get("days", type=int) or 30
    win = f"-{days} days"

    # ── artist support: the purchase funnel (intent → bought → where) ──
    pi = {k: one(f"SELECT COUNT(*) n FROM purchase_intents WHERE {c}")["n"]
          for k, c in (("logged", "1=1"), ("bought", "status='bought'"),
                       ("pending", "status='pending'"), ("dismissed", "status='dismissed'"))}
    pi_store = {(r["bought_via"] or "unknown"): r["n"] for r in query(
        "SELECT bought_via, COUNT(*) n FROM purchase_intents WHERE status='bought' GROUP BY bought_via")}
    wl = {k: one(f"SELECT COUNT(*) n FROM wishlist WHERE {c}")["n"]
          for k, c in (("total", "1=1"), ("wishing", "status='wishing'"), ("supported", "status='supported'"))}
    wl_via = {(r["bought_via"] or "unknown"): r["n"] for r in query(
        "SELECT bought_via, COUNT(*) n FROM wishlist WHERE status='supported' GROUP BY bought_via")}
    # WHY you want/buy — the self-log from the play game (categories; 'other' is free text)
    wl_reasons = {(r["buy_reason"]): r["n"] for r in query(
        "SELECT buy_reason, COUNT(*) n FROM wishlist WHERE COALESCE(buy_reason,'')!='' GROUP BY buy_reason ORDER BY n DESC")}
    supported_artists = one("""
        SELECT COUNT(DISTINCT a) n FROM (
            SELECT LOWER(artist) a FROM purchase_intents WHERE status='bought' AND COALESCE(artist,'')!=''
            UNION SELECT LOWER(artist) a FROM wishlist WHERE status='supported' AND COALESCE(artist,'')!='')""")["n"]
    bought_total = sum(pi_store.values()) + sum(wl_via.values())
    bc = pi_store.get("bandcamp", 0) + wl_via.get("bandcamp", 0)

    # ── listening ──
    cw = one("""SELECT COUNT(*) total, SUM(completed) done FROM play_events
                WHERE started_at >= datetime('now', ?) AND duration_listened IS NOT NULL""", (win,))
    listen = {
        "plays_all_time": one("SELECT COUNT(*) n FROM play_events")["n"],
        "plays_window":   one("SELECT COUNT(*) n FROM play_events WHERE started_at >= datetime('now', ?)", (win,))["n"],
        "hours_window":   round((one("SELECT COALESCE(SUM(duration_listened),0) s FROM play_events WHERE started_at >= datetime('now', ?)", (win,))["s"] or 0) / 3600, 1),
        "never_played":   one("SELECT COUNT(*) n FROM tracks WHERE is_deleted=0 AND COALESCE(play_count,0)=0")["n"],
        "completion_rate": (round((cw.get("done") or 0) * 100 / cw["total"]) if cw.get("total") else None),
        "top_tracks": [dict(r) for r in query("""
            SELECT t.title, t.artist_name, COUNT(*) plays FROM play_events pe JOIN tracks t ON t.id=pe.track_id
            WHERE pe.started_at >= datetime('now', ?) GROUP BY pe.track_id ORDER BY plays DESC LIMIT 8""", (win,))],
        "by_source": {(r["source"] or "?"): r["n"] for r in query(
            "SELECT source, COUNT(*) n FROM play_events WHERE started_at >= datetime('now', ?) GROUP BY source", (win,))},
    }

    # ── library + discovery ──
    lib = {k: one(q)["n"] for k, q in (
        ("tracks",   "SELECT COUNT(*) n FROM tracks WHERE is_deleted=0"),
        ("with_art", "SELECT COUNT(*) n FROM tracks WHERE is_deleted=0 AND artwork_path IS NOT NULL"),
        ("artists",  "SELECT COUNT(DISTINCT artist_name) n FROM tracks WHERE is_deleted=0 AND COALESCE(artist_name,'')!=''"),
        ("labels",   "SELECT COUNT(DISTINCT label) n FROM tracks WHERE is_deleted=0 AND COALESCE(label,'')!=''"),
        ("genres",   "SELECT COUNT(*) n FROM genres"))}
    disc = {
        "by_source": {(r["source"] or "?"): r["n"] for r in query(
            "SELECT source, COUNT(*) n FROM wishlist GROUP BY source ORDER BY n DESC")},
        "by_provenance": [dict(r) for r in query(
            "SELECT provenance, COUNT(*) n FROM wishlist WHERE COALESCE(provenance,'')!='' GROUP BY provenance ORDER BY n DESC LIMIT 8")],
    }
    return jsonify({
        "window_days": days,
        "artist_support": {
            "intents": pi, "intents_by_store": pi_store, "wishlist": wl, "wishlist_by_via": wl_via,
            "buy_reasons": wl_reasons,
            "supported_artists": supported_artists,
            "conversion_rate": (round(pi["bought"] * 100 / pi["logged"]) if pi["logged"] else None),
            "bandcamp_share": (round(bc * 100 / bought_total) if bought_total else None),
        },
        "listening": listen, "library": lib, "discovery": disc,
    })


# ── Phase-2 telemetry (VISION §4.14): consented, anonymised, aggregate rollups ──
# OFF by default. Two separate consents. Only bucketed monthly rollups of the
# telemetry.py allowlist are ever built; the rotating id's seed never leaves here.
def _current_month():
    return query_one("SELECT strftime('%Y-%m','now') m")["m"]

def _telemetry_seed(create=False):
    seed = get_setting("tel_install_seed", "") or ""
    if not seed and create:
        import uuid
        seed = uuid.uuid4().hex
        set_setting("tel_install_seed", seed)
    return seed

def _telemetry_raw_stats(month):
    """Raw monthly counts, used ONLY to build bucketed rollups (never sent raw)."""
    one = lambda q, p=(): (query_one(q, p) or {}).get("n", 0)
    usage = {
        "plays":        one("SELECT COUNT(*) n FROM play_events WHERE strftime('%Y-%m', started_at)=?", (month,)),
        "active_days":  one("SELECT COUNT(DISTINCT date(started_at)) n FROM play_events WHERE strftime('%Y-%m', started_at)=?", (month,)),
        "notes_made":   one("SELECT COUNT(*) n FROM listening_notes WHERE strftime('%Y-%m', created_at)=?", (month,)),
        "wishlist_adds":one("SELECT COUNT(*) n FROM wishlist WHERE strftime('%Y-%m', created_at)=?", (month,)),
        "imports_made": one("SELECT COUNT(*) n FROM wishlist WHERE strftime('%Y-%m', created_at)=? AND source IN ('mix','shazam','import')", (month,)),
        "library_size": one("SELECT COUNT(*) n FROM tracks WHERE is_deleted=0"),
        "research_entities": one("SELECT COUNT(*) n FROM research_entities"),
        "maps_made":    one("SELECT COUNT(*) n FROM research_maps"),
    }
    purchase = {
        "buy_clicks": one("SELECT COUNT(*) n FROM purchase_intents WHERE strftime('%Y-%m', created_at)=?", (month,)),
        "bought":     one("SELECT COUNT(*) n FROM purchase_intents WHERE status='bought' AND strftime('%Y-%m', COALESCE(resolved_at,created_at))=?", (month,)),
    }
    by_store = {}
    for r in query("SELECT bought_via, COUNT(*) n FROM purchase_intents WHERE status='bought' "
                   "AND strftime('%Y-%m', COALESCE(resolved_at,created_at))=? GROUP BY bought_via", (month,)):
        k = "bandcamp" if r["bought_via"] == "bandcamp" else "other"
        by_store[k] = by_store.get(k, 0) + r["n"]
    return usage, purchase, by_store

@app.route("/api/telemetry/status")
def api_telemetry_status():
    import telemetry as tel
    month = _current_month()
    seed = _telemetry_seed()
    try:
        receipt = _json.loads(get_setting("tel_consent_receipt", "") or "null")
    except Exception:
        receipt = None
    return jsonify({
        "usage_consent":    get_setting("tel_usage_consent", "0") == "1",
        "purchase_consent": get_setting("tel_purchase_consent", "0") == "1",
        "ingest_url":       get_setting("tel_ingest_url", "") or "",
        "autosend":         get_setting("tel_autosend", "1") == "1",
        "last_send_month":  get_setting("tel_last_send_month", "") or None,
        "month": month,
        "install_hash": tel.install_hash(seed, month) if seed else None,
        "manifest": tel.field_manifest(),
        "consent_receipt": receipt,
        "outbox": query("SELECT id, month, kind, built_at, sent_at FROM telemetry_outbox ORDER BY id DESC LIMIT 24"),
    })

@app.route("/api/telemetry/consent", methods=["POST"])
def api_telemetry_consent():
    import telemetry as tel
    d = request.get_json() or {}
    usage = "1" if d.get("usage") else "0"
    purchase = "1" if d.get("purchase") else "0"
    url = (d.get("ingest_url") or "").strip()
    if url and not (url.startswith("https://") or url.startswith("http://")):
        return jsonify({"error": "ingest URL must start with http:// or https://"}), 400
    set_setting("tel_usage_consent", usage)
    set_setting("tel_purchase_consent", purchase)
    set_setting("tel_ingest_url", url)
    set_setting("tel_autosend", "0" if d.get("autosend") is False else "1")   # auto monthly send (default on)
    if usage == "1" or purchase == "1":
        _telemetry_seed(create=True)   # mint the local-only seed so an id can rotate
    when = query_one("SELECT datetime('now') t")["t"]
    receipt = {"at": when, "usage": usage == "1", "purchase": purchase == "1",
               "field_list_version": tel.FIELD_LIST_VERSION, "ingest_url": url}
    set_setting("tel_consent_receipt", _json.dumps(receipt))
    return jsonify({"ok": True, "receipt": receipt})

@app.route("/api/telemetry/preview", methods=["POST"])
def api_telemetry_preview():
    """Show EXACTLY what would be sent this month — before agreeing. Sends nothing."""
    import telemetry as tel
    month = _current_month()
    seed = _telemetry_seed() or "(seed minted on consent)"
    iid = tel.install_hash(seed, month) if _telemetry_seed() else "<rotating-id>"
    usage_raw, purchase_raw, by_store = _telemetry_raw_stats(month)
    # show BOTH rollups so the user sees each consent's payload, marked as preview
    rollups = [tel.build_usage_rollup(iid, month, usage_raw),
               tel.build_purchase_rollup(iid, month, purchase_raw, by_store)]
    return jsonify({"month": month, "rollups": rollups})

def _prev_month(month):
    """'YYYY-MM' → the previous month."""
    y, m = int(month[:4]), int(month[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"

def _telemetry_send_month(month):
    """Build + send the rollups for one month. Shared by the manual endpoint and the
    monthly auto-send. Records each in the outbox. Returns the per-rollup results."""
    import telemetry as tel
    url = (get_setting("tel_ingest_url", "") or "").strip()
    usage_c = get_setting("tel_usage_consent", "0") == "1"
    purchase_c = get_setting("tel_purchase_consent", "0") == "1"
    if not url or not (usage_c or purchase_c):
        return []
    seed = _telemetry_seed(create=True)
    usage_raw, purchase_raw, by_store = _telemetry_raw_stats(month)
    rollups = tel.build_rollups(usage_consent=usage_c, purchase_consent=purchase_c,
                                install_id=tel.install_hash(seed, month), month=month,
                                usage_raw=usage_raw, purchase_raw=purchase_raw, by_store=by_store)
    results = []
    for r in rollups:
        try:
            status, body = tel.send_rollup(url, r)
            ok = 200 <= status < 300
        except Exception as e:
            status, body, ok = 0, str(e)[:200], False
        execute("INSERT INTO telemetry_outbox (month, kind, payload, sent_at, response) "
                "VALUES (?,?,?, CASE WHEN ?=1 THEN datetime('now') ELSE NULL END, ?)",
                (month, r["kind"], _json.dumps(r), 1 if ok else 0, f"{status} {body}"[:300]))
        results.append({"kind": r["kind"], "ok": ok, "status": status})
    return results

def telemetry_maybe_autosend():
    """Once per calendar month (when the app is open + consented + auto-send on), send the
    just-completed month's rollup. Idempotent: guarded by tel_last_send_month, and the
    ingest server overwrites by month, so a stray double-send is harmless. Runs in a
    background thread, so it needs its own app context (query()/get_setting() are g-bound)."""
    try:
        with app.app_context():
            if get_setting("tel_autosend", "1") != "1":
                return
            if not (get_setting("tel_usage_consent", "0") == "1" or get_setting("tel_purchase_consent", "0") == "1"):
                return
            if not (get_setting("tel_ingest_url", "") or "").strip():
                return
            cur = _current_month()
            last = get_setting("tel_last_send_month", "") or ""
            if last == cur:
                return                                  # already handled this month
            target = _prev_month(cur) if last else cur  # first run sends the current month; later runs send the completed one
            sent = _telemetry_send_month(target)
            if any(s.get("ok") for s in sent):
                set_setting("tel_last_send_month", cur)
    except Exception:
        pass

_tel_autosend_last_check = [0.0]
@app.before_request
def _telemetry_autosend_hook():
    """Opportunistic monthly send — checked at most every 30 min, off the request thread,
    and only when telemetry is actually on. No effect while consent is OFF (the default)."""
    import time
    now = time.time()
    if now - _tel_autosend_last_check[0] < 1800:
        return
    _tel_autosend_last_check[0] = now
    if get_setting("tel_usage_consent", "0") == "1" or get_setting("tel_purchase_consent", "0") == "1":
        threading.Thread(target=telemetry_maybe_autosend, daemon=True).start()

@app.route("/api/telemetry/send", methods=["POST"])
def api_telemetry_send():
    url = (get_setting("tel_ingest_url", "") or "").strip()
    if not url:
        return jsonify({"error": "no ingest URL set"}), 400
    if not (get_setting("tel_usage_consent", "0") == "1" or get_setting("tel_purchase_consent", "0") == "1"):
        return jsonify({"error": "no consent given — nothing to send"}), 400
    results = _telemetry_send_month(_current_month())
    if any(s.get("ok") for s in results):
        set_setting("tel_last_send_month", _current_month())
    return jsonify({"sent": results})

@app.route("/api/telemetry/withdraw", methods=["POST"])
def api_telemetry_withdraw():
    """Withdraw consent: turn both off, clear the local outbox, and (optionally) ask the
    ingest server to delete this device's rotating ids."""
    import telemetry as tel
    d = request.get_json() or {}
    url = (get_setting("tel_ingest_url", "") or "").strip()
    remote = None
    if d.get("delete_remote") and url:
        seed = _telemetry_seed()
        ids = set()
        if seed:
            ids.add(tel.install_hash(seed, _current_month()))
            for r in query("SELECT DISTINCT month FROM telemetry_outbox"):
                ids.add(tel.install_hash(seed, r["month"]))
        try:
            status, _ = tel.send_rollup(url, {"_delete": True, "ids": sorted(filter(None, ids))})
            remote = status
        except Exception as e:
            remote = str(e)[:120]
    set_setting("tel_usage_consent", "0")
    set_setting("tel_purchase_consent", "0")
    execute("DELETE FROM telemetry_outbox")
    return jsonify({"ok": True, "delete_remote": remote})


@app.route("/api/explore3d/shelf")
def api_explore3d_shelf():
    """The desk's shelf: every source with a feed, carrying how much has
    piled up (recent unhidden items) — stack height in the 3D Explore."""
    rows = query("""
        SELECT s.id, s.name, s.kind, s.colour,
               (SELECT COUNT(*) FROM feed_items fi
                 WHERE fi.source_id = s.id AND fi.is_hidden = 0
                   AND fi.published_at >= datetime('now', '-30 days')) AS recent,
               (SELECT COUNT(*) FROM feed_items fi
                 WHERE fi.source_id = s.id AND fi.is_hidden = 0) AS total
        FROM sources s
        WHERE s.feed_url IS NOT NULL AND s.feed_url != ''
        ORDER BY s.name COLLATE NOCASE""")
    return jsonify([dict(r) for r in rows])


@app.route("/map")
def world_map_page():
    """The Map (VISION §4.15) — the collection on a 3D realistic-Earth globe,
    countries raised by how much music you have from them."""
    return render_template("worldmap.html")


@app.route("/explore3d")
def explore3d():
    """PROTOTYPE — Explore as a desk (VISION §4.7): the morning's gathered
    items spread on a work surface, triaged by hand; the sources as a shelf
    behind it, stacks growing as items pile up. Additive — /explore stays."""
    return render_template("explore3d.html")


@app.route("/justlisten")
def justlisten():
    """Just Listen (VISION §4.10): sit back. Album art hovering, a scrubber,
    title + artist — nothing else, made for being off the laptop. A calm skin
    over the persistent Seinnteoir, so the cue keeps playing without a pause as
    you arrive and as you leave — one of the three listening lenses (work /
    deep / sit-back) over a single continuous player."""
    return render_template("justlisten.html")


@app.route("/api/feed/serendipity", methods=["POST"])
def api_feed_serendipity():
    d = request.get_json() or {}
    pct = max(0, min(50, int(d.get("pct") or 0)))
    set_setting("feed_serendipity_pct", str(pct))
    return jsonify({"ok": True, "pct": pct})


@app.route("/listen")
def listen_page():
    return render_template("listening.html")


@app.route("/research")
def research_page():
    """The research desk: artists / labels / venues / genres as structured pages
    AND connectable cards. A curator's running log on each, joined into one
    associative web (research_links) and tied back to the real collection."""
    return render_template("research.html")


@app.route("/listen/canvas")
def listen_canvas_page():
    """The mind-map lens over the deep-listening substrate: moments as
    draggable nodes, note_links as drawn connections. Non-linear idea-mapping
    (VISION §4.2/§3.4) — the user arranges; nothing is auto-laid-out."""
    return render_template("listen_canvas.html")


@app.route("/listen/compose")
def listen_compose_page():
    """The OUTPUT lens (VISION §4.4, research/04): assemble moments, references
    and prose into a publishable document — radio script, essay, review, liner
    notes — then export it. Builds on the listening substrate; the user writes
    every word (§3.7 — nothing is auto-generated)."""
    return render_template("compose.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


# =============================================================
# API — SETTINGS
# =============================================================

def get_library_dest():
    """The Music Room Library — the one folder Organise copies/moves into.
    An explicit setting wins; otherwise infer it from files that already live
    in a '…/Music Room Library' folder, so an existing library 'just knows'
    its own home and the user never has to specify it. Empty only on a truly
    fresh install (where they'll choose it once)."""
    v = (get_setting("library_dest", "") or "").strip()
    if v:
        return v
    row = query_one(
        "SELECT file_path FROM files WHERE is_deleted=0 "
        "AND file_path LIKE '%/Music Room Library/%' ORDER BY id LIMIT 1"
    )
    if row and row.get("file_path"):
        marker = "/Music Room Library/"
        i = row["file_path"].find(marker)
        if i != -1:
            return row["file_path"][: i + len(marker) - 1]   # …/Music Room Library
    return ""


@app.route("/api/settings")
def api_get_settings():
    rows = query("SELECT key, value FROM settings")
    out = {r["key"]: r["value"] for r in rows}
    # Expose the resolved data dir (not a stored setting) so Settings can
    # show "where Music Room keeps your data" + the Reveal button.
    out["data_dir"] = DATA_DIR
    # The Music Room Library destination — resolved (setting or inferred), so
    # the Import page can default to it without the user specifying anything.
    out["library_dest"] = get_library_dest()
    # How you use Music Room: explicit choice, else inferred from where the
    # library lives (on an external /Volumes drive → 'external', else 'local').
    ml = (out.get("music_location") or "").strip()
    if ml not in ("local", "external"):
        ml = "external" if (out["library_dest"] or "").startswith("/Volumes/") else "local"
    out["music_location"] = ml
    return jsonify(out)


@app.route("/api/reveal-path", methods=["POST"])
def api_reveal_path():
    """Reveal a folder/file in Finder. Used by Settings ('Reveal data folder')
    and anywhere we want to point the user at a path on disk."""
    d = request.get_json() or {}
    p = os.path.expanduser((d.get("path") or "").strip())
    if not p or not os.path.exists(p):
        return jsonify({"ok": False, "error": "path not found"}), 400
    try:
        mr_os.reveal(p)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/choose-path", methods=["POST"])
def api_choose_path():
    """Open a native macOS Finder chooser and return the picked path — so the
    user never has to type or paste a path. Works in dev (browser) and in the
    bundled app, because the server runs locally and pops a real system dialog
    via osascript. {kind:'folder'|'file', start:'optional starting dir',
    prompt:'optional'}. Returns {path} or {cancelled:true}."""
    d = request.get_json(silent=True) or {}
    kind = "file" if d.get("kind") == "file" else "folder"
    path, why = mr_os.choose(kind, os.path.expanduser((d.get("start") or "").strip()),
                             d.get("prompt") or ("Choose a file" if kind == "file" else "Choose a folder"))
    if why == "cancelled":
        return jsonify({"ok": True, "cancelled": True})
    if not path:
        return jsonify({"ok": False, "error": why or "chooser failed"}), 500
    if path.endswith(("/", "\\")) and len(path) > 3:
        path = path.rstrip("/\\")
    return jsonify({"ok": True, "cancelled": False, "path": path})


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    d = request.get_json() or {}
    for key, value in d.items():
        set_setting(key, value)
    return jsonify({"ok": True})


# =============================================================
# API — FIRST-RUN SETUP / ONBOARDING
# =============================================================

@app.route("/api/setup/state")
def api_setup_state():
    """Drives the welcome wizard + the 'your room is empty' nudge on the
    Forest. needs_setup = empty library AND the walkthrough hasn't been
    finished/dismissed yet."""
    track_count = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"]
    analysed = query_one(
        "SELECT COUNT(*) AS n FROM files WHERE is_primary=1 AND is_deleted=0 AND has_analysis=1"
    )["n"]
    complete = (get_setting("setup_complete", "") == "1")
    return jsonify({
        "needs_setup":        _needs_onboarding(),
        "setup_complete":     complete,
        "track_count":        track_count,
        "analysed_count":     analysed,
        "data_dir":           DATA_DIR,
        "music_library_path": get_setting("music_library_path", ""),
        "backup_dir":         get_setting("backup_dir", ""),
        "has_discogs":        bool((get_setting("discogs_token", "") or "").strip()),
        "has_lastfm":         bool((get_setting("lastfm_api_key", "") or "").strip()),
    })


@app.route("/api/setup/complete", methods=["POST"])
def api_setup_complete():
    """Mark the walkthrough done so it won't auto-appear again. Re-runnable
    any time from Help → 'Run the setup walkthrough'."""
    set_setting("setup_complete", "1")
    return jsonify({"ok": True})


# --- Organise (tidy files into one library folder) --------------------------
_organise_state = {
    "running": False, "phase": "idle", "mode": None, "dest": None,
    "total": 0, "done": 0, "failed": 0, "already": 0, "missing": 0, "error": None,
}
_organise_stop = threading.Event()


@app.route("/api/organise/start", methods=["POST"])
def api_organise_start():
    """Sort every track into dest/<Letter>/<Artist>/<Album>/… — copy (originals
    stay) or move (consolidate). Runs in the background; poll /status."""
    if _organise_state["running"]:
        return jsonify({"ok": False, "error": "Already organising"}), 409
    d = request.get_json() or {}
    dest = os.path.expanduser((d.get("dest") or "").strip())
    mode = "copy" if d.get("mode") == "copy" else "move"
    only_under = os.path.expanduser((d.get("only_under") or "").strip()) or None
    if not dest:
        return jsonify({"ok": False, "error": "Destination folder required"}), 400
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Can't create {dest}: {e}"}), 400

    # Remember the library destination so the Import page pre-fills it.
    set_setting("library_dest", dest)

    _organise_stop.clear()
    _organise_state.update({
        "running": True, "phase": "starting", "mode": mode, "dest": dest,
        "total": 0, "done": 0, "failed": 0, "already": 0, "missing": 0, "error": None,
    })

    def _worker():
        try:
            import organiser
            def _prog(p):
                _organise_state.update(p)
                _organise_state["running"] = True
            organiser.organise_library(
                dest, mode=mode, progress=_prog,
                should_stop=_organise_stop.is_set, only_under=only_under,
            )
            # If they moved everything into the tidy library, that's where the
            # music now lives — point future imports there. (Only when not
            # scoped to a specific source folder.)
            if mode == "move" and not only_under:
                set_setting("music_library_path", dest)
        except Exception as e:
            _organise_state["error"] = str(e)
        finally:
            _organise_state["running"] = False
            if _organise_state["phase"] != "done":
                _organise_state["phase"] = "done"

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/organise/status")
def api_organise_status():
    return jsonify(_organise_state)


@app.route("/api/organise/stop", methods=["POST"])
def api_organise_stop():
    _organise_stop.set()
    return jsonify({"ok": True})


@app.route("/welcome")
def welcome_page():
    if IS_GR:                       # (the trial has no wizard: bringing music in is one page)
        return redirect("/import")
    return render_template("welcome.html")


_SPOTIFY_RE = _re.compile(
    r"^https?://(?:open\.)?spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)"
)
_APPLE_MUSIC_RE = _re.compile(r"^https?://music\.apple\.com/")
_TIDAL_RE       = _re.compile(r"^https?://(?:listen\.)?tidal\.com/")


def _rewrite_to_native_uri(url):
    """Rewrite well-known web URLs to URI schemes that open the desktop app
    directly, skipping the browser redirect. Returns the original URL if no
    rewrite applies."""
    m = _SPOTIFY_RE.match(url)
    if m:
        kind, sid = m.group(1), m.group(2)
        # spotify:track:..., spotify:playlist:..., etc — handled by Spotify.app
        return f"spotify:{kind}:{sid}"
    if _APPLE_MUSIC_RE.match(url):
        # music.apple.com → music:// opens Apple Music app
        return url.replace("https://music.apple.com", "music://music.apple.com", 1) \
                  .replace("http://music.apple.com",  "music://music.apple.com", 1)
    if _TIDAL_RE.match(url):
        # tidal.com → tidal:// opens Tidal app (if installed)
        return url.replace("https://listen.tidal.com", "tidal://listen.tidal.com", 1) \
                  .replace("https://tidal.com",        "tidal://tidal.com",        1)
    return url


@app.route("/api/open-url", methods=["POST"])
def api_open_url():
    """Open an external URL in the user's preferred browser, OR hand it to a
    native desktop app via its URI scheme when we recognise the host.

    Why: pywebview's window.open / target=_blank don't reliably hand the URL
    off to the OS default browser. Going through `open` on macOS gives us
    proper handoff plus the ability to honour `preferred_browser`. Spotify /
    Apple Music / Tidal links get rewritten to their app URI scheme so they
    open directly in the desktop app instead of bouncing through a tab.
    """
    d   = request.get_json() or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    if _re.fullmatch(r"mailto:[^\s/@?]+@[^\s/@?]+", url):
        # a plain email address (a digital artist's "how to reach me"): the mail program writes to it
        try:
            mr_os.open_target(url)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "opened": url, "native": True, "intent_id": None})
    if not url.startswith(("http://", "https://", "spotify:", "music:", "tidal:")):
        url = "https://" + url
    native = _rewrite_to_native_uri(url)
    is_native = native != url
    browser = (get_setting("preferred_browser", "") or "").strip()
    try:
        # a native scheme (spotify: …) goes to the app registered for it; a web address to the browser.
        # No app for the scheme (no Spotify on this computer) → the web page instead: on a Mac the
        # ask used to fail unseen and nothing opened, on Windows it was an error.
        if is_native:
            try:
                if mr_os.IS_MAC:
                    if subprocess.run(["open", native], capture_output=True, timeout=8).returncode != 0:
                        raise OSError("nothing opens " + native.split(":", 1)[0] + ": links here")
                else:
                    mr_os.open_target(native)
            except (OSError, subprocess.SubprocessError):
                is_native = False
        if not is_native:
            mr_os.open_target(url, app=browser or None)
        # Purchase tracker: clicking a record's buy link is a purchase INTENT.
        intent_id = _maybe_log_purchase_intent(url, d)
        return jsonify({"ok": True, "opened": native if is_native else url,
                        "native": is_native, "intent_id": intent_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================
# BANDCAMP / PURCHASE TRACKER
# Clicking a record's buy link from inside the app logs a purchase INTENT — a
# click anchored to a record/wishlist item. The buy happens off-site, so the
# intent stays 'pending' until confirmed: by you (✓ "I bought it"), or by the
# Bandcamp collection sync, which matches releases you actually own against
# your intents. This anchors "Supported the artist" to a real click +
# (optionally) a verified collection match, instead of a free toggle.
# =============================================================
_BUY_STORES = ("bandcamp", "beatport", "discogs")

def _is_release_page(url):
    """A specific release/buy page — not a search, home or browse page."""
    u = (url or "").lower()
    return ("/album/" in u or "/track/" in u
            or "beatport.com/release" in u or "beatport.com/track" in u
            or "discogs.com/release" in u or "discogs.com/sell/item" in u)

def _maybe_log_purchase_intent(url, d):
    """Log an intent for a buy-store RECORD page, or any click the caller
    anchored to a track/wishlist item. Searches/browses are ignored. Returns
    the intent id or None. Never raises — logging must not break opening a link."""
    try:
        store = _detect_source(url or "")
        if store not in _BUY_STORES:
            return None
        track_id    = d.get("track_id")    if isinstance(d.get("track_id"), int)    else None
        wishlist_id = d.get("wishlist_id") if isinstance(d.get("wishlist_id"), int) else None
        if not (track_id or wishlist_id or _is_release_page(url)):
            return None                      # discovery/search click — not a purchase intent
        artist = (d.get("artist") or "").strip() or None
        title  = (d.get("title")  or "").strip() or None
        if not (artist and title):           # fill from the anchored record
            if wishlist_id:
                w = query_one("SELECT artist, title FROM wishlist WHERE id=?", (wishlist_id,))
                if w: artist = artist or w.get("artist"); title = title or w.get("title")
            elif track_id:
                t = query_one("SELECT artist_name, title FROM tracks WHERE id=?", (track_id,))
                if t: artist = artist or t.get("artist_name"); title = title or t.get("title")
        existing = query_one(
            "SELECT id FROM purchase_intents WHERE url=? AND status='pending' "
            "AND created_at > datetime('now','-1 day')", (url,))   # don't stack dupes
        if existing:
            return existing["id"]
        return execute(
            "INSERT INTO purchase_intents (url, store, track_id, wishlist_id, artist, title) "
            "VALUES (?,?,?,?,?,?)", (url, store, track_id, wishlist_id, artist, title))
    except Exception:
        return None


@app.route("/api/purchase-intents")
def api_purchase_intents():
    status = (request.args.get("status") or "pending").strip()
    where, params = [], []
    if status in ("pending", "bought", "dismissed"):
        where.append("pi.status=?"); params.append(status)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    return jsonify(query(
        f"""SELECT pi.*, t.artwork_path, t.colour_code
            FROM purchase_intents pi LEFT JOIN tracks t ON t.id = pi.track_id
            {wsql} ORDER BY pi.created_at DESC LIMIT 200""", params))


@app.route("/api/purchase-intent/<int:iid>/confirm", methods=["POST"])
def api_purchase_intent_confirm(iid):
    """'I bought it.' Mark bought; if it came from a wishlist item, flip that to
    Supported — so the supported state is anchored to a real click-through."""
    pi = query_one("SELECT * FROM purchase_intents WHERE id=?", (iid,))
    if not pi:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    via = (d.get("via") or "").strip().lower()           # 'bandcamp' | 'other' (where they actually bought it)
    via = via if via in ("bandcamp", "other") else None
    execute("UPDATE purchase_intents SET status='bought', bought_via=?, resolved_at=datetime('now'), "
            "resolved_by='user' WHERE id=?", (via, iid))
    if pi.get("wishlist_id"):
        execute("UPDATE wishlist SET status='supported', bought_via=?, last_visited_at=datetime('now') WHERE id=?",
                (via, pi["wishlist_id"]))
    return jsonify({"ok": True})


@app.route("/api/purchase-intent/<int:iid>/dismiss", methods=["POST"])
def api_purchase_intent_dismiss(iid):
    """'Didn't buy it' — clear the prompt without claiming support."""
    execute("UPDATE purchase_intents SET status='dismissed', resolved_at=datetime('now'), "
            "resolved_by='user' WHERE id=?", (iid,))
    return jsonify({"ok": True})


def _bandcamp_collection(username, limit=400):
    """Read a Bandcamp fan's OWNED releases from their PUBLIC collection page —
    no auth, no API key: parse the #pagedata data-blob, then page older items
    via the fancollection API when a token is present. Returns
    [{artist,title,url}]. Raises on network/parse failure."""
    import urllib.request, urllib.parse as _up, json as _json, html as _html, re as _re
    name = username.strip().lstrip("@")
    req = urllib.request.Request("https://bandcamp.com/" + _up.quote(name),
                                 headers={"User-Agent": "MusicRoom/1.0 (personal DJ library)"})
    page = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
    m = _re.search(r'id="pagedata"[^>]*data-blob="([^"]+)"', page)
    if not m:
        raise ValueError("no collection data on the page — is the profile public?")
    blob = _json.loads(_html.unescape(m.group(1)))
    out, seen = [], set()
    def _add(it):
        key = (it.get("item_url") or "") + (it.get("item_title") or "")
        if key in seen: return
        seen.add(key)
        out.append({"artist": it.get("band_name") or "", "title": it.get("item_title") or "",
                    "url": it.get("item_url") or ""})
    for it in ((blob.get("item_cache") or {}).get("collection") or {}).values():
        _add(it)
    try:
        fan_id = (blob.get("fan_data") or {}).get("fan_id")
        cd     = blob.get("collection_data") or {}
        token  = cd.get("last_token")
        total  = cd.get("item_count") or 0
        while fan_id and token and len(out) < min(limit, total or limit):
            body = _json.dumps({"fan_id": fan_id, "older_than_token": token, "count": 40}).encode()
            r = urllib.request.Request(
                "https://bandcamp.com/api/fancollection/1/collection_items", data=body,
                headers={"User-Agent": "MusicRoom/1.0", "Content-Type": "application/json"})
            data = _json.loads(urllib.request.urlopen(r, timeout=15).read().decode("utf-8", "ignore"))
            items = data.get("items") or []
            if not items: break
            for it in items: _add(it)
            token = data.get("last_token")
            if not data.get("more_available"): break
    except Exception:
        pass                                  # initial blob is enough; pagination is best-effort
    return out


def _norm_title(s):
    return mr_text.words(s)       # accents off, the letter kept (it used to drop the letter)


@app.route("/api/bandcamp/sync", methods=["POST"])
def api_bandcamp_sync():
    """Match releases you OWN on Bandcamp against pending purchase intents +
    open wishlist items, and confirm the matches. Reads your PUBLIC collection;
    needs the 'bandcamp_username' setting (your bandcamp.com/<name>)."""
    d = request.get_json() or {}
    username = (d.get("username") or get_setting("bandcamp_username", "")).strip()
    if not username:
        return jsonify({"ok": False, "error": "Set your Bandcamp username (bandcamp.com/<name>) first."}), 400
    set_setting("bandcamp_username", username)
    try:
        owned = _bandcamp_collection(username)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read your collection: {e}"}), 502
    pairs  = {(_norm_title(o["artist"]), _norm_title(o["title"])) for o in owned}
    titles = {_norm_title(o["title"]) for o in owned if o["title"]}
    def _owned(artist, title):
        t = _norm_title(title)
        if not t: return False
        return (_norm_title(artist), t) in pairs or t in titles   # title-only fallback (artist naming varies)
    intents_confirmed = wishes_supported = 0
    for pi in query("SELECT * FROM purchase_intents WHERE status='pending'"):
        if _owned(pi.get("artist"), pi.get("title")):
            execute("UPDATE purchase_intents SET status='bought', bought_via='bandcamp', resolved_at=datetime('now'), "
                    "resolved_by='collection-sync' WHERE id=?", (pi["id"],))   # found in the Bandcamp collection
            if pi.get("wishlist_id"):
                execute("UPDATE wishlist SET status='supported' WHERE id=?", (pi["wishlist_id"],))
            intents_confirmed += 1
    for w in query("SELECT * FROM wishlist WHERE status='wishing'"):
        if _owned(w.get("artist"), w.get("title")):
            execute("UPDATE wishlist SET status='supported' WHERE id=?", (w["id"],))
            wishes_supported += 1
    return jsonify({"ok": True, "owned_count": len(owned), "username": username,
                    "intents_confirmed": intents_confirmed, "wishlist_supported": wishes_supported})


@app.route("/api/reveal-in-finder", methods=["POST"])
def api_reveal_in_finder():
    """Reveal a file in Finder (selected, parent folder open)."""
    d  = request.get_json() or {}
    fp = (d.get("path") or "").strip()
    if not fp:
        return jsonify({"error": "path required"}), 400
    resolved = _resolve_audio_path(fp) or fp
    try:
        mr_os.reveal(resolved)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup", methods=["POST"])
def api_trigger_backup():
    d = request.get_json() or {}
    dest = d.get("dest", "").strip() or None
    backup_to_drive(dest_dir=dest, block=False)
    return jsonify({"ok": True, "message": "Backup started"})


@app.route("/api/backup/full", methods=["POST"])
def api_full_backup():
    """Mirror everything — app data + ALL music — onto a chosen external drive."""
    d = request.get_json() or {}
    dest = (d.get("dest") or "").strip()
    if not dest:
        return jsonify({"ok": False, "error": "Choose a backup drive first"}), 400
    if _full_backup_state["running"]:
        return jsonify({"ok": False, "error": "A full backup is already running"}), 409
    full_backup_to_drive(dest)
    return jsonify({"ok": True, "message": "Full backup started"})


@app.route("/api/backup/full/status")
def api_full_backup_status():
    s = dict(_full_backup_state)
    s["last_full_backup_at"]   = get_setting("last_full_backup_at", "")
    s["last_full_backup_dest"] = get_setting("last_full_backup_dest", "")
    return jsonify(s)


@app.route("/api/backup/status")
def api_backup_status():
    configured = get_setting("backup_dir", "").strip()
    backup_path = Path(configured) if configured else HD_BACKUP_DIR
    return jsonify({
        "running":        _backup_running,
        "last_backup_at": get_setting("last_backup_at", ""),
        "drive_mounted":  backup_path.parent.exists(),
        "backup_dir":     str(backup_path),
    })


# =============================================================
# API — ACTIVE LISTENING (Éisteacht)
# =============================================================

@app.route("/api/listen/session", methods=["POST"])
def api_create_listen_session():
    d = request.get_json() or {}
    tid = d.get("track_id")
    if not tid:
        return jsonify({"error": "track_id required"}), 400
    sid = execute(
        "INSERT INTO listening_sessions (track_id, started_at) VALUES (?, datetime('now'))",
        (tid,)
    )
    return jsonify({"id": sid})


@app.route("/api/listen/session/<int:sid>/note", methods=["POST"])
def api_add_listen_note(sid):
    d    = request.get_json() or {}
    text = (d.get("note_text") or "").strip()
    if not text:
        return jsonify({"error": "note_text required"}), 400
    session = query_one("SELECT track_id FROM listening_sessions WHERE id=?", (sid,))
    if not session:
        return jsonify({"error": "session not found"}), 404
    nid = execute(
        "INSERT INTO listening_notes (session_id,track_id,timestamp_sec,note_text,note_category,is_voice) VALUES (?,?,?,?,?,?)",
        (sid, session["track_id"],
         d.get("timestamp_sec", 0), text,
         d.get("note_category", ""), int(bool(d.get("is_voice", False))))
    )
    note = query_one("SELECT * FROM listening_notes WHERE id=?", (nid,))
    return jsonify(note)


@app.route("/api/listen/session/<int:sid>/end", methods=["POST"])
def api_end_listen_session(sid):
    execute("UPDATE listening_sessions SET ended_at=datetime('now') WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/listen/note/<int:nid>", methods=["DELETE"])
def api_delete_listen_note(nid):
    execute("DELETE FROM listening_notes WHERE id=?", (nid,))
    return jsonify({"ok": True})


@app.route("/api/listen/note/<int:nid>", methods=["PATCH"])
def api_patch_listen_note(nid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "timestamp_sec" in d:
        try:
            ups.append("timestamp_sec=?"); vals.append(float(d["timestamp_sec"]))
        except (TypeError, ValueError):
            return jsonify({"error": "timestamp_sec must be a number"}), 400
    if "note_category" in d:
        ups.append("note_category=?"); vals.append((d["note_category"] or "").strip())
    if "note_text" in d:
        text = (d["note_text"] or "").strip()
        if not text:
            return jsonify({"error": "note_text cannot be empty"}), 400
        ups.append("note_text=?"); vals.append(text)
    if "detail_text" in d:
        # the long-form "page" beneath a moment — may be emptied freely
        ups.append("detail_text=?"); vals.append((d["detail_text"] or "").rstrip() or None)
    for k in ("canvas_x", "canvas_y"):        # the user's placement on the idea canvas
        if k in d:
            try:
                ups.append(f"{k}=?"); vals.append(None if d[k] is None else float(d[k]))
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} must be a number"}), 400
    if not ups:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(nid)
    execute(f"UPDATE listening_notes SET {', '.join(ups)} WHERE id=?", vals)
    note = query_one("SELECT * FROM listening_notes WHERE id=?", (nid,))
    if not note:
        return jsonify({"error": "note not found"}), 404
    return jsonify(note)


@app.route("/api/listen/reference", methods=["POST"])
def api_save_reference():
    """Save something READ (an Explore article, a video, a mix page) into the
    research substrate as a reference note — a moment with no track. It can
    then join threads/projects, take custom fields, sit on the canvas and be
    linked to listening moments like any other atom (the polyrhythm-video →
    polyrhythmic-techno-moments connection). Idempotent per URL: saving the
    same article twice returns the existing note.

    A video can be saved at a moment in it: "timestamp_sec" (seconds, 0 or more) is kept on
    the note, and the link should carry the same time (youtu.be/…?t=559), so each moment of
    one video is its own note."""
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    title = (d.get("title") or "").strip()
    if not url or not title:
        return jsonify({"error": "url and title required"}), 400
    at = d.get("timestamp_sec")
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= at < 1_000_000 else 0.0
    existing = query_one(
        "SELECT id FROM listening_notes WHERE kind='reference' AND ref_url=?", (url,))
    if existing:
        nid, created = existing["id"], False
    else:
        # the article's own summary seeds the note body — the curator rewrites
        # it into their own words as the research grows
        text = (d.get("summary") or "").strip() or title
        nid = execute(
            """INSERT INTO listening_notes
               (kind, note_text, note_category, ref_url, ref_title, ref_source, ref_image_url, timestamp_sec)
               VALUES ('reference', ?, ?, ?, ?, ?, ?, ?)""",
            (text, (d.get("category") or "").strip(), url, title,
             (d.get("source") or "").strip() or None,
             (d.get("image_url") or "").strip() or None, at))
        created = True
    # optionally land it straight inside a project / on a thread
    pid = d.get("project_id")
    if pid and query_one("SELECT 1 FROM listening_projects WHERE id=?", (pid,)):
        execute("INSERT OR IGNORE INTO project_notes (project_id, note_id) VALUES (?,?)", (pid, nid))
    tid = d.get("thread_id")
    if tid and query_one("SELECT 1 FROM threads WHERE id=?", (tid,)):
        pos = query_one("SELECT COALESCE(MAX(position),0)+1 AS p FROM thread_moments WHERE thread_id=?", (tid,))["p"]
        execute("INSERT OR IGNORE INTO thread_moments (thread_id, note_id, position) VALUES (?,?,?)", (tid, nid, pos))
    note = query_one("SELECT * FROM listening_notes WHERE id=?", (nid,))
    return jsonify({"created": created, **dict(note)})


@app.route("/api/listen/reference/upload", methods=["POST"])
def api_upload_reference():
    """Attach a LOCAL file (a PDF paper, a scan of liner notes, a photo of a
    sleeve or a venue) as a reference note. Saved under the data dir's static
    root and served at /static/research/<name>, so it embeds in a composition
    like any other reference atom. Images render inline; PDFs/others link out."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    import uuid, re
    base = os.path.basename(f.filename)
    ext = ""
    if "." in base:
        ext = "." + re.sub(r"[^A-Za-z0-9]", "", base.rsplit(".", 1)[1].lower())[:8]
    fname = uuid.uuid4().hex + ext
    research_dir = os.path.join(_DATA_STATIC, "research")
    os.makedirs(research_dir, exist_ok=True)
    f.save(os.path.join(research_dir, fname))
    served = f"/static/research/{fname}"
    title  = (request.form.get("title") or "").strip() or base
    source = (request.form.get("source") or "").strip() or None
    # an uploaded image doubles as its own thumbnail
    img = served if ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg") else None
    nid = execute(
        """INSERT INTO listening_notes
           (kind, note_text, ref_title, ref_source, ref_file_path, ref_image_url)
           VALUES ('reference', ?, ?, ?, ?, ?)""",
        (title, title, source, served, img))
    note = query_one("SELECT * FROM listening_notes WHERE id=?", (nid,))
    return jsonify({"created": True, **dict(note)}), 201


# =============================================================
# THE RESEARCH DESK — artist / label / venue / genre entities, each a
# structured PAGE and a connectable canvas CARD. research_links is one
# generalized graph joining entities to each other AND to listening notes
# (and reference attachments), the way a curator's mind connects things.
# =============================================================
RESEARCH_KINDS = ("artist", "label", "venue", "genre")

def _entity_tracks(ent):
    """Records in the library that match this entity, so research connects to the
    real collection. Artist/label by name; genre via track_genres."""
    k, name = ent["kind"], ent["name"]
    if k == "artist":
        return query("SELECT id, title, artist_name, artwork_path, colour_code, art_colour FROM tracks "
                     "WHERE is_deleted=0 AND LOWER(artist_name)=LOWER(?) ORDER BY title COLLATE NOCASE LIMIT 300", (name,))
    if k == "label":
        return query("SELECT id, title, artist_name, artwork_path, colour_code, art_colour FROM tracks "
                     "WHERE is_deleted=0 AND LOWER(COALESCE(label,''))=LOWER(?) ORDER BY artist_name COLLATE NOCASE LIMIT 300", (name,))
    if k == "genre":
        return query("SELECT t.id, t.title, t.artist_name, t.artwork_path, t.colour_code, t.art_colour FROM tracks t "
                     "JOIN track_genres tg ON tg.track_id=t.id JOIN genres g ON g.id=tg.genre_id "
                     "WHERE t.is_deleted=0 AND LOWER(g.name)=LOWER(?) ORDER BY t.artist_name COLLATE NOCASE LIMIT 300", (name,))
    if k == "venue":
        # A venue ties to a PLACE of the same name: the tracks curated into that
        # place (place_tracks) plus everything played in sets held there (set_tracks
        # via sets.place_id). Free-text venues with no matching place simply match 0.
        return query(
            "SELECT t.id, t.title, t.artist_name, t.artwork_path, t.colour_code, t.art_colour FROM tracks t "
            "JOIN place_tracks pt ON pt.track_id=t.id "
            "JOIN places p ON p.id=pt.place_id AND LOWER(p.name)=LOWER(?) WHERE t.is_deleted=0 "
            "UNION "
            "SELECT t.id, t.title, t.artist_name, t.artwork_path, t.colour_code, t.art_colour FROM tracks t "
            "JOIN set_tracks st ON st.track_id=t.id JOIN sets s ON s.id=st.set_id "
            "JOIN places p ON p.id=s.place_id AND LOWER(p.name)=LOWER(?) WHERE t.is_deleted=0 "
            "ORDER BY artist_name COLLATE NOCASE LIMIT 300", (name, name))
    return []

def _entity_connections(eid):
    """Everything linked to this entity — other entities AND listening notes /
    reference attachments — resolved for display, with the link id + label."""
    rows = query("SELECT * FROM research_links WHERE (from_kind='entity' AND from_id=?) "
                 "OR (to_kind='entity' AND to_id=?) ORDER BY created_at", (eid, eid))
    out = []
    for r in rows:
        if r["from_kind"] == "entity" and r["from_id"] == eid:
            ok, oid = r["to_kind"], r["to_id"]
        else:
            ok, oid = r["from_kind"], r["from_id"]
        item = {"link_id": r["id"], "label": r["label"], "kind": ok, "id": oid}
        if ok == "entity":
            e = query_one("SELECT id, kind, name, image_path, image_url FROM research_entities WHERE id=?", (oid,))
            if e:
                item.update({"entity_kind": e["kind"], "name": e["name"],
                             "image": e["image_path"] or e["image_url"]})
            else:
                continue
        else:  # a listening note or a reference attachment
            n = query_one("SELECT ln.*, t.title AS track_title, t.artist_name AS track_artist "
                          "FROM listening_notes ln LEFT JOIN tracks t ON t.id=ln.track_id WHERE ln.id=?", (oid,))
            if not n:
                continue
            item.update({"note_kind": n["kind"], "note_text": n["note_text"],
                         "ref_title": n["ref_title"], "ref_url": n["ref_url"],
                         "ref_image_url": n["ref_image_url"], "ref_file_path": n["ref_file_path"],
                         "ref_source": n["ref_source"], "track_title": n["track_title"],
                         "track_artist": n["track_artist"],
                         "name": n["ref_title"] or n["track_title"] or (n["note_text"] or "note")[:48]})
        out.append(item)
    return out

def _entity_notes(ent):
    """The curator's listening notes that already touch this entity — notes on
    tracks by this artist / on this label / in this genre. So opening an artist
    surfaces what you've already written while listening."""
    k, name = ent["kind"], ent["name"]
    base = ("SELECT ln.id, ln.note_text, ln.timestamp_sec, ln.note_category, ln.created_at, "
            "t.id AS track_id, t.title AS track_title, t.artist_name AS track_artist, t.artwork_path "
            "FROM listening_notes ln JOIN tracks t ON t.id=ln.track_id "
            "WHERE ln.kind='moment' AND t.is_deleted=0 AND ")
    if k == "artist":
        return query(base + "LOWER(t.artist_name)=LOWER(?) ORDER BY ln.created_at DESC LIMIT 120", (name,))
    if k == "label":
        return query(base + "LOWER(COALESCE(t.label,''))=LOWER(?) ORDER BY ln.created_at DESC LIMIT 120", (name,))
    if k == "genre":
        return query(base.replace("JOIN tracks t ON t.id=ln.track_id",
                                  "JOIN tracks t ON t.id=ln.track_id "
                                  "JOIN track_genres tg ON tg.track_id=t.id JOIN genres g ON g.id=tg.genre_id")
                     + "LOWER(g.name)=LOWER(?) ORDER BY ln.created_at DESC LIMIT 120", (name,))
    if k == "venue":
        return query(
            base + "t.id IN ("
            "SELECT pt.track_id FROM place_tracks pt JOIN places p ON p.id=pt.place_id AND LOWER(p.name)=LOWER(?) "
            "UNION SELECT st.track_id FROM set_tracks st JOIN sets s ON s.id=st.set_id "
            "JOIN places p ON p.id=s.place_id AND LOWER(p.name)=LOWER(?)) "
            "ORDER BY ln.created_at DESC LIMIT 120", (name, name))
    return []

def _entity_full(eid):
    e = query_one("SELECT * FROM research_entities WHERE id=?", (eid,))
    if not e:
        return None
    d = dict(e)
    try:
        d["links_list"] = _json.loads(e["links"] or "[]")
    except Exception:
        d["links_list"] = []
    d["connections"] = _entity_connections(eid)
    d["tracks"] = _entity_tracks(e)
    d["record_count"] = len(d["tracks"])
    d["notes"] = _entity_notes(e)
    return d

@app.route("/api/research/roster")
def api_research_roster():
    """The full desk: EVERY artist / label / genre in the library (with record
    counts) merged with the user's research entities, so the rail is populated
    from the real collection. Venues are user-created only. An item gets an
    entity_id once it's been materialised (clicked / researched)."""
    ents = query("SELECT id, kind, name, body FROM research_entities")
    ent_by = {(e["kind"], (e["name"] or "").lower()): e for e in ents}
    def conn_count(eid):
        return query_one("SELECT COUNT(*) n FROM research_links WHERE (from_kind='entity' AND from_id=?) "
                         "OR (to_kind='entity' AND to_id=?)", (eid, eid))["n"]
    def merge(kind, lib_rows):
        out = {}
        for r in lib_rows:
            nm = (r["name"] or "").strip()
            if not nm:
                continue
            out[nm.lower()] = {"kind": kind, "name": nm, "record_count": r["record_count"],
                               "entity_id": None, "has_research": False, "conn_count": 0}
        for (k, lk), e in ent_by.items():
            if k != kind:
                continue
            it = out.get(lk) or {"kind": kind, "name": e["name"], "record_count": 0,
                                 "entity_id": None, "has_research": False, "conn_count": 0}
            cc = conn_count(e["id"])
            it.update({"entity_id": e["id"], "conn_count": cc,
                       "has_research": bool((e["body"] or "").strip()) or cc > 0})
            out[lk] = it
        rows = list(out.values())
        rows.sort(key=lambda x: (-x["record_count"], x["name"].lower()))
        return rows
    artists = merge("artist", query(
        "SELECT artist_name AS name, COUNT(*) AS record_count FROM tracks "
        "WHERE is_deleted=0 AND COALESCE(artist_name,'')!='' GROUP BY artist_name COLLATE NOCASE"))
    labels = merge("label", query(
        "SELECT label AS name, COUNT(*) AS record_count FROM tracks "
        "WHERE is_deleted=0 AND COALESCE(label,'')!='' GROUP BY label COLLATE NOCASE"))
    genres = merge("genre", query(
        "SELECT g.name AS name, COUNT(DISTINCT tg.track_id) AS record_count FROM genres g "
        "JOIN track_genres tg ON tg.genre_id=g.id JOIN tracks t ON t.id=tg.track_id AND t.is_deleted=0 "
        "GROUP BY g.id"))
    # Venues are the curator's PLACES — venue-like spaces (crowd notes, set times).
    # Record count = distinct live tracks curated into the place or played in a set
    # held there. Places with no tracks still list (a venue you've made, not yet filled).
    venues = merge("venue", query(
        "SELECT p.name AS name, COUNT(DISTINCT t.id) AS record_count FROM places p "
        "LEFT JOIN (SELECT place_id, track_id AS tid FROM place_tracks "
        "           UNION SELECT s.place_id, st.track_id FROM set_tracks st JOIN sets s ON s.id=st.set_id) x "
        "  ON x.place_id=p.id "
        "LEFT JOIN tracks t ON t.id=x.tid AND t.is_deleted=0 "
        "WHERE COALESCE(p.name,'')!='' AND COALESCE(p.is_well,0)=0 GROUP BY p.id"))
    return jsonify({"artist": artists, "label": labels, "genre": genres, "venue": venues})

@app.route("/api/research/entities")
def api_research_entities():
    kind = (request.args.get("kind") or "").strip().lower()
    where, params = [], []
    if kind in RESEARCH_KINDS:
        where.append("kind=?"); params.append(kind)
    sql = "SELECT * FROM research_entities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY kind, name COLLATE NOCASE"
    rows = query(sql, params)
    for r in rows:
        r["conn_count"] = query_one(
            "SELECT COUNT(*) n FROM research_links WHERE (from_kind='entity' AND from_id=?) "
            "OR (to_kind='entity' AND to_id=?)", (r["id"], r["id"]))["n"]
    return jsonify(rows)

@app.route("/api/research/entities", methods=["POST"])
def api_create_research_entity():
    d = request.get_json() or {}
    kind = (d.get("kind") or "").strip().lower()
    name = (d.get("name") or "").strip()
    if kind not in RESEARCH_KINDS or not name:
        return jsonify({"error": "kind (artist|label|venue|genre) and name required"}), 400
    existing = query_one("SELECT * FROM research_entities WHERE kind=? AND LOWER(name)=LOWER(?)", (kind, name))
    if existing:
        return jsonify(_entity_full(existing["id"])), 200
    eid = execute("INSERT INTO research_entities (kind, name, body) VALUES (?,?,?)",
                  (kind, name, (d.get("body") or "").strip() or None))
    return jsonify(_entity_full(eid)), 201

@app.route("/api/research/entity/<int:eid>")
def api_get_research_entity(eid):
    full = _entity_full(eid)
    if not full:
        return jsonify({"error": "not found"}), 404
    return jsonify(full)

_ENTITY_FIELDS = ("name", "body", "image_url", "country", "city", "founded", "canvas_x", "canvas_y")

@app.route("/api/research/entity/<int:eid>", methods=["PATCH"])
def api_patch_research_entity(eid):
    if not query_one("SELECT 1 FROM research_entities WHERE id=?", (eid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    sets, vals = [], []
    for k in _ENTITY_FIELDS:
        if k in d:
            v = d[k]
            if k in ("canvas_x", "canvas_y"):
                v = float(v) if v is not None else None
            elif isinstance(v, str):
                v = v.strip() or None
            sets.append(f"{k}=?"); vals.append(v)
    if "links" in d:
        sets.append("links=?"); vals.append(_json.dumps(d["links"]) if d["links"] else None)
    if not sets:
        return jsonify({"error": "no fields"}), 400
    sets.append("updated_at=datetime('now')")
    execute(f"UPDATE research_entities SET {', '.join(sets)} WHERE id=?", vals + [eid])
    return jsonify(_entity_full(eid))

@app.route("/api/research/entity/<int:eid>", methods=["DELETE"])
def api_delete_research_entity(eid):
    execute("DELETE FROM research_links WHERE (from_kind='entity' AND from_id=?) OR (to_kind='entity' AND to_id=?)", (eid, eid))
    execute("DELETE FROM research_entities WHERE id=?", (eid,))
    return jsonify({"ok": True})

@app.route("/api/research/entity/<int:eid>/image", methods=["POST"])
def api_research_entity_image(eid):
    if not query_one("SELECT 1 FROM research_entities WHERE id=?", (eid,)):
        return jsonify({"error": "not found"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    import uuid, re
    base = os.path.basename(f.filename); ext = ""
    if "." in base:
        ext = "." + re.sub(r"[^A-Za-z0-9]", "", base.rsplit(".", 1)[1].lower())[:8]
    fname = uuid.uuid4().hex + ext
    research_dir = os.path.join(_DATA_STATIC, "research"); os.makedirs(research_dir, exist_ok=True)
    f.save(os.path.join(research_dir, fname))
    served = f"/static/research/{fname}"
    execute("UPDATE research_entities SET image_path=?, updated_at=datetime('now') WHERE id=?", (served, eid))
    return jsonify({"ok": True, "image_path": served})

@app.route("/api/research/links")
def api_research_links_all():
    """Every connection in the graph — used by the canvas to draw entity↔entity
    and entity↔note edges."""
    return jsonify(query("SELECT * FROM research_links ORDER BY id"))

@app.route("/api/research/links", methods=["POST"])
def api_create_research_link():
    """Join any two cards — entity<->entity or entity<->note — with a labelled
    edge. Idempotent + direction-insensitive (A↔B == B↔A)."""
    d = request.get_json() or {}
    fk, tk = (d.get("from_kind") or "").strip(), (d.get("to_kind") or "").strip()
    fi, ti = d.get("from_id"), d.get("to_id")
    if fk not in ("entity", "note") or tk not in ("entity", "note") or not isinstance(fi, int) or not isinstance(ti, int):
        return jsonify({"error": "from_kind/to_kind (entity|note) + integer from_id/to_id required"}), 400
    if fk == tk and fi == ti:
        return jsonify({"error": "cannot link to itself"}), 400
    ex = query_one("SELECT id FROM research_links WHERE (from_kind=? AND from_id=? AND to_kind=? AND to_id=?) "
                   "OR (from_kind=? AND from_id=? AND to_kind=? AND to_id=?)",
                   (fk, fi, tk, ti, tk, ti, fk, fi))
    if ex:
        if "label" in d:
            execute("UPDATE research_links SET label=? WHERE id=?", ((d.get("label") or "").strip() or None, ex["id"]))
        return jsonify({"ok": True, "id": ex["id"]}), 200
    lid = execute("INSERT INTO research_links (from_kind, from_id, to_kind, to_id, label) VALUES (?,?,?,?,?)",
                  (fk, fi, tk, ti, (d.get("label") or "").strip() or None))
    return jsonify({"ok": True, "id": lid}), 201

@app.route("/api/research/link/<int:lid>", methods=["PATCH"])
def api_patch_research_link(lid):
    d = request.get_json() or {}
    execute("UPDATE research_links SET label=? WHERE id=?", ((d.get("label") or "").strip() or None, lid))
    return jsonify({"ok": True})

@app.route("/api/research/link/<int:lid>", methods=["DELETE"])
def api_delete_research_link(lid):
    execute("DELETE FROM research_links WHERE id=?", (lid,))
    return jsonify({"ok": True})


# ── saved maps — a map made on the canvas IS a piece of research ──────────────
def _map_node_rows(d):
    """Pull a clean [{kind,ref_id,x,y}] list out of a request body's 'nodes'."""
    out = []
    for n in (d.get("nodes") or []):
        k = (n.get("kind") or "").strip()
        if k not in ("entity", "note"):
            continue
        try:
            out.append((k, int(n["ref_id"]), float(n.get("x") or 0), float(n.get("y") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out

@app.route("/api/research/maps")
def api_research_maps():
    rows = query("SELECT * FROM research_maps ORDER BY updated_at DESC")
    for r in rows:
        r["node_count"] = query_one("SELECT COUNT(*) n FROM research_map_nodes WHERE map_id=?", (r["id"],))["n"]
    return jsonify(rows)

@app.route("/api/research/maps", methods=["POST"])
def api_create_research_map():
    d = request.get_json() or {}
    title = (d.get("title") or "").strip() or "Untitled map"
    mid = execute("INSERT INTO research_maps (title, description) VALUES (?,?)",
                  (title, (d.get("description") or "").strip() or None))
    for (k, rid, x, y) in _map_node_rows(d):          # optional snapshot of the current canvas
        execute("INSERT OR REPLACE INTO research_map_nodes (map_id, node_kind, node_ref_id, x, y) VALUES (?,?,?,?,?)",
                (mid, k, rid, x, y))
    return jsonify(_map_full(mid)), 201

def _map_full(mid):
    m = query_one("SELECT * FROM research_maps WHERE id=?", (mid,))
    if not m:
        return None
    d = dict(m)
    d["nodes"] = [{"kind": r["node_kind"], "ref_id": r["node_ref_id"], "x": r["x"], "y": r["y"]}
                  for r in query("SELECT * FROM research_map_nodes WHERE map_id=?", (mid,))]
    return d

@app.route("/api/research/map/<int:mid>")
def api_get_research_map(mid):
    full = _map_full(mid)
    if not full:
        return jsonify({"error": "not found"}), 404
    return jsonify(full)

@app.route("/api/research/map-background", methods=["POST"])
def api_research_map_background():
    """Upload an image to use as a mind-map background (atmosphere, not content).
    Saves under the data static root, returns its served URL; the caller stores it
    on the map (PATCH bg_image) or in settings (the free canvas). Images only."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    import uuid, re
    base = os.path.basename(f.filename)
    ext = ("." + re.sub(r"[^A-Za-z0-9]", "", base.rsplit(".", 1)[1].lower())[:8]) if "." in base else ""
    # raster only — SVG can carry script; a board background never needs it
    if ext.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return jsonify({"error": "image files only (png/jpg/jpeg/gif/webp)"}), 400
    fname = "mapbg_" + uuid.uuid4().hex + ext
    research_dir = os.path.join(_DATA_STATIC, "research")
    os.makedirs(research_dir, exist_ok=True)
    f.save(os.path.join(research_dir, fname))
    return jsonify({"url": f"/static/research/{fname}"}), 201

@app.route("/api/research/map/<int:mid>", methods=["PATCH"])
def api_patch_research_map(mid):
    if not query_one("SELECT 1 FROM research_maps WHERE id=?", (mid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    sets, vals = [], []
    # title/description + the map's own background (a tone and/or an uploaded image).
    # bg_colour/bg_image accept None to clear (one-click remove).
    for k in ("title", "description", "bg_colour", "bg_image"):
        if k in d:
            v = d[k]
            sets.append(f"{k}=?"); vals.append((v or "").strip() or None if isinstance(v, str) else None)
    if sets:
        sets.append("updated_at=datetime('now')")
        execute(f"UPDATE research_maps SET {', '.join(sets)} WHERE id=?", vals + [mid])
    return jsonify(_map_full(mid))

@app.route("/api/research/map/<int:mid>", methods=["DELETE"])
def api_delete_research_map(mid):
    execute("DELETE FROM research_map_nodes WHERE map_id=?", (mid,))
    execute("DELETE FROM research_maps WHERE id=?", (mid,))
    return jsonify({"ok": True})

@app.route("/api/research/map/<int:mid>/node", methods=["POST"])
def api_research_map_node(mid):
    """Add a card to a map, or move it — upsert by (map, kind, ref)."""
    if not query_one("SELECT 1 FROM research_maps WHERE id=?", (mid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    k = (d.get("kind") or "").strip()
    if k not in ("entity", "note") or not isinstance(d.get("ref_id"), int):
        return jsonify({"error": "kind (entity|note) + integer ref_id required"}), 400
    execute("INSERT OR REPLACE INTO research_map_nodes (map_id, node_kind, node_ref_id, x, y) VALUES (?,?,?,?,?)",
            (mid, k, d["ref_id"], float(d.get("x") or 0), float(d.get("y") or 0)))
    execute("UPDATE research_maps SET updated_at=datetime('now') WHERE id=?", (mid,))
    return jsonify({"ok": True})

@app.route("/api/research/map/<int:mid>/node", methods=["DELETE"])
def api_research_map_node_remove(mid):
    d = request.get_json(silent=True) or request.args
    k = (d.get("kind") or "").strip()
    try:
        rid = int(d.get("ref_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "kind + ref_id required"}), 400
    execute("DELETE FROM research_map_nodes WHERE map_id=? AND node_kind=? AND node_ref_id=?", (mid, k, rid))
    return jsonify({"ok": True})


# ── research projects — a curated subset of the desk to explore ──────────────
def _folder_is_descendant(candidate_id, ancestor_id):
    """True if candidate_id lives inside ancestor_id's subtree (cycle guard)."""
    cur, seen = candidate_id, set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        if cur == ancestor_id:
            return True
        row = query_one("SELECT parent_id FROM research_folders WHERE id=?", (cur,))
        cur = row["parent_id"] if row else None
    return False

def _project_full(pid):
    p = query_one("SELECT * FROM research_projects WHERE id=?", (pid,))
    if not p:
        return None
    d = dict(p)
    ents, maps = [], []
    for it in query("SELECT * FROM research_project_items WHERE project_id=? ORDER BY added_at", (pid,)):
        if it["item_kind"] == "entity":
            e = query_one("SELECT id, kind, name, body, image_path, image_url FROM research_entities WHERE id=?", (it["item_ref_id"],))
            if e:
                er = dict(e); er["record_count"] = len(_entity_tracks(e)); er["folder_id"] = it["folder_id"]; ents.append(er)
        elif it["item_kind"] == "map":
            m = query_one("SELECT id, title FROM research_maps WHERE id=?", (it["item_ref_id"],))
            if m:
                mr = dict(m)
                mr["node_count"] = query_one("SELECT COUNT(*) n FROM research_map_nodes WHERE map_id=?", (m["id"],))["n"]
                mr["folder_id"] = it["folder_id"]
                maps.append(mr)
    d["entities"] = ents
    d["maps"] = maps
    d["folders"] = [{"id": f["id"], "parent_id": f["parent_id"], "name": f["name"], "position": f["position"]}
                    for f in query("SELECT * FROM research_folders WHERE project_id=? ORDER BY position, name", (pid,))]
    return d

@app.route("/api/research/projects")
def api_research_projects():
    rows = query("SELECT * FROM research_projects ORDER BY updated_at DESC")
    for r in rows:
        r["item_count"] = query_one("SELECT COUNT(*) n FROM research_project_items WHERE project_id=?", (r["id"],))["n"]
    return jsonify(rows)

@app.route("/api/research/projects", methods=["POST"])
def api_create_research_project():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip() or "Untitled project"
    pid = execute("INSERT INTO research_projects (name, description) VALUES (?,?)",
                  (name, (d.get("description") or "").strip() or None))
    for it in (d.get("items") or []):
        k = (it.get("kind") or "").strip()
        if k in ("entity", "map") and isinstance(it.get("ref_id"), int):
            execute("INSERT OR IGNORE INTO research_project_items (project_id, item_kind, item_ref_id) VALUES (?,?,?)",
                    (pid, k, it["ref_id"]))
    return jsonify(_project_full(pid)), 201

@app.route("/api/research/project/<int:pid>")
def api_get_research_project(pid):
    full = _project_full(pid)
    if not full:
        return jsonify({"error": "not found"}), 404
    return jsonify(full)

@app.route("/api/research/project/<int:pid>", methods=["PATCH"])
def api_patch_research_project(pid):
    if not query_one("SELECT 1 FROM research_projects WHERE id=?", (pid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    sets, vals = [], []
    for k in ("name", "description"):
        if k in d:
            sets.append(f"{k}=?"); vals.append((d[k] or "").strip() or None)
    if sets:
        sets.append("updated_at=datetime('now')")
        execute(f"UPDATE research_projects SET {', '.join(sets)} WHERE id=?", vals + [pid])
    return jsonify(_project_full(pid))

@app.route("/api/research/project/<int:pid>", methods=["DELETE"])
def api_delete_research_project(pid):
    execute("DELETE FROM research_project_items WHERE project_id=?", (pid,))
    execute("DELETE FROM research_folders WHERE project_id=?", (pid,))
    execute("DELETE FROM research_projects WHERE id=?", (pid,))
    return jsonify({"ok": True})

@app.route("/api/research/project/<int:pid>/items", methods=["POST"])
def api_research_project_add_item(pid):
    """Drop a thing (or a group of things) into a project — entities and/or maps."""
    if not query_one("SELECT 1 FROM research_projects WHERE id=?", (pid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    items = d.get("items") or [{"kind": d.get("kind"), "ref_id": d.get("ref_id")}]
    fld_default = d.get("folder_id") if isinstance(d.get("folder_id"), int) else None
    n = 0
    for it in items:
        k = (it.get("kind") or "").strip()
        if k in ("entity", "map") and isinstance(it.get("ref_id"), int):
            fld = it.get("folder_id", fld_default)
            fld = fld if isinstance(fld, int) else None
            execute("INSERT OR IGNORE INTO research_project_items (project_id, item_kind, item_ref_id, folder_id) VALUES (?,?,?,?)",
                    (pid, k, it["ref_id"], fld))
            n += 1
    execute("UPDATE research_projects SET updated_at=datetime('now') WHERE id=?", (pid,))
    return jsonify({"ok": True, "added": n})

@app.route("/api/research/project/<int:pid>/items", methods=["DELETE"])
def api_research_project_remove_item(pid):
    d = request.get_json(silent=True) or request.args
    k = (d.get("kind") or "").strip()
    try:
        rid = int(d.get("ref_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "kind + ref_id required"}), 400
    execute("DELETE FROM research_project_items WHERE project_id=? AND item_kind=? AND item_ref_id=?", (pid, k, rid))
    return jsonify({"ok": True})

@app.route("/api/research/project/<int:pid>/item-folder", methods=["POST"])
def api_research_item_folder(pid):
    """Move an item into a folder within the project (folder_id null = root)."""
    d = request.get_json() or {}
    k = (d.get("kind") or "").strip()
    try:
        rid = int(d.get("ref_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "kind + ref_id required"}), 400
    fld = d.get("folder_id")
    fld = fld if (isinstance(fld, int) and query_one("SELECT 1 FROM research_folders WHERE id=? AND project_id=?", (fld, pid))) else None
    execute("UPDATE research_project_items SET folder_id=? WHERE project_id=? AND item_kind=? AND item_ref_id=?",
            (fld, pid, k, rid))
    return jsonify({"ok": True})

@app.route("/api/research/project/<int:pid>/folders", methods=["POST"])
def api_create_research_folder(pid):
    if not query_one("SELECT 1 FROM research_projects WHERE id=?", (pid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    name = (d.get("name") or "").strip() or "Folder"
    parent = d.get("parent_id")
    parent = parent if (isinstance(parent, int) and query_one("SELECT 1 FROM research_folders WHERE id=? AND project_id=?", (parent, pid))) else None
    pos = query_one("SELECT COALESCE(MAX(position),0)+1 AS p FROM research_folders WHERE project_id=?", (pid,))["p"]
    fid = execute("INSERT INTO research_folders (project_id, parent_id, name, position) VALUES (?,?,?,?)",
                  (pid, parent, name, pos))
    execute("UPDATE research_projects SET updated_at=datetime('now') WHERE id=?", (pid,))
    return jsonify({"ok": True, "id": fid}), 201

@app.route("/api/research/folder/<int:fid>", methods=["PATCH"])
def api_patch_research_folder(fid):
    f = query_one("SELECT * FROM research_folders WHERE id=?", (fid,))
    if not f:
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    if "name" in d:
        execute("UPDATE research_folders SET name=? WHERE id=?", ((d["name"] or "").strip() or "Folder", fid))
    if "parent_id" in d:
        np = d["parent_id"]
        if np is None:
            execute("UPDATE research_folders SET parent_id=NULL WHERE id=?", (fid,))
        elif isinstance(np, int):
            pf = query_one("SELECT project_id FROM research_folders WHERE id=?", (np,))
            if pf and pf["project_id"] == f["project_id"] and np != fid and not _folder_is_descendant(np, fid):
                execute("UPDATE research_folders SET parent_id=? WHERE id=?", (np, fid))
    return jsonify({"ok": True})

@app.route("/api/research/folder/<int:fid>", methods=["DELETE"])
def api_delete_research_folder(fid):
    f = query_one("SELECT * FROM research_folders WHERE id=?", (fid,))
    if not f:
        return jsonify({"ok": True})
    parent = f["parent_id"]                              # children + items rise to this folder's parent — nothing lost
    execute("UPDATE research_folders SET parent_id=? WHERE parent_id=?", (parent, fid))
    execute("UPDATE research_project_items SET folder_id=? WHERE folder_id=?", (parent, fid))
    execute("DELETE FROM research_folders WHERE id=?", (fid,))
    return jsonify({"ok": True})


@app.route("/api/listen/notes")
def api_listen_notes():
    category = request.args.get("category", "")
    tid      = request.args.get("track_id", type=int)
    thread_id = request.args.get("thread_id", type=int)
    project_id = request.args.get("project_id", type=int)
    filters, params = [], []
    if category:
        filters.append("ln.note_category = ?"); params.append(category)
    if tid:
        filters.append("ln.track_id = ?"); params.append(tid)
    if thread_id:
        filters.append("ln.id IN (SELECT note_id FROM thread_moments WHERE thread_id = ?)")
        params.append(thread_id)
    if project_id:
        filters.append("ln.id IN (SELECT note_id FROM project_notes WHERE project_id = ?)")
        params.append(project_id)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    notes = query(f"""
        SELECT ln.*, t.title, t.artist_name, t.year, t.artwork_path, t.colour_code,
               ls.started_at as session_started_at,
               f.id as file_id, f.duration_sec, f.bpm, f.key_note, f.key_scale,
               (SELECT GROUP_CONCAT(tg.name, ',')
                  FROM track_tags tt
                  JOIN tags tg ON tg.id = tt.tag_id
                  WHERE tt.track_id = ln.track_id) AS tag_csv,
               (SELECT GROUP_CONCAT(g.name, ',')
                  FROM track_genres trg
                  JOIN genres g ON g.id = trg.genre_id
                  WHERE trg.track_id = ln.track_id) AS genre_csv,
               (SELECT GROUP_CONCAT(p.id || ':' || p.name, '|')
                  FROM place_tracks pt
                  JOIN places p ON p.id = pt.place_id
                  WHERE pt.track_id = ln.track_id) AS place_csv,
               (SELECT GROUP_CONCAT(th.id || ':' || th.name || ':' || COALESCE(th.colour,''), '|')
                  FROM thread_moments tm
                  JOIN threads th ON th.id = tm.thread_id
                  WHERE tm.note_id = ln.id) AS thread_csv,
               (SELECT GROUP_CONCAT(lp.id || ':' || lp.name, '|')
                  FROM project_notes pn
                  JOIN listening_projects lp ON lp.id = pn.project_id
                  WHERE pn.note_id = ln.id) AS project_csv
        FROM listening_notes ln
        LEFT JOIN tracks t ON t.id = ln.track_id
        LEFT JOIN listening_sessions ls ON ls.id = ln.session_id
        LEFT JOIN files f ON f.track_id = ln.track_id AND f.is_primary = 1 AND f.is_deleted = 0
        {where}
        ORDER BY ln.created_at DESC
    """, params)
    for n in notes:
        tag_csv = n.pop("tag_csv", None) or ""
        n["tag_list"] = [t for t in (s.strip() for s in tag_csv.split(",")) if t]
        genre_csv = n.pop("genre_csv", None) or ""
        n["genre_list"] = [g for g in (s.strip() for s in genre_csv.split(",")) if g]
        place_csv = n.pop("place_csv", None) or ""
        places = []
        for part in place_csv.split("|"):
            if not part: continue
            try:
                pid, pname = part.split(":", 1)
                places.append({"id": int(pid), "name": pname})
            except Exception:
                pass
        n["places"] = places
        thread_csv = n.pop("thread_csv", None) or ""
        threads = []
        for part in thread_csv.split("|"):
            if not part: continue
            bits = part.split(":", 2)
            if len(bits) == 3:
                try: threads.append({"id": int(bits[0]), "name": bits[1], "colour": bits[2] or None})
                except Exception: pass
        n["threads"] = threads
        project_csv = n.pop("project_csv", None) or ""
        projects = []
        for part in project_csv.split("|"):
            if not part: continue
            try:
                ppid, ppname = part.split(":", 1)
                projects.append({"id": int(ppid), "name": ppname})
            except Exception:
                pass
        n["projects"] = projects
    # Custom fields (user-defined key:value) — fetched separately so free-text
    # values can't collide with CSV delimiters.
    note_ids = [n["id"] for n in notes]
    props_by_note = {}
    if note_ids:
        ph = ",".join("?" * len(note_ids))
        for r in query(f"SELECT id, note_id, name, value FROM note_properties WHERE note_id IN ({ph})", note_ids):
            props_by_note.setdefault(r["note_id"], []).append(
                {"id": r["id"], "name": r["name"], "value": r["value"]})
    for n in notes:
        n["properties"] = props_by_note.get(n["id"], [])
    return jsonify(notes)


@app.route("/api/note/<int:nid>/property", methods=["POST"])
def api_add_note_property(nid):
    """Add a custom field (key:value) to a moment. The user invents the field."""
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    value = (d.get("value") or "").strip()
    if not name:
        return jsonify({"error": "field name required"}), 400
    if not query_one("SELECT 1 FROM listening_notes WHERE id=?", (nid,)):
        return jsonify({"error": "note not found"}), 404
    execute("INSERT INTO note_properties (note_id, name, value) VALUES (?,?,?)", (nid, name, value or None))
    row = query_one("SELECT id, name, value FROM note_properties WHERE note_id=? ORDER BY id DESC LIMIT 1", (nid,))
    return jsonify({"ok": True, **dict(row)})

@app.route("/api/note/property/<int:pid>", methods=["DELETE"])
def api_delete_note_property(pid):
    execute("DELETE FROM note_properties WHERE id=?", (pid,))
    return jsonify({"ok": True})

@app.route("/api/note-property-names")
def api_note_property_names():
    """Distinct field names already in use — for autocomplete / the field filter."""
    rows = query("SELECT DISTINCT name FROM note_properties ORDER BY name COLLATE NOCASE")
    return jsonify([r["name"] for r in rows])


# =============================================================
# DEEP-LISTENING RESEARCH: threads (lines of enquiry) + note links
# Moments (listening_notes) are the atoms; threads are an ordered line through
# them (the Thread/Timeline lens); note_links are free connections (Mind-map).
# =============================================================

@app.route("/api/threads")
def api_threads():
    rows = query("""SELECT th.id, th.name, th.colour, th.description, th.created_at,
                    (SELECT COUNT(*) FROM thread_moments tm WHERE tm.thread_id = th.id) AS moment_count
                    FROM threads th ORDER BY th.name COLLATE NOCASE""")
    return jsonify([dict(r) for r in rows])

@app.route("/api/threads", methods=["POST"])
def api_create_thread():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    execute("INSERT INTO threads (name, colour, description) VALUES (?,?,?)",
            (name, (d.get("colour") or "").strip() or None, (d.get("description") or "").strip() or None))
    row = query_one("SELECT id, name, colour FROM threads ORDER BY id DESC LIMIT 1")
    return jsonify({"ok": True, **dict(row)})

@app.route("/api/thread/<int:tid>", methods=["PATCH"])
def api_update_thread(tid):
    d = request.get_json() or {}
    fields, params = [], []
    for k in ("name", "colour", "description"):
        if k in d:
            fields.append(f"{k}=?"); params.append((d.get(k) or "").strip() or None)
    if "detail_text" in d:
        fields.append("detail_text=?"); params.append((d.get("detail_text") or "").rstrip() or None)
    if fields:
        params.append(tid)
        execute(f"UPDATE threads SET {','.join(fields)}, updated_at=datetime('now') WHERE id=?", params)
    return jsonify({"ok": True})

@app.route("/api/thread/<int:tid>", methods=["DELETE"])
def api_delete_thread(tid):
    execute("DELETE FROM thread_moments WHERE thread_id=?", (tid,))
    execute("DELETE FROM threads WHERE id=?", (tid,))
    return jsonify({"ok": True})

@app.route("/api/thread/<int:tid>/moments", methods=["POST"])
def api_thread_add_moment(tid):
    d = request.get_json() or {}
    note_id = d.get("note_id")
    if not isinstance(note_id, int):
        return jsonify({"error": "note_id (int) required"}), 400
    nextpos = (query_one("SELECT COALESCE(MAX(position),0)+1 AS p FROM thread_moments WHERE thread_id=?", (tid,)) or {}).get("p", 1)
    execute("INSERT OR IGNORE INTO thread_moments (thread_id, note_id, position) VALUES (?,?,?)",
            (tid, note_id, nextpos))
    return jsonify({"ok": True})

@app.route("/api/thread/<int:tid>/moments/<int:note_id>", methods=["DELETE"])
def api_thread_remove_moment(tid, note_id):
    execute("DELETE FROM thread_moments WHERE thread_id=? AND note_id=?", (tid, note_id))
    return jsonify({"ok": True})

@app.route("/api/thread/<int:tid>/reorder", methods=["POST"])
def api_thread_reorder(tid):
    d = request.get_json() or {}
    ids = d.get("note_ids") or []
    for i, nid in enumerate(ids):
        execute("UPDATE thread_moments SET position=? WHERE thread_id=? AND note_id=?", (i, tid, nid))
    return jsonify({"ok": True})

@app.route("/api/thread/<int:tid>")
def api_thread(tid):
    """A thread's moments in order — each playable, with the musical context
    (year, key, place) so it can be shown as a sequence, a timeline, or a map."""
    th = query_one("SELECT id, name, colour, description, detail_text FROM threads WHERE id=?", (tid,))
    if not th:
        return jsonify({"error": "not found"}), 404
    moments = query("""
        SELECT ln.id, ln.track_id, ln.timestamp_sec, ln.note_text, ln.note_category,
               ln.is_voice, ln.kind, ln.ref_url, ln.ref_title, ln.ref_source, ln.ref_image_url,
               tm.position,
               t.title, t.artist_name, t.year,
               f.id AS file_id, f.duration_sec, f.bpm, f.key_note, f.key_scale,
               (SELECT GROUP_CONCAT(p.name, ', ') FROM place_tracks pt JOIN places p ON p.id=pt.place_id
                  WHERE pt.track_id = ln.track_id) AS places
        FROM thread_moments tm
        JOIN listening_notes ln ON ln.id = tm.note_id
        LEFT JOIN tracks t ON t.id = ln.track_id
        LEFT JOIN files f ON f.track_id = ln.track_id AND f.is_primary=1 AND f.is_deleted=0
        WHERE tm.thread_id=?
        ORDER BY tm.position, t.year, ln.id
    """, (tid,))
    return jsonify({"thread": dict(th), "moments": [dict(m) for m in moments]})

# =============================================================
# SET RECORDINGS — recorded/uploaded takes of a set. Many versions
# per set; linkable to a DAW project; optionally added to the Music
# Room as a clearly-marked record (filtered out by default).
# =============================================================

def _set_rec_dir(sid):
    d = os.path.join(RECORDINGS_DIR, "sets", str(sid))
    os.makedirs(d, exist_ok=True)
    return d


@app.route("/api/set/<int:sid>/recordings")
def api_set_recordings(sid):
    rows = query("SELECT * FROM set_recordings WHERE set_id=? ORDER BY recorded_at DESC", (sid,))
    return jsonify([dict(r) for r in rows])


@app.route("/api/set/<int:sid>/recordings", methods=["POST"])
def api_add_set_recording(sid):
    """Multipart upload: `audio` (a recorded blob or a dropped/picked file) +
    optional `name`. Recorded webm/ogg is converted to wav when ffmpeg exists."""
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "audio file required"}), 400
    name = (request.form.get("name") or "").strip() or None
    base = f"take_{int(_time.time())}"
    ext = os.path.splitext(f.filename or "")[1].lower() or ".webm"
    raw_path = os.path.join(_set_rec_dir(sid), base + ext)
    f.save(raw_path)
    final_path = raw_path
    if ext in (".webm", ".ogg", ".opus"):
        from mr_ffmpeg import ffmpeg_path
        ffmpeg = ffmpeg_path()
        if ffmpeg:
            wav_path = os.path.join(_set_rec_dir(sid), base + ".wav")
            try:
                subprocess.run([ffmpeg, "-y", "-i", raw_path, wav_path],
                               capture_output=True, timeout=300, check=True)
                os.remove(raw_path)
                final_path = wav_path
            except Exception:
                final_path = raw_path        # keep the original if conversion fails
    rid = execute("INSERT INTO set_recordings (set_id, name, file_path) VALUES (?,?,?)",
                  (sid, name, final_path))
    return jsonify(dict(query_one("SELECT * FROM set_recordings WHERE id=?", (rid,)))), 201


@app.route("/api/set/recording/<int:rid>/audio")
def api_set_recording_audio(rid):
    r = query_one("SELECT file_path FROM set_recordings WHERE id=?", (rid,))
    if not r or not os.path.isfile(r["file_path"]):
        abort(404)
    return send_file(r["file_path"])


@app.route("/api/set/recording/<int:rid>", methods=["PATCH"])
def api_update_set_recording(rid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "name" in d:
        ups.append("name=?"); vals.append((d.get("name") or "").strip() or None)
    if "daw_project_path" in d:
        # link, don't move/parse — same principle as own-productions
        ups.append("daw_project_path=?"); vals.append((d.get("daw_project_path") or "").strip() or None)
    if not ups:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(rid)
    execute(f"UPDATE set_recordings SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/set/recording/<int:rid>", methods=["DELETE"])
def api_delete_set_recording(rid):
    r = query_one("SELECT * FROM set_recordings WHERE id=?", (rid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    if r["room_track_id"]:
        execute("UPDATE tracks SET is_deleted=1 WHERE id=?", (r["room_track_id"],))
    try:
        from trash_util import move_to_trash
        if os.path.isfile(r["file_path"]):
            move_to_trash(r["file_path"])     # never hard-delete audio
    except Exception:
        pass
    execute("DELETE FROM set_recordings WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.route("/api/set/recording/<int:rid>/to-room", methods=["POST"])
def api_set_recording_to_room(rid):
    """Add this take to the Music Room as a clearly-marked record."""
    r = query_one("""SELECT sr.*, s.name AS set_name FROM set_recordings sr
                     JOIN sets s ON s.id = sr.set_id WHERE sr.id=?""", (rid,))
    if not r:
        return jsonify({"error": "not found"}), 404
    if r["room_track_id"]:
        return jsonify({"ok": True, "track_id": r["room_track_id"]})
    title = r["set_name"] + (f" — {r['name']}" if r["name"] else "")
    tid = execute("""INSERT INTO tracks (title, artist_name, source_type, is_set_recording)
                     VALUES (?,?,?,1)""", (title, "my set", "set_recording"))
    execute("""INSERT INTO files (track_id, file_path, file_format, is_primary)
               VALUES (?,?,?,1)""", (tid, r["file_path"],
                                     os.path.splitext(r["file_path"])[1].lstrip(".") or "wav"))
    well = query_one("SELECT id FROM places WHERE is_well=1")
    if well:
        execute("INSERT OR IGNORE INTO place_tracks (place_id, track_id) VALUES (?,?)", (well["id"], tid))
    execute("UPDATE set_recordings SET room_track_id=? WHERE id=?", (tid, rid))
    return jsonify({"ok": True, "track_id": tid})


@app.route("/api/set/recording/<int:rid>/from-room", methods=["POST"])
def api_set_recording_from_room(rid):
    r = query_one("SELECT room_track_id FROM set_recordings WHERE id=?", (rid,))
    if r and r["room_track_id"]:
        execute("UPDATE tracks SET is_deleted=1 WHERE id=?", (r["room_track_id"],))
        execute("UPDATE set_recordings SET room_track_id=NULL WHERE id=?", (rid,))
    return jsonify({"ok": True})


# SET INTERLUDES — short audio BETWEEN a set's tracks (mic-recorded or a
# dropped file): the radio DJ talking between records.

def _save_set_audio(sid, f, subdir):
    """Save an uploaded/recorded blob under recordings/sets/<sid>/<subdir>,
    converting recorded webm/ogg to wav when ffmpeg is available."""
    base = f"{subdir}_{int(_time.time())}"
    ext = os.path.splitext(f.filename or "")[1].lower() or ".webm"
    d = os.path.join(_set_rec_dir(sid), subdir)
    os.makedirs(d, exist_ok=True)
    raw_path = os.path.join(d, base + ext)
    f.save(raw_path)
    if ext in (".webm", ".ogg", ".opus"):
        from mr_ffmpeg import ffmpeg_path
        ffmpeg = ffmpeg_path()
        if ffmpeg:
            wav_path = os.path.join(d, base + ".wav")
            try:
                subprocess.run([ffmpeg, "-y", "-i", raw_path, wav_path],
                               capture_output=True, timeout=300, check=True)
                os.remove(raw_path)
                return wav_path
            except Exception:
                pass
    return raw_path


@app.route("/api/set/<int:sid>/interludes")
def api_set_interludes(sid):
    rows = query("SELECT * FROM set_interludes WHERE set_id=? ORDER BY after_position, id", (sid,))
    return jsonify([dict(r) for r in rows])


@app.route("/api/set/<int:sid>/interludes", methods=["POST"])
def api_add_set_interlude(sid):
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "audio file required"}), 400
    pos = request.form.get("after_position", type=int) or 0
    name = (request.form.get("name") or "").strip() or None
    path = _save_set_audio(sid, f, "interlude")
    iid = execute("INSERT INTO set_interludes (set_id, after_position, name, file_path) VALUES (?,?,?,?)",
                  (sid, pos, name, path))
    return jsonify(dict(query_one("SELECT * FROM set_interludes WHERE id=?", (iid,)))), 201


@app.route("/api/set/interlude/<int:iid>/audio")
def api_set_interlude_audio(iid):
    r = query_one("SELECT file_path FROM set_interludes WHERE id=?", (iid,))
    if not r or not os.path.isfile(r["file_path"]):
        abort(404)
    return send_file(r["file_path"])


@app.route("/api/set/interlude/<int:iid>", methods=["PATCH"])
def api_update_set_interlude(iid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "name" in d:
        ups.append("name=?"); vals.append((d.get("name") or "").strip() or None)
    if "after_position" in d:
        try:
            ups.append("after_position=?"); vals.append(int(d["after_position"]))
        except (TypeError, ValueError):
            return jsonify({"error": "after_position must be an integer"}), 400
    if not ups:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(iid)
    execute(f"UPDATE set_interludes SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/set/interlude/<int:iid>", methods=["DELETE"])
def api_delete_set_interlude(iid):
    r = query_one("SELECT file_path FROM set_interludes WHERE id=?", (iid,))
    if r:
        try:
            from trash_util import move_to_trash
            if os.path.isfile(r["file_path"]):
                move_to_trash(r["file_path"])
        except Exception:
            pass
    execute("DELETE FROM set_interludes WHERE id=?", (iid,))
    return jsonify({"ok": True})


# =============================================================
# LISTENING PROJECTS — containers for moments. A note can live in
# many projects; working inside one scopes the workspace to it.
# =============================================================

@app.route("/api/listen/projects")
def api_listen_projects():
    rows = query("""SELECT p.id, p.name, p.description, p.created_at,
                    (SELECT COUNT(*) FROM project_notes pn WHERE pn.project_id = p.id) AS note_count
                    FROM listening_projects p ORDER BY p.name COLLATE NOCASE""")
    return jsonify([dict(r) for r in rows])


@app.route("/api/listen/projects", methods=["POST"])
def api_create_listen_project():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    pid = execute("INSERT INTO listening_projects (name, description) VALUES (?,?)",
                  (name, (d.get("description") or "").strip() or None))
    return jsonify(dict(query_one("SELECT * FROM listening_projects WHERE id=?", (pid,)))), 201


@app.route("/api/listen/project/<int:pid>", methods=["PATCH"])
def api_update_listen_project(pid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "name" in d:
        name = (d.get("name") or "").strip()
        if not name: return jsonify({"error": "name cannot be empty"}), 400
        ups.append("name=?"); vals.append(name)
    if "description" in d:
        ups.append("description=?"); vals.append((d.get("description") or "").strip() or None)
    if not ups:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(pid)
    execute(f"UPDATE listening_projects SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/listen/project/<int:pid>", methods=["DELETE"])
def api_delete_listen_project(pid):
    """Removes the project + its memberships. The notes themselves stay."""
    execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
    execute("DELETE FROM listening_projects WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.route("/api/listen/project/<int:pid>/notes", methods=["POST"])
def api_project_add_notes(pid):
    d = request.get_json() or {}
    nids = d.get("note_ids") or ([d["note_id"]] if d.get("note_id") else [])
    if not nids:
        return jsonify({"error": "note_ids required"}), 400
    for nid in nids:
        execute("INSERT OR IGNORE INTO project_notes (project_id, note_id) VALUES (?,?)", (pid, nid))
    return jsonify({"ok": True, "added": len(nids)})


@app.route("/api/listen/project/<int:pid>/notes/<int:nid>", methods=["DELETE"])
def api_project_remove_note(pid, nid):
    execute("DELETE FROM project_notes WHERE project_id=? AND note_id=?", (pid, nid))
    return jsonify({"ok": True})


@app.route("/api/thread/<int:tid>/to-set", methods=["POST"])
def api_thread_to_set(tid):
    """Shape → output (VISION §4.4): turn a thread (a researched line of
    enquiry) into a real Set — its moments' tracks, in thread order, deduped.
    The thread itself is untouched; the set is a new, ordinary set."""
    th = query_one("SELECT id, name, description FROM threads WHERE id=?", (tid,))
    if not th:
        return jsonify({"error": "thread not found"}), 404
    rows = query("""SELECT ln.track_id FROM thread_moments tm
                    JOIN listening_notes ln ON ln.id = tm.note_id
                    WHERE tm.thread_id=? ORDER BY tm.position, ln.id""", (tid,))
    seen, tids = set(), []
    for r in rows:
        # reference notes (saved articles) carry no track — they shape the
        # research, not the setlist
        if r["track_id"] and r["track_id"] not in seen:
            seen.add(r["track_id"]); tids.append(r["track_id"])
    if not tids:
        return jsonify({"error": "thread has no moments"}), 400
    research = f"From the thread “{th['name']}”." + (f"\n{th['description']}" if th["description"] else "")
    sid = execute("INSERT INTO sets (name,research,set_type) VALUES (?,?,?)",
                  (th["name"], research, "charted"))
    for i, t in enumerate(tids):
        execute("INSERT INTO set_tracks (set_id,track_id,position) VALUES (?,?,?)", (sid, t, i + 1))
    return jsonify({"ok": True, "set_id": sid, "tracks": len(tids)}), 201


# ── Compose — the research OUTPUT (VISION §4.4, research/04) ──────────────────
# A composition is a document of ordered BLOCKS held as JSON. A block is one of:
#   {t:'heading', level:1|2, text}   {t:'text', text}   {t:'divider'}
#   {t:'moment', note_id}   {t:'reference', note_id}
# moment/reference blocks store only the note_id and are RESOLVED live on read,
# so the document always reflects the current substrate (and tolerates a deleted
# moment). capture→organise→PRODUCE: a notebook/thread becomes a script/essay.

def _resolve_blocks(blocks):
    """Attach a `resolved` note object to every moment/reference block so the
    editor + preview can render without N round-trips. None if the note is gone."""
    if not isinstance(blocks, list):
        return []
    ids = [b.get("note_id") for b in blocks
           if isinstance(b, dict) and b.get("t") in ("moment", "reference") and b.get("note_id")]
    by_id = {}
    if ids:
        ph = ",".join("?" * len(ids))
        rows = query(f"""
            SELECT ln.id, ln.track_id, ln.kind, ln.timestamp_sec, ln.note_text, ln.detail_text,
                   ln.note_category, ln.is_voice, ln.ref_url, ln.ref_title,
                   ln.ref_source, ln.ref_image_url, ln.ref_file_path,
                   t.title, t.artist_name, t.album, t.year, t.artwork_path, t.colour_code
            FROM listening_notes ln
            LEFT JOIN tracks t ON t.id = ln.track_id
            WHERE ln.id IN ({ph})
        """, ids)
        by_id = {r["id"]: r for r in rows}
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        b = dict(b)
        if b.get("t") in ("moment", "reference"):
            b["resolved"] = by_id.get(b.get("note_id"))
        out.append(b)
    return out


@app.route("/api/listen/compositions")
def api_list_compositions():
    rows = query("SELECT id, title, kind, subtitle, updated_at, blocks FROM compositions "
                 "ORDER BY updated_at DESC, id DESC")
    out = []
    for r in rows:
        try:    n = len(_json.loads(r.get("blocks") or "[]"))
        except Exception: n = 0
        out.append({"id": r["id"], "title": r["title"], "kind": r["kind"],
                    "subtitle": r["subtitle"], "updated_at": r["updated_at"], "block_count": n})
    return jsonify(out)


@app.route("/api/listen/compositions", methods=["POST"])
def api_create_composition():
    d = request.get_json() or {}
    title = (d.get("title") or "Untitled").strip() or "Untitled"
    kind  = (d.get("kind") or "piece").strip() or "piece"
    cid = execute("INSERT INTO compositions (title, kind, subtitle, blocks) VALUES (?,?,?,?)",
                  (title, kind, (d.get("subtitle") or "").strip() or None, "[]"))
    return jsonify(dict(query_one("SELECT * FROM compositions WHERE id=?", (cid,)))), 201


@app.route("/api/listen/composition/<int:cid>")
def api_get_composition(cid):
    c = query_one("SELECT * FROM compositions WHERE id=?", (cid,))
    if not c:
        return jsonify({"error": "not found"}), 404
    c = dict(c)
    try:    raw = _json.loads(c.get("blocks") or "[]")
    except Exception: raw = []
    c["blocks"] = _resolve_blocks(raw)
    return jsonify(c)


@app.route("/api/listen/composition/<int:cid>", methods=["PATCH"])
def api_update_composition(cid):
    d = request.get_json() or {}
    ups, vals = [], []
    if "title" in d:
        title = (d.get("title") or "").strip()
        if not title: return jsonify({"error": "title cannot be empty"}), 400
        ups.append("title=?"); vals.append(title)
    if "kind" in d:
        ups.append("kind=?"); vals.append((d.get("kind") or "piece").strip() or "piece")
    if "subtitle" in d:
        ups.append("subtitle=?"); vals.append((d.get("subtitle") or "").strip() or None)
    if "blocks" in d:
        # persist only the STRUCTURE (t/note_id/text/level) — never the resolved data
        clean = []
        for b in (d.get("blocks") or []):
            if not isinstance(b, dict) or not b.get("t"): continue
            nb = {"t": b["t"]}
            for k in ("note_id", "text", "level"):
                if k in b: nb[k] = b[k]
            clean.append(nb)
        ups.append("blocks=?"); vals.append(_json.dumps(clean))
    if not ups:
        return jsonify({"error": "no fields"}), 400
    ups.append("updated_at=datetime('now')")
    vals.append(cid)
    execute(f"UPDATE compositions SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/listen/composition/<int:cid>", methods=["DELETE"])
def api_delete_composition(cid):
    execute("DELETE FROM compositions WHERE id=?", (cid,))
    return jsonify({"ok": True})


@app.route("/api/note-links", methods=["GET", "POST"])
def api_note_links():
    if request.method == "POST":
        d = request.get_json() or {}
        a, b = d.get("from_note_id"), d.get("to_note_id")
        if not (isinstance(a, int) and isinstance(b, int)) or a == b:
            return jsonify({"error": "two distinct note ids required"}), 400
        execute("INSERT OR IGNORE INTO note_links (from_note_id, to_note_id, label) VALUES (?,?,?)",
                (a, b, (d.get("label") or "").strip() or None))
        return jsonify({"ok": True})
    note_id = request.args.get("note_id", type=int)
    if note_id:
        rows = query("SELECT * FROM note_links WHERE from_note_id=? OR to_note_id=?", (note_id, note_id))
    else:
        rows = query("SELECT * FROM note_links")
    return jsonify([dict(r) for r in rows])

@app.route("/api/note-link/<int:lid>", methods=["PATCH"])
def api_update_note_link(lid):
    """Edit the note on a link — *why* the two moments are connected."""
    d = request.get_json() or {}
    label = (d.get("label") or "").strip() or None
    execute("UPDATE note_links SET label=? WHERE id=?", (label, lid))
    return jsonify({"ok": True})

@app.route("/api/note-link/<int:lid>", methods=["DELETE"])
def api_delete_note_link(lid):
    execute("DELETE FROM note_links WHERE id=?", (lid,))
    return jsonify({"ok": True})


@app.route("/api/listen/categories", methods=["GET", "POST"])
def api_listen_categories():
    # POST {shortcuts?: {key: name}, presets?: [{name, shortcuts}]} → persist the
    # user's 1–0 tag mapping and/or their named presets (the Éisteacht editor).
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        if isinstance(d.get("shortcuts"), dict):
            set_setting("listening_shortcuts", _json.dumps(d["shortcuts"]))
        if isinstance(d.get("presets"), list):
            set_setting("listening_presets", _json.dumps(d["presets"]))
        return jsonify({"ok": True})
    shortcuts_raw = get_setting("listening_shortcuts", "{}")
    try:
        shortcuts = _json.loads(shortcuts_raw)
    except Exception:
        shortcuts = {}
    try:
        presets = _json.loads(get_setting("listening_presets", "[]"))
    except Exception:
        presets = []
    used = [r["note_category"] for r in query(
        "SELECT DISTINCT note_category FROM listening_notes WHERE note_category != '' ORDER BY note_category"
    )]
    return jsonify({"shortcuts": shortcuts, "presets": presets, "used_categories": used})


@app.route("/api/listen/transcribe", methods=["POST"])
def api_listen_transcribe():
    """Transcribe audio blob with local Whisper. Runs synchronously — may take 5-15s."""
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "no audio uploaded"}), 400
    import tempfile
    suffix = ".mp4"  # WebKit MediaRecorder uses mp4 on macOS
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name
    try:
        model  = _get_whisper_model()
        result = model.transcribe(tmp_path, fp16=False, language="en")
        return jsonify({"transcript": (result.get("text") or "").strip()})
    except ImportError:
        return jsonify({"error": "Whisper not installed — run: pip install openai-whisper"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# =============================================================
# API — TRACKS
# =============================================================

@app.route("/api/tracks")
def api_tracks():
    search = request.args.get("search", "").strip()
    sort   = request.args.get("sort", "artist_name")
    order  = request.args.get("order", "asc")
    limit  = int(request.args.get("limit", 200))
    offset = int(request.args.get("offset", 0))
    nr     = request.args.get("needs_review", "")
    up     = request.args.get("unplaced", "")
    fmt    = request.args.get("format", "")

    if sort not in {"artist_name","title","year","date_added","last_played","album"}: sort = "artist_name"
    if order not in ("asc","desc"): order = "asc"

    cond, params = ["t.is_deleted = 0"], []
    if search:
        cond.append("(LOWER(t.title) LIKE ? OR LOWER(t.artist_name) LIKE ?)")
        params.extend([f"%{search.lower()}%"]*2)
    if nr == "1": cond.append("t.needs_review = 1")
    if up == "1": cond.append("t.is_placed = 0")
    if fmt and fmt != "all":
        cond.append("EXISTS (SELECT 1 FROM files f WHERE f.track_id=t.id AND f.file_format=? AND f.is_deleted=0)")
        params.append(fmt)

    where = "WHERE " + " AND ".join(cond)
    tracks = query(f"""
        SELECT t.id, t.title, t.artist_name, t.album, t.year, t.colour_code,
               t.needs_review, t.is_placed, t.has_image, t.date_added,
               t.last_played, t.vinyl_owned, t.source_type, t.discovered_via,
               f.id as file_id, f.file_path, f.file_format, f.file_size_mb, f.duration_sec,
               f.bpm, f.bpm_verified, f.key_note, f.key_scale, f.key_verified,
               f.has_analysis, f.analysis_status
        FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        {where} ORDER BY t.{sort} {order} LIMIT ? OFFSET ?
    """, params + [limit, offset])
    total = query_one(f"SELECT COUNT(*) as n FROM tracks t {where}", params)["n"]
    return jsonify({"tracks": tracks, "total": total, "limit": limit, "offset": offset})


@app.route("/api/track/<int:tid>")
def api_track(tid):
    t = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t: return jsonify({"error": "Track not found"}), 404
    t = dict(t)
    t["art_v"] = _artwork_version(t.get("artwork_path"))   # cover cache token (busts the <img> on a cover change)
    # a Green Room borrow: the panel shows its state + buy / offload / return
    borrowed = None
    if t.get("gr_borrow_id"):
        b = green_room.borrow_of_track(tid)
        if b:
            borrowed = {"local_id": b["id"], "on_disk": b["on_disk"],
                        "price_cents": b.get("price_cents"), "server_track_id": b["server_track_id"],
                        "borrowed_at": b.get("borrowed_at"),
                        # an exclusive record (only in the Music Room); kept = bought, held for good
                        "exclusive": bool(b.get("exclusive")), "kept": bool(b.get("kept"))}
    return jsonify({
        "track":  t,
        "borrowed": borrowed,
        "files":  query("SELECT * FROM files WHERE track_id=? AND is_deleted=0 ORDER BY is_primary DESC, file_size_mb DESC", (tid,)),
        "notes":  query("SELECT * FROM notes WHERE track_id=? ORDER BY created_at ASC", (tid,)),
        # Éisteacht's deep-listen moments — a separate store (listening_notes),
        # shown read-only on the panel (they're edited in deep listening).
        "deep_notes": [dict(r, audio_timestamp=_secs_to_ts(r["timestamp_sec"]))
                       for r in query(
                           "SELECT id, note_text, note_category, timestamp_sec, created_at "
                           "FROM listening_notes WHERE track_id=? AND kind='moment' "
                           "AND note_text != '' ORDER BY timestamp_sec ASC", (tid,))],
        "tags":   query("SELECT tg.id,tg.name,tg.colour FROM tags tg JOIN track_tags tt ON tt.tag_id=tg.id WHERE tt.track_id=? ORDER BY tg.name", (tid,)),
        "genres": query("SELECT g.id,g.name FROM genres g JOIN track_genres tg ON tg.genre_id=g.id WHERE tg.track_id=? ORDER BY g.name", (tid,)),
        "places": query("SELECT p.id,p.name,p.colour_primary,p.is_well,pt.moment,pt.added_at FROM places p JOIN place_tracks pt ON pt.place_id=p.id WHERE pt.track_id=? ORDER BY p.name", (tid,)),
        # the sets this track sits in — shown on the panel beside the places
        # (a track can be in a set more than once; `times` says so)
        "sets": query("SELECT s.id, s.name, s.set_type, COUNT(st.id) AS times FROM sets s JOIN set_tracks st ON st.set_id=s.id WHERE st.track_id=? GROUP BY s.id ORDER BY s.name", (tid,)),
        # If this track fulfilled any wishlist rows, surface them so the
        # panel can show "supported on YYYY-MM-DD" and a link back.
        "fulfilled_wishlist": query(
            "SELECT id, url, source, fulfilled_at FROM wishlist WHERE fulfilled_track_id=?",
            (tid,)
        ),
        # external analysis layers (rekordbox, later Mixed In Key…) — the
        # Music Room's own analysis stays on the primary file (files.bpm/key)
        "analysis": query(
            "SELECT source, bpm, key_note, key_scale, analysed_at FROM track_analysis WHERE track_id=?",
            (tid,)),
    })


# ── Duplicate-finder + merge (the Deisigh / Mála fix tool) ──────────
# Finds candidates via the same loose normalisation we use everywhere
# else for cross-source matching. Merge reparents every reference to
# secondary_id over to primary_id in one transaction; secondary becomes
# is_deleted=1 (preserved for history, hidden from queries).

DSP_STATE_PRIORITY = {
    "untested": 0, "error": 1, "skipped": 2,
    "probable": 3, "not_on_dsp": 4, "on_dsp": 5,
}


@app.route("/api/track/<int:tid>/duplicates")
def api_track_duplicates(tid):
    """Return tracks that look like duplicates of <tid> by normalised
    artist+title match. The first track is the subject; matches are
    everything else."""
    t = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "track not found"}), 404
    artist = t.get("artist_name") or ""
    title  = t.get("title") or ""
    if not artist or not title:
        return jsonify({
            "subject": {"id": tid, "title": title, "artist": artist},
            "items": [],
            "reason": "Need both artist and title to find duplicates."
        })
    na, nt = _norm_match(artist), _norm_match(title)
    candidates = query(
        "SELECT t.id, t.title, t.artist_name, t.album, t.year, t.label, "
        "       t.artwork_path, t.spotify_id, t.dsp_check_state, "
        "       f.file_format, f.duration_sec, f.bpm "
        "FROM tracks t "
        "LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0 "
        "WHERE t.id != ? AND t.is_deleted = 0 "
        "  AND t.title IS NOT NULL AND t.artist_name IS NOT NULL",
        (tid,)
    )
    matches = []
    for c in candidates:
        ca, ct = _norm_match(c["artist_name"]), _norm_match(c["title"])
        if not ca or not ct: continue
        if ca == na and ct == nt:
            score, reason = 100, "exact match (normalised)"
        elif ca == na and (nt in ct or ct in nt):
            score, reason = 70, "same artist, title overlap"
        else:
            continue
        c["match_score"] = score
        c["match_reason"] = reason
        c["file_count"]  = query_one("SELECT COUNT(*) AS n FROM files WHERE track_id=? AND is_deleted=0", (c["id"],))["n"]
        c["place_count"] = query_one("SELECT COUNT(*) AS n FROM place_tracks WHERE track_id=?", (c["id"],))["n"]
        c["note_count"]  = query_one("SELECT COUNT(*) AS n FROM notes WHERE track_id=?", (c["id"],))["n"]
        matches.append(c)
    matches.sort(key=lambda m: (-m["match_score"], -m["file_count"]))
    return jsonify({
        "subject": {"id": tid, "title": title, "artist": artist,
                    "file_count":  query_one("SELECT COUNT(*) AS n FROM files WHERE track_id=? AND is_deleted=0", (tid,))["n"],
                    "place_count": query_one("SELECT COUNT(*) AS n FROM place_tracks WHERE track_id=?", (tid,))["n"],
                    "note_count":  query_one("SELECT COUNT(*) AS n FROM notes WHERE track_id=?", (tid,))["n"]},
        "items": matches,
    })


@app.route("/api/tracks/merge", methods=["POST"])
def api_merge_tracks():
    """Merge secondary INTO primary. Every relationship gets re-parented
    to primary_id; secondary is then marked is_deleted=1.

    Conflict policy:
      - track_tags / track_genres / place_tracks / bosca → INSERT OR IGNORE
        (keep primary's existing memberships, just add secondary's extras)
      - track_enrichment → primary keeps its enrichment if any; else
        secondary's is reparented
      - tracks.artwork_path / spotify_id → backfilled onto primary if it
        lacked them
      - tracks.dsp_check_state → upgraded to the more informative state
      - tracks.vinyl_owned → ORed
    """
    d = request.get_json() or {}
    primary_id   = d.get("primary_id")
    secondary_id = d.get("secondary_id")
    if not isinstance(primary_id, int) or not isinstance(secondary_id, int):
        return jsonify({"error": "primary_id and secondary_id (int) required"}), 400
    if primary_id == secondary_id:
        return jsonify({"error": "cannot merge a track with itself"}), 400
    primary   = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (primary_id,))
    secondary = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (secondary_id,))
    if not primary or not secondary:
        return jsonify({"error": "one or both tracks not found / already deleted"}), 404

    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("BEGIN")
        # Files — secondary's files become alternate versions of primary.
        # If primary already has a primary file, incoming primary-flagged
        # files get demoted so we don't end up with two primaries.
        primary_has_primary_file = cur.execute(
            "SELECT 1 FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0", (primary_id,)
        ).fetchone() is not None
        if primary_has_primary_file:
            cur.execute(
                "UPDATE files SET track_id=?, is_primary=0, updated_at=datetime('now') WHERE track_id=?",
                (primary_id, secondary_id)
            )
        else:
            cur.execute(
                "UPDATE files SET track_id=?, updated_at=datetime('now') WHERE track_id=?",
                (primary_id, secondary_id)
            )

        # Notes (no UNIQUE)
        cur.execute("UPDATE notes SET track_id=? WHERE track_id=?", (primary_id, secondary_id))

        # Listening
        cur.execute("UPDATE listening_sessions SET track_id=? WHERE track_id=?", (primary_id, secondary_id))
        cur.execute("UPDATE listening_notes    SET track_id=? WHERE track_id=?", (primary_id, secondary_id))

        # Tags — composite PK (track_id, tag_id)
        cur.execute(
            "INSERT OR IGNORE INTO track_tags (track_id, tag_id) "
            "SELECT ?, tag_id FROM track_tags WHERE track_id=?",
            (primary_id, secondary_id)
        )
        cur.execute("DELETE FROM track_tags WHERE track_id=?", (secondary_id,))

        # Genres — composite PK (track_id, genre_id)
        cur.execute(
            "INSERT OR IGNORE INTO track_genres (track_id, genre_id) "
            "SELECT ?, genre_id FROM track_genres WHERE track_id=?",
            (primary_id, secondary_id)
        )
        cur.execute("DELETE FROM track_genres WHERE track_id=?", (secondary_id,))

        # Places — composite PK; INSERT OR IGNORE keeps primary's moment/order metadata.
        cur.execute(
            "INSERT OR IGNORE INTO place_tracks (place_id, track_id, moment, moment_order, added_at) "
            "SELECT place_id, ?, moment, moment_order, added_at FROM place_tracks WHERE track_id=?",
            (primary_id, secondary_id)
        )
        cur.execute("DELETE FROM place_tracks WHERE track_id=?", (secondary_id,))

        # Bosca — UNIQUE on track_id
        cur.execute(
            "INSERT OR IGNORE INTO bosca (track_id, position, note, added_at) "
            "SELECT ?, position, note, added_at FROM bosca WHERE track_id=?",
            (primary_id, secondary_id)
        )
        cur.execute("DELETE FROM bosca WHERE track_id=?", (secondary_id,))

        # Enrichment — UNIQUE on track_id
        primary_has_enrichment = cur.execute(
            "SELECT 1 FROM track_enrichment WHERE track_id=?", (primary_id,)
        ).fetchone() is not None
        if primary_has_enrichment:
            cur.execute("DELETE FROM track_enrichment WHERE track_id=?", (secondary_id,))
        else:
            cur.execute("UPDATE track_enrichment SET track_id=? WHERE track_id=?", (primary_id, secondary_id))

        # Set tracks / overlays / quiz history — just re-parent.
        cur.execute("UPDATE set_tracks     SET track_id=? WHERE track_id=?", (primary_id, secondary_id))
        cur.execute("UPDATE track_overlays SET track_id=? WHERE track_id=?", (primary_id, secondary_id))
        cur.execute("UPDATE quiz_questions SET track_id=? WHERE track_id=?", (primary_id, secondary_id))

        # Wishlist back-reference
        cur.execute(
            "UPDATE wishlist SET fulfilled_track_id=? WHERE fulfilled_track_id=?",
            (primary_id, secondary_id)
        )

        # Backfill missing fields onto primary from secondary.
        if not primary.get("artwork_path") and secondary.get("artwork_path"):
            cur.execute(
                "UPDATE tracks SET artwork_path=?, artwork_source=?, artwork_verified=?, "
                "art_colour=COALESCE(art_colour, ?) WHERE id=?",
                (secondary["artwork_path"], secondary.get("artwork_source"),
                 secondary.get("artwork_verified") or 0,
                 secondary.get("art_colour"), primary_id)
            )
        if not primary.get("spotify_id") and secondary.get("spotify_id"):
            cur.execute("UPDATE tracks SET spotify_id=? WHERE id=?",
                        (secondary["spotify_id"], primary_id))
        p_state = primary.get("dsp_check_state") or "untested"
        s_state = secondary.get("dsp_check_state") or "untested"
        if DSP_STATE_PRIORITY.get(s_state, 0) > DSP_STATE_PRIORITY.get(p_state, 0):
            cur.execute(
                "UPDATE tracks SET dsp_check_state=?, dsp_checked_at=? WHERE id=?",
                (s_state, secondary.get("dsp_checked_at"), primary_id)
            )
        if secondary.get("vinyl_owned") and not primary.get("vinyl_owned"):
            cur.execute("UPDATE tracks SET vinyl_owned=1 WHERE id=?", (primary_id,))

        # Retire the secondary track.
        cur.execute(
            "UPDATE tracks SET is_deleted=1, deleted_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (secondary_id,)
        )
        # Bump primary so it sorts to recent.
        cur.execute("UPDATE tracks SET updated_at=datetime('now') WHERE id=?", (primary_id,))

        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": f"merge failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "primary_id":   primary_id,
        "secondary_id": secondary_id,
    })


def _merge_pair_sql(cur, primary, secondary):
    """Re-parent everything from `secondary` onto `primary` and retire secondary.
    Same operations as /api/tracks/merge; used by the duplicate-track audit so a
    whole batch merges in one transaction. Caller owns BEGIN/commit."""
    pid, sid = primary["id"], secondary["id"]
    has_pf = cur.execute("SELECT 1 FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0", (pid,)).fetchone() is not None
    if has_pf:
        cur.execute("UPDATE files SET track_id=?, is_primary=0, updated_at=datetime('now') WHERE track_id=?", (pid, sid))
    else:
        cur.execute("UPDATE files SET track_id=?, updated_at=datetime('now') WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE notes SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE listening_sessions SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE listening_notes    SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("INSERT OR IGNORE INTO track_tags (track_id, tag_id) SELECT ?, tag_id FROM track_tags WHERE track_id=?", (pid, sid))
    cur.execute("DELETE FROM track_tags WHERE track_id=?", (sid,))
    cur.execute("INSERT OR IGNORE INTO track_genres (track_id, genre_id) SELECT ?, genre_id FROM track_genres WHERE track_id=?", (pid, sid))
    cur.execute("DELETE FROM track_genres WHERE track_id=?", (sid,))
    cur.execute("INSERT OR IGNORE INTO place_tracks (place_id, track_id, moment, moment_order, added_at) "
                "SELECT place_id, ?, moment, moment_order, added_at FROM place_tracks WHERE track_id=?", (pid, sid))
    cur.execute("DELETE FROM place_tracks WHERE track_id=?", (sid,))
    cur.execute("INSERT OR IGNORE INTO bosca (track_id, position, note, added_at) SELECT ?, position, note, added_at FROM bosca WHERE track_id=?", (pid, sid))
    cur.execute("DELETE FROM bosca WHERE track_id=?", (sid,))
    if cur.execute("SELECT 1 FROM track_enrichment WHERE track_id=?", (pid,)).fetchone():
        cur.execute("DELETE FROM track_enrichment WHERE track_id=?", (sid,))
    else:
        cur.execute("UPDATE track_enrichment SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE set_tracks     SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE track_overlays SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE quiz_questions SET track_id=? WHERE track_id=?", (pid, sid))
    cur.execute("UPDATE wishlist SET fulfilled_track_id=? WHERE fulfilled_track_id=?", (pid, sid))
    if not primary.get("artwork_path") and secondary.get("artwork_path"):
        cur.execute("UPDATE tracks SET artwork_path=?, artwork_source=?, artwork_verified=?, "
                    "art_colour=COALESCE(art_colour, ?) WHERE id=?",
                    (secondary["artwork_path"], secondary.get("artwork_source"),
                     secondary.get("artwork_verified") or 0, secondary.get("art_colour"), pid))
    cur.execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now'), updated_at=datetime('now') WHERE id=?", (sid,))
    cur.execute("UPDATE tracks SET updated_at=datetime('now') WHERE id=?", (pid,))


def _find_duplicate_track_groups():
    """High-confidence duplicate-track groups: tracks sharing a byte-identical
    primary file (content_hash), or the same artist+title+~duration. Returns a
    list of {keeper, dups[], reason} with the keeper chosen by: has a playable
    file → most notes/genres/places → lowest id (the original)."""
    import re as _re
    def _norm(s):
        if not s: return ""
        s = s.lower(); s = _re.sub(r"\(.*?\)|\[.*?\]", " ", s)
        s = _re.sub(r"[^\w\s]", " ", s); return _re.sub(r"\s+", " ", s).strip()
    rows = query("""SELECT t.id, t.title, t.artist_name,
                    (SELECT content_hash FROM files WHERE track_id=t.id AND is_deleted=0 AND content_hash IS NOT NULL LIMIT 1) ch,
                    (SELECT duration_sec FROM files WHERE track_id=t.id AND is_primary=1 LIMIT 1) dur,
                    (SELECT file_path FROM files WHERE track_id=t.id AND is_primary=1 LIMIT 1) fp,
                    (SELECT COUNT(*) FROM notes WHERE track_id=t.id) nnotes,
                    (SELECT COUNT(*) FROM track_genres WHERE track_id=t.id) ngenres,
                    (SELECT COUNT(*) FROM place_tracks WHERE track_id=t.id) nplaces
                    FROM tracks t WHERE t.is_deleted=0""")
    info = {r["id"]: r for r in rows}
    groups_by_key = {}
    for r in rows:
        if r["ch"]:
            groups_by_key.setdefault(("hash", r["ch"]), set()).add(r["id"])
        if r["artist_name"] and r["title"]:
            a, ti = _norm(r["artist_name"]), _norm(r["title"])
            if a and ti:
                groups_by_key.setdefault(("name", a, ti, round((r["dur"] or 0) / 2) * 2), set()).add(r["id"])
    # merge overlapping id-sets so a track in two keys lands in one group
    sets = [s for s in groups_by_key.values() if len(s) > 1]
    merged = []
    for s in sets:
        hit = next((m for m in merged if m & s), None)
        if hit: hit |= s
        else: merged.append(set(s))
    out = []
    for ids in merged:
        def score(i):
            r = info[i]
            playable = bool(_resolve_audio_path(r["fp"]))
            return (1 if playable else 0, r["nnotes"], r["ngenres"], r["nplaces"], -i)
        keeper = max(ids, key=score)
        dups = sorted(ids - {keeper})
        def brief(i):
            r = info[i]
            return {"id": i, "title": r["title"], "artist": r["artist_name"],
                    "playable": bool(_resolve_audio_path(r["fp"]))}
        out.append({"keeper": brief(keeper), "dups": [brief(i) for i in dups]})
    out.sort(key=lambda g: ((g["keeper"]["artist"] or "").lower(), (g["keeper"]["title"] or "").lower()))
    return out


@app.route("/api/tracks/duplicate-groups")
def api_duplicate_track_groups():
    groups = _find_duplicate_track_groups()
    return jsonify({"group_count": len(groups),
                    "extra_tracks": sum(len(g["dups"]) for g in groups),
                    "groups": groups})


@app.route("/api/tracks/dedupe-merge", methods=["POST"])
def api_dedupe_merge():
    """Merge every detected duplicate-track group into its keeper, in one
    transaction. Writes a reversible log first (retired tracks are is_deleted=1,
    not gone)."""
    groups = _find_duplicate_track_groups()
    if not groups:
        return jsonify({"ok": True, "merged": 0})
    # reversible record
    try:
        import json as _json
        log = [{"keeper": g["keeper"]["id"], "dups": [d["id"] for d in g["dups"]]} for g in groups]
        with open(os.path.join(DATA_DIR, "dedupe_merge_log.json"), "w") as fh:
            _json.dump({"at": _now_iso(), "merges": log}, fh, indent=1)
    except Exception:
        pass
    db = get_db(); cur = db.cursor(); merged = 0
    try:
        cur.execute("BEGIN")
        for g in groups:
            primary = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (g["keeper"]["id"],))
            if not primary: continue
            for d in g["dups"]:
                secondary = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (d["id"],))
                if secondary:
                    _merge_pair_sql(cur, primary, secondary)
                    merged += 1
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "merged": merged, "groups": len(groups)})


def _load_last_import_journal():
    p = get_setting("last_import_journal", "").strip()
    if not p or not os.path.isfile(p):
        return None, None
    try:
        import json as _json
        with open(p) as fh:
            j = _json.load(fh)
        if j.get("undone"):
            return None, None
        return p, j
    except Exception:
        return None, None


@app.route("/api/import/undo-status")
def api_import_undo_status():
    p, j = _load_last_import_journal()
    if not j:
        return jsonify({"available": False})
    return jsonify({"available": True, "at": j.get("at"), "scan_path": j.get("scan_path"),
                    "count_new": j.get("count_new", len(j.get("new_track_ids", []))),
                    "count_relinked": j.get("count_relinked", len(j.get("relinked", [])))})


@app.route("/api/import/add-to-places", methods=["POST"])
def api_import_add_to_places():
    """Optionally add the LAST import's new tracks to chosen places. Everything is
    always in The Well already; this is a non-destructive convenience that just
    adds place memberships. Reads new_track_ids from the import journal."""
    d = request.get_json() or {}
    place_ids = []
    for p in (d.get("place_ids") or []):
        try: place_ids.append(int(p))
        except (TypeError, ValueError): pass
    if not place_ids:
        return jsonify({"ok": True, "added": 0, "places": 0})
    _, j = _load_last_import_journal()
    new_ids = [int(t) for t in (j.get("new_track_ids", []) if j else []) if t]
    if not new_ids:
        return jsonify({"ok": True, "added": 0, "places": len(place_ids)})
    ph = ",".join("?" * len(new_ids))
    n_places = 0
    for pid in place_ids:
        if not query_one("SELECT 1 FROM places WHERE id=?", (pid,)):
            continue
        execute(f"INSERT OR IGNORE INTO place_tracks (place_id, track_id, added_at) "
                f"SELECT ?, id, datetime('now') FROM tracks WHERE id IN ({ph}) AND is_deleted=0",
                [pid] + new_ids)
        n_places += 1
    return jsonify({"ok": True, "added": len(new_ids), "places": n_places})


@app.route("/api/import/undo", methods=["POST"])
def api_import_undo():
    """Undo the most recent import: restore relinked paths and RETIRE the new
    tracks (is_deleted=1 — recoverable, never hard-deleted). Files are left on
    disk. One-shot: the journal is marked undone."""
    p, j = _load_last_import_journal()
    if not j:
        return jsonify({"ok": False, "error": "Nothing to undo"}), 400
    db = get_db(); cur = db.cursor()
    restored = retired = 0
    try:
        cur.execute("BEGIN")
        for r in j.get("relinked", []):
            if r.get("file_id") and r.get("old_path"):
                cur.execute("UPDATE files SET file_path=?, updated_at=datetime('now') WHERE id=?",
                            (r["old_path"], r["file_id"]))
                restored += 1
        new_ids = [int(t) for t in j.get("new_track_ids", []) if t]
        if new_ids:
            ph = ",".join("?" * len(new_ids))
            cur.execute(f"UPDATE tracks SET is_deleted=1, deleted_at=datetime('now'), updated_at=datetime('now') WHERE id IN ({ph})", new_ids)
            retired = cur.rowcount
            cur.execute(f"UPDATE files SET is_deleted=1, updated_at=datetime('now') WHERE track_id IN ({ph})", new_ids)
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    try:
        import json as _json
        j["undone"] = True
        with open(p, "w") as fh: _json.dump(j, fh, indent=1)
    except Exception:
        pass
    set_setting("last_import_journal", "")
    return jsonify({"ok": True, "retired_tracks": retired, "restored_relinks": restored})


@app.route("/api/track/<int:tid>", methods=["DELETE"])
def api_delete_track(tid):
    t = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t: return jsonify({"error": "not found"}), 404
    if t.get("gr_borrow_id"):
        # deleting a borrowed record = giving it back
        return _gr_guard(lambda: green_room.give_back(t["gr_borrow_id"]))
    files = query("SELECT * FROM files WHERE track_id=? AND is_deleted=0", (tid,))
    deleted, failed = [], []
    for f in files:
        ok, err = move_to_trash(f["file_path"])
        if ok:
            deleted.append(f["file_path"])
            execute("UPDATE files SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (f["id"],))
        else:
            failed.append({"path": f["file_path"], "error": err})
    execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (tid,))
    execute("DELETE FROM place_tracks WHERE track_id=?", (tid,))
    execute("DELETE FROM set_tracks WHERE track_id=?", (tid,))
    return jsonify({"ok": True, "deleted_files": deleted, "errors": failed})


@app.route("/api/file/<int:fid>", methods=["DELETE"])
def api_delete_file(fid):
    f = query_one("SELECT * FROM files WHERE id=? AND is_deleted=0", (fid,))
    if not f: return jsonify({"error": "not found"}), 404
    tid, primary = f["track_id"], f["is_primary"]
    ok, err = move_to_trash(f["file_path"])
    if not ok: return jsonify({"error": err}), 500
    execute("UPDATE files SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (fid,))
    promoted = None
    if primary:
        nxt = query_one("SELECT id FROM files WHERE track_id=? AND is_deleted=0 AND id!=? ORDER BY file_size_mb DESC LIMIT 1", (tid, fid))
        if nxt:
            execute("UPDATE files SET is_primary=1 WHERE id=?", (nxt["id"],))
            promoted = nxt["id"]
        else:
            execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (tid,))
            execute("DELETE FROM place_tracks WHERE track_id=?", (tid,))
    return jsonify({"ok": True, "promoted_primary_id": promoted})


@app.route("/api/track/<int:tid>/remove", methods=["POST"])
def api_remove_track(tid):
    """Remove track from Music Room — moves files to Removed folder, not Trash."""
    t = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t: return jsonify({"error": "not found"}), 404
    if t.get("gr_borrow_id"):
        # removing a borrowed record = giving it back
        return _gr_guard(lambda: green_room.give_back(t["gr_borrow_id"]))
    files = query("SELECT * FROM files WHERE track_id=? AND is_deleted=0", (tid,))
    moved, failed = [], []
    for f in files:
        ok, err = move_to_removed(f["file_path"], tid)
        if ok:
            moved.append(f["file_path"])
            execute("UPDATE files SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (f["id"],))
        else:
            failed.append({"path": f["file_path"], "error": err})
    execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (tid,))
    execute("DELETE FROM place_tracks WHERE track_id=?", (tid,))
    execute("DELETE FROM set_tracks WHERE track_id=?", (tid,))
    return jsonify({"ok": True, "moved_files": moved, "errors": failed})


@app.route("/api/file/<int:fid>/remove", methods=["POST"])
def api_remove_file(fid):
    """Remove one file version from Music Room — moves to Removed folder, not Trash."""
    f = query_one("SELECT * FROM files WHERE id=? AND is_deleted=0", (fid,))
    if not f: return jsonify({"error": "not found"}), 404
    tid, primary = f["track_id"], f["is_primary"]
    ok, err = move_to_removed(f["file_path"], tid)
    if not ok: return jsonify({"error": err}), 500
    execute("UPDATE files SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (fid,))
    promoted = None
    if primary:
        nxt = query_one("SELECT id FROM files WHERE track_id=? AND is_deleted=0 AND id!=? ORDER BY file_size_mb DESC LIMIT 1", (tid, fid))
        if nxt:
            execute("UPDATE files SET is_primary=1 WHERE id=?", (nxt["id"],))
            promoted = nxt["id"]
        else:
            execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now') WHERE id=?", (tid,))
            execute("DELETE FROM place_tracks WHERE track_id=?", (tid,))
    return jsonify({"ok": True, "promoted_primary_id": promoted})


@app.route("/api/file/<int:fid>/set_primary", methods=["POST"])
def api_set_primary(fid):
    f = query_one("SELECT * FROM files WHERE id=? AND is_deleted=0", (fid,))
    if not f: return jsonify({"error": "not found"}), 404
    execute("UPDATE files SET is_primary=0 WHERE track_id=?", (f["track_id"],))
    execute("UPDATE files SET is_primary=1 WHERE id=?", (fid,))
    return jsonify({"ok": True, "primary_file_id": fid})


@app.route("/api/file/<int:fid>/analyse", methods=["POST"])
def api_analyse_file(fid):
    f = query_one("SELECT * FROM files WHERE id=? AND is_deleted=0", (fid,))
    if not f: return jsonify({"error": "not found"}), 404
    cur = _analysis_running.get(fid)
    if cur in ("running", "pending"): return jsonify({"status": cur})
    fp_resolved = _resolve_audio_path(f["file_path"])
    if not fp_resolved: return jsonify({"error": "file not on disk"}), 404
    execute("UPDATE files SET analysis_status='pending' WHERE id=?", (fid,))
    _analysis_running[fid] = "pending"
    threading.Thread(target=_run_analysis_background, args=(fid, fp_resolved), daemon=True).start()
    return jsonify({"status": "pending", "file_id": fid}), 202


@app.route("/api/file/<int:fid>/analysis_status")
def api_analysis_status(fid):
    f = query_one("SELECT id,has_analysis,analysis_status,bpm,key_note,key_scale,loudness_rms FROM files WHERE id=? AND is_deleted=0", (fid,))
    if not f: return jsonify({"error": "not found"}), 404
    status = _analysis_running.get(fid) or f.get("analysis_status") or "none"
    return jsonify({"status": status, "file_id": fid, **{k: f.get(k) for k in ["has_analysis","bpm","key_note","key_scale","loudness_rms"]}})


# =============================================================
# REMOVED FROM MUSIC ROOM — list + restore (change-of-mind)
# =============================================================

import removed as _removed  # noqa: E402  (list + restore: removed.py)
_removed.register(app, query, query_one, execute, _removed_dir, get_library_dest)


def _spectrum_dup_groups():
    """CHEAP (no hashing): per track, file-groups sharing the EXACT spectrum —
    same format + rounded size + rounded duration + bitrate + sample-rate.
    A strong duplicate signal (and the user's own criteria), used for the fast
    count. The actual dedupe confirms byte-identity by SHA-256 before removing
    anything, so a coincidental spectrum match is never deleted."""
    from collections import defaultdict
    rows = query(
        "SELECT id, track_id, file_path, content_hash, is_primary, file_format, "
        "file_size_mb, duration_sec, bitrate_kbps, sample_rate "
        "FROM files WHERE is_deleted=0"
    )
    buckets = defaultdict(list)
    for f in rows:
        key = (f["track_id"], f["file_format"], round(f["file_size_mb"] or 0, 2),
               round(f["duration_sec"] or 0, 1), f["bitrate_kbps"], f["sample_rate"])
        buckets[key].append(f)
    return [fs for fs in buckets.values() if len(fs) > 1]


@app.route("/api/files/duplicate-versions")
def api_duplicate_versions():
    """Fast estimate of redundant duplicate versions (spectrum match only, no
    hashing — instant). Drives the Analyse-page cleanup card."""
    groups = _spectrum_dup_groups()
    redundant = sum(len(g) - 1 for g in groups)
    return jsonify({"groups": len(groups), "redundant_files": redundant})


_dedupe_state = {"running": False, "done": False, "checked": 0, "total": 0,
                 "removed": 0, "error": None}


@app.route("/api/files/dedupe", methods=["POST"])
def api_dedupe_versions():
    """Collapse BYTE-IDENTICAL file versions to one per track, in the
    background (hashing big files over a drive is slow).

    Always: confirm byte-identity, keep one copy (preferring the organised
    library copy, made primary), retire the redundant DB rows.

    With {"delete_files": true}: ALSO move the redundant *physical* files to
    the Trash (recoverable). The kept copy is never touched, and only files
    proven byte-identical to it are trashed — so no audio is ever lost, just
    redundant copies of it. Poll /api/files/dedupe-status."""
    if _dedupe_state["running"]:
        return jsonify({"ok": False, "error": "already running"}), 409
    d = request.get_json(silent=True) or {}
    delete_files = bool(d.get("delete_files"))
    groups = _spectrum_dup_groups()
    _dedupe_state.update({"running": True, "done": False, "checked": 0,
                          "total": sum(len(g) for g in groups), "removed": 0,
                          "trashed": 0, "delete_files": delete_files, "error": None})

    def _worker():
        import importer as _imp
        import dedupe as _dedupe
        conn = sqlite3.connect(DB_PATH, timeout=20)
        try:
            def _checked():
                _dedupe_state["checked"] += 1
            res = _dedupe.run_dedupe(
                conn, groups, delete_files,
                file_hash=_imp.file_content_hash,
                trash=move_to_trash,
                isfile=_is_file_safe,
                on_checked=_checked,
            )
            _dedupe_state["removed"] = res["removed"]
            _dedupe_state["trashed"] = res["trashed"]
        except Exception as e:
            _dedupe_state["error"] = str(e)
        finally:
            conn.close()
            _dedupe_state["running"] = False
            _dedupe_state["done"] = True

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/files/dedupe-status")
def api_dedupe_status():
    return jsonify(_dedupe_state)


@app.route("/api/track/<int:tid>/note", methods=["POST"])
def api_add_note(tid):
    d = request.get_json() or {}
    text = d.get("note_text", "").strip() or ""
    if not text and not d.get("voice_note_path"):
        return jsonify({"error": "note_text or voice_note_path required"}), 400
    nid = execute("INSERT INTO notes (track_id,note_text,audio_timestamp,note_type,voice_note_path) VALUES (?,?,?,?,?)",
                  (tid, text, d.get("audio_timestamp","").strip() or None, d.get("note_type","general"), d.get("voice_note_path")))
    return jsonify(query_one("SELECT * FROM notes WHERE id=?", (nid,))), 201


@app.route("/api/note/<int:nid>", methods=["DELETE"])
def api_delete_note(nid):
    execute("DELETE FROM notes WHERE id=?", (nid,))
    return jsonify({"ok": True})


@app.route("/api/track/<int:tid>/voice_note", methods=["POST"])
def api_voice_note(tid):
    if "audio" not in request.files: return jsonify({"error": "audio required"}), 400
    af = request.files["audio"]
    ct = af.content_type or "audio/webm"
    ext = ".ogg" if "ogg" in ct else ".mp4" if "mp4" in ct else ".webm"
    fp = os.path.join(VOICE_NOTES_DIR, f"voice_{uuid.uuid4().hex}{ext}")
    af.save(fp)
    nid = execute("INSERT INTO notes (track_id,note_text,audio_timestamp,note_type,voice_note_path) VALUES (?,?,?,'voice',?)",
                  (tid, request.form.get("note_text","").strip() or "",
                   request.form.get("audio_timestamp","").strip() or None, fp))
    note = query_one("SELECT * FROM notes WHERE id=?", (nid,))
    # Kick off background Whisper transcription (non-blocking)
    threading.Thread(target=_run_whisper_background, args=(nid, fp), daemon=True).start()
    return jsonify(note), 201


def _hsl_to_hex(h, s, l):
    s /= 100.0; l /= 100.0
    c = (1 - abs(2 * l - 1)) * s; x = c * (1 - abs((h / 60.0) % 2 - 1)); m = l - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return "#%02x%02x%02x" % (round((r + m) * 255), round((g + m) * 255), round((b + m) * 255))


def _tag_colour_for(name):
    """A colour for a tag so it BLOOMS on the record (curation-as-play). Honours
    Daithí's system: black/white/grey channel + energy 1-6 (calm→hot); everything
    else gets a stable muted hue from its name. Warm + muted, never neon."""
    import re as _re, hashlib as _hl
    n = (name or "").strip().lower()
    if not n:
        return None
    if "black" in n: return "#3a352c"
    if "white" in n: return "#d9cfb8"
    if "grey" in n or "gray" in n: return "#8a8273"
    m = _re.search(r"\b([1-6])\b", n)
    if m:                                    # energy: 1 ≈ cool teal → 6 ≈ warm red
        return _hsl_to_hex(round(205 - (int(m.group(1)) - 1) * 41), 34, 48)
    h = int(_hl.sha1(n.encode()).hexdigest(), 16) % 360
    return _hsl_to_hex(h, 26, 46)


@app.route("/api/track/<int:tid>/tag", methods=["POST"])
def api_add_tag(tid):
    d = request.get_json() or {}
    name = d.get("name","").strip().lower()
    if not name: return jsonify({"error": "name required"}), 400
    tag = query_one("SELECT * FROM tags WHERE LOWER(name)=?", (name,))
    if not tag:
        tag_id = execute("INSERT INTO tags (name,colour) VALUES (?,?)", (name, d.get("colour") or _tag_colour_for(name)))
        tag = query_one("SELECT * FROM tags WHERE id=?", (tag_id,))
    execute("INSERT OR IGNORE INTO track_tags (track_id,tag_id) VALUES (?,?)", (tid, tag["id"]))
    return jsonify(tag), 201


@app.route("/api/tracks/tag-batch", methods=["POST"])
def api_tag_batch():
    """One tag onto MANY tracks — the room's '＋ tag all in hand' (narrow with
    search/filters, then tag the lot). Same get-or-create as the single-track
    route; INSERT OR IGNORE per track so re-tagging never duplicates."""
    d = request.get_json() or {}
    name = (d.get("name") or "").strip().lower()
    try:
        ids = [int(i) for i in (d.get("track_ids") or [])][:10000]
    except (TypeError, ValueError):
        ids = []
    if not name or not ids:
        return jsonify({"error": "name and track_ids required"}), 400
    tag = query_one("SELECT * FROM tags WHERE LOWER(name)=?", (name,))
    if not tag:
        tag_id = execute("INSERT INTO tags (name,colour) VALUES (?,?)",
                         (name, _tag_colour_for(name)))
        tag = query_one("SELECT * FROM tags WHERE id=?", (tag_id,))
    for tid in ids:
        execute("INSERT OR IGNORE INTO track_tags (track_id,tag_id) VALUES (?,?)",
                (tid, tag["id"]))
    return jsonify({"ok": True, "tag": dict(tag), "applied": len(ids)}), 201


@app.route("/api/track/<int:tid>/tag/<int:tag_id>", methods=["DELETE"])
def api_remove_tag(tid, tag_id):
    execute("DELETE FROM track_tags WHERE track_id=? AND tag_id=?", (tid, tag_id))
    return jsonify({"ok": True})


# ─── Tags & genres: rename + merge, applied to every track ───────────
# The filter desk lets the curator tidy their own vocabulary in place: rename
# a tag/genre (changes it on every track at once) or fold several into one.
# A rename that collides with an existing name IS a merge — the survivor keeps
# the target name. track_tags/track_genres and structure_nodes are all
# repointed so nothing dangles.

# kind -> (taxonomy table, link table, link column)
_TAXO = {"tag":   ("tags",   "track_tags",   "tag_id"),
         "genre": ("genres", "track_genres", "genre_id")}


def _taxo_resolve(cur, table, name):
    """Find-or-create a tag/genre by (lowercased) name; return its id."""
    row = cur.execute(f"SELECT id FROM {table} WHERE LOWER(name)=?", (name,)).fetchone()
    if row:
        return row[0]
    if table == "tags":
        cur.execute("INSERT INTO tags (name, colour) VALUES (?, NULL)", (name,))
    else:
        cur.execute("INSERT INTO genres (name) VALUES (?)", (name,))
    return cur.lastrowid


def _taxo_merge(cur, kind, source_ids, target_id):
    """Fold every source tag/genre into target_id: re-point its tracks and
    structure nodes, then delete the emptied source rows. target_id is never
    in source_ids."""
    table, link, col = _TAXO[kind]
    for sid in source_ids:
        if sid == target_id:
            continue
        cur.execute(f"INSERT OR IGNORE INTO {link} (track_id, {col}) "
                    f"SELECT track_id, ? FROM {link} WHERE {col}=?", (target_id, sid))
        cur.execute(f"DELETE FROM {link} WHERE {col}=?", (sid,))
        # structure nodes reference the taxonomy by ref_id (UNIQUE per structure)
        cur.execute("UPDATE OR IGNORE structure_nodes SET ref_id=? WHERE kind=? AND ref_id=?",
                    (target_id, kind, sid))
        cur.execute("DELETE FROM structure_nodes WHERE kind=? AND ref_id=?", (kind, sid))
        cur.execute(f"DELETE FROM {table} WHERE id=?", (sid,))


@app.route("/api/taxonomy/rename", methods=["POST"])
def api_taxonomy_rename():
    """Rename a tag/genre on every track. If the new name already exists, the
    two are merged (the existing one survives, wearing that name)."""
    d = request.get_json() or {}
    kind = d.get("kind")
    if kind not in _TAXO:
        return jsonify({"error": "kind must be tag or genre"}), 400
    table, _, _ = _TAXO[kind]
    try:
        tid = int(d.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "id required"}), 400
    name = (d.get("name") or "").strip().lower()
    if not name:
        return jsonify({"error": "name required"}), 400
    db, cur = get_db(), get_db().cursor()
    try:
        cur.execute("BEGIN")
        cur.execute(f"SELECT id, name FROM {table} WHERE id=?", (tid,))
        row = cur.fetchone()
        if not row:
            db.rollback(); return jsonify({"error": "not found"}), 404
        existing = cur.execute(f"SELECT id FROM {table} WHERE LOWER(name)=? AND id<>?",
                               (name, tid)).fetchone()
        if existing:                                    # collision → merge tid INTO existing
            target_id = existing[0]
            _taxo_merge(cur, kind, [tid], target_id)
            merged = True
        else:                                           # plain rename in place
            cur.execute(f"UPDATE {table} SET name=? WHERE id=?", (name, tid))
            target_id = tid
            merged = False
        db.commit()
    except Exception as e:
        db.rollback(); return jsonify({"error": str(e)}), 500
    n = query_one(f"SELECT COUNT(*) c FROM {_TAXO[kind][1]} WHERE {_TAXO[kind][2]}=?", (target_id,))
    return jsonify({"ok": True, "id": target_id, "name": name,
                    "merged": merged, "track_count": n["c"] if n else 0})


@app.route("/api/taxonomy/merge", methods=["POST"])
def api_taxonomy_merge():
    """Fold several tags/genres into one. `ids` are the chips selected; `name`
    is what the survivor should be called (an existing or brand-new name)."""
    d = request.get_json() or {}
    kind = d.get("kind")
    if kind not in _TAXO:
        return jsonify({"error": "kind must be tag or genre"}), 400
    table, _, _ = _TAXO[kind]
    ids = [int(i) for i in (d.get("ids") or []) if str(i).strip()]
    name = (d.get("name") or "").strip().lower()
    if len(ids) < 2 or not name:
        return jsonify({"error": "two or more ids and a name required"}), 400
    db, cur = get_db(), get_db().cursor()
    try:
        cur.execute("BEGIN")
        target_id = _taxo_resolve(cur, table, name)     # may be one of ids, or new
        _taxo_merge(cur, kind, [i for i in ids if i != target_id], target_id)
        db.commit()
    except Exception as e:
        db.rollback(); return jsonify({"error": str(e)}), 500
    n = query_one(f"SELECT COUNT(*) c FROM {_TAXO[kind][1]} WHERE {_TAXO[kind][2]}=?", (target_id,))
    return jsonify({"ok": True, "id": target_id, "name": name,
                    "track_count": n["c"] if n else 0})


import structures as _structures  # noqa: E402  (tag/genre mind-maps: structures.py)
_structures.register(app, query, query_one, execute)


# ─── Genres ──────────────────────────────────────────────────────────
@app.route("/api/genres")
def api_genres():
    """All genres with usage counts."""
    rows = query("""
        SELECT g.id, g.name, COUNT(tg.track_id) as track_count
        FROM genres g LEFT JOIN track_genres tg ON tg.genre_id = g.id
        GROUP BY g.id, g.name
        ORDER BY track_count DESC, g.name ASC
    """)
    return jsonify(rows)


@app.route("/api/track/<int:tid>/genre", methods=["POST"])
def api_add_genre(tid):
    d = request.get_json() or {}
    name = (d.get("name") or "").strip().lower()
    if not name: return jsonify({"error": "name required"}), 400
    g = query_one("SELECT * FROM genres WHERE LOWER(name)=?", (name,))
    if not g:
        gid = execute("INSERT INTO genres (name) VALUES (?)", (name,))
        g = query_one("SELECT * FROM genres WHERE id=?", (gid,))
    execute("INSERT OR IGNORE INTO track_genres (track_id, genre_id) VALUES (?,?)", (tid, g["id"]))
    return jsonify(g), 201


@app.route("/api/track/<int:tid>/genre/<int:gid>", methods=["DELETE"])
def api_remove_genre(tid, gid):
    execute("DELETE FROM track_genres WHERE track_id=? AND genre_id=?", (tid, gid))
    return jsonify({"ok": True})


@app.route("/api/album/track-ids")
def api_album_track_ids():
    """Every non-deleted track that shares an album name (case-insensitive),
    in track order. Powers the panel's quiet "apply this tag/genre to the
    whole album" shortcut — the frontend only offers it when count >= 2 so a
    one-track 'album' never shows it. Album = the album NAME (a DJ thinks of a
    physical release), so compilations tag through too; the visible count is
    the safeguard against the rare same-name-different-release collision."""
    album = (request.args.get("album") or "").strip()
    if not album:
        return jsonify({"album": "", "count": 0, "track_ids": []})
    rows = query(
        "SELECT id FROM tracks WHERE is_deleted=0 "
        "AND album IS NOT NULL AND TRIM(album) <> '' "
        "AND LOWER(TRIM(album)) = LOWER(TRIM(?)) "
        "ORDER BY track_number, title",
        (album,))
    ids = [r["id"] for r in rows]
    return jsonify({"album": album, "count": len(ids), "track_ids": ids})


@app.route("/api/track/<int:tid>/genre-suggestions")
def api_track_genre_suggestions(tid):
    """Pull candidate genres from multiple free, no-AI sources (Discogs,
    MusicBrainz, Deezer, iTunes, Last.fm if a key is set). ISRC-bridged
    where possible; artist+title fallback. The UI offers them as chips
    the user clicks to apply — nothing is auto-added."""
    t = query_one(
        "SELECT id, artist_name, title, isrc FROM tracks "
        "WHERE id=? AND is_deleted=0",
        (tid,),
    )
    if not t:
        return jsonify({"error": "track not found"}), 404
    artist = (t.get("artist_name") or "").strip()
    title  = (t.get("title") or "").strip()
    if not (artist or title):
        return jsonify({"error": "track has no artist/title to look up"}), 400

    # Pick a known ISRC (multi-ISRC table since v27; fall back to legacy col).
    isrc_rows = query("SELECT isrc FROM track_isrcs WHERE track_id=?", (tid,))
    isrcs = [r["isrc"] for r in isrc_rows if r.get("isrc")]
    if not isrcs and t.get("isrc"):
        isrcs = [t["isrc"]]
    isrc = isrcs[0] if isrcs else None

    discogs_token = (get_setting("discogs_token",   "") or "").strip() or None
    lastfm_key    = (get_setting("lastfm_api_key",  "") or "").strip() or None

    suggestions = genre_suggest.suggest_genres(
        artist, title,
        isrc=isrc,
        discogs_token=discogs_token,
        lastfm_key=lastfm_key,
        limit=12,
    )

    current = query("""
        SELECT g.id, g.name FROM genres g
        JOIN track_genres tg ON tg.genre_id = g.id
        WHERE tg.track_id=? ORDER BY g.name
    """, (tid,))

    return jsonify({
        "track_id":    tid,
        "artist":      artist,
        "title":       title,
        "isrc":        isrc,
        "current":     current,
        "suggestions": suggestions,
        "source_codes": genre_suggest.SOURCE_CODES,
        "credentials": {
            "discogs_configured": bool(discogs_token),
            "lastfm_configured":  bool(lastfm_key),
        },
    })


def _match_local_track(artist, title):
    if artist and title:
        r = query_one("SELECT id FROM tracks WHERE LOWER(TRIM(artist_name))=LOWER(TRIM(?)) "
                      "AND LOWER(TRIM(title))=LOWER(TRIM(?)) AND is_deleted=0", (artist, title))
        return r["id"] if r else None
    return None

def _mb_url(mbid):
    return f"https://musicbrainz.org/recording/{mbid}" if mbid else None

@app.route("/api/track/<int:tid>/samples")
def api_track_samples(tid):
    """Sample lineage = the user's CONFIRMED samples (with timestamps) + open
    MusicBrainz SUGGESTIONS (free, no key) that haven't been confirmed or
    dismissed. 'sampled_in' derives from other tracks' confirmed samples + MB.
    The user confirms/dismisses/adds; that's where the timestamps live (MB has
    none)."""
    import sample_lookup
    t = query_one("SELECT id, artist_name, title, isrc FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "track not found"}), 404
    artist = (t.get("artist_name") or "").strip()
    title  = (t.get("title") or "").strip()

    # 1) the user's stored rows for this track
    rows = query("SELECT * FROM track_samples WHERE track_id=?", (tid,))
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    dismissed_mbids  = {r["source_mbid"] for r in rows if r["status"] == "dismissed" and r["source_mbid"]}
    committed_mbids  = {r["source_mbid"] for r in confirmed if r["source_mbid"]}

    def _confirmed_item(r):
        local_id = r["source_track_id"]
        a, ti = r["source_artist"], r["source_title"]
        if local_id:
            lt = query_one("SELECT artist_name, title FROM tracks WHERE id=?", (local_id,))
            if lt: a, ti = a or lt["artist_name"], ti or lt["title"]
        return {"id": r["id"], "confirmed": True, "source": r["source"],
                "artist": a, "title": ti, "mbid": r["source_mbid"], "mb_url": _mb_url(r["source_mbid"]),
                "local_track_id": local_id or _match_local_track(a, ti),
                "timestamp_sec": r["timestamp_sec"], "source_timestamp_sec": r["source_timestamp_sec"],
                "note": r["note"]}

    # 2) live MB suggestions, minus anything already confirmed or dismissed
    suggestions = []
    if artist or title:
        isrc_rows = query("SELECT isrc FROM track_isrcs WHERE track_id=?", (tid,))
        isrcs = [r["isrc"] for r in isrc_rows if r.get("isrc")] or ([t["isrc"]] if t.get("isrc") else [])
        mb = sample_lookup.find_samples(artist, title, isrcs[0] if isrcs else None)
        for it in mb.get("samples", []):
            if it.get("mbid") and (it["mbid"] in dismissed_mbids or it["mbid"] in committed_mbids):
                continue
            suggestions.append({"id": None, "confirmed": False, "source": "musicbrainz",
                                "artist": it.get("artist"), "title": it.get("title"),
                                "mbid": it.get("mbid"), "mb_url": _mb_url(it.get("mbid")),
                                "local_track_id": _match_local_track(it.get("artist"), it.get("title")),
                                "timestamp_sec": None, "source_timestamp_sec": None,
                                "partial": it.get("partial")})
        mb_sampled_in = mb.get("sampled_in", [])
    else:
        mb_sampled_in = []

    # 3) sampled_in: other tracks whose CONFIRMED sample points at this one + MB
    rev = query("""SELECT ts.*, t.artist_name AS sampler_artist, t.title AS sampler_title
                   FROM track_samples ts JOIN tracks t ON t.id=ts.track_id
                   WHERE ts.source_track_id=? AND ts.status='confirmed' AND t.is_deleted=0""", (tid,))
    sampled_in = [{"id": r["id"], "confirmed": True, "artist": r["sampler_artist"], "title": r["sampler_title"],
                   "local_track_id": r["track_id"], "mb_url": None,
                   "timestamp_sec": r["timestamp_sec"]} for r in rev]
    for it in mb_sampled_in:
        sampled_in.append({"id": None, "confirmed": False, "artist": it.get("artist"), "title": it.get("title"),
                           "mbid": it.get("mbid"), "mb_url": _mb_url(it.get("mbid")),
                           "local_track_id": _match_local_track(it.get("artist"), it.get("title"))})

    return jsonify({
        "track_id": tid,
        "samples": [_confirmed_item(r) for r in confirmed] + suggestions,
        "sampled_in": sampled_in,
    })


@app.route("/api/track/<int:tid>/samples", methods=["POST"])
def api_add_track_sample(tid):
    """Confirm a MB suggestion (with a marked timestamp) or add the user's own."""
    if not query_one("SELECT 1 FROM tracks WHERE id=? AND is_deleted=0", (tid,)):
        return jsonify({"error": "track not found"}), 404
    d = request.get_json() or {}
    st = (d.get("source_title") or "").strip() or None
    sa = (d.get("source_artist") or "").strip() or None
    stid = d.get("source_track_id")
    stid = int(stid) if isinstance(stid, int) or (isinstance(stid, str) and stid.isdigit()) else None
    # if linked to a local track and no free-text given, borrow its name
    if stid and not (st or sa):
        lt = query_one("SELECT artist_name, title FROM tracks WHERE id=?", (stid,))
        if lt: sa, st = lt["artist_name"], lt["title"]
    if not (st or stid or d.get("source_mbid")):
        return jsonify({"error": "need a source (a track, a title, or an MBID)"}), 400
    def _num(x):
        try: return float(x)
        except (TypeError, ValueError): return None
    execute("""INSERT INTO track_samples
               (track_id, source_track_id, source_title, source_artist, source_mbid,
                timestamp_sec, source_timestamp_sec, note, status, source)
               VALUES (?,?,?,?,?,?,?,?,'confirmed',?)""",
            (tid, stid, st, sa, (d.get("source_mbid") or None),
             _num(d.get("timestamp_sec")), _num(d.get("source_timestamp_sec")),
             (d.get("note") or "").strip() or None,
             "musicbrainz" if d.get("source_mbid") else "user"))
    return jsonify({"ok": True})


@app.route("/api/track/<int:tid>/samples/dismiss", methods=["POST"])
def api_dismiss_track_sample(tid):
    """Dismiss a MB suggestion so it stops being suggested for this track."""
    d = request.get_json() or {}
    mbid = (d.get("source_mbid") or "").strip()
    if not mbid:
        return jsonify({"error": "source_mbid required"}), 400
    execute("INSERT INTO track_samples (track_id, source_mbid, source_title, source_artist, status, source) "
            "VALUES (?,?,?,?,'dismissed','musicbrainz')",
            (tid, mbid, (d.get("source_title") or "").strip() or None, (d.get("source_artist") or "").strip() or None))
    return jsonify({"ok": True})


@app.route("/api/track/sample/<int:sid>", methods=["DELETE"])
def api_delete_track_sample(sid):
    execute("DELETE FROM track_samples WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/track/<int:tid>/update", methods=["POST"])
def api_update_track(tid):
    d = request.get_json() or {}
    allowed = {"title","artist_name","album","year","label","track_number","colour_code",
               "vinyl_owned","vinyl_notes","spotify_url","soundcloud_url","youtube_url",
               "bandcamp_url","other_url","discovered_via","discovery_date","source_type","needs_review"}
    updates = {k: v for k, v in d.items() if k in allowed}
    if not updates: return jsonify({"error": "no valid fields"}), 400
    set_c = ", ".join(f"{k}=?" for k in updates)
    execute(f"UPDATE tracks SET {set_c}, updated_at=datetime('now') WHERE id=?", list(updates.values()) + [tid])
    return jsonify(query_one("SELECT * FROM tracks WHERE id=?", (tid,)))


@app.route("/api/track/<int:tid>/write_tags", methods=["POST"])
def api_write_tags(tid):
    t = query_one("SELECT * FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    p = query_one("SELECT * FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0", (tid,))
    if not t: return jsonify({"error": "not found"}), 404
    if not p: return jsonify({"error": "no primary file"}), 404
    if t.get("gr_borrow_id"):
        return jsonify({"error": "borrowed record — its file can't be written to"}), 403
    ok, err = write_tags_to_file(p["file_path"], t.get("title"), t.get("artist_name"), t.get("album"), t.get("year"), t.get("track_number"))
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": err}), 500


@app.route("/api/track/<int:tid>/verify", methods=["POST"])
def api_verify_analysis(tid):
    d = request.get_json() or {}
    p = query_one("SELECT id FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0", (tid,))
    if not p: return jsonify({"error": "no primary file"}), 404
    fid = p["id"]
    ups = {}
    if "bpm"       in d: ups["bpm"] = float(d["bpm"]); ups["bpm_verified"] = 1
    if "key_note"  in d: ups["key_note"] = d["key_note"]; ups["key_verified"] = 1
    if "key_scale" in d: ups["key_scale"] = d["key_scale"]; ups["key_verified"] = 1
    if ups:
        sc = ", ".join(f"{k}=?" for k in ups)
        execute(f"UPDATE files SET {sc}, updated_at=datetime('now') WHERE id=?", list(ups.values()) + [fid])
    return jsonify(query_one("SELECT * FROM files WHERE id=?", (fid,)))


# =============================================================
# API — SHOWS (radio show mode)
# =============================================================

@app.route("/api/shows")
def api_shows():
    return jsonify(query("""
        SELECT s.*,
               COUNT(DISTINCT ss.set_id) as set_count
        FROM shows s
        LEFT JOIN show_sets ss ON ss.show_id = s.id
        GROUP BY s.id ORDER BY s.show_date DESC, s.created_at DESC
    """))


@app.route("/api/shows", methods=["POST"])
def api_create_show():
    d = request.get_json() or {}
    if not d.get("title","").strip(): return jsonify({"error": "title required"}), 400
    sid = execute("""
        INSERT INTO shows (title, show_date, series, episode_number, description,
                           recording_url, duration_sec, is_published)
        VALUES (?,?,?,?,?,?,?,?)
    """, (d["title"].strip(), d.get("show_date") or None, d.get("series") or None,
          d.get("episode_number") or None, d.get("description") or None,
          d.get("recording_url") or None, d.get("duration_sec") or None, 0))
    return jsonify(query_one("SELECT * FROM shows WHERE id=?", (sid,))), 201


@app.route("/api/show/<int:sid>")
def api_show(sid):
    s = query_one("SELECT * FROM shows WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404
    sets = query("""
        SELECT ss.id as show_set_id, ss.position,
               st.id, st.name, st.set_date, st.place_id,
               p.name as place_name,
               COUNT(str.track_id) as track_count
        FROM show_sets ss
        JOIN sets st ON st.id = ss.set_id
        LEFT JOIN places p ON p.id = st.place_id
        LEFT JOIN set_tracks str ON str.set_id = st.id
        WHERE ss.show_id = ?
        GROUP BY ss.id ORDER BY ss.position ASC
    """, (sid,))
    return jsonify({"show": s, "sets": sets})


@app.route("/api/show/<int:sid>", methods=["POST"])
def api_update_show(sid):
    d = request.get_json() or {}
    allowed = {"title","show_date","series","episode_number","description",
               "recording_url","duration_sec","is_published"}
    ups = {k: v for k, v in d.items() if k in allowed}
    if not ups: return jsonify({"error": "no valid fields"}), 400
    sc = ", ".join(f"{k}=?" for k in ups)
    execute(f"UPDATE shows SET {sc}, updated_at=datetime('now') WHERE id=?",
            list(ups.values()) + [sid])
    return jsonify(query_one("SELECT * FROM shows WHERE id=?", (sid,)))


@app.route("/api/show/<int:sid>", methods=["DELETE"])
def api_delete_show(sid):
    execute("DELETE FROM show_sets WHERE show_id=?", (sid,))
    execute("DELETE FROM shows WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/show/<int:sid>/recording", methods=["POST"])
def api_upload_show_recording(sid):
    s = query_one("SELECT * FROM shows WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404
    if "recording" not in request.files:
        return jsonify({"error": "recording file required"}), 400

    recordings_dir = RECORDINGS_DIR
    os.makedirs(recordings_dir, exist_ok=True)

    rf   = request.files["recording"]
    ext  = os.path.splitext(rf.filename)[1].lower() if rf.filename else ".mp3"
    fp   = os.path.join(recordings_dir, f"show_{sid}_{uuid.uuid4().hex[:8]}{ext}")
    rf.save(fp)

    # Get duration with mutagen if available, fall back to None
    dur = None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(fp)
        if mf and mf.info: dur = int(mf.info.length)
    except Exception:
        pass

    execute("UPDATE shows SET recording_path=?, duration_sec=?, updated_at=datetime('now') WHERE id=?",
            (fp, dur, sid))
    return jsonify({"ok": True, "path": fp, "duration_sec": dur}), 201


@app.route("/api/show/<int:sid>/sets", methods=["POST"])
def api_add_set_to_show(sid):
    d = request.get_json() or {}
    set_id = d.get("set_id")
    if not set_id: return jsonify({"error": "set_id required"}), 400
    maxp = query_one("SELECT COALESCE(MAX(position),0) as m FROM show_sets WHERE show_id=?", (sid,))["m"]
    execute("INSERT OR IGNORE INTO show_sets (show_id, set_id, position) VALUES (?,?,?)",
            (sid, set_id, maxp + 1))
    return jsonify({"ok": True}), 201


@app.route("/api/show/<int:sid>/set/<int:show_set_id>", methods=["DELETE"])
def api_remove_set_from_show(sid, show_set_id):
    execute("DELETE FROM show_sets WHERE id=? AND show_id=?", (show_set_id, sid))
    return jsonify({"ok": True})


@app.route("/recording/<int:show_id>")
def serve_recording(show_id):
    s = query_one("SELECT * FROM shows WHERE id=?", (show_id,))
    if not s or not s.get("recording_path"): abort(404)
    p = s["recording_path"]
    if not _is_file_safe(p): abort(404)
    mime = mimetypes.guess_type(p)[0] or "audio/mpeg"
    return _stream_file(p, mime)


# =============================================================
# API — RANDOM FILTERS
# Returns a set of filter values drawn from actual library data.
# Used by the "shuffle filters" button in the UI.
# =============================================================

@app.route("/api/tracks/random_filters")
def api_random_filters():
    bpm_rows    = query("SELECT DISTINCT ROUND(bpm) as b FROM files WHERE bpm IS NOT NULL AND is_primary=1 AND is_deleted=0 AND bpm > 0")
    key_rows    = query("SELECT DISTINCT key_note, key_scale FROM files WHERE key_note IS NOT NULL AND is_primary=1 AND is_deleted=0")
    year_rows   = query("SELECT DISTINCT year FROM tracks WHERE year IS NOT NULL AND year > 1900 AND is_deleted=0")
    format_rows = query("SELECT DISTINCT file_format FROM files WHERE is_deleted=0 AND is_primary=1 AND file_format IS NOT NULL")
    moment_rows = query("SELECT DISTINCT moment FROM place_tracks WHERE moment IS NOT NULL AND moment != ''")

    bpm_vals    = [int(r["b"]) for r in bpm_rows if r["b"]]
    key_vals    = [f"{r['key_note']} {r['key_scale']}" for r in key_rows if r["key_note"]]
    year_vals   = [r["year"] for r in year_rows if r["year"]]
    format_vals = [r["file_format"] for r in format_rows if r["file_format"]]
    moment_vals = [r["moment"] for r in moment_rows if r["moment"]]

    # Format and moment are weighted toward None so they don't always filter
    chosen_bpm    = random.choice(bpm_vals)    if bpm_vals    else None
    chosen_key    = random.choice(key_vals)    if key_vals    else None
    chosen_year   = random.choice(year_vals)   if year_vals   else None
    chosen_format = random.choice(format_vals + [None, None]) if format_vals else None
    chosen_moment = random.choice(moment_vals + [None, None, None]) if moment_vals else None

    return jsonify({
        "bpm":    chosen_bpm,
        "key":    chosen_key,
        "year":   chosen_year,
        "format": chosen_format,
        "moment": chosen_moment,
    })


# =============================================================
# API — CAMELOT MAP
# =============================================================

@app.route("/api/camelot_map")
def api_camelot_map():
    return jsonify(CAMELOT_MAP)



# =============================================================
# API — PLACES
# =============================================================

@app.route("/api/places")
def api_places():
    return jsonify(query("""
        SELECT p.*, COUNT(pt.track_id) as track_count
        FROM places p LEFT JOIN place_tracks pt ON pt.place_id=p.id
        GROUP BY p.id ORDER BY p.is_well DESC, p.name ASC
    """))


@app.route("/api/places", methods=["POST"])
def api_create_place():
    d = request.get_json() or {}
    if not d.get("name","").strip(): return jsonify({"error": "name required"}), 400
    pid = execute("""
        INSERT INTO places (name,description,inside_outside,size,time_start,time_end,
                            moods,energy,crowd_notes,colour_primary,colour_secondary,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (d.get("name","").strip(), d.get("description","") or None, d.get("inside_outside") or None,
          d.get("size") or None, d.get("time_start") or None, d.get("time_end") or None,
          d.get("moods") or None, d.get("energy") or None, d.get("crowd_notes") or None,
          d.get("colour_primary") or None, d.get("colour_secondary") or None, d.get("notes") or None))
    return jsonify(query_one("SELECT * FROM places WHERE id=?", (pid,))), 201


PLACE_EDITABLE_FIELDS = {
    "name", "description", "inside_outside", "size", "time_start", "time_end",
    "moods", "energy", "crowd_notes", "colour_primary", "colour_secondary",
    "notes", "moodboard_notes", "image_path", "video_path", "room_bg_path", "ambient_audio_path",
    "ambient_audio_volume", "moodboard_images", "reference_mixes", "illustration_kind",
    "theme_bg", "theme_text", "theme_font_title", "theme_font_body", "theme_mood",
    "illustration_light", "illustration_structure", "illustration_outline", "illustration_detail",
}

@app.route("/api/place/<int:pid>", methods=["PATCH"])
def api_patch_place(pid):
    p = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    updates = {}
    for k, v in d.items():
        if k in PLACE_EDITABLE_FIELDS:
            if k == "name":
                val = (v or "").strip()
                if not val:
                    return jsonify({"error": "name cannot be empty"}), 400
                updates[k] = val
            else:
                if isinstance(v, str):
                    v = v.strip()
                updates[k] = v if v not in ("", None) else None
    if not updates:
        return jsonify({"error": "no editable fields"}), 400
    set_c = ", ".join(f"{k}=?" for k in updates)
    execute(f"UPDATE places SET {set_c} WHERE id=?", list(updates.values()) + [pid])
    return jsonify(query_one("SELECT * FROM places WHERE id=?", (pid,)))


@app.route("/api/place/<int:pid>/position", methods=["POST"])
def api_set_place_position(pid):
    """Save a place's position on the forest map.
    x_pct / y_pct are percent offsets from the Well's centre,
    where 0,0 means right on top of the Well."""
    p = query_one("SELECT id, is_well FROM places WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    if p["is_well"]:
        return jsonify({"error": "The Well doesn't move."}), 400
    d = request.get_json() or {}
    try:
        x = float(d.get("x_pct"))
        y = float(d.get("y_pct"))
    except (TypeError, ValueError):
        return jsonify({"error": "x_pct and y_pct numbers required"}), 400
    execute("UPDATE places SET x_pct=?, y_pct=? WHERE id=?", (x, y, pid))
    return jsonify({"ok": True, "x_pct": x, "y_pct": y})


@app.route("/api/place/<int:pid>", methods=["DELETE"])
def api_delete_place(pid):
    p = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not p:
        return jsonify({"error": "not found"}), 404
    if p["is_well"]:
        return jsonify({"error": "The Well cannot be removed."}), 400
    execute("DELETE FROM place_tracks WHERE place_id=?", (pid,))
    execute("DELETE FROM places WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.route("/api/place/<int:pid>/tracks")
def api_place_tracks(pid):
    moment = request.args.get("moment","").strip()
    sort   = request.args.get("sort","artist_name")
    order  = request.args.get("order","asc")
    if sort not in {"artist_name","title","year","pt.added_at","t.date_added","pt.moment_order","f.bpm","f.key_note","t.label"}: sort = "artist_name"
    if order not in ("asc","desc"): order = "asc"
    cond, params = ["pt.place_id=?","t.is_deleted=0"], [pid]
    if moment:
        cond.append("LOWER(pt.moment)=LOWER(?)")
        params.append(moment)
    where = "WHERE " + " AND ".join(cond)
    tracks = query(f"""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.colour_code,
               t.needs_review,t.vinyl_owned,t.has_image,t.source_type,
               t.discovered_via,t.last_played,t.date_added,t.gr_borrow_id,
               COALESCE(t.play_count,0) AS play_count, t.last_played_at,
               t.artwork_path,t.artwork_source,t.artwork_verified,
               t.dsp_check_state,t.dsp_checked_at,t.dsp_probable,
               pt.moment,pt.moment_order,pt.added_at as placed_at,
               f.id as file_id,f.file_path,f.file_format,f.duration_sec,
               f.bpm,f.bpm_verified,f.key_note,f.key_scale,f.key_verified,
               f.loudness_rms,f.dynamic_range,f.spectral_centroid,
               f.band_sub,f.band_bass,f.band_lowmid,
               f.band_body,f.band_presence,f.band_brilliance,
               f.brightness_score,
               f.has_analysis,f.analysis_status,f.integrity
        FROM tracks t JOIN place_tracks pt ON pt.track_id=t.id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        {where} ORDER BY {sort} {order} NULLS LAST, t.id {order}
    """, params)
    if tracks:
        track_ids = [t["id"] for t in tracks]
        ph = ",".join("?" * len(track_ids))
        genre_rows = query(
            f"""SELECT tg.track_id, g.id, g.name
                FROM track_genres tg JOIN genres g ON g.id = tg.genre_id
                WHERE tg.track_id IN ({ph})""",
            track_ids,
        )
        gbt = {}
        for gr in genre_rows:
            gbt.setdefault(gr["track_id"], []).append({"id": gr["id"], "name": gr["name"]})
        for t in tracks:
            t["genres"] = gbt.get(t["id"], [])
            t["art_v"] = _artwork_version(t.get("artwork_path"))   # cover cache token

        # Tags
        tag_rows = query(
            f"""SELECT tt.track_id, tg.id, tg.name, tg.colour
                FROM track_tags tt JOIN tags tg ON tg.id = tt.tag_id
                WHERE tt.track_id IN ({ph})""",
            track_ids,
        )
        tbt = {}
        for tr in tag_rows:
            tbt.setdefault(tr["track_id"], []).append({"id": tr["id"], "name": tr["name"], "colour": tr["colour"]})
        for t in tracks:
            t["tags"] = tbt.get(t["id"], [])

        # Notes (count + cue-point count)
        note_rows = query(
            f"""SELECT track_id, note_type, COUNT(*) AS n
                FROM notes WHERE track_id IN ({ph})
                GROUP BY track_id, note_type""",
            track_ids,
        )
        nbt = {}
        for nr in note_rows:
            d = nbt.setdefault(nr["track_id"], {"total": 0, "cue": 0})
            d["total"] += nr["n"]
            if nr["note_type"] == "cue_point":
                d["cue"] += nr["n"]
        for t in tracks:
            d = nbt.get(t["id"], {"total": 0, "cue": 0})
            t["note_count"] = d["total"]
            t["cue_count"]  = d["cue"]

        # Per-track AUDIO HEALTH for the Well's health filter:
        #   'missing'  — the primary file isn't on any connected drive
        #   'corrupt'  — present but the last decode scan found it damaged
        #   'ok'       — playable
        # Fast present-check (exact path + cached Library index), so this stays
        # quick even across the whole Well.
        _idx = _library_basename_index()
        def _present(p):
            if not p: return False
            if _is_file_safe(p): return True
            hit = _idx.get(os.path.basename(p).lower())
            return bool(hit and _is_file_safe(hit))
        for t in tracks:
            if t.get("gr_borrow_id"):
                t["borrowed"] = "ok" if _is_file_safe(t.get("file_path") or "") else "offloaded"
                t["audio_status"] = t["borrowed"]
            elif not _present(t.get("file_path")):
                t["audio_status"] = "missing"
            elif (t.get("integrity") or "") in ("corrupt", "zero"):
                t["audio_status"] = "corrupt"
            else:
                t["audio_status"] = "ok"

        # Places (names + count) — for the "lives in" chips
        place_rows = query(
            f"""SELECT pt.track_id, p.id, p.name, p.colour_primary, p.is_well
                FROM place_tracks pt JOIN places p ON p.id = pt.place_id
                WHERE pt.track_id IN ({ph})""",
            track_ids,
        )
        pbt = {}
        for pr in place_rows:
            pbt.setdefault(pr["track_id"], []).append({
                "id": pr["id"], "name": pr["name"],
                "colour_primary": pr["colour_primary"], "is_well": pr["is_well"],
            })
        for t in tracks:
            t["track_places"] = pbt.get(t["id"], [])
            t["place_count"]  = len(t["track_places"])

        # File version count (counts all formats per track)
        ver_rows = query(
            f"""SELECT track_id, COUNT(*) AS n FROM files
                WHERE track_id IN ({ph}) AND is_deleted=0
                GROUP BY track_id""",
            track_ids,
        )
        vct = {r["track_id"]: r["n"] for r in ver_rows}
        for t in tracks:
            t["version_count"] = vct.get(t["id"], 0)

        # Overlays — pull both place-scoped and all-places. Per-track rule:
        # if THIS track has any place-specific overlays here, hide the
        # all-places ones for this track. Else show the all-places ones.
        ov_rows = query(
            f"""SELECT track_id, id, overlay_type, content,
                       pos_x_pct, pos_y_pct, width_pct, height_pct,
                       rotation, z_index, color, place_id
                FROM track_overlays
                WHERE track_id IN ({ph})
                  AND (place_id = ? OR place_id IS NULL)
                ORDER BY z_index ASC, id ASC""",
            track_ids + [pid],
        )
        obt = {}
        for o in ov_rows:
            obt.setdefault(o["track_id"], []).append(o)
        for tid_k, ovs in obt.items():
            has_scoped = any(o["place_id"] == pid for o in ovs)
            if has_scoped:
                obt[tid_k] = [o for o in ovs if o["place_id"] == pid]
        for t in tracks:
            t["overlays"] = obt.get(t["id"], [])
    moments = [m["moment"] for m in query("SELECT DISTINCT moment FROM place_tracks WHERE place_id=? AND moment IS NOT NULL ORDER BY moment", (pid,))]
    return jsonify({"tracks": tracks, "moments": moments})


@app.route("/api/place/<int:pid>/add_track", methods=["POST"])
def api_add_track_to_place(pid):
    d = request.get_json() or {}
    moment = d.get("moment","").strip() or None
    tids   = d.get("track_ids") or ([d["track_id"]] if d.get("track_id") else [])
    if not tids: return jsonify({"error": "track_id(s) required"}), 400
    for tid in tids:
        execute("INSERT OR IGNORE INTO place_tracks (place_id,track_id,moment) VALUES (?,?,?)", (pid, tid, moment))
        execute("UPDATE tracks SET is_placed=1, updated_at=datetime('now') WHERE id=?", (tid,))
    return jsonify({"ok": True, "added": len(tids)}), 201


@app.route("/api/place/<int:pid>/track/<int:tid>", methods=["DELETE"])
def api_remove_track_from_place(pid, tid):
    well = query_one("SELECT id FROM places WHERE is_well=1")
    if well and pid == well["id"]: return jsonify({"error": "Cannot remove from The Well"}), 400
    execute("DELETE FROM place_tracks WHERE place_id=? AND track_id=?", (pid, tid))
    other = query_one("SELECT COUNT(*) as n FROM place_tracks pt JOIN places p ON p.id=pt.place_id WHERE pt.track_id=? AND p.is_well=0", (tid,))
    if other and other["n"] == 0:
        execute("UPDATE tracks SET is_placed=0, updated_at=datetime('now') WHERE id=?", (tid,))
    return jsonify({"ok": True})


# =============================================================
# PILES — the user's loose piles & clusters inside a 3D room.
# A pile is named + positioned by the USER. It may be tied to a real
# place / set / the Bosca (membership lives there — dropping a record on
# the pile adds it to that thing), or stand alone ('loose').
# =============================================================

def _pile_track_ids(p):
    """Member track ids for a pile, from wherever its membership lives."""
    kind = (p["kind"] or "loose")
    if kind == "place":
        rows = query("SELECT track_id FROM place_tracks WHERE place_id=?", (p["ref_id"],))
    elif kind == "set":
        rows = query("SELECT track_id FROM set_tracks WHERE set_id=? ORDER BY position", (p["ref_id"],))
    elif kind == "bosca":
        rows = query("SELECT track_id FROM bosca ORDER BY position")
    else:
        rows = query("SELECT track_id FROM room_pile_tracks WHERE pile_id=? ORDER BY added_at", (p["id"],))
    return [r["track_id"] for r in rows]


@app.route("/api/room/<int:pid>/piles")
def api_room_piles(pid):
    piles = query("SELECT * FROM room_piles WHERE room_place_id=? ORDER BY id", (pid,))
    out = []
    for p in piles:
        d = dict(p)
        d["track_ids"] = _pile_track_ids(p)
        out.append(d)
    return jsonify(out)


@app.route("/api/room/<int:pid>/piles", methods=["POST"])
def api_create_pile(pid):
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    kind = d.get("kind") or "loose"
    if kind not in ("loose", "place", "set", "bosca"):
        return jsonify({"error": "bad kind"}), 400
    ref = d.get("ref_id") if kind in ("place", "set") else None
    if kind in ("place", "set") and not ref:
        return jsonify({"error": f"{kind} pile needs ref_id"}), 400
    try:
        x, y, z = (float(d.get(k) or 0) for k in ("x", "y", "z"))
    except (TypeError, ValueError):
        return jsonify({"error": "x/y/z must be numbers"}), 400
    execute("INSERT INTO room_piles (room_place_id,name,kind,ref_id,x,y,z) VALUES (?,?,?,?,?,?,?)",
            (pid, name, kind, ref, x, y, z))
    row = query_one("SELECT * FROM room_piles WHERE room_place_id=? ORDER BY id DESC LIMIT 1", (pid,))
    out = dict(row)
    out["track_ids"] = _pile_track_ids(row)
    return jsonify(out), 201


@app.route("/api/room/pile/<int:pile_id>", methods=["PATCH"])
def api_update_pile(pile_id):
    d = request.get_json() or {}
    ups, vals = [], []
    if "name" in d:
        name = (d.get("name") or "").strip()
        if not name: return jsonify({"error": "name cannot be empty"}), 400
        ups.append("name=?"); vals.append(name)
    for k in ("x", "y", "z"):
        if k in d:
            try:
                ups.append(f"{k}=?"); vals.append(float(d[k]))
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} must be a number"}), 400
    if not ups:
        return jsonify({"error": "no fields to update"}), 400
    vals.append(pile_id)
    execute(f"UPDATE room_piles SET {', '.join(ups)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/room/pile/<int:pile_id>", methods=["DELETE"])
def api_delete_pile(pile_id):
    """Removes the pile (and a loose pile's own membership). NEVER touches the
    tied place/set/bosca — un-piling is not deleting the curator's work."""
    execute("DELETE FROM room_pile_tracks WHERE pile_id=?", (pile_id,))
    execute("DELETE FROM room_piles WHERE id=?", (pile_id,))
    return jsonify({"ok": True})


@app.route("/api/room/pile/<int:pile_id>/tracks", methods=["POST"])
def api_pile_add_track(pile_id):
    p = query_one("SELECT * FROM room_piles WHERE id=?", (pile_id,))
    if not p: return jsonify({"error": "pile not found"}), 404
    tid = (request.get_json() or {}).get("track_id")
    if not tid: return jsonify({"error": "track_id required"}), 400
    kind = (p["kind"] or "loose")
    if kind == "place":
        execute("INSERT OR IGNORE INTO place_tracks (place_id,track_id) VALUES (?,?)", (p["ref_id"], tid))
        execute("UPDATE tracks SET is_placed=1, updated_at=datetime('now') WHERE id=?", (tid,))
    elif kind == "set":
        nxt = query_one("SELECT COALESCE(MAX(position),0)+1 AS n FROM set_tracks WHERE set_id=?", (p["ref_id"],))["n"]
        execute("INSERT INTO set_tracks (set_id,track_id,position) VALUES (?,?,?)", (p["ref_id"], tid, nxt))
    elif kind == "bosca":
        nxt = query_one("SELECT COALESCE(MAX(position),0)+1 AS n FROM bosca")["n"]
        execute("INSERT OR IGNORE INTO bosca (track_id,position) VALUES (?,?)", (tid, nxt))
    else:
        execute("INSERT OR IGNORE INTO room_pile_tracks (pile_id,track_id) VALUES (?,?)", (pile_id, tid))
    return jsonify({"ok": True, "track_ids": _pile_track_ids(p)})


@app.route("/api/room/pile/<int:pile_id>/tracks/<int:tid>", methods=["DELETE"])
def api_pile_remove_track(pile_id, tid):
    p = query_one("SELECT * FROM room_piles WHERE id=?", (pile_id,))
    if not p: return jsonify({"error": "pile not found"}), 404
    kind = (p["kind"] or "loose")
    if kind == "place":
        execute("DELETE FROM place_tracks WHERE place_id=? AND track_id=?", (p["ref_id"], tid))
    elif kind == "set":
        execute("DELETE FROM set_tracks WHERE set_id=? AND track_id=?", (p["ref_id"], tid))
    elif kind == "bosca":
        execute("DELETE FROM bosca WHERE track_id=?", (tid,))
    else:
        execute("DELETE FROM room_pile_tracks WHERE pile_id=? AND track_id=?", (pile_id, tid))
    return jsonify({"ok": True, "track_ids": _pile_track_ids(p)})


@app.route("/api/place/<int:pid>/moodboard", methods=["POST"])
def api_moodboard_upload(pid):
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404
    if "images" not in request.files:
        return jsonify({"error": "no images uploaded"}), 400
    place_dir = os.path.join(MOODBOARD_DIR, str(pid))
    os.makedirs(place_dir, exist_ok=True)
    existing = _json.loads(place["moodboard_images"]) if place.get("moodboard_images") else []
    added = []
    for img_file in request.files.getlist("images"):
        ext = os.path.splitext(img_file.filename)[1].lower() if img_file.filename else ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}: ext = ".jpg"
        fname = f"{uuid.uuid4().hex}{ext}"
        fpath = os.path.join(place_dir, fname)
        img_file.save(fpath)
        url = f"/static/moodboard/{pid}/{fname}"
        existing.append(url)
        added.append(url)
    execute("UPDATE places SET moodboard_images=? WHERE id=?", (_json.dumps(existing), pid))
    return jsonify({"ok": True, "added": added, "images": existing}), 201


@app.route("/api/place/<int:pid>/ambient", methods=["POST"])
def api_ambient_upload(pid):
    """Upload an ambient-sound file for a place (rain, room tone, a field
    recording…). Stored under static/ambient/<pid>/ and set as the place's
    ambient_audio_path. The place page loops it quietly under everything."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place:
        return jsonify({"error": "not found"}), 404
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify({"error": "no audio uploaded"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in {".mp3", ".ogg", ".m4a", ".wav", ".aac", ".flac", ".webm"}:
        return jsonify({"error": "unsupported audio type"}), 400
    place_dir = os.path.join(AMBIENT_DIR, str(pid))
    os.makedirs(place_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    f.save(os.path.join(place_dir, fname))
    url = f"/static/ambient/{pid}/{fname}"
    execute("UPDATE places SET ambient_audio_path=? WHERE id=?", (url, pid))
    return jsonify({"ok": True, "ambient_audio_path": url}), 201


@app.route("/api/place/<int:pid>/ambient", methods=["DELETE"])
def api_ambient_clear(pid):
    """Remove a place's ambient sound (clears the path; leaves the file)."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place:
        return jsonify({"error": "not found"}), 404
    execute("UPDATE places SET ambient_audio_path=NULL WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.route("/api/place/<int:pid>/background", methods=["POST"])
def api_place_background_upload(pid):
    """Upload a background image for a place. Stored under static/place_bg/<pid>/
    and set as places.image_path. The 3D room renders it as a backdrop behind the
    records (dimmed by the global room_bg_dim setting so covers stay legible)."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place:
        return jsonify({"error": "not found"}), 404
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "no image uploaded"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return jsonify({"error": "unsupported image type"}), 400
    place_dir = os.path.join(PLACE_BG_DIR, str(pid))
    os.makedirs(place_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    f.save(os.path.join(place_dir, fname))
    url = f"/static/place_bg/{pid}/{fname}"
    # room_bg_path, NOT image_path — image_path drives the forest's "custom"
    # billboard, so reusing it would change the place's look in /forest3d.
    execute("UPDATE places SET room_bg_path=? WHERE id=?", (url, pid))
    return jsonify({"ok": True, "room_bg_path": url}), 201


@app.route("/api/place/<int:pid>/background", methods=["DELETE"])
def api_place_background_clear(pid):
    """Remove a place's room backdrop (clears the path; leaves the file)."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place:
        return jsonify({"error": "not found"}), 404
    execute("UPDATE places SET room_bg_path=NULL WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.route("/api/pixabay/search")
def api_pixabay_search():
    """Proxy Pixabay image search using the user's saved API key."""
    key = (get_setting("pixabay_api_key", "") or "").strip()
    if not key:
        return jsonify({"error": "no Pixabay key — add one in Settings"}), 400
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "query required"}), 400
    per_page = max(3, min(50, int(request.args.get("per_page", 24))))
    page     = max(1, int(request.args.get("page", 1)))
    try:
        import urllib.request, urllib.parse
        params = urllib.parse.urlencode({
            "key": key, "q": q,
            "image_type": "photo",
            "orientation": "horizontal",
            "safesearch": "true",
            "per_page": per_page, "page": page,
        })
        url = f"https://pixabay.com/api/?{params}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.loads(r.read().decode("utf-8"))
        results = [{
            "id":        h.get("id"),
            "thumb":     h.get("webformatURL"),
            "full":      h.get("largeImageURL") or h.get("webformatURL"),
            "alt":       h.get("tags", ""),
            "user_name": h.get("user"),
            "user_link": f"https://pixabay.com/users/{h.get('user')}-{h.get('user_id')}/",
            "link":      h.get("pageURL"),
        } for h in (data.get("hits") or [])]
        return jsonify({"results": results, "total": data.get("total", 0)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/place/<int:pid>/moodboard/url", methods=["POST"])
def api_moodboard_add_url(pid):
    """Add an external image URL (e.g. an Unsplash one) to a place's moodboard."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404
    d = request.get_json() or {}
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    existing = _json.loads(place["moodboard_images"]) if place.get("moodboard_images") else []
    if url not in existing:
        existing.append(url)
    execute("UPDATE places SET moodboard_images=? WHERE id=?", (_json.dumps(existing), pid))
    return jsonify({"ok": True, "images": existing})


@app.route("/api/place/<int:pid>/moodboard/<int:index>", methods=["DELETE"])
def api_moodboard_delete(pid, index):
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404
    images = _json.loads(place["moodboard_images"]) if place.get("moodboard_images") else []
    if index < 0 or index >= len(images):
        return jsonify({"error": "index out of range"}), 400
    url = images.pop(index)
    rel = url.lstrip("/")
    fpath = os.path.join(BASE_DIR, "static", *rel.split("/")[1:]) if rel.startswith("static/") else os.path.join(BASE_DIR, rel)
    try:
        if os.path.exists(fpath): os.remove(fpath)
    except Exception:
        pass
    execute("UPDATE places SET moodboard_images=? WHERE id=?", (_json.dumps(images), pid))
    return jsonify({"ok": True, "images": images})


@app.route("/api/place/<int:pid>/ambient", methods=["POST"])
def api_place_ambient(pid):
    d = request.get_json() or {}
    execute("UPDATE places SET reference_mixes=? WHERE id=?", (d.get("reference_mixes") or None, pid))
    return jsonify({"ok": True})


# =============================================================
# API — SETS
# =============================================================

@app.route("/api/sets")
def api_sets():
    return jsonify(query("""
        SELECT s.*, COUNT(st.track_id) as track_count,
               p.name as place_name, p.colour_primary as place_colour
        FROM sets s
        LEFT JOIN set_tracks st ON st.set_id=s.id
        LEFT JOIN places p ON p.id=s.place_id
        GROUP BY s.id ORDER BY s.created_at DESC
    """))


@app.route("/api/sets", methods=["POST"])
def api_create_set():
    d = request.get_json() or {}
    if not d.get("name","").strip(): return jsonify({"error": "name required"}), 400
    sid = execute("INSERT INTO sets (name,place_id,set_date,notes,research,set_type) VALUES (?,?,?,?,?,?)",
                  (d["name"].strip(), d.get("place_id") or None, d.get("set_date") or None,
                   d.get("notes") or None, d.get("research") or None,
                   d.get("set_type") or "charted"))
    return jsonify(query_one("SELECT * FROM sets WHERE id=?", (sid,))), 201


@app.route("/api/set/<int:sid>")
def api_set(sid):
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404
    tracks = query("""
        SELECT st.id as set_track_id, st.position, st.transition_note, st.public_note,
               t.id, t.title, t.artist_name, t.album, t.year, t.label,
               t.source_type, COALESCE(t.is_own_work,0) AS is_own_work,
               t.bandcamp_url, t.soundcloud_url, t.youtube_url, t.spotify_url, t.other_url,
               f.id as file_id, f.file_format, f.duration_sec,
               f.bpm, f.key_note, f.key_scale
        FROM set_tracks st JOIN tracks t ON t.id=st.track_id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE st.set_id=? ORDER BY st.position ASC
    """, (sid,))
    return jsonify({"set": s, "tracks": tracks})


@app.route("/api/set/<int:sid>", methods=["POST"])
def api_update_set(sid):
    d = request.get_json() or {}
    allowed = {"name","place_id","set_date","notes","research","external_link","set_type",
               "script","show_links"}
    ups = {k: v for k, v in d.items() if k in allowed}
    if not ups: return jsonify({"error": "no valid fields"}), 400
    sc = ", ".join(f"{k}=?" for k in ups)
    execute(f"UPDATE sets SET {sc}, updated_at=datetime('now') WHERE id=?", list(ups.values()) + [sid])
    return jsonify(query_one("SELECT * FROM sets WHERE id=?", (sid,)))


@app.route("/api/set/<int:sid>", methods=["DELETE"])
def api_delete_set(sid):
    execute("DELETE FROM set_tracks WHERE set_id=?", (sid,))
    execute("DELETE FROM sets WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/set/<int:sid>/tracks", methods=["POST"])
def api_add_to_set(sid):
    d = request.get_json() or {}
    tids = d.get("track_ids") or ([d["track_id"]] if d.get("track_id") else [])
    if not tids: return jsonify({"error": "track_ids required"}), 400
    maxp = query_one("SELECT COALESCE(MAX(position),0) as m FROM set_tracks WHERE set_id=?", (sid,))["m"]
    for i, tid in enumerate(tids):
        execute("INSERT INTO set_tracks (set_id,track_id,position) VALUES (?,?,?)", (sid, tid, maxp + i + 1))
    execute("UPDATE sets SET updated_at=datetime('now') WHERE id=?", (sid,))
    return jsonify({"ok": True, "added": len(tids)}), 201


@app.route("/api/set/<int:sid>/track/<int:stid>", methods=["DELETE"])
def api_remove_from_set(sid, stid):
    execute("DELETE FROM set_tracks WHERE id=? AND set_id=?", (stid, sid))
    execute("UPDATE sets SET updated_at=datetime('now') WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/set/<int:sid>/reorder", methods=["POST"])
def api_reorder_set(sid):
    for i, stid in enumerate((request.get_json() or {}).get("order", [])):
        execute("UPDATE set_tracks SET position=? WHERE id=? AND set_id=?", (i + 1, stid, sid))
    execute("UPDATE sets SET updated_at=datetime('now') WHERE id=?", (sid,))
    return jsonify({"ok": True})


@app.route("/api/set/<int:sid>/transition", methods=["POST"])
def api_update_transition(sid):
    d = request.get_json() or {}
    execute("UPDATE set_tracks SET transition_note=? WHERE id=? AND set_id=?",
            (d.get("transition_note","").strip() or None, d["set_track_id"], sid))
    return jsonify({"ok": True})


@app.route("/api/set/<int:sid>/public-note", methods=["POST"])
def api_update_public_note(sid):
    """The per-track note for a radio show's PUBLISHED tracklist — who made
    it, why it's here. Separate from transition_note (private planning)."""
    d = request.get_json() or {}
    execute("UPDATE set_tracks SET public_note=? WHERE id=? AND set_id=?",
            (d.get("public_note", "").strip() or None, d["set_track_id"], sid))
    return jsonify({"ok": True})


# ── RADIO SHOWS: the published tracklist + the show folder ─────────────────

def _radio_display_name(t):
    """What a track is called in the PUBLISHED tracklist. Titled tracks read
    'Artist — Title'; untitled ones (found sound, own audio) get a plain
    functional label — the owner's public_note does the real telling."""
    title = (t.get("title") or "").strip()
    artist = (t.get("artist_name") or "").strip()
    if title:
        return f"{artist} — {title}" if artist and artist.lower() != "unknown" else title
    if t.get("is_own_work"):
        return "original audio"
    st = (t.get("source_type") or "").strip()
    return {"personal_recording": "own recording", "found_sound": "found sound",
            "sample": "sample", "sound_effect": "found sound",
            "dj_tool": "dj tool", "acapella": "acapella"}.get(st, "untitled")


def _radio_set_rows(sid):
    return query("""
        SELECT st.position, st.public_note,
               t.id, t.title, t.artist_name, t.year, t.label,
               t.source_type, COALESCE(t.is_own_work,0) AS is_own_work,
               t.bandcamp_url, t.soundcloud_url, t.youtube_url, t.spotify_url, t.other_url,
               f.file_path, f.duration_sec
        FROM set_tracks st JOIN tracks t ON t.id=st.track_id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE st.set_id=? ORDER BY st.position ASC
    """, (sid,))


def _radio_tracklist_md(s, rows):
    """The descriptive published tracklist as markdown — pasteable into
    Substack (or anywhere). Structure only; the words are the owner's."""
    lines = [f"# {s['name']}"]
    if s.get("set_date"):
        lines.append(f"*{s['set_date']}*")
    lines.append("")
    for i, t in enumerate(rows, start=1):
        head = f"**{i}. {_radio_display_name(t)}**"
        bits = [str(t["year"]) if t.get("year") else None, t.get("label") or None]
        bits = [b for b in bits if b]
        if bits:
            head += f" ({', '.join(bits)})"
        lines.append(head)
        note = (t.get("public_note") or "").strip()
        if note:
            lines.append(note)
        links = []
        for col, lbl in (("bandcamp_url", "Bandcamp"), ("soundcloud_url", "SoundCloud"),
                         ("youtube_url", "YouTube"), ("spotify_url", "Spotify"),
                         ("other_url", "Link")):
            if (t.get(col) or "").strip():
                links.append(f"[{lbl}]({t[col].strip()})")
        if not (t.get("bandcamp_url") or "").strip():
            artist, title = (t.get("artist_name") or "").strip(), (t.get("title") or "").strip()
            if artist and title and artist.lower() != "unknown":
                from urllib.parse import quote
                links.append(f"[find on Bandcamp](https://bandcamp.com/search?q={quote(artist + ' ' + title)})")
        if links:
            lines.append(" · ".join(links))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@app.route("/api/set/<int:sid>/radio/tracklist")
def api_radio_tracklist(sid):
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s:
        return jsonify({"error": "not found"}), 404
    rows = _radio_set_rows(sid)
    if not rows:
        return jsonify({"error": "no tracks in this show yet"}), 400
    return jsonify({"ok": True, "markdown": _radio_tracklist_md(s, rows)})


@app.route("/api/set/<int:sid>/radio/export-folder", methods=["POST"])
def api_radio_export_folder(sid):
    """Everything the show needs, in ONE folder: the audio as numbered COPIES
    (originals never move — the library stays exactly as organised), a
    playlist file pointing at the copies, the published tracklist, the
    script, and the research + links. Take the folder to rekordbox, a DAW,
    anywhere."""
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s:
        return jsonify({"error": "not found"}), 404
    rows = _radio_set_rows(sid)
    if not rows:
        return jsonify({"error": "no tracks in this show yet"}), 400
    d = request.get_json() or {}
    dest = (d.get("dest") or "").strip()
    if dest:
        base, err = _export_dest_guard(dest)
        if err:
            return jsonify({"error": err}), 400
    else:
        base = get_export_dir()
    out_dir = base / _usb_clean(f"{s['name']} — radio show")
    audio_dir = out_dir / "audio"
    try:
        audio_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"could not create folder: {e}"}), 500
    copied = missing = 0
    missing_names, m3u, entries, used = [], ["#EXTM3U"], [], set()
    for i, t in enumerate(rows, start=1):
        src = _resolve_audio_path(t.get("file_path")) if t.get("file_path") else None
        if not src or not os.path.exists(src):
            missing += 1
            if len(missing_names) < 8:
                missing_names.append(_radio_display_name(t))
            continue
        ext = os.path.splitext(src)[1] or ""
        fbase = _usb_clean(f"{i:02d} {_radio_display_name(t)}")[:200] + ext
        if fbase.lower() in used:
            b, e = os.path.splitext(fbase); fbase = f"{b} ({t['id']}){e}"
        used.add(fbase.lower())
        dst = audio_dir / fbase
        try:
            if not (dst.exists() and dst.stat().st_size == os.path.getsize(src)):
                shutil.copy2(src, dst)          # copy only, idempotent — never a move
            m3u.append(f"#EXTINF:{int(t.get('duration_sec') or 0)},{_radio_display_name(t)}")
            m3u.append(f"audio/{fbase}")
            entries.append({"id": t["id"], "file": f"audio/{fbase}"})
            copied += 1
        except Exception:
            missing += 1
    try:
        (out_dir / "playlist.m3u8").write_text("\n".join(m3u) + "\n", encoding="utf-8")
        (out_dir / "tracklist.md").write_text(_radio_tracklist_md(s, rows), encoding="utf-8")
        if (s.get("script") or "").strip():
            (out_dir / "script.md").write_text(f"# {s['name']} — script\n\n{s['script'].strip()}\n",
                                               encoding="utf-8")
        research_bits = []
        if (s.get("research") or "").strip():
            research_bits.append(s["research"].strip())
        try:
            links = _json.loads(s.get("show_links") or "[]")
        except Exception:
            links = []
        if links:
            research_bits.append("\n".join(
                f"- [{(l.get('title') or l.get('url'))}]({l.get('url')})"
                for l in links if isinstance(l, dict) and l.get("url")))
        if research_bits:
            (out_dir / "research.md").write_text(
                f"# {s['name']} — research\n\n" + "\n\n".join(research_bits) + "\n",
                encoding="utf-8")
        with open(out_dir / ".musicroom.json", "w", encoding="utf-8") as f:
            _json.dump({"name": s["name"], "kind": "radio_show", "set_id": sid,
                        "tracks": entries}, f, indent=1)
    except Exception as e:
        return jsonify({"error": f"could not write the show papers: {e}"}), 500
    return jsonify({"ok": True, "folder": str(out_dir), "copied": copied,
                    "missing": missing, "missing_names": missing_names})


# =============================================================
# API — REKORDBOX XML EXPORT
# =============================================================

def _rb_location(fp):
    """rekordbox/Engine <TRACK Location> — a percent-encoded file URL.
    Raw paths with spaces or accents (e.g. '…/EXTERNAL HARD DRIVE 1/Music
    Room Library/…') import as 'missing/relocate' in both rekordbox and
    Engine DJ; encoding (space→%20, keep '/') makes them resolve."""
    if not fp:
        return ""
    import urllib.parse
    return "file://localhost" + urllib.parse.quote(fp)


def _rekordbox_xml(tracks, collection_name):
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="Music Room", Version="1.0", Company="Music Room")
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(tracks)))
    seen, pl_ids = set(), []
    for t in tracks:
        tid = str(t["id"])
        if tid in seen: continue
        seen.add(tid); pl_ids.append(tid)
        fp  = t.get("file_path") or ""
        loc = _rb_location(fp)
        bpm = f"{float(t['bpm']):.2f}" if t.get("bpm") else ""
        key_full = f"{t.get('key_note')} {t.get('key_scale')}" if t.get("key_note") else ""
        key = CAMELOT_MAP.get(key_full, t.get("key_note") or "") if key_full else ""
        el = ET.SubElement(coll, "TRACK",
            TrackID=tid, Name=t.get("title") or "", Artist=t.get("artist_name") or "",
            Album=t.get("album") or "", Year=str(t.get("year") or ""),
            Genre="", Kind="", Size=str(t.get("file_size_mb") or ""),
            TotalTime=str(int(t.get("duration_sec") or 0)),
            DiscNumber="", TrackNumber=str(t.get("track_number") or ""),
            BitRate=str(t.get("bitrate_kbps") or ""), SampleRate=str(t.get("sample_rate") or ""),
            Location=loc, Remixer="", Tonality=key, Label=t.get("label") or "", Mix="")
        if bpm: ET.SubElement(el, "TEMPO", Bpm=bpm, Metro="4/4", Battito="1")
        for note in t.get("notes", []):
            if note.get("audio_timestamp"):
                parts = note["audio_timestamp"].split(":")
                try:
                    ms = (int(parts[0])*60+int(parts[1]))*1000 if len(parts)==2 else (int(parts[0])*3600+int(parts[1])*60+int(parts[2]))*1000
                except Exception:
                    ms = 0
                ET.SubElement(el, "POSITION_MARK", Name=note.get("note_text","")[:64], Type="0", Start=f"{ms/1000:.3f}", Num="-1")
    plists = ET.SubElement(root, "PLAYLISTS")
    rn     = ET.SubElement(plists, "NODE", Type="0", Name="ROOT", Count="1")
    pln    = ET.SubElement(rn, "NODE", Type="1", Name=collection_name, Entries=str(len(pl_ids)), KeyType="0")
    for tid in pl_ids: ET.SubElement(pln, "TRACK", Key=tid)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


@app.route("/api/rekordbox/open", methods=["POST"])
def rekordbox_open():
    """Bring Rekordbox to the front (or open it) and return track metadata
    so the frontend can display what to load onto the decks manually.
    We don't pass file paths to 'open -a' — Rekordbox's document handler
    imports files but doesn't load them onto decks, and it fails silently
    when already in Performance mode."""
    data = request.get_json() or {}
    file_ids = data.get("file_ids", [])

    tracks_info = []
    for fid in file_ids:
        row = query_one(
            """SELECT t.title, t.artist_name, f.bpm, f.key_note, f.key_scale, f.file_path
               FROM tracks t JOIN files f ON f.track_id = t.id
               WHERE f.id = ? AND f.is_deleted = 0""",
            (int(fid),)
        )
        if row:
            tracks_info.append({
                "title":  row["title"] or "Untitled",
                "artist": row["artist_name"] or "Unknown",
                "bpm":    round(row["bpm"]) if row["bpm"] else None,
                "key":    f'{row["key_note"]} {row["key_scale"]}' if row["key_note"] else None,
            })

    try:
        result = subprocess.run(
            ["open", "-a", "rekordbox"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            if "unable to find" in err.lower() or "not found" in err.lower():
                return jsonify({"error": "Rekordbox not found — is it installed?"}), 404
            return jsonify({"error": err or "Failed to open Rekordbox"}), 500
        return jsonify({"ok": True, "tracks": tracks_info})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out launching Rekordbox"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reveal_in_finder", methods=["POST"])
def reveal_in_finder():
    """Reveal a single file in Finder (used per-slot in the Decks panel)."""
    data = request.get_json() or {}
    file_id = data.get("file_id")
    if not file_id:
        return jsonify({"error": "file_id required"}), 400
    row = query_one("SELECT file_path FROM files WHERE id=? AND is_deleted=0", (int(file_id),))
    if not row:
        return jsonify({"error": "file not found"}), 404
    fp = _resolve_audio_path(row["file_path"])
    if not fp:
        return jsonify({"error": "file not on disk"}), 404
    safe = fp.replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript",
             "-e", f'tell application "Finder" to reveal POSIX file "{safe}"',
             "-e", 'tell application "Finder" to activate'],
            capture_output=True, text=True, timeout=10
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reveal_files_in_finder", methods=["POST"])
def reveal_files_in_finder():
    """Reveal multiple files in Finder simultaneously (all appear selected).
    AppleScript's reveal command accepts a list — the user can then drag
    the whole selection into Rekordbox decks."""
    data = request.get_json() or {}
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "file_ids required"}), 400

    rows = [query_one("SELECT file_path FROM files WHERE id=? AND is_deleted=0", (int(fid),))
            for fid in file_ids]
    paths = [r["file_path"] for r in rows if r and os.path.exists(r["file_path"])]
    if not paths:
        return jsonify({"error": "no files found on disk"}), 404

    # Build: tell application "Finder" to reveal {POSIX file "p1", POSIX file "p2", ...}
    posix_list = ", ".join(f'POSIX file "{p.replace(chr(34), chr(92)+chr(34))}"' for p in paths)
    try:
        subprocess.run(
            ["osascript",
             "-e", f'tell application "Finder" to reveal {{{posix_list}}}',
             "-e", 'tell application "Finder" to activate'],
            capture_output=True, text=True, timeout=10
        )
        # Open Rekordbox non-blocking so it launches alongside Finder
        try:
            subprocess.Popen(["open", "-a", "Rekordbox"])
        except Exception:
            pass
        return jsonify({"ok": True, "count": len(paths)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/track/<int:tid>/open-in-studio", methods=["POST"])
def api_open_in_studio(tid):
    """Open Ableton Live and reveal the track in Finder so the user can drag it onto a lane.

    Why not auto-load into an audio track: Ableton's macOS `open` handler
    only imports the file when launching cold; once Live is already running
    the open command just brings the app forward without touching the
    session. There's no public AppleScript/OSC verb to drop a file onto a
    clip slot. So we do the next best thing: bring Ableton forward and
    open Finder with the file selected — one drag and it's on a lane.
    """
    f = query_one(
        "SELECT file_path FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0",
        (tid,)
    )
    if not f or not f["file_path"]:
        return jsonify({"error": "no file"}), 404
    fp_studio = _resolve_audio_path(f["file_path"])
    if not fp_studio:
        return jsonify({"error": "file not on disk"}), 404

    try:
        # 1. Launch / activate Ableton (uses bundle id — version-independent)
        subprocess.run(
            ["osascript", "-e", 'tell application id "com.ableton.live" to activate'],
            capture_output=True, timeout=10
        )
        # 2. Reveal the file in Finder (selected in its parent folder)
        subprocess.Popen(["open", "-R", fp_studio])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================
# STUDIO — the artist's own productions
# A track can be your own work, linked to its DAW project; each version (file)
# remembers the session it was bounced from. We only ever LINK — never move,
# rename or parse an artist's project files (their most precious, irreplaceable
# work). Bounces play and get notes/threads like any track; sessions open in the
# DAW. See daw_projects.py for the scan convention.
# =============================================================

@app.route("/api/studio/scan", methods=["POST"])
def api_studio_scan():
    """Preview: what tracks/versions/sessions are in a productions folder.
    Touches nothing on disk beyond reading the directory."""
    import daw_projects
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Folder not found."}), 400
    projects = daw_projects.scan_productions(path)
    out = []
    for p in projects:
        out.append({
            "name": p["name"], "folder": p["folder"],
            "sessions": p["sessions"], "bounces": p["bounces"],
            "primary_session": p.get("primary_session"),
            "n_sessions": len(p["sessions"]), "n_bounces": len(p["bounces"]),
        })
    return jsonify({"projects": out, "total": len(out)})


def _studio_album_for(path):
    """The album name = the folder you pointed at, when it's a parent of track
    folders. A single standalone project has no album."""
    import daw_projects
    projects = daw_projects.scan_productions(path)
    if len(projects) == 1 and os.path.abspath(projects[0]["folder"]) == os.path.abspath(path):
        return None
    return os.path.basename(path.rstrip("/")) or None


def _studio_import_path(path, album=None):
    """Link the productions under `path` by REFERENCE — never moving/copying a
    file. Each track folder → one own-work track; each .als + its same-named
    bounce → a version (the file remembers exactly which session it came from)."""
    import daw_projects
    projects = daw_projects.scan_productions(path)
    well = query_one("SELECT id FROM places WHERE is_well=1")
    well_id = well["id"] if well else None
    # An album is a place (a Studio project). Find-or-create it.
    album_place_id = None
    if album:
        ap = query_one("SELECT id FROM places WHERE name=? AND is_studio_project=1", (album,))
        album_place_id = ap["id"] if ap else execute(
            "INSERT INTO places (name, is_studio_project, colour_primary, illustration_kind) VALUES (?,1,?,?)",
            (album, "#6d6aa0", "headphones"))
    tracks_created = files_created = 0
    for p in projects:
        versions = p.get("versions") or [{"bounce": b, "session": None} for b in (p.get("bounces") or [])]
        versions = [v for v in versions
                    if not query_one("SELECT 1 FROM files WHERE file_path=? AND is_deleted=0", (v["bounce"],))]
        if not versions:
            continue   # nothing new (session-only project, or every version already in)
        primary = p.get("primary_session") or (p["sessions"][0] if p.get("sessions") else None)
        # the current version (its session is the primary set) is the primary file
        prim_idx = next((i for i, v in enumerate(versions) if v.get("session") and v["session"] == primary), 0)
        tid = execute(
            "INSERT INTO tracks (title, album, artist_name, source_type, is_own_work, "
            "daw_project_path, daw_type, dsp_check_state, has_artist, is_deleted, date_added) "
            "VALUES (?,?,?,?,1,?,?, 'skipped', 0, 0, datetime('now'))",
            (p["name"], album, None, "own", (primary or p["folder"]), "ableton"))
        tracks_created += 1
        if well_id:
            execute("INSERT OR IGNORE INTO place_tracks (place_id, track_id) VALUES (?,?)", (well_id, tid))
        if album_place_id:
            execute("INSERT OR IGNORE INTO place_tracks (place_id, track_id) VALUES (?,?)", (album_place_id, tid))
        for i, v in enumerate(versions):
            b = v["bounce"]
            execute(
                "INSERT INTO files (track_id, file_path, file_format, is_primary, "
                "session_path, is_deleted, date_added) VALUES (?,?,?,?,?,0,datetime('now'))",
                (tid, b, os.path.splitext(b)[1].lower().lstrip("."),
                 1 if i == prim_idx else 0, v.get("session") or primary))
            files_created += 1
    return {"projects": len(projects), "tracks_created": tracks_created,
            "files_created": files_created, "album": album, "album_place_id": album_place_id}


@app.route("/api/studio/import", methods=["POST"])
def api_studio_import():
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Folder not found."}), 400
    return jsonify({"ok": True, **_studio_import_path(path, _studio_album_for(path))})


@app.route("/api/studio/organise", methods=["POST"])
def api_studio_organise():
    """Tidy a messy folder into the convention at `dest`, then import it. COPY by
    default (originals untouched); MOVE frees the source. Whole project folders
    move as a unit so Ableton references never break."""
    import daw_projects
    d = request.get_json() or {}
    source = (d.get("source") or "").strip()
    dest   = (d.get("dest") or "").strip()
    mode   = (d.get("mode") or "copy").strip().lower()
    if mode not in ("copy", "move"):
        mode = "copy"
    if not source or not os.path.isdir(source):
        return jsonify({"error": "Source folder not found."}), 400
    if not dest:
        try:
            base = get_library_dest()
        except Exception:
            base = None
        dest = os.path.join(base, "Music Room Productions") if base else (source.rstrip("/") + " (tidied)")
    res = daw_projects.organise_productions(source, dest, mode)
    if res.get("error"):
        return jsonify(res), 400
    res.update(_studio_import_path(dest, _studio_album_for(dest)))   # link the tidy result
    return jsonify({"ok": True, **res})


@app.route("/studio")
def studio_page():
    return render_template("studio.html")


@app.route("/api/studio/albums")
def api_studio_albums():
    """The album-projects (own-work places) for the Studio screen."""
    rows = query("""
        SELECT p.id, p.name, p.colour_primary,
               (SELECT COUNT(*) FROM place_tracks pt JOIN tracks t ON t.id = pt.track_id
                WHERE pt.place_id = p.id AND t.is_deleted = 0) AS track_count
        FROM places p WHERE p.is_studio_project = 1
        ORDER BY p.name COLLATE NOCASE
    """)
    return jsonify({"albums": [dict(r) for r in rows]})


@app.route("/api/track/<int:tid>/own-work", methods=["POST"])
def api_set_own_work(tid):
    """Mark a track as your own work and/or link it to a DAW project."""
    d = request.get_json() or {}
    sets, vals = [], []
    if "is_own_work" in d:
        sets.append("is_own_work=?"); vals.append(1 if d["is_own_work"] else 0)
    if "daw_project_path" in d:
        sets.append("daw_project_path=?"); vals.append((d.get("daw_project_path") or "").strip() or None)
    if "daw_type" in d:
        sets.append("daw_type=?"); vals.append((d.get("daw_type") or "").strip() or None)
    if not sets:
        return jsonify({"error": "no fields"}), 400
    vals.append(tid)
    execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/file/<int:fid>/session", methods=["POST"])
def api_set_file_session(fid):
    """Link a version (file) to the DAW session it was bounced from."""
    d = request.get_json() or {}
    sp = (d.get("session_path") or "").strip() or None
    execute("UPDATE files SET session_path=? WHERE id=?", (sp, fid))
    return jsonify({"ok": True})


@app.route("/api/open-daw", methods=["POST"])
def api_open_daw():
    """Open a session / project / folder. An .als launches in Ableton; a folder
    opens in Finder. We only ever open what the user explicitly linked."""
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "Not found on disk."}), 404
    try:
        if path.lower().endswith(".als"):
            subprocess.run(["osascript", "-e", 'tell application id "com.ableton.live" to activate'],
                           capture_output=True, timeout=10)
        subprocess.Popen(["open", path])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================
# BOSCA — persistent DJ crate
# One global box carried between places. Notes are scoped to
# the bosca and carry through to the set on conversion.
# =============================================================

@app.route("/api/bosca")
def api_bosca():
    if not _table_exists("bosca"):
        return jsonify({"tracks": [], "count": 0})
    tracks = query("""
        SELECT b.id as bosca_entry_id, b.track_id, b.position, b.note, b.added_at,
               t.title, t.artist_name, t.album, t.year,
               f.id as file_id, f.file_format, f.duration_sec, f.bpm, f.key_note, f.key_scale
        FROM bosca b JOIN tracks t ON t.id = b.track_id
        LEFT JOIN files f ON f.track_id = b.track_id AND f.is_primary = 1
        WHERE t.is_deleted = 0
        ORDER BY b.position ASC, b.added_at ASC
    """)
    return jsonify({"tracks": tracks, "count": len(tracks)})


@app.route("/api/bosca/tracks", methods=["POST"])
def api_bosca_add():
    if not _table_exists("bosca"):
        return jsonify({"error": "run migrate.py first"}), 500
    d = request.get_json() or {}
    track_ids = d.get("track_ids") or ([d["track_id"]] if d.get("track_id") else [])
    if not track_ids:
        return jsonify({"error": "track_ids required"}), 400
    maxp = (query_one("SELECT COALESCE(MAX(position),0) as m FROM bosca") or {"m": 0})["m"]
    added = 0
    for i, tid in enumerate(track_ids):
        try:
            execute("INSERT OR IGNORE INTO bosca (track_id, position) VALUES (?,?)",
                    (int(tid), maxp + i + 1))
            added += 1
        except Exception:
            pass
    return jsonify({"ok": True, "added": added}), 201


@app.route("/api/bosca/track/<int:track_id>", methods=["DELETE"])
def api_bosca_remove(track_id):
    if _table_exists("bosca"):
        execute("DELETE FROM bosca WHERE track_id=?", (track_id,))
    return jsonify({"ok": True})


@app.route("/api/bosca/reorder", methods=["POST"])
def api_bosca_reorder():
    if not _table_exists("bosca"):
        return jsonify({"error": "run migrate.py first"}), 500
    d = request.get_json() or {}
    order = d.get("order") or []
    if not isinstance(order, list):
        return jsonify({"error": "order must be a list of track_ids"}), 400
    for pos, tid in enumerate(order):
        try:
            execute("UPDATE bosca SET position=? WHERE track_id=?", (pos, int(tid)))
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/bosca/track/<int:track_id>/note", methods=["POST"])
def api_bosca_note(track_id):
    if not _table_exists("bosca"):
        return jsonify({"error": "run migrate.py first"}), 500
    d = request.get_json() or {}
    note = (d.get("note") or "").strip() or None
    execute("UPDATE bosca SET note=? WHERE track_id=?", (note, int(track_id)))
    return jsonify({"ok": True})


@app.route("/api/bosca/to-set", methods=["POST"])
def api_bosca_to_set():
    if not _table_exists("bosca"):
        return jsonify({"error": "run migrate.py first"}), 500
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    tracks = query("""
        SELECT b.track_id, b.note, b.position
        FROM bosca b JOIN tracks t ON t.id = b.track_id
        WHERE t.is_deleted = 0
        ORDER BY b.position ASC, b.added_at ASC
    """)
    if not tracks:
        return jsonify({"error": "box is empty"}), 400
    sid = execute(
        "INSERT INTO sets (name, created_at, updated_at) VALUES (?,datetime('now'),datetime('now'))",
        (name,)
    )
    for i, bt in enumerate(tracks):
        execute(
            "INSERT INTO set_tracks (set_id, track_id, position, transition_note) VALUES (?,?,?,?)",
            (sid, bt["track_id"], i + 1, bt["note"])
        )
    if d.get("clear", True):
        execute("DELETE FROM bosca")
    return jsonify({"ok": True, "set_id": sid, "set_name": name, "track_count": len(tracks)}), 201


@app.route("/api/bosca", methods=["DELETE"])
def api_bosca_clear():
    if _table_exists("bosca"):
        execute("DELETE FROM bosca")
    return jsonify({"ok": True})


@app.route("/api/place/<int:pid>/export/rekordbox")
def api_export_place_rekordbox(pid):
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404
    tracks = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               f.file_path,f.file_format,f.duration_sec,f.bpm,f.key_note,f.key_scale,
               f.file_size_mb,f.bitrate_kbps,f.sample_rate
        FROM tracks t JOIN place_tracks pt ON pt.track_id=t.id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE pt.place_id=? AND t.is_deleted=0 ORDER BY t.artist_name,t.title
    """, (pid,))
    for t in tracks: t["notes"] = query("SELECT * FROM notes WHERE track_id=? ORDER BY created_at", (t["id"],))
    xml  = _rekordbox_xml(tracks, place["name"])
    safe = "".join(c for c in place["name"] if c.isalnum() or c in " -_").strip()
    fname = f'musicroom_{safe.replace(" ","_")}.xml'
    # ?save=1 → write into the exports folder and answer JSON. The native
    # WKWebView can't handle attachment downloads (an <a download> click just
    # navigates to the raw XML = a blank screen), so the app UI uses this.
    if request.args.get("save"):
        out = EXPORTS_DIR / fname
        out.write_text(xml, encoding="utf-8")
        return jsonify({"ok": True, "path": str(out), "filename": fname, "tracks": len(tracks)})
    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.route("/api/set/<int:sid>/export/rekordbox")
def api_export_set_rekordbox(sid):
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404
    tracks = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               f.file_path,f.file_format,f.duration_sec,f.bpm,f.key_note,f.key_scale,
               f.file_size_mb,f.bitrate_kbps,f.sample_rate
        FROM set_tracks st JOIN tracks t ON t.id=st.track_id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE st.set_id=? AND t.is_deleted=0 ORDER BY st.position
    """, (sid,))
    for t in tracks: t["notes"] = query("SELECT * FROM notes WHERE track_id=? ORDER BY created_at", (t["id"],))
    xml  = _rekordbox_xml(tracks, s["name"])
    safe = "".join(c for c in s["name"] if c.isalnum() or c in " -_").strip()
    fname = f'musicroom_set_{safe.replace(" ","_")}.xml'
    if request.args.get("save"):                   # see the place route: webview-safe path
        out = EXPORTS_DIR / fname
        out.write_text(xml, encoding="utf-8")
        return jsonify({"ok": True, "path": str(out), "filename": fname, "tracks": len(tracks)})
    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _rekordbox_xml_full(all_tracks, playlists):
    """Whole-library rekordbox XML: every track in COLLECTION once, every
    place as its own playlist NODE. Import this one file into rekordbox (→ club
    USB) or Engine Desktop (→ exFAT stick for the SC Live 4) and you get the
    full collection + all your crates, with BPM, Camelot key and cue points.
    `playlists` = [{"name": str, "track_ids": [int, …]}]."""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="Music Room", Version="1.0", Company="Music Room")
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(all_tracks)))
    for t in all_tracks:
        tid = str(t["id"])
        # BPM + key are deliberately NOT exported: rekordbox's own analysis is
        # better, and seeding it with ours (which can be a metrical-error octave
        # off) just makes work. We send no TEMPO and a blank Tonality so rekordbox
        # analyses clean; its results come back into the Music Room as a layer.
        # genres → Genre; tags (incl. the energy/black-white-grey colour system)
        # → Comments, so the user's whole tagging vocabulary travels and is searchable.
        el = ET.SubElement(coll, "TRACK",
            TrackID=tid, Name=t.get("title") or "", Artist=t.get("artist_name") or "",
            Album=t.get("album") or "", Year=str(t.get("year") or ""),
            Genre=t.get("genre_csv") or "", Kind="", Size=str(t.get("file_size_mb") or ""),
            TotalTime=str(int(t.get("duration_sec") or 0)),
            DiscNumber="", TrackNumber=str(t.get("track_number") or ""),
            BitRate=str(t.get("bitrate_kbps") or ""), SampleRate=str(t.get("sample_rate") or ""),
            Comments=t.get("tag_csv") or "",
            Location=_rb_location(t.get("file_path") or ""),
            Remixer="", Tonality="", Label=t.get("label") or "", Mix="")
        for note in t.get("notes", []):
            if note.get("audio_timestamp"):
                parts = note["audio_timestamp"].split(":")
                try:
                    ms = (int(parts[0])*60+int(parts[1]))*1000 if len(parts) == 2 \
                         else (int(parts[0])*3600+int(parts[1])*60+int(parts[2]))*1000
                except Exception:
                    ms = 0
                ET.SubElement(el, "POSITION_MARK", Name=note.get("note_text", "")[:64],
                              Type="0", Start=f"{ms/1000:.3f}", Num="-1")
    plists = ET.SubElement(root, "PLAYLISTS")
    rn = ET.SubElement(plists, "NODE", Type="0", Name="ROOT", Count=str(len(playlists)))
    for g in playlists:
        pln = ET.SubElement(rn, "NODE", Type="1", Name=g["name"], KeyType="0",
                            Entries=str(len(g["track_ids"])))
        for tid in g["track_ids"]:
            ET.SubElement(pln, "TRACK", Key=str(tid))
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ── size intelligence: what a place / pile / selection WEIGHS ──────────────
# The USB question starts here: before anything can be "filled to the stick",
# the curator needs to see GB, hours, format makeup and count at a glance.
def _size_stats_for(sel_sql, params):
    """Aggregate primary-file size/duration/format for the track ids produced
    by sel_sql (one column: track_id). DB-stored sizes — what an export would
    copy, without touching the drive."""
    rows = query(f"""
        SELECT LOWER(COALESCE(f.file_format,'')) AS fmt, COUNT(*) AS n,
               SUM(f.file_size_mb) AS mb, SUM(f.duration_sec) AS sec
        FROM ({sel_sql}) sel
        JOIN tracks t ON t.id = sel.track_id AND t.is_deleted = 0
        LEFT JOIN files f ON f.track_id = t.id AND f.is_primary = 1 AND f.is_deleted = 0
        GROUP BY fmt ORDER BY mb DESC
    """, params)
    total_mb  = sum(r["mb"] or 0 for r in rows)
    total_sec = sum(r["sec"] or 0 for r in rows)
    no_file   = sum(r["n"] for r in rows if not r["fmt"])
    formats   = [{"format": r["fmt"], "n": r["n"],
                  "gb": round((r["mb"] or 0) / 1024, 2),
                  "hours": round((r["sec"] or 0) / 3600, 1)} for r in rows if r["fmt"]]
    return {"tracks": sum(r["n"] for r in rows), "no_file": no_file,
            "gb": round(total_mb / 1024, 2), "seconds": int(total_sec),
            "hours": round(total_sec / 3600, 1), "formats": formats}


@app.route("/api/place/<int:pid>/stats")
def api_place_stats(pid):
    return jsonify(_size_stats_for(
        "SELECT track_id FROM place_tracks WHERE place_id = ?", (pid,)))


@app.route("/api/room/pile/<int:pile_id>/stats")
def api_pile_stats(pile_id):
    return jsonify(_size_stats_for(
        "SELECT track_id FROM room_pile_tracks WHERE pile_id = ?", (pile_id,)))


@app.route("/api/tracks/size-stats", methods=["POST"])
def api_tracks_size_stats():
    """Stats for an arbitrary selection (the room's marquee, a fill gauge)."""
    ids = [int(i) for i in (request.get_json() or {}).get("track_ids", []) if i]
    return jsonify(_size_stats_for(
        "SELECT value AS track_id FROM json_each(?)", (_json.dumps(ids),)))


def _library_export_tracks(include_hidden=False):
    """The exportable library: tracks whose audio is actually on disk.
    By default HIDDEN tracks stay home — same definition as the 3D room's
    default view: no title (samples / unknowns) or a recorded set. Returns
    (present_tracks, skipped_missing)."""
    rows = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               t.colour_code,
               COALESCE(t.is_set_recording,0) AS is_set_recording,
               f.file_path,f.duration_sec,f.bpm,f.key_note,f.key_scale,
               f.file_size_mb,f.bitrate_kbps,f.sample_rate,
               (SELECT GROUP_CONCAT(g.name, ', ')
                  FROM track_genres trg JOIN genres g ON g.id = trg.genre_id
                  WHERE trg.track_id = t.id) AS genre_csv,
               (SELECT GROUP_CONCAT(tg.name, ', ')
                  FROM track_tags tt JOIN tags tg ON tg.id = tt.tag_id
                  WHERE tt.track_id = t.id) AS tag_csv
        FROM tracks t
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE t.is_deleted=0 AND f.file_path IS NOT NULL
              AND COALESCE(t.is_recording,0)=0   -- recordings are not library tracks
              AND t.gr_borrow_id IS NULL         -- borrowed records never leave the app
        ORDER BY t.artist_name,t.title
    """)
    present, skipped = [], 0
    for t in rows:
        if not include_hidden and (not (t.get("title") or "").strip() or t["is_set_recording"]):
            continue
        p = _resolve_audio_path(t.get("file_path"))
        if p and os.path.exists(p):
            t["file_path"] = p
            present.append(t)
        else:
            skipped += 1
    return present, skipped


def _xml_for_tracks(present, cue_track_ids=None):
    """Per-place/structure playlists + the rekordbox XML for an already-resolved
    track list. Memory cues (from TIMESTAMPED NOTES) are written ONLY for tracks
    in cue_track_ids — the export-time set of not-yet-seeded tracks — so a track's
    cues reach rekordbox at most once and re-exports never clobber grid work.
    cue_track_ids=None → no cues at all (the safe, repeatable default)."""
    have = {t["id"] for t in present}
    from collections import defaultdict
    cues = defaultdict(list)
    if cue_track_ids:
        ids = [i for i in cue_track_ids if i in have]
        if ids:
            ph = ",".join("?" * len(ids))
            for r in query(f"SELECT track_id, note_text, audio_timestamp FROM notes "
                           f"WHERE audio_timestamp IS NOT NULL AND audio_timestamp != '' "
                           f"AND track_id IN ({ph})", ids):
                cues[r["track_id"]].append({"note_text": r["note_text"],
                                            "audio_timestamp": r["audio_timestamp"]})
            for r in _deep_listen_cue_rows(ids):        # deep-listen moments seed too
                cues[r["track_id"]].append({"note_text": r["note_text"],
                                            "audio_timestamp": r["audio_timestamp"]})
    for t in present:
        t["notes"] = cues.get(t["id"], [])
    playlists = []
    for pl in query("SELECT id, name FROM places ORDER BY is_well DESC, name"):
        ids = [r["track_id"] for r in
               query("SELECT track_id FROM place_tracks WHERE place_id=? ORDER BY added_at", (pl["id"],))
               if r["track_id"] in have]
        if ids:
            playlists.append({"name": pl["name"], "track_ids": ids})
    # piles are playlists too — a pile built to fit a stick should arrive in
    # rekordbox/Engine ready to export
    for pl in query("SELECT id, name FROM room_piles ORDER BY name"):
        ids = [r["track_id"] for r in
               query("SELECT track_id FROM room_pile_tracks WHERE pile_id=? ORDER BY added_at", (pl["id"],))
               if r["track_id"] in have]
        if ids:
            playlists.append({"name": pl["name"], "track_ids": ids})
    # structures — each node (and axis-cross) becomes a playlist, so a saved
    # spatial arrangement arrives in rekordbox as a set of crates ("<structure> · <node>")
    for s in query("SELECT id, name FROM structures ORDER BY name"):
        playlists.extend(_structure_playlists(s["id"], s["name"], have))
    # Set-prep sets — each becomes a playlist in the set's running order, so a set
    # you prepped lands in rekordbox ready to play ("Set · <name>"). Charted sets
    # stay out (they're live performance plans / per-set exports).
    for st in query("SELECT id, name FROM sets WHERE COALESCE(set_type,'charted') != 'charted' ORDER BY name"):
        ids, seen = [], set()
        for r in query("SELECT track_id FROM set_tracks WHERE set_id=? ORDER BY position", (st["id"],)):
            tid = r["track_id"]
            if tid in have and tid not in seen:
                seen.add(tid); ids.append(tid)
        if ids:
            playlists.append({"name": f"Set · {st['name']}", "track_ids": ids})
    return _rekordbox_xml_full(present, playlists)


def _build_library_xml(include_hidden=False, seed_cues=False, seed_all=False):
    """Build the whole-library rekordbox XML (non-hidden tracks by default).
    If seed_cues, memory cues are written for tracks with timestamped notes and
    those tracks are stamped so a later export never re-asserts them:
      - seed_all=False → only NOT-yet-seeded tracks (safe; won't clobber cues
        you've adjusted in rekordbox).
      - seed_all=True  → EVERY track with notes (for building a fresh rekordbox
        collection); they're then all stamped, so edits are protected afterwards.
    Returns (xml_string, present_count, skipped_count, seeded_track_ids)."""
    present, skipped = _library_export_tracks(include_hidden)
    cue_ids = None
    if seed_cues:
        present_ids = [t["id"] for t in present]
        if present_ids:
            ph = ",".join("?" * len(present_ids))
            guard = "" if seed_all else "AND t.cues_seeded_at IS NULL "
            cue_ids = {r["id"] for r in query(
                f"SELECT t.id FROM tracks t WHERE t.id IN ({ph}) {guard}"
                f"AND (EXISTS (SELECT 1 FROM notes n WHERE n.track_id=t.id "
                f"             AND n.audio_timestamp IS NOT NULL AND n.audio_timestamp != '') "
                f"  OR EXISTS (SELECT 1 FROM listening_notes ln WHERE ln.track_id=t.id "
                f"             AND ln.kind='moment' AND ln.timestamp_sec > 0 AND ln.note_text != ''))",
                present_ids)}
    xml = _xml_for_tracks(present, cue_track_ids=cue_ids)
    return xml, len(present), skipped, (cue_ids or set())


# ── USB export: the files AND a self-contained rekordbox XML on the stick ──
# The pipeline this serves (research/01): Music Room → rekordbox XML →
# rekordbox → USB → Engine DJ → SC Live 4. The stick gets a flat
# "Music Room" folder (Denon-browsable in folder mode) plus a
# musicroom_library.xml whose Locations point AT the stick — import that
# one file into rekordbox/Engine on any laptop and the whole collection
# arrives with BPM, key and cue points.
_usb_progress = {"running": False, "done": 0, "total": 0, "copied_gb": 0.0,
                 "failed": 0, "current": "", "xml_path": "", "stop_reason": None,
                 "finished": None}

_ILLEGAL_FS = '\\/:*?"<>|'


def _usb_clean(name):
    return "".join("_" if c in _ILLEGAL_FS else c for c in name).strip().strip(".") or "track"


def _run_usb_export(volume, include_hidden=False):
    import datetime as _dt
    global _usb_progress
    _usb_progress = {"running": True, "done": 0, "total": 0, "copied_gb": 0.0,
                     "failed": 0, "current": "", "xml_path": "", "stop_reason": None,
                     "finished": None}
    try:
      with app.app_context():                       # query() is g-bound — give the thread a context
        present, _ = _library_export_tracks(include_hidden)
        dest_dir = os.path.join(volume, "Music Room")
        os.makedirs(dest_dir, exist_ok=True)
        _usb_progress["total"] = len(present)
        used = set()
        for t in present:
            if not _usb_progress["running"]:
                _usb_progress["stop_reason"] = "Cancelled — everything copied so far stays on the stick."
                break
            src = t["file_path"]
            ext = os.path.splitext(src)[1] or ""
            artist, title = (t.get("artist_name") or "").strip(), (t.get("title") or "").strip()
            base = (_usb_clean(f"{artist} - {title}")[:200] + ext) if (artist and title) \
                   else os.path.basename(src)
            if base.lower() in used:
                b, e = os.path.splitext(base)
                base = f"{b} ({t['id']}){e}"
            used.add(base.lower())
            dst = os.path.join(dest_dir, base)
            _usb_progress["current"] = base[:80]
            try:
                if not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src)):
                    shutil.copy2(src, dst)            # resume: size-matched copies skip
                t["file_path"] = dst                  # the XML points at the stick
                t["_on_usb"] = True
                _usb_progress["copied_gb"] += (t.get("file_size_mb") or 0) / 1024
            except Exception:
                _usb_progress["failed"] += 1
            _usb_progress["done"] += 1
        # the stick's XML lists exactly what made it onto the stick — a
        # cancelled run must not reference tracks still back on the drive
        on_stick = [t for t in present if t.get("_on_usb")]
        if on_stick:
            xml = _xml_for_tracks(on_stick)
            xml_path = os.path.join(volume, "musicroom_library.xml")
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write(xml)
            _usb_progress["xml_path"] = xml_path
        try:
            os.sync()                                 # a yank can't lose buffered writes
        except Exception:
            pass
    except Exception as e:
        _usb_progress["stop_reason"] = f"Failed: {type(e).__name__}: {e}"
    finally:
        _usb_progress["running"] = False
        _usb_progress["finished"] = _dt.datetime.utcnow().isoformat()


# ── SCOPED stick export: one place or pile → the stick, sized to fit ───────
# The practice-stick road (SC Live 4 at home): copy the scope's files into
# "Music Room/<name>/" on the stick, keep a manifest beside them, rebuild the
# stick's rekordbox XML and Engine DJ database from EVERY manifest on the
# stick — so several places/piles can live on one stick, each a playlist,
# and re-exports know exactly what to add and what was dropped.

def _scope_name(scope_type, scope_id):
    if scope_type == "place":
        row = query_one("SELECT name FROM places WHERE id=?", (scope_id,))
    elif scope_type == "structure":
        row = query_one("SELECT name FROM structures WHERE id=?", (scope_id,))
    else:
        row = query_one("SELECT name FROM room_piles WHERE id=?", (scope_id,))
    return row["name"] if row else None


def _structure_node_track_ids(sid):
    """Per node: name, group (axis) and the track ids carrying its tag/genre.
    [{name, group, ids}, …] in node order."""
    out = []
    for n in query("""SELECT n.id, n.kind, n.ref_id, n.group_name,
                      CASE n.kind WHEN 'tag' THEN (SELECT name FROM tags WHERE id=n.ref_id)
                                  ELSE (SELECT name FROM genres WHERE id=n.ref_id) END AS name
                      FROM structure_nodes n WHERE n.structure_id=? ORDER BY n.id""", (sid,)):
        if n["kind"] == "tag":
            ids = {r["track_id"] for r in query(
                "SELECT track_id FROM track_tags WHERE tag_id=?", (n["ref_id"],))}
        else:
            ids = {r["track_id"] for r in query(
                "SELECT track_id FROM track_genres WHERE genre_id=?", (n["ref_id"],))}
        out.append({"name": n["name"] or "node", "group": n["group_name"], "ids": ids})
    return out


def _structure_playlists(sid, name, aboard):
    """The structure as playlists, limited to what's aboard the stick:
    one per node, PLUS — when the user has grouped nodes into axes — one per
    CROSS of nodes from different axes ("4 · black"), so standing at energy 4
    on the player the three moods sit side by side. Axis order = alphabetical
    group name, so the naming is stable."""
    nodes = _structure_node_track_ids(sid)
    playlists = []
    for nd in nodes:
        ids = sorted(nd["ids"] & aboard)
        if ids:
            playlists.append({"name": f"{name} · {nd['name']}", "track_ids": ids})
    grouped = [nd for nd in nodes if nd["group"]]
    groups = sorted({nd["group"] for nd in grouped})
    for gi in range(len(groups)):
        for gj in range(gi + 1, len(groups)):
            for a in (nd for nd in grouped if nd["group"] == groups[gi]):
                for b in (nd for nd in grouped if nd["group"] == groups[gj]):
                    ids = sorted(a["ids"] & b["ids"] & aboard)
                    if ids:
                        playlists.append({"name": f"{name} · {a['name']} · {b['name']}",
                                          "track_ids": ids})
    return playlists


def _scope_track_ids(scope_type, scope_id):
    """The raw set of track ids in a scope (place / structure / pile), before
    any presence/hidden filtering."""
    if scope_type == "place":
        return {r["track_id"] for r in query(
            "SELECT track_id FROM place_tracks WHERE place_id=?", (scope_id,))}
    if scope_type == "structure":        # the union of every node's records
        ids = set()
        for nd in _structure_node_track_ids(scope_id):
            ids |= nd["ids"]
        return ids
    return {r["track_id"] for r in query(
        "SELECT track_id FROM room_pile_tracks WHERE pile_id=?", (scope_id,))}


def _scoped_export_tracks(scope_type, scope_id, include_hidden=False):
    """The scope's exportable tracks — same presence/hidden rules as the
    whole-library export, filtered to the place/pile/structure."""
    ids = _scope_track_ids(scope_type, scope_id)
    present, skipped = _library_export_tracks(include_hidden)
    return [t for t in present if t["id"] in ids], skipped


def _missing_on_drive_for_ids(ids, present_ids):
    """Of `ids`, those that WOULD export (titled, not a recorded set, have a
    primary file) but whose audio isn't on this drive — so an export silently
    leaves them off. Returns (count, sample_names); present_ids already resolved."""
    rest = [i for i in ids if i not in present_ids]
    if not rest:
        return 0, []
    ph = ",".join("?" * len(rest))
    rows = query(f"""SELECT t.id, t.title, t.artist_name, f.file_path,
                            COALESCE(t.is_set_recording,0) AS sr
                     FROM tracks t
                     LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0
                     WHERE t.is_deleted=0 AND t.id IN ({ph})""", rest)
    count, names = 0, []
    for r in rows:
        if not r["file_path"]:                       # no file at all — not a "lost from disk" case
            continue
        if not (r["title"] or "").strip() or r["sr"]:  # wouldn't export anyway (hidden)
            continue
        if not _resolve_audio_path(r["file_path"]):
            count += 1
            if len(names) < 8:
                names.append(f"{(r['artist_name'] or '?')} — {r['title']}")
    return count, names


def _scope_missing_on_drive(scope_type, scope_id, present_ids):
    """In-scope tracks whose audio isn't on this drive — so an export leaves them off."""
    return _missing_on_drive_for_ids(_scope_track_ids(scope_type, scope_id), present_ids)


def _usb_scope_dir(volume, name):
    return os.path.join(volume, "Music Room", _usb_clean(name))


def _usb_read_manifests(volume):
    out = []
    root = os.path.join(volume, "Music Room")
    if not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        mf = os.path.join(root, d, ".musicroom.json")
        if os.path.isfile(mf):
            try:
                with open(mf) as f:
                    m = _json.load(f)
                m["_dir"] = os.path.join(root, d)
                out.append(m)
            except Exception:
                pass
    return out


def _tracks_meta_by_ids(ids):
    if not ids:
        return {}
    rows = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               f.duration_sec,f.bpm,f.key_note,f.key_scale,f.file_size_mb,
               f.bitrate_kbps,f.sample_rate,f.file_format,f.has_analysis
        FROM tracks t
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0
        WHERE t.is_deleted=0 AND t.id IN (SELECT value FROM json_each(?))
    """, (_json.dumps(list(ids)),))
    return {r["id"]: r for r in rows}


def _deep_listen_cue_rows(track_ids=None):
    """Éisteacht's deep-listen moments as cue-seed rows, shaped like `notes`
    rows ({track_id, note_text, audio_timestamp}). They live in a separate
    table (listening_notes) and were stranded — never exported as cues, never
    shown on the panel — which broke the annotate→export spine (gap audit)."""
    where = "ln.kind='moment' AND ln.timestamp_sec > 0 AND ln.note_text != '' AND ln.track_id IS NOT NULL"
    args = []
    if track_ids is not None:
        if not track_ids:
            return []
        ph = ",".join("?" * len(track_ids))
        where += f" AND ln.track_id IN ({ph})"
        args = list(track_ids)
    return [{"track_id": r["track_id"], "note_text": r["note_text"],
             "audio_timestamp": _secs_to_ts(r["timestamp_sec"])}
            for r in query(
                f"SELECT ln.track_id, ln.note_text, ln.timestamp_sec "
                f"FROM listening_notes ln WHERE {where}", args)]


def _attach_cues(present):
    from collections import defaultdict
    # Music Room no longer has "cue points" — just TIMESTAMPED NOTES (a time +
    # a note/title; there's no waveform here for precise cues). Any timestamped
    # note becomes a rekordbox memory cue at that time, named with the note.
    # Both stores count: the panel's `notes` and deep-listen moments.
    cues = defaultdict(list)
    for r in query("SELECT track_id, note_text, audio_timestamp FROM notes "
                   "WHERE audio_timestamp IS NOT NULL AND audio_timestamp != ''"):
        cues[r["track_id"]].append({"note_text": r["note_text"],
                                    "audio_timestamp": r["audio_timestamp"]})
    for r in _deep_listen_cue_rows():
        cues[r["track_id"]].append({"note_text": r["note_text"],
                                    "audio_timestamp": r["audio_timestamp"]})
    for t in present:
        t["notes"] = cues.get(t["id"], [])


def _stick_rebuild(volume, write_engine=True):
    """Rebuild the stick's catalogue from its manifests: one rekordbox XML
    (Locations → the stick) and, if asked, an Engine DJ Database2 m.db with a
    playlist per exported place/pile — plug-and-play on Engine OS hardware."""
    manifests = _usb_read_manifests(volume)
    if not manifests:
        return
    all_ids = {e["id"] for m in manifests for e in (m.get("tracks") or [])}
    meta = _tracks_meta_by_ids(all_ids)
    present, engine_paths, seen = [], {}, set()
    for m in manifests:
        sub = os.path.join("Music Room", os.path.basename(m["_dir"]))
        for e in (m.get("tracks") or []):
            t = meta.get(e["id"])
            fp = os.path.join(m["_dir"], e["file"])
            if not t or not os.path.exists(fp):
                continue
            if e["id"] not in seen:
                seen.add(e["id"])
                t = dict(t)
                t["file_path"] = fp                               # XML → stick
                present.append(t)
                # Engine OS reads Track.path relative to the folder holding
                # "Engine Library" — i.e. up out of it, into our folders
                engine_paths[e["id"]] = "../" + os.path.join(sub, e["file"])
    playlists = []
    for m in manifests:
        aboard = {e["id"] for e in (m.get("tracks") or []) if e["id"] in seen}
        if m.get("playlists"):              # a structure: one playlist per node
            for p in m["playlists"]:
                ids = [i for i in p.get("track_ids", []) if i in aboard]
                if ids:
                    playlists.append({"name": p["name"], "track_ids": ids})
        elif aboard:
            playlists.append({"name": m.get("name") or os.path.basename(m["_dir"]),
                              "track_ids": [e["id"] for e in (m.get("tracks") or []) if e["id"] in aboard]})
    _attach_cues(present)
    xml = _rekordbox_xml_full(present, playlists)
    with open(os.path.join(volume, "musicroom_library.xml"), "w", encoding="utf-8") as f:
        f.write(xml)
    if write_engine:
        from engine_exporter import write_engine_db
        db2 = os.path.join(volume, "Engine Library", "Database2")
        os.makedirs(db2, exist_ok=True)
        write_engine_db(os.path.join(db2, "m.db"), present, playlists,
                        path_for=lambda t: engine_paths.get(t["id"], t["file_path"]))


def _run_usb_export_scoped(volume, scope_type, scope_id, include_hidden,
                           remove_dropped, write_engine):
    import datetime as _dt
    global _usb_progress
    _usb_progress = {"running": True, "done": 0, "total": 0, "copied_gb": 0.0,
                     "failed": 0, "current": "", "xml_path": "", "stop_reason": None,
                     "finished": None}
    try:
      with app.app_context():
        name = _scope_name(scope_type, scope_id)
        tracks, _ = _scoped_export_tracks(scope_type, scope_id, include_hidden)
        sdir = _usb_scope_dir(volume, name)
        os.makedirs(sdir, exist_ok=True)
        mf_path = os.path.join(sdir, ".musicroom.json")
        old_entries = {}
        if os.path.isfile(mf_path):
            try:
                with open(mf_path) as f:
                    for e in (_json.load(f).get("tracks") or []):
                        old_entries[e["id"]] = e
            except Exception:
                pass
        _usb_progress["total"] = len(tracks)
        used, entries = set(), {}
        cancelled = False
        for t in tracks:
            if not _usb_progress["running"]:
                cancelled = True
                _usb_progress["stop_reason"] = "Cancelled — copied files stay; re-run to finish."
                break
            src = t["file_path"]
            ext = os.path.splitext(src)[1] or ""
            artist, title = (t.get("artist_name") or "").strip(), (t.get("title") or "").strip()
            base = (_usb_clean(f"{artist} - {title}")[:200] + ext) if (artist and title) \
                   else os.path.basename(src)
            if base.lower() in used:
                b, e = os.path.splitext(base)
                base = f"{b} ({t['id']}){e}"
            used.add(base.lower())
            dst = os.path.join(sdir, base)
            _usb_progress["current"] = base[:80]
            try:
                if not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src)):
                    shutil.copy2(src, dst)            # resume: size-matched copies skip
                entries[t["id"]] = {"id": t["id"], "file": base,
                                    "size_mb": t.get("file_size_mb")}
                _usb_progress["copied_gb"] += (t.get("file_size_mb") or 0) / 1024
            except Exception:
                _usb_progress["failed"] += 1
            _usb_progress["done"] += 1
        # dropped from the place/pile since last export → off the stick, but
        # ONLY when the user said so this time (ask-each-time). Otherwise
        # whatever is still physically aboard stays ON THE BOOKS — the
        # manifest/XML/Engine catalogue must always match the stick, or a
        # kept file becomes an orphan no later mirror can see.
        if remove_dropped and not cancelled:
            keep_files = {e["file"] for e in entries.values()}
            for tid, e in old_entries.items():
                if e["file"] not in keep_files:
                    try:
                        os.remove(os.path.join(sdir, e["file"]))
                    except Exception:
                        pass
        else:
            for tid, e in old_entries.items():
                if tid not in entries and os.path.exists(os.path.join(sdir, e["file"])):
                    entries[tid] = e
        manifest = {"scope_type": scope_type, "scope_id": scope_id, "name": name,
                    "exported_at": _dt.datetime.utcnow().isoformat(),
                    "tracks": list(entries.values())}
        if scope_type == "structure":       # nodes + axis-crossings travel as playlists
            manifest["playlists"] = _structure_playlists(scope_id, name, set(entries.keys()))
        with open(mf_path, "w", encoding="utf-8") as f:
            _json.dump(manifest, f, indent=1)
        _stick_rebuild(volume, write_engine=write_engine)
        _usb_progress["xml_path"] = os.path.join(volume, "musicroom_library.xml")
        try:
            os.sync()                                 # a yank can't lose buffered writes
        except Exception:
            pass
    except Exception as e:
        _usb_progress["stop_reason"] = f"Failed: {type(e).__name__}: {e}"
    finally:
        _usb_progress["running"] = False
        _usb_progress["finished"] = _dt.datetime.utcnow().isoformat()


def _export_dest_guard(dest):
    """Validate a user-picked export destination. Returns (Path, None) or
    (None, error). An export only ever COPIES, and only ever into a fresh
    <dest>/<scope name>/ subfolder — but we still refuse anywhere inside the
    music library or the app's own data dir, so a copy can never collide with,
    overwrite, or get re-imported from the library. Belt and braces."""
    try:
        p = Path(dest).expanduser().resolve()
    except Exception:
        return None, "bad path"
    if not p.is_dir():
        return None, "pick an existing folder"
    if not os.access(str(p), os.W_OK):
        return None, "that folder isn't writable"
    forbidden = []
    try: forbidden.append(Path(DATA_DIR).resolve())
    except Exception: pass
    try:
        lib = get_library_dest()
        if lib: forbidden.append(Path(lib).resolve())
    except Exception: pass
    for f in forbidden:
        if p == f or f in p.parents:          # dest is, or is inside, the library / app data
            return None, "choose a folder outside your music library and app data"
    return p, None


@app.route("/api/export/folder", methods=["POST"])
def api_export_folder():
    """Export a place / pile / structure into a plain FOLDER on this laptop —
    no USB stick, no DJ hardware. COPIES the scope's audio into <base>/<name>/
    with an .m3u8 playlist + a .musicroom.json manifest. Originals ALWAYS stay
    (this never moves or deletes). <base> is a Finder-picked folder (validated)
    or the configured export folder."""
    d = request.get_json() or {}
    include_hidden = bool(d.get("include_hidden"))
    raw_ids = d.get("track_ids")
    # Two ways to say what to export: an explicit list of track ids (a record, a
    # search, a filter combo's results) OR a named scope (place / pile / structure).
    if raw_ids is not None:
        ids = []
        for x in raw_ids:
            try: ids.append(int(x))
            except (TypeError, ValueError): pass
        if not ids:
            return jsonify({"error": "no tracks to export"}), 400
        name = (d.get("label") or "").strip() or "Selection"
        present, _ = _library_export_tracks(include_hidden)
        idset = set(ids)
        tracks = [t for t in present if t["id"] in idset]
        miss_ids = ids
    else:
        scope_type = d.get("scope_type")
        scope_id   = int(d.get("scope_id") or 0)
        if scope_type not in ("place", "pile", "structure"):
            return jsonify({"error": "scope_type must be place, pile or structure, or pass track_ids"}), 400
        name = _scope_name(scope_type, scope_id)
        if not name:
            return jsonify({"error": "scope not found"}), 404
        tracks, _ = _scoped_export_tracks(scope_type, scope_id, include_hidden)
        miss_ids = None
    dest = (d.get("dest") or "").strip()
    if dest:
        base, err = _export_dest_guard(dest)
        if err:
            return jsonify({"error": err}), 400
    else:
        base = get_export_dir()
    # copy (default, always safe) · move_out (leaves the Room, recoverable) ·
    # relocate (file moves, track stays). Move needs a chosen dest + explicit confirm.
    mode = (d.get("mode") or "copy").strip()
    if mode not in ("copy", "move_out", "relocate"):
        mode = "copy"
    if mode != "copy":
        if not dest:
            return jsonify({"error": "moving needs a destination folder you chose"}), 400
        if not d.get("confirm"):
            return jsonify({"error": "moving needs confirm:true"}), 400
    out_dir = base / _usb_clean(name)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"could not create folder: {e}"}), 500
    from importer import file_content_hash       # SHA-256, for verify-before-touch on a move
    used, entries, m3u = set(), [], ["#EXTM3U"]
    copied = failed = moved = verify_failed = move_failed = 0
    for t in tracks:
        src = t["file_path"]
        ext = os.path.splitext(src)[1] or ""
        artist, title = (t.get("artist_name") or "").strip(), (t.get("title") or "").strip()
        fbase = (_usb_clean(f"{artist} - {title}")[:200] + ext) if (artist and title) \
                else os.path.basename(src)
        if fbase.lower() in used:
            b, e = os.path.splitext(fbase); fbase = f"{b} ({t['id']}){e}"
        used.add(fbase.lower())
        dst = out_dir / fbase
        try:
            if not (dst.exists() and dst.stat().st_size == os.path.getsize(src)):
                shutil.copy2(src, dst)               # idempotent: size-matched copies skip
            m3u.append(f"#EXTINF:{int(t.get('duration_sec') or 0)},{artist} - {title}")
            m3u.append(fbase)
            entries.append({"id": t["id"], "file": fbase})
            copied += 1
        except Exception:
            failed += 1
            continue
        # ── MOVE: never touch the original until the copy is byte-verified ──
        if mode != "copy":
            try:
                ok = (dst.exists() and os.path.getsize(str(dst)) == os.path.getsize(src)
                      and file_content_hash(str(dst)) == file_content_hash(src))
            except Exception:
                ok = False
            if not ok:
                verify_failed += 1                   # unverified → original left EXACTLY as it was
                continue
            if mode == "move_out":                   # leaves the Room → recoverable Removed folder
                done, _e = move_to_removed(src, t["id"])
                if done:
                    execute("UPDATE tracks SET is_deleted=1, deleted_at=datetime('now'), "
                            "updated_at=datetime('now') WHERE id=?", (t["id"],))
                    moved += 1
                else:
                    move_failed += 1                 # file untouched on disk
            elif mode == "relocate":                 # file moves, track stays → relink THEN trash original
                execute("UPDATE files SET file_path=? WHERE track_id=? AND file_path=?",
                        (str(dst), t["id"], src))
                move_to_trash(src)                   # recoverable; a failure just leaves a harmless duplicate
                moved += 1
    safe = _usb_clean(name)
    present_ids = {t["id"] for t in tracks}
    try:
        (out_dir / f"{safe}.m3u8").write_text("\n".join(m3u) + "\n", encoding="utf-8")
        manifest = {"name": name, "kind": "laptop_folder", "tracks": entries}
        if miss_ids is None:
            manifest["scope_type"], manifest["scope_id"] = scope_type, scope_id
        with open(out_dir / ".musicroom.json", "w", encoding="utf-8") as f:
            _json.dump(manifest, f, indent=1)
    except Exception:
        pass
    if miss_ids is not None:
        miss_n, miss_names = _missing_on_drive_for_ids(miss_ids, present_ids)
    else:
        miss_n, miss_names = _scope_missing_on_drive(scope_type, scope_id, present_ids)
    return jsonify({"ok": True, "folder": str(out_dir), "mode": mode,
                    "copied": copied, "failed": failed, "moved": moved,
                    "verify_failed": verify_failed, "move_failed": move_failed,
                    "missing": miss_n, "missing_names": miss_names})


# ── share a room as a static page: metadata + notes + links + covers, NO audio ──

_SHARE_FONTS = ("ebgaramond-400-latin.woff2", "ebgaramond-400-italic-latin.woff2",
                "ebgaramond-500-latin.woff2", "inter-400-latin.woff2",
                "inter-500-latin.woff2", "jetbrainsmono-400-latin.woff2")


def _share_track_payload(ids):
    """Metadata-only rows for a shared room page. Presence on disk is
    IRRELEVANT here (nothing plays), but the hidden rules still apply:
    untitled tracks and recordings stay home."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = query(f"""
        SELECT t.id, t.title, t.artist_name, t.album, t.year, t.label,
               t.artwork_path, t.art_colour,
               t.bandcamp_url, t.soundcloud_url, t.youtube_url, t.spotify_url, t.other_url,
               COALESCE(t.is_set_recording,0) AS is_set_recording,
               COALESCE(t.is_recording,0) AS is_recording,
               f.bpm, f.key_note, f.key_scale,
               (SELECT GROUP_CONCAT(g.name, '') FROM track_genres trg
                  JOIN genres g ON g.id = trg.genre_id WHERE trg.track_id = t.id) AS genre_csv,
               (SELECT GROUP_CONCAT(tg.name, '') FROM track_tags tt
                  JOIN tags tg ON tg.id = tt.tag_id WHERE tt.track_id = t.id) AS tag_csv
        FROM tracks t
        LEFT JOIN files f ON f.track_id = t.id AND f.is_primary = 1
        WHERE t.is_deleted = 0 AND t.id IN ({ph})""", ids)
    notes = {}
    for r in query(f"""SELECT track_id, note_text, audio_timestamp FROM notes
                       WHERE track_id IN ({ph}) AND COALESCE(note_type,'') != 'cue_point'
                             AND note_text IS NOT NULL AND TRIM(note_text) != ''
                       ORDER BY created_at""", ids):
        notes.setdefault(r["track_id"], []).append(
            {"text": r["note_text"], "ts": r["audio_timestamp"] or ""})
    out = []
    for t in rows:
        if not (t.get("title") or "").strip() or t["is_set_recording"] or t["is_recording"]:
            continue
        links = {k: t.get(c) for k, c in (("bandcamp", "bandcamp_url"),
                                          ("soundcloud", "soundcloud_url"),
                                          ("youtube", "youtube_url"),
                                          ("spotify", "spotify_url"),
                                          ("other", "other_url")) if (t.get(c) or "").strip()}
        key = " ".join(x for x in (t.get("key_note"), t.get("key_scale")) if x)
        out.append({
            "id": t["id"], "title": t["title"], "artist": t.get("artist_name") or "",
            "album": t.get("album") or "", "year": t.get("year") or None,
            "label": t.get("label") or "", "bpm": t.get("bpm") or None, "key": key,
            "genres": [g for g in (t.get("genre_csv") or "").split("") if g],
            "tags": [g for g in (t.get("tag_csv") or "").split("") if g],
            "notes": notes.get(t["id"], []), "links": links,
            "colour": t.get("art_colour") or "", "art": False,   # flipped after the copy
            "_artwork_path": t.get("artwork_path"),              # stripped before writing
        })
    out.sort(key=lambda t: ((t["artist"] or "~").lower(), (t["title"] or "").lower()))
    return out


@app.route("/api/export/share", methods=["POST"])
def api_export_share():
    """Publish a place / pile / structure (or an explicit track list) as a
    SHAREABLE STATIC PAGE: a folder holding index.html + covers + fonts.
    Song metadata, the owner's notes, outbound links and the room's look —
    never the audio and never file paths. Host it anywhere or send the folder."""
    import datetime as _dt
    d = request.get_json() or {}
    raw_ids = d.get("track_ids")
    description, room_links, accent = "", [], ""
    items = None
    if d.get("showcase"):
        # the curated shop front — mixed kinds, the owner's order
        items = _showcase_publish_payload()
        if not items:
            return jsonify({"error": "the showcase is empty — add some things first"}), 400
        name = (get_setting("showcase_name", "") or "").strip() or "Showcase"
        description = (get_setting("showcase_intro", "") or "").strip()
        tracks = []
        seen = set()
        for it in items:
            for t in ([it] if it["kind"] == "track" else it.get("tracks", [])):
                tracks.append(t)
                seen.add(t["id"])
    elif raw_ids is not None:
        ids = []
        for x in raw_ids:
            try: ids.append(int(x))
            except (TypeError, ValueError): pass
        if not ids:
            return jsonify({"error": "no tracks to share"}), 400
        name = (d.get("label") or "").strip() or "Selection"
        scope_type = scope_id = None
    else:
        scope_type = d.get("scope_type")
        scope_id   = int(d.get("scope_id") or 0)
        if scope_type not in ("place", "pile", "structure"):
            return jsonify({"error": "scope_type must be place, pile or structure, or pass track_ids"}), 400
        name = _scope_name(scope_type, scope_id)
        if not name:
            return jsonify({"error": "scope not found"}), 404
        ids = list(_scope_track_ids(scope_type, scope_id))
        if scope_type == "place":       # the place's own words + links + colour come along
            pl = query_one("SELECT description, reference_mixes, colour_primary "
                           "FROM places WHERE id=?", (scope_id,))
            if pl:
                description = (pl.get("description") or "").strip()
                accent = (pl.get("colour_primary") or "").strip()
                try:
                    for m in _json.loads(pl.get("reference_mixes") or "[]"):
                        if isinstance(m, str):
                            room_links.append({"url": m, "title": ""})
                        elif isinstance(m, dict) and m.get("url"):
                            room_links.append({"url": m["url"], "title": m.get("title") or ""})
                except Exception:
                    pass
    if items is None:
        tracks = _share_track_payload(ids)
        if not tracks:
            return jsonify({"error": "nothing shareable in that selection"}), 400
    dest = (d.get("dest") or "").strip()
    if dest:
        base, err = _export_dest_guard(dest)
        if err:
            return jsonify({"error": err}), 400
    else:
        base = get_export_dir()
    out_dir = base / _usb_clean(name + " — room page")
    art_dir = out_dir / "art"
    font_dir = out_dir / "fonts"
    try:
        art_dir.mkdir(parents=True, exist_ok=True)
        font_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"could not create folder: {e}"}), 500
    art_count = 0
    for t in tracks:
        fp = _resolve_artwork_path(t.pop("_artwork_path", None))
        if fp:
            thumb = _artwork_thumb(fp, t["id"], 768) or fp
        else:
            # drive unplugged: the local thumb cache still has most covers
            thumb = next((c for c in (os.path.join(_THUMB_DIR, str(s), f"{t['id']}.jpg")
                                      for s in (768, 384, 192)) if os.path.isfile(c)), None)
        if not thumb:
            continue
        try:
            shutil.copy2(thumb, art_dir / f"{t['id']}.jpg")
            t["art"] = True
            art_count += 1
        except Exception:
            pass
    for fn in _SHARE_FONTS:
        src = os.path.join(BASE_DIR, "static", "fonts", "ui", fn)
        try:
            if os.path.isfile(src):
                shutil.copy2(src, font_dir / fn)
        except Exception:
            pass
    room = {"kind": "room_page", "name": name, "description": description,
            "links": room_links, "accent": accent,
            "generated": _dt.date.today().isoformat()}
    if items is not None:
        room["items"] = items
    else:
        room["tracks"] = tracks
    try:
        tmpl = open(os.path.join(BASE_DIR, "static", "share", "room_page.html"),
                    encoding="utf-8").read()
        blob = _json.dumps(room, ensure_ascii=False).replace("</", "<\\/")
        (out_dir / "index.html").write_text(tmpl.replace("__ROOM_JSON__", blob),
                                            encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"could not write the page: {e}"}), 500
    return jsonify({"ok": True, "folder": str(out_dir),
                    "count": len(items) if items is not None else len(tracks),
                    "with_art": art_count})


# ── THE SHOWCASE: the user's shop-front page, dressed item by item ─────────
# Not a view of a scope — a curated MIX the user chose to show: tracks, whole
# albums, links, saved articles, listening notes, in their own order. Publishes
# through the same static room-page renderer.

def _album_track_ids(album, album_artist=None):
    if album_artist:
        rows = query("""SELECT id FROM tracks WHERE is_deleted=0
                        AND album IS NOT NULL AND LOWER(album)=LOWER(?)
                        AND LOWER(COALESCE(artist_name,''))=LOWER(?)""",
                     (album, album_artist))
    else:
        rows = query("""SELECT id FROM tracks WHERE is_deleted=0
                        AND album IS NOT NULL AND LOWER(album)=LOWER(?)""", (album,))
    return [r["id"] for r in rows]


@app.route("/showcase")
def showcase_page():
    return render_template("showcase.html")


@app.route("/api/showcase")
def api_showcase():
    """The showcase's items, in order, resolved enough to draw the builder."""
    items = query("SELECT * FROM showcase_items ORDER BY position, id")
    tids = [i["track_id"] for i in items if i["kind"] == "track" and i["track_id"]]
    tinfo = {}
    if tids:
        ph = ",".join("?" * len(tids))
        for r in query(f"""SELECT t.id, t.title, t.artist_name, t.album,
                                  t.artwork_path IS NOT NULL AS has_art
                           FROM tracks t WHERE t.id IN ({ph})""", tids):
            tinfo[r["id"]] = r
    nids = [i["note_id"] for i in items if i["note_id"]]
    ninfo = {}
    if nids:
        ph = ",".join("?" * len(nids))
        for r in query(f"""SELECT ln.id, ln.kind, ln.note_text, ln.ref_url, ln.ref_title,
                                  ln.ref_source, ln.ref_image_url, ln.timestamp_sec,
                                  t.title AS track_title, t.artist_name AS track_artist
                           FROM listening_notes ln
                           LEFT JOIN tracks t ON t.id = ln.track_id
                           WHERE ln.id IN ({ph})""", nids):
            ninfo[r["id"]] = r
    out = []
    for i in items:
        row = {"id": i["id"], "kind": i["kind"], "title": i["title"] or "",
               "subtitle": i["subtitle"] or "", "url": i["url"] or "",
               "image_url": i["image_url"] or ""}
        if i["kind"] == "track":
            t = tinfo.get(i["track_id"])
            if not t:
                continue
            row.update({"track_id": t["id"], "title": t["title"] or "",
                        "subtitle": t["artist_name"] or "", "has_art": bool(t["has_art"])})
        elif i["kind"] == "album":
            ids = _album_track_ids(i["album"], i["album_artist"])
            row.update({"album": i["album"], "album_artist": i["album_artist"] or "",
                        "title": i["album"], "subtitle": i["album_artist"] or "",
                        "n_tracks": len(ids), "cover_track_id": ids[0] if ids else None})
        elif i["kind"] in ("article", "note"):
            n = ninfo.get(i["note_id"])
            if not n:
                continue
            row["note_id"] = i["note_id"]
            if i["kind"] == "article":
                row.update({"title": n["ref_title"] or n["note_text"] or "",
                            "subtitle": n["ref_source"] or "", "url": n["ref_url"] or "",
                            "image_url": n["ref_image_url"] or ""})
            else:
                ctx = " — ".join(x for x in (n["track_artist"], n["track_title"]) if x)
                row.update({"title": n["note_text"] or "", "subtitle": ctx})
        out.append(row)
    return jsonify({"items": out,
                    "name": get_setting("showcase_name", ""),
                    "intro": get_setting("showcase_intro", "")})


@app.route("/api/showcase/add", methods=["POST"])
def api_showcase_add():
    """Add things to the showcase. kind: tracks (track_ids[]) · album (album,
    album_artist?) · link (url, title?, subtitle?, image_url?) · article /
    note (note_id → listening_notes). Idempotent per thing."""
    d = request.get_json() or {}
    kind = (d.get("kind") or "").strip()
    if kind == "scope":      # a whole place / pile / structure → its tracks
        st, sid = d.get("scope_type"), int(d.get("scope_id") or 0)
        if st not in ("place", "pile", "structure"):
            return jsonify({"error": "bad scope"}), 400
        d = {"kind": "tracks", "track_ids": sorted(_scope_track_ids(st, sid))}
        kind = "tracks"
    posrow = query_one("SELECT COALESCE(MAX(position),0) AS p FROM showcase_items")
    pos = (posrow["p"] or 0) + 1
    added = 0
    if kind == "tracks":
        ids = []
        for x in (d.get("track_ids") or []):
            try: ids.append(int(x))
            except (TypeError, ValueError): pass
        have = {r["track_id"] for r in query(
            "SELECT track_id FROM showcase_items WHERE kind='track'")}
        for tid in ids:
            if tid in have:
                continue
            if not query_one("SELECT 1 AS x FROM tracks WHERE id=? AND is_deleted=0", (tid,)):
                continue
            execute("INSERT INTO showcase_items (kind, track_id, position) VALUES ('track',?,?)",
                    (tid, pos)); pos += 1; added += 1
    elif kind == "album":
        album = (d.get("album") or "").strip()
        artist = (d.get("album_artist") or "").strip() or None
        if not album:
            return jsonify({"error": "album needs a name"}), 400
        dup = query_one("""SELECT 1 AS x FROM showcase_items WHERE kind='album'
                           AND LOWER(album)=LOWER(?)
                           AND LOWER(COALESCE(album_artist,''))=LOWER(?)""",
                        (album, artist or ""))
        if not dup:
            execute("INSERT INTO showcase_items (kind, album, album_artist, position) "
                    "VALUES ('album',?,?,?)", (album, artist, pos)); added += 1
    elif kind == "link":
        url = (d.get("url") or "").strip()
        if not url:
            return jsonify({"error": "a link needs a url"}), 400
        dup = query_one("SELECT 1 AS x FROM showcase_items WHERE kind='link' AND url=?", (url,))
        if not dup:
            execute("INSERT INTO showcase_items (kind, url, title, subtitle, image_url, position) "
                    "VALUES ('link',?,?,?,?,?)",
                    (url, (d.get("title") or "").strip(), (d.get("subtitle") or "").strip(),
                     (d.get("image_url") or "").strip(), pos)); added += 1
    elif kind in ("article", "note"):
        try:
            nid = int(d.get("note_id") or 0)
        except (TypeError, ValueError):
            nid = 0
        want = "reference" if kind == "article" else "moment"
        if not nid or not query_one(
                "SELECT 1 AS x FROM listening_notes WHERE id=? AND kind=?", (nid, want)):
            return jsonify({"error": f"no such {kind}"}), 404
        dup = query_one("SELECT 1 AS x FROM showcase_items WHERE kind=? AND note_id=?", (kind, nid))
        if not dup:
            execute("INSERT INTO showcase_items (kind, note_id, position) VALUES (?,?,?)",
                    (kind, nid, pos)); added += 1
    else:
        return jsonify({"error": "kind must be tracks, album, link, article or note"}), 400
    return jsonify({"ok": True, "added": added})


@app.route("/api/showcase/item/<int:iid>", methods=["DELETE"])
def api_showcase_remove(iid):
    execute("DELETE FROM showcase_items WHERE id=?", (iid,))
    return jsonify({"ok": True})


@app.route("/api/showcase/reorder", methods=["POST"])
def api_showcase_reorder():
    order = (request.get_json() or {}).get("order") or []
    for pos, iid in enumerate(order, start=1):
        try:
            execute("UPDATE showcase_items SET position=? WHERE id=?", (pos, int(iid)))
        except (TypeError, ValueError):
            pass
    return jsonify({"ok": True})


@app.route("/api/showcase/search")
def api_showcase_search():
    """Find things to add: matching tracks, albums, saved articles and
    listening notes for the builder's add panel."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"tracks": [], "albums": [], "articles": [], "notes": []})
    like = f"%{q}%"
    tracks = query("""SELECT id, title, artist_name, album FROM tracks
                      WHERE is_deleted=0 AND COALESCE(is_recording,0)=0
                            AND COALESCE(is_set_recording,0)=0
                            AND TRIM(COALESCE(title,'')) != ''
                            AND (title LIKE ? OR artist_name LIKE ?)
                      ORDER BY artist_name, title LIMIT 30""", (like, like))
    albums = query("""SELECT album, artist_name AS album_artist, COUNT(*) AS n
                      FROM tracks WHERE is_deleted=0 AND album IS NOT NULL
                            AND TRIM(album) != '' AND (album LIKE ? OR artist_name LIKE ?)
                      GROUP BY LOWER(album), LOWER(COALESCE(artist_name,''))
                      HAVING COUNT(*) > 1
                      ORDER BY n DESC LIMIT 12""", (like, like))
    articles = query("""SELECT id, ref_title, ref_source, ref_url FROM listening_notes
                        WHERE kind='reference' AND (ref_title LIKE ? OR ref_source LIKE ?
                              OR note_text LIKE ?)
                        ORDER BY id DESC LIMIT 15""", (like, like, like))
    notes = query("""SELECT ln.id, ln.note_text, t.title AS track_title,
                            t.artist_name AS track_artist
                     FROM listening_notes ln LEFT JOIN tracks t ON t.id = ln.track_id
                     WHERE ln.kind='moment' AND TRIM(COALESCE(ln.note_text,'')) != ''
                           AND ln.note_text LIKE ?
                     ORDER BY ln.id DESC LIMIT 15""", (like,))
    return jsonify({"tracks": tracks, "albums": albums, "articles": articles, "notes": notes})


def _showcase_publish_payload():
    """The mixed items list for the published page, in the owner's order."""
    items = query("SELECT * FROM showcase_items ORDER BY position, id")
    track_ids = set()
    album_ids = {}
    for i in items:
        if i["kind"] == "track" and i["track_id"]:
            track_ids.add(i["track_id"])
        elif i["kind"] == "album":
            ids = _album_track_ids(i["album"], i["album_artist"])
            album_ids[i["id"]] = ids
            track_ids.update(ids)
    payload = {t["id"]: t for t in _share_track_payload(track_ids)}
    nids = [i["note_id"] for i in items if i["note_id"]]
    ninfo = {}
    if nids:
        ph = ",".join("?" * len(nids))
        for r in query(f"""SELECT ln.id, ln.kind, ln.note_text, ln.detail_text,
                                  ln.timestamp_sec, ln.ref_url, ln.ref_title,
                                  ln.ref_source, ln.ref_image_url,
                                  t.title AS track_title, t.artist_name AS track_artist
                           FROM listening_notes ln
                           LEFT JOIN tracks t ON t.id = ln.track_id
                           WHERE ln.id IN ({ph})""", nids):
            ninfo[r["id"]] = r
    out = []
    for i in items:
        if i["kind"] == "track":
            t = payload.get(i["track_id"])
            if t:
                out.append(dict(t, kind="track"))
        elif i["kind"] == "album":
            ts = [payload[tid] for tid in album_ids.get(i["id"], []) if tid in payload]
            if ts:
                out.append({"kind": "album", "album": i["album"],
                            "artist": i["album_artist"] or ts[0].get("artist") or "",
                            "tracks": ts})
        elif i["kind"] == "link":
            out.append({"kind": "link", "url": i["url"], "title": i["title"] or i["url"],
                        "subtitle": i["subtitle"] or "", "image": i["image_url"] or ""})
        elif i["kind"] == "article":
            n = ninfo.get(i["note_id"])
            if n and (n["ref_url"] or n["ref_title"]):
                out.append({"kind": "article", "url": n["ref_url"] or "",
                            "title": n["ref_title"] or n["note_text"] or "",
                            "subtitle": n["ref_source"] or "",
                            "image": n["ref_image_url"] or ""})
        elif i["kind"] == "note":
            n = ninfo.get(i["note_id"])
            if n and (n["note_text"] or "").strip():
                ctx = " — ".join(x for x in (n["track_artist"], n["track_title"]) if x)
                out.append({"kind": "note", "text": n["note_text"],
                            "detail": n["detail_text"] or "", "context": ctx})
    return out


@app.route("/api/usb/scopes")
def api_usb_scopes():
    """What can go on a stick: every place and pile, with track counts."""
    places = query("""SELECT p.id, p.name, COUNT(pt.track_id) AS n FROM places p
                      LEFT JOIN place_tracks pt ON pt.place_id=p.id
                      GROUP BY p.id ORDER BY p.is_well DESC, p.name""")
    piles = query("""SELECT rp.id, rp.name, COUNT(rt.track_id) AS n FROM room_piles rp
                     LEFT JOIN room_pile_tracks rt ON rt.pile_id=rp.id
                     GROUP BY rp.id ORDER BY rp.name""")
    structures = []
    for s in query("SELECT id, name FROM structures ORDER BY name"):
        ids = set()
        for nd in _structure_node_track_ids(s["id"]):
            ids |= nd["ids"]
        structures.append({"id": s["id"], "name": s["name"], "n": len(ids)})
    return jsonify({"places": [dict(r) for r in places],
                    "piles": [dict(r) for r in piles],
                    "structures": structures})


@app.route("/api/usb/preview", methods=["POST"])
def api_usb_preview():
    """The ask-each-time look before a scoped export: what's new, what's
    already aboard, what was dropped from the scope, and whether it fits."""
    d = request.get_json() or {}
    volume = (d.get("volume") or "").strip()
    scope_type, scope_id = d.get("scope_type"), int(d.get("scope_id") or 0)
    if scope_type not in ("place", "pile", "structure"):
        return jsonify({"error": "scope_type must be place, pile or structure"}), 400
    if not volume or not os.path.isdir(volume):
        return jsonify({"error": "volume not mounted"}), 400
    name = _scope_name(scope_type, scope_id)
    if not name:
        return jsonify({"error": "scope not found"}), 404
    tracks, _ = _scoped_export_tracks(scope_type, scope_id, bool(d.get("include_hidden")))
    old = {}
    mf = os.path.join(_usb_scope_dir(volume, name), ".musicroom.json")
    if os.path.isfile(mf):
        try:
            with open(mf) as f:
                for e in (_json.load(f).get("tracks") or []):
                    old[e["id"]] = e
        except Exception:
            pass
    have = {t["id"]: t for t in tracks}
    new_ids = [i for i in have if i not in old]
    dropped = [e for i, e in old.items() if i not in have]
    gb_new = sum((have[i].get("file_size_mb") or 0) for i in new_ids) / 1024
    gb_total = sum((t.get("file_size_mb") or 0) for t in tracks) / 1024
    # tracks in this scope that CAN'T export because their audio isn't on the
    # drive — surfaced so the stick is never silently incomplete for a gig
    missing_count, missing_names = _scope_missing_on_drive(scope_type, scope_id, set(have.keys()))
    try:
        stv = os.statvfs(volume)
        free_gb = stv.f_bavail * stv.f_frsize / (1024 ** 3)
    except Exception:
        free_gb = None
    return jsonify({
        "scope_name": name, "tracks": len(tracks),
        "new": len(new_ids), "kept": len(have) - len(new_ids), "dropped": len(dropped),
        "dropped_names": [e.get("file") for e in dropped][:8],
        "missing": missing_count, "missing_names": missing_names,
        "gb_to_copy": round(gb_new, 2), "scope_gb": round(gb_total, 2),
        "free_gb": round(free_gb, 2) if free_gb is not None else None,
        "fits": (free_gb is None) or (gb_new <= free_gb)})


@app.route("/api/usb/volumes")
def api_usb_volumes():
    """Mounted volumes a USB export could write to, with free space and the
    size the export needs."""
    present, _ = _library_export_tracks(include_hidden=False)
    needed_gb = sum((t.get("file_size_mb") or 0) for t in present) / 1024
    vols = []
    try:
        for name in sorted(os.listdir("/Volumes")):
            path = os.path.join("/Volumes", name)
            if not os.path.isdir(path) or not os.access(path, os.W_OK):
                continue
            try:
                st = os.statvfs(path)
                free_gb = st.f_bavail * st.f_frsize / (1024 ** 3)
            except Exception:
                free_gb = None
            vols.append({"name": name, "path": path, "free_gb": round(free_gb, 1) if free_gb else None})
    except Exception:
        pass
    return jsonify({"volumes": vols, "tracks": len(present), "needed_gb": round(needed_gb, 1)})


@app.route("/api/usb/export", methods=["POST"])
def api_usb_export():
    if _usb_progress.get("running"):
        return jsonify({"ok": False, "error": "already running"}), 409
    d = request.get_json() or {}
    volume = (d.get("volume") or "").strip()
    if not volume or not os.path.isdir(volume):
        return jsonify({"ok": False, "error": "volume not mounted"}), 400
    scope_type = d.get("scope_type")
    if scope_type in ("place", "pile", "structure"):     # one scope → the stick
        scope_id = int(d.get("scope_id") or 0)
        if not _scope_name(scope_type, scope_id):
            return jsonify({"ok": False, "error": "scope not found"}), 404
        threading.Thread(target=_run_usb_export_scoped,
                         args=(volume, scope_type, scope_id,
                               bool(d.get("include_hidden")),
                               bool(d.get("remove_dropped")),
                               d.get("write_engine", True)),
                         daemon=True).start()
    else:                                                # the whole library, as before
        threading.Thread(target=_run_usb_export,
                         args=(volume, bool(d.get("include_hidden"))), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/usb/export-status")
def api_usb_export_status():
    return jsonify(_usb_progress)


@app.route("/api/usb/export-stop", methods=["POST"])
def api_usb_export_stop():
    if _usb_progress.get("running"):
        _usb_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/export/rekordbox/library")
def api_export_library_rekordbox():
    """Download the whole-library XML as a file."""
    xml, present, skipped, _seeded = _build_library_xml()
    resp = Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="musicroom_library.xml"'})
    resp.headers["X-Track-Count"]     = str(present)
    resp.headers["X-Skipped-Missing"] = str(skipped)
    return resp


def _library_xml_path():
    """The fixed LOCAL home for the whole-library XML — in the app's export dir,
    which lives inside DATA_DIR alongside the database. The Music Room always
    reads/writes the XML HERE, so it's available even with the drive unplugged.
    Because it sits in DATA_DIR (and isn't an excluded type), the normal backup
    mirrors it to the drive's app-data folder AND the laptop backup — i.e. it's
    backed up exactly like the database + metadata, not stranded on the drive."""
    return get_export_dir() / "musicroom_library.xml"


@app.route("/api/export/rekordbox/preflight")
def api_rekordbox_preflight():
    """Present/skipped counts for the library export, so the export page can
    warn LOUDLY before a drive-unplugged export writes a nearly-empty XML
    (the old footnote read as fine-print; testers export into silence)."""
    present, skipped = _library_export_tracks(False)
    return jsonify({"present": len(present), "skipped": skipped})


@app.route("/api/export/rekordbox/library/save", methods=["POST"])
def api_save_library_rekordbox():
    """(Re)write the whole-library XML to its fixed LOCAL home (in DATA_DIR). Each
    run is a full regeneration = the current picture of the Music Room (new places
    + new tracks appear; nothing else changes), so you point rekordbox at this one
    file once and just re-run to refresh. The Music Room keeps + serves the XML
    locally; a background backup then mirrors it to the drive (and the laptop
    backup) the same way the database + metadata are backed up. Returns the local
    path + the drive backup target; reveals it in Finder."""
    d = request.get_json(silent=True) or {}
    seed_cues = bool(d.get("seed_cues"))
    xml, present, skipped, seeded = _build_library_xml(
        include_hidden=bool(d.get("include_hidden")), seed_cues=seed_cues,
        seed_all=bool(d.get("seed_all")))
    path = _library_xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    # stamp the tracks whose cues we just seeded, so a future export never
    # re-asserts them (and clobbers the user's rekordbox grid work)
    if seeded:
        ph = ",".join("?" * len(seeded))
        execute(f"UPDATE tracks SET cues_seeded_at = datetime('now') WHERE id IN ({ph})", list(seeded))
    # back the fresh XML up to the drive (+ laptop) exactly like the DB/metadata:
    # it lives in DATA_DIR, so the normal mirror carries it across (preserving the
    # exports/ subpath). Kick the mirror now so the drive copy is fresh after an
    # export. Non-blocking, best-effort; the next launch backup catches it anyway.
    drive_backup = None
    try:
        drive = _drive_appdata_dir()
        ml = (get_setting("music_location", "") or "").strip()
        use_drive = (ml == "external") or (ml not in ("local", "external") and drive is not None)
        if use_drive and drive:
            drive_backup = os.path.join(drive, os.path.relpath(str(path), str(DATA_DIR)))
        backup_to_drive()
    except Exception:
        pass
    try:
        _reveal_in_finder(str(path))
    except Exception:
        pass
    return jsonify({"ok": True, "path": str(path), "tracks": present, "skipped": skipped,
                    "cues_seeded": len(seeded), "backed_up_to": drive_backup})


# ── Analysis layers: ingest an external analyser's BPM/key (rekordbox now) ────
# rekordbox's analysis beats ours, so we INGEST it as a layer rather than fight
# it. Match by file path → filename → artist+title (path wins once rekordbox is
# rebuilt FROM the Music Room export; artist+title recovers a current, otherwise-
# located collection). Music Room's own analysis stays in files.bpm/key; this
# stores the external sources in track_analysis.
def _import_rekordbox_analysis(xml_path, persist=True):
    import xml.etree.ElementTree as ET, urllib.parse
    coll = ET.parse(xml_path).getroot().find("COLLECTION")
    rows = coll.findall("TRACK") if coll is not None else []
    cam2key = {v: k for k, v in CAMELOT_MAP.items()}          # "8A" -> "A minor"
    def parse_key(raw):
        s = (raw or "").strip()
        if not s: return None, None
        if s.upper() in cam2key:
            n, sc = cam2key[s.upper()].split(" ", 1); return n, sc
        m = _re.match(r"^([A-Ga-g][#b]?)\s*(m|min|minor|maj|major)?$", s)
        if m:
            note = m.group(1)[0].upper() + m.group(1)[1:].replace("B", "b")
            suf = (m.group(2) or "").lower()
            scale = "minor" if (suf.startswith("m") and not suf.startswith("maj")) else "major"
            return note, scale
        return s, None                                        # unknown format — keep raw
    norm = mr_text.words
    mr = query("SELECT t.id, t.title, t.artist_name, f.file_path FROM tracks t "
               "JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0 WHERE t.is_deleted=0")
    by_path, by_base, by_at = {}, {}, {}
    for r in mr:
        fp = r["file_path"] or ""
        if fp:
            by_path.setdefault(fp, r["id"]); by_base.setdefault(os.path.basename(fp).lower(), r["id"])
        by_at.setdefault((norm(r["artist_name"]), norm(r["title"])), r["id"])
    matched = unmatched = 0
    methods = {"path": 0, "filename": 0, "artist_title": 0}
    samples, seen = [], set()
    for tr in rows:
        try: bpm = float(tr.get("AverageBpm") or 0)
        except Exception: bpm = 0.0
        note, scale = parse_key(tr.get("Tonality"))
        if bpm <= 0 and not note:
            continue
        loc = urllib.parse.unquote((tr.get("Location") or "").replace("file://localhost", "").replace("file://", ""))
        base = os.path.basename(loc).lower()
        tid = method = None
        if loc and loc in by_path: tid, method = by_path[loc], "path"
        elif base and base in by_base: tid, method = by_base[base], "filename"
        else:
            at = (norm(tr.get("Artist")), norm(tr.get("Name")))
            if at[0] and at[1] and at in by_at: tid, method = by_at[at], "artist_title"
        if tid is None: unmatched += 1; continue
        if tid in seen: continue
        seen.add(tid); matched += 1; methods[method] += 1
        if persist:
            execute("""INSERT INTO track_analysis (track_id, source, bpm, key_note, key_scale, analysed_at)
                       VALUES (?, 'rekordbox', ?, ?, ?, datetime('now'))
                       ON CONFLICT(track_id, source) DO UPDATE SET
                         bpm=excluded.bpm, key_note=excluded.key_note,
                         key_scale=excluded.key_scale, analysed_at=excluded.analysed_at""",
                    (tid, bpm or None, note, scale))
        if len(samples) < 6:
            samples.append({"track_id": tid, "method": method, "bpm": round(bpm, 1) if bpm else None,
                            "key": (f"{note} {scale}" if note and scale else note), "name": tr.get("Name")})
    return {"tracks_in_xml": len(rows), "matched": matched, "unmatched": unmatched,
            "by_method": methods, "samples": samples}


@app.route("/api/analysis/import-rekordbox", methods=["POST"])
def api_import_rekordbox_analysis():
    """Ingest a rekordbox collection XML's BPM/key as the 'rekordbox' analysis
    layer. dry_run=true matches + parses but writes nothing (verification)."""
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return jsonify({"error": "file not found"}), 400
    try:
        res = _import_rekordbox_analysis(path, persist=not bool(d.get("dry_run")))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "dry_run": bool(d.get("dry_run")), **res})


# ── Reverse-import: bring rekordbox's OWN genres / cues / track-colours INTO
# the Music Room (the inverse of the rekordbox XML export). For users who
# already curated in rekordbox and want that work to land in the Music Room.
# NON-DESTRUCTIVE by construction: genres are ADDED (never removed), cues are
# ADDED as timestamped notes (de-duped by timestamp), and a track colour is
# only filled where the record has NONE (an existing cover tint is never
# clobbered). dry_run previews exactly what would change without writing —
# the import is a SUGGESTION the user confirms (§3 "only the user knows").
# NOTE: rekordbox MyTags are deliberately NOT handled here — they aren't in
# the collection XML (they live in rekordbox's own database), so there's
# nothing reliable to import. Genre/Comments/Colour/cues are all we can trust.
_RB_COLOUR_NAMES = {
    "ff007f": "Rose", "ff0000": "Red", "ffa500": "Orange", "ffff00": "Lemon",
    "00ff00": "Green", "25fde9": "Turquoise", "0000ff": "Blue", "660099": "Violet",
}


def _rb_colour_to_hex(raw):
    """rekordbox TRACK Colour ('0xFF007F' RGB) -> '#ff007f', or None."""
    s = (raw or "").strip().lower().replace("0x", "").lstrip("#")
    if len(s) == 6 and all(c in "0123456789abcdef" for c in s):
        return "#" + s
    return None


def _secs_to_ts(sec):
    """Float seconds (rekordbox POSITION_MARK Start) -> the app's audio_timestamp
    string: 'M:SS', or 'H:MM:SS' past an hour (matching how cues are parsed back)."""
    try:
        sec = max(0, int(round(float(sec))))
    except Exception:
        return None
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _import_rekordbox_metadata(xml_path, fields, dry_run=True):
    import xml.etree.ElementTree as ET, urllib.parse
    want = set(fields or [])
    coll = ET.parse(xml_path).getroot().find("COLLECTION")
    rows = coll.findall("TRACK") if coll is not None else []

    norm = mr_text.words

    mr = query("SELECT t.id, t.title, t.artist_name, t.colour_code, f.file_path "
               "FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0 "
               "WHERE t.is_deleted=0")
    by_path, by_base, by_at, colour_now = {}, {}, {}, {}
    for r in mr:
        fp = r["file_path"] or ""
        if fp:
            by_path.setdefault(fp, r["id"]); by_base.setdefault(os.path.basename(fp).lower(), r["id"])
        by_at.setdefault((norm(r["artist_name"]), norm(r["title"])), r["id"])
        colour_now[r["id"]] = (r["colour_code"] or "").strip()

    # preload existing state so the dry-run count is exact and writes stay idempotent
    gname2id = {row["name"].lower(): row["id"] for row in query("SELECT id, name FROM genres")}
    have_genre = {}                                          # tid -> set(lower genre name)
    for row in query("SELECT tg.track_id AS tid, LOWER(g.name) AS n FROM track_genres tg JOIN genres g ON g.id=tg.genre_id"):
        have_genre.setdefault(row["tid"], set()).add(row["n"])
    have_ts = {}                                             # tid -> set(audio_timestamp)
    for row in query("SELECT track_id AS tid, audio_timestamp AS ts FROM notes WHERE audio_timestamp IS NOT NULL AND audio_timestamp!=''"):
        have_ts.setdefault(row["tid"], set()).add(row["ts"])

    matched = unmatched = 0
    methods = {"path": 0, "filename": 0, "artist_title": 0}
    add_genres = add_cues = add_colours = 0
    skip_colour_existing = skip_cue_dupe = 0
    samples, seen = [], set()

    for tr in rows:
        loc = urllib.parse.unquote((tr.get("Location") or "").replace("file://localhost", "").replace("file://", ""))
        base = os.path.basename(loc).lower()
        tid = method = None
        if loc and loc in by_path: tid, method = by_path[loc], "path"
        elif base and base in by_base: tid, method = by_base[base], "filename"
        else:
            at = (norm(tr.get("Artist")), norm(tr.get("Name")))
            if at[0] and at[1] and at in by_at: tid, method = by_at[at], "artist_title"
        if tid is None: unmatched += 1; continue
        if tid in seen: continue
        seen.add(tid); matched += 1; methods[method] += 1
        samp = {"track_id": tid, "name": tr.get("Name"), "method": method,
                "genres": [], "cues": 0, "colour": None}

        if "genres" in want:
            cur = have_genre.setdefault(tid, set())
            for g in (tr.get("Genre") or "").split(","):
                g = g.strip()
                if not g or g.lower() in cur:
                    continue
                samp["genres"].append(g); add_genres += 1; cur.add(g.lower())
                if not dry_run:
                    gid = gname2id.get(g.lower())
                    if gid is None:
                        execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (g,))
                        gid = query_one("SELECT id FROM genres WHERE LOWER(name)=?", (g.lower(),))["id"]
                        gname2id[g.lower()] = gid
                    execute("INSERT OR IGNORE INTO track_genres (track_id, genre_id) VALUES (?, ?)", (tid, gid))

        if "cues" in want:
            cur_ts = have_ts.setdefault(tid, set())
            for pm in tr.findall("POSITION_MARK"):
                ts = _secs_to_ts(pm.get("Start"))
                if not ts or ts in cur_ts:
                    if ts: skip_cue_dupe += 1
                    continue
                cur_ts.add(ts)
                name = (pm.get("Name") or "").strip()
                if not name:
                    num = pm.get("Num")
                    if num in (None, "-1"): name = "Memory cue"
                    else:
                        try: name = "Hot cue " + chr(65 + int(num))
                        except Exception: name = "Cue"
                samp["cues"] += 1; add_cues += 1
                if not dry_run:
                    execute("INSERT INTO notes (track_id, note_text, audio_timestamp, note_type) VALUES (?, ?, ?, 'cue_point')",
                            (tid, name, ts))

        if "colours" in want:
            hexv = _rb_colour_to_hex(tr.get("Colour"))
            if hexv:
                if colour_now.get(tid):
                    skip_colour_existing += 1
                else:
                    nm = _RB_COLOUR_NAMES.get(hexv.lstrip("#"), hexv)
                    samp["colour"] = nm; add_colours += 1; colour_now[tid] = hexv
                    if not dry_run:
                        execute("UPDATE tracks SET colour_code=? WHERE id=?", (hexv, tid))

        if len(samples) < 8 and (samp["genres"] or samp["cues"] or samp["colour"]):
            samples.append(samp)

    return {
        "tracks_in_xml": len(rows), "matched": matched, "unmatched": unmatched, "by_method": methods,
        "would_add" if dry_run else "added": {"genres": add_genres, "cues": add_cues, "colours": add_colours},
        "skipped": {"colour_has_existing": skip_colour_existing, "cue_duplicate": skip_cue_dupe},
        "samples": samples,
    }


@app.route("/api/import/rekordbox-metadata", methods=["POST"])
def api_import_rekordbox_metadata():
    """Reverse-import rekordbox genres / cues / track-colours into the Music Room.
    Body: {path, fields:["genres","cues","colours"], dry_run}. dry_run previews
    exactly what would change (matched tracks + per-field counts) without writing."""
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    fields = d.get("fields") or ["genres", "cues", "colours"]
    if not path or not os.path.isfile(path):
        return jsonify({"error": "file not found"}), 400
    try:
        res = _import_rekordbox_metadata(path, fields, dry_run=bool(d.get("dry_run")))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "dry_run": bool(d.get("dry_run")), **res})


# ── The recorder: capture the laptop's audio OUTPUT (via a loopback device like
# BlackHole) and drop it into the room as a SAMPLE. Capture runs in Python
# (recorder.py) — the WebView can't grab system audio (WKWebView screen-capture is
# video-only) — and saves a track the same way a set recording becomes a Room
# track: titled (so it's findable), own-recording source, one-shot if short, NO
# enrichment, added to the Well, tagged 'sample'.
_SAMPLES_DIR = os.path.join(RECORDINGS_DIR, "samples")


@app.route("/api/recorder/devices")
def api_recorder_devices():
    return jsonify(mr_recorder.list_devices())


@app.route("/api/recorder/start", methods=["POST"])
def api_recorder_start():
    d = request.get_json(silent=True) or {}
    di = d.get("device_index")
    return jsonify(mr_recorder.recorder.start(_SAMPLES_DIR, device_index=di))


@app.route("/api/recorder/status")
def api_recorder_status():
    return jsonify(mr_recorder.recorder.status())


@app.route("/api/recorder/cancel", methods=["POST"])
def api_recorder_cancel():
    return jsonify(mr_recorder.recorder.cancel())


def _in_samples_dir(path):
    """Guard: a client-supplied recording path must live inside the samples dir."""
    try:
        base = os.path.realpath(_SAMPLES_DIR)
        return os.path.realpath(path).startswith(base + os.sep)
    except Exception:
        return False


@app.route("/api/recorder/stop", methods=["POST"])
def api_recorder_stop():
    """Stop the capture and FINALIZE the WAV — but don't save it yet. Returns the
    pending file + a suggested name so the user can NAME it before saving
    (/api/recorder/save) or throw it away (/api/recorder/discard)."""
    res = mr_recorder.recorder.stop()
    if not res.get("ok"):
        return jsonify(res), 400
    path, dur = res.get("path"), res.get("duration") or 0.0
    if not path or not os.path.isfile(path) or os.path.getsize(path) < 2000:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        return jsonify({"ok": True, "captured": False, "reason": "nothing captured — is your output routed through the loopback?"})
    return jsonify({"ok": True, "captured": True, "path": path, "duration": dur,
                    "suggested_name": os.path.splitext(os.path.basename(path))[0]})


@app.route("/api/recorder/save", methods=["POST"])
def api_recorder_save():
    """Commit a finalized capture as a RECORDING — a distinct, flagged track
    (is_recording=1), with the user's chosen name. Recordings stay OUT of the Well
    grid and the rekordbox library export, but can be added to sets / Listen /
    Research like any track."""
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    title = (d.get("title") or "").strip()
    if not path or not os.path.isfile(path) or not _in_samples_dir(path):
        return jsonify({"ok": False, "error": "unknown recording"}), 400
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    dur = 0.0
    try:
        import soundfile as sf
        dur = round(sf.info(path).duration, 2)
    except Exception:
        pass
    size_mb = round(os.path.getsize(path) / (1024 * 1024), 3)
    tid = execute("""INSERT INTO tracks (title, artist_name, source_type, is_recording, is_oneshot, needs_review)
                     VALUES (?, '', 'recording', 1, ?, 0)""", (title, 1 if (dur and dur < 5) else 0))
    fid = execute("""INSERT INTO files (track_id, file_path, file_format, is_primary, duration_sec, file_size_mb)
                     VALUES (?, ?, 'wav', 1, ?, ?)""", (tid, path, dur, size_mb))
    return jsonify({"ok": True, "track_id": tid, "file_id": fid, "title": title, "duration": dur})


@app.route("/api/recorder/discard", methods=["POST"])
def api_recorder_discard():
    """Throw away a finalized-but-unsaved capture."""
    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    if path and _in_samples_dir(path) and os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass
    return jsonify({"ok": True})


# ── Recordings: the distinct list of captured audio (play, reveal, rename, add) ─
@app.route("/api/recordings")
def api_recordings():
    rows = query("""
        SELECT t.id, t.title, t.date_added,
               f.id AS file_id, f.file_path, f.duration_sec, f.file_size_mb,
               (SELECT COUNT(*) FROM set_tracks st WHERE st.track_id=t.id) AS in_sets
        FROM tracks t
        JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0
        WHERE t.is_deleted=0 AND COALESCE(t.is_recording,0)=1
        ORDER BY t.date_added DESC, t.id DESC""")
    out = [{"id": r["id"], "title": r["title"], "file_id": r["file_id"],
            "duration": r["duration_sec"], "size_mb": r["file_size_mb"],
            "date_added": r["date_added"], "in_sets": r["in_sets"],
            "on_disk": bool(_resolve_audio_path(r["file_path"]))} for r in rows]
    return jsonify({"recordings": out, "count": len(out)})


@app.route("/api/recording/<int:tid>/rename", methods=["POST"])
def api_recording_rename(tid):
    d = request.get_json() or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "name required"}), 400
    execute("UPDATE tracks SET title=? WHERE id=? AND COALESCE(is_recording,0)=1", (title, tid))
    return jsonify({"ok": True, "title": title})


@app.route("/api/recording/<int:tid>", methods=["DELETE"])
def api_recording_delete(tid):
    """Delete a recording — its track row, set links, and the WAV on disk."""
    t = query_one("SELECT id FROM tracks WHERE id=? AND COALESCE(is_recording,0)=1", (tid,))
    if not t:
        return jsonify({"ok": False, "error": "not a recording"}), 404
    for f in query("SELECT file_path FROM files WHERE track_id=?", (tid,)):
        p = f["file_path"]
        if p and _in_samples_dir(p) and os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass
    execute("DELETE FROM set_tracks WHERE track_id=?", (tid,))
    execute("DELETE FROM files WHERE track_id=?", (tid,))
    execute("DELETE FROM place_tracks WHERE track_id=?", (tid,))
    execute("DELETE FROM tracks WHERE id=?", (tid,))
    return jsonify({"ok": True})


@app.route("/recordings")
def recordings_page():
    return render_template("recordings.html")


# ── Tester feedback: the in-app "log a thought" button (testing phase) ─────────
_FEEDBACK_KINDS = {"like", "dislike", "confused", "bug", "idea"}


def _tester_account():
    acct = {"account_id": (get_setting("tester_account_id", "") or "").strip(),
            "tester": (get_setting("tester_name", "") or "").strip()}
    if not acct["account_id"] and get_setting("gr_token", ""):
        # signed in to the trial server: that identity IS the tester account
        acct = {"account_id": "gr-" + (get_setting("gr_fingerprint", "") or "")[:12],
                "tester": get_setting("gr_name", "") or ""}
    return acct


@app.route("/api/feedback/me", methods=["GET", "POST"])
def api_feedback_me():
    """The LOCAL tester account. Created once (a name → a stable account_id); after
    that the identity is fixed — no logging in as anyone else. The account labels
    this install's feedback in the master sheet."""
    acct = _tester_account()
    if request.method == "POST":
        if acct["account_id"]:
            # already created — locked, don't let the name be reassigned
            return jsonify({"ok": True, "tester": acct["tester"], "account_id": acct["account_id"], "locked": True})
        name = ((request.get_json() or {}).get("tester") or "").strip()[:60]
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        aid = uuid.uuid4().hex
        for k, v in (("tester_account_id", aid), ("tester_name", name)):
            execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        return jsonify({"ok": True, "tester": name, "account_id": aid})
    return jsonify({"tester": acct["tester"], "account_id": acct["account_id"],
                    "has_account": bool(acct["account_id"])})


@app.route("/api/feedback", methods=["POST"])
def api_feedback_add():
    d = request.get_json() or {}
    kind = (d.get("kind") or "").strip().lower()
    if kind not in _FEEDBACK_KINDS:
        return jsonify({"ok": False, "error": "bad kind"}), 400
    note = (d.get("note") or "").strip()[:2000]
    if not note:                                            # the note is REQUIRED
        return jsonify({"ok": False, "error": "Tell us what — a note is required."}), 400
    acct = _tester_account()
    if not acct["account_id"]:
        return jsonify({"ok": False, "error": "no account"}), 400
    page = (d.get("page") or "").strip()[:300] or None
    ctx = d.get("context")
    ctx = _json.dumps(ctx)[:1000] if ctx else None
    # the trial server keeps a copy beside the activity log (best effort, off-thread)
    threading.Thread(target=green_room.send_feedback, args=(kind, note, page, ctx), daemon=True).start()
    execute("INSERT INTO tester_feedback (uid, account_id, kind, note, page, context, tester) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, acct["account_id"], kind, note, page, ctx, acct["tester"] or None))
    _feedback_push_async()                       # push to the collection endpoint if configured
    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["GET"])
def api_feedback_list():
    rows = query("SELECT id, uid, account_id, kind, note, page, tester, created_at "
                 "FROM tester_feedback ORDER BY created_at DESC, id DESC")
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    acct = _tester_account()
    return jsonify({"feedback": [dict(r) for r in rows], "count": len(rows),
                    "by_kind": counts, "tester": acct["tester"], "account_id": acct["account_id"]})


@app.route("/api/feedback/export.csv")
def api_feedback_export():
    """A CSV of all feedback on this install — drop it into the master tracker (the
    `uid` column dedupes when merging multiple testers' exports)."""
    import csv, io
    rows = query("SELECT uid, account_id, created_at, tester, kind, note, page "
                 "FROM tester_feedback ORDER BY created_at, id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["uid", "account_id", "created_at", "tester", "kind", "note", "page"])
    for r in rows:
        w.writerow([r["uid"] or "", r["account_id"] or "", r["created_at"],
                    r["tester"] or "", r["kind"], r["note"] or "", r["page"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="music_room_feedback.csv"'})


# ── Feedback outbox: push unsent feedback to Daithí's collection endpoint ───────
# Self-hosted: each tester's app POSTs its feedback to the endpoint Daithí runs
# (telemetry_server/server.py's /feedback). Outbox pattern — stored locally, sent
# when online, stamped sent_at; offline-safe. Configured via the feedback_endpoint
# / feedback_secret settings, or the MR_FEEDBACK_ENDPOINT / MR_FEEDBACK_SECRET env
# (baked into the tester build).
def _feedback_push():
    import urllib.request
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            c.row_factory = sqlite3.Row
            def s(k):
                r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
                return ((r[0] if r else "") or "").strip()
            url = s("feedback_endpoint") or os.environ.get("MR_FEEDBACK_ENDPOINT", "").strip()
            secret = s("feedback_secret") or os.environ.get("MR_FEEDBACK_SECRET", "").strip()
            if not url:
                return
            rows = c.execute("SELECT uid, account_id, tester, kind, note, page, created_at "
                             "FROM tester_feedback WHERE sent_at IS NULL AND uid IS NOT NULL "
                             "ORDER BY id LIMIT 500").fetchall()
            if not rows:
                return
            payload = _json.dumps({"secret": secret, "rows": [dict(r) for r in rows]}).encode()
            # POST to the endpoint URL exactly as configured — works for any webhook:
            # a Google Apps Script web app (a Sheet you own), a self-hosted /feedback
            # endpoint, etc. (No path is appended.)
            req = urllib.request.Request(url, data=payload,
                                         method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status not in (200, 201, 302):
                    return
            uids = [r["uid"] for r in rows]
            ph = ",".join("?" * len(uids))
            c.execute(f"UPDATE tester_feedback SET sent_at=datetime('now') WHERE uid IN ({ph})", uids)
            c.commit()
    except Exception as e:
        print("[feedback] push failed:", e)


def _feedback_push_async():
    threading.Thread(target=_feedback_push, daemon=True).start()


_fb_push_last = [0.0]
@app.before_request
def _feedback_push_hook():
    """Opportunistic outbox flush — at most every ~3 min, off-thread; catches rows
    that couldn't send while offline. No-op when no endpoint is configured."""
    import time
    now = time.time()
    if now - _fb_push_last[0] < 180:
        return
    _fb_push_last[0] = now
    if (get_setting("feedback_endpoint", "") or os.environ.get("MR_FEEDBACK_ENDPOINT", "")).strip():
        _feedback_push_async()


# ── artwork self-heal ────────────────────────────────────────────────────────
# Covers are fetched at the END of an import (the enrich stage). A killed
# import — the tester closed the "second Music Room" the worker used to show
# in the Dock — left a whole library with no covers and no record of ever
# trying (day one of testing, 2026-07-14). Once per launch: if a meaningful
# number of tracks have NEVER been through an artwork attempt, quietly run the
# standard batch (embedded art first — local + fast; online sources throttled;
# rows already marked "not_found" are never re-searched, so this converges and
# costs nothing on a healthy library).
_artwork_selfheal_done = [False]

@app.before_request
def _artwork_selfheal_maybe():
    if _artwork_selfheal_done[0]:
        return
    _artwork_selfheal_done[0] = True
    try:
        from artwork_fetcher import run_batch_fetch, get_batch_progress
        if get_batch_progress().get("running"):
            return
        row = query_one(
            "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 "
            "AND COALESCE(is_recording,0)=0 AND COALESCE(is_set_recording,0)=0 "
            "AND artwork_path IS NULL AND artwork_source IS NULL "
            "AND COALESCE(TRIM(title),'') != ''")
        if (row["n"] if row else 0) < 5:
            return
        threading.Thread(target=run_batch_fetch, daemon=True).start()
    except Exception:
        pass


@app.route("/api/feedback/sync", methods=["POST"])
def api_feedback_sync():
    """Manual 'send my feedback now'. Returns how many are still unsent after."""
    _feedback_push()
    unsent = query_one("SELECT COUNT(*) n FROM tester_feedback WHERE sent_at IS NULL")["n"]
    endpoint = (get_setting("feedback_endpoint", "") or os.environ.get("MR_FEEDBACK_ENDPOINT", "")).strip()
    return jsonify({"ok": True, "unsent": unsent, "configured": bool(endpoint)})


def _rb_export_path(name, kind="set"):
    export_dir = get_export_dir()
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or kind
    return export_dir / f"{safe.replace(' ', '_')}.xml"


def _reveal_in_finder(path):
    try:
        subprocess.run(
            ["osascript",
             "-e", f'tell application "Finder" to reveal POSIX file "{path}"',
             "-e", 'tell application "Finder" to activate'],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass


def _update_master_xml(tracks, playlist_name):
    """Upsert a playlist into the single master music_room.xml Rekordbox points at."""
    try:
        RB_XML_DIR.mkdir(parents=True, exist_ok=True)
        # Parse existing master or build a fresh skeleton
        root = None
        if MASTER_XML.exists():
            try:
                root = ET.parse(str(MASTER_XML)).getroot()
            except ET.ParseError:
                root = None
        if root is None:
            root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
            ET.SubElement(root, "PRODUCT", Name="Music Room", Version="1.0", Company="Music Room")
            ET.SubElement(root, "COLLECTION", Entries="0")
            plists = ET.SubElement(root, "PLAYLISTS")
            ET.SubElement(plists, "NODE", Type="0", Name="ROOT", Count="0")

        coll      = root.find("COLLECTION")
        root_node = root.find("PLAYLISTS").find("NODE")
        existing  = {el.get("TrackID"): el for el in coll.findall("TRACK")}

        seen, pl_ids = set(), []
        for t in tracks:
            tid = str(t["id"])
            if tid in seen:
                continue
            seen.add(tid)
            pl_ids.append(tid)
            # Remove stale entry so we re-add with fresh metadata
            if tid in existing:
                coll.remove(existing[tid])
            fp       = t.get("file_path") or ""
            loc      = _rb_location(fp)
            bpm_val  = f"{float(t['bpm']):.2f}" if t.get("bpm") else ""
            key_full = f"{t.get('key_note')} {t.get('key_scale')}" if t.get("key_note") else ""
            key      = CAMELOT_MAP.get(key_full, t.get("key_note") or "") if key_full else ""
            el = ET.SubElement(coll, "TRACK",
                TrackID=tid, Name=t.get("title") or "", Artist=t.get("artist_name") or "",
                Album=t.get("album") or "", Year=str(t.get("year") or ""),
                Genre="", Kind="", Size=str(t.get("file_size_mb") or ""),
                TotalTime=str(int(t.get("duration_sec") or 0)),
                DiscNumber="", TrackNumber=str(t.get("track_number") or ""),
                BitRate=str(t.get("bitrate_kbps") or ""), SampleRate=str(t.get("sample_rate") or ""),
                Location=loc, Remixer="", Tonality=key, Label=t.get("label") or "", Mix="")
            if bpm_val:
                ET.SubElement(el, "TEMPO", Bpm=bpm_val, Metro="4/4", Battito="1")
            for note in t.get("notes", []):
                if note.get("note_type") == "cue_point" and note.get("audio_timestamp"):
                    parts = note["audio_timestamp"].split(":")
                    try:
                        ms = (int(parts[0])*60+int(parts[1]))*1000 if len(parts)==2 \
                             else (int(parts[0])*3600+int(parts[1])*60+int(parts[2]))*1000
                    except Exception:
                        ms = 0
                    ET.SubElement(el, "POSITION_MARK", Name=note.get("note_text","")[:64],
                                  Type="0", Start=f"{ms/1000:.3f}", Num="-1")
            existing[tid] = el

        coll.set("Entries", str(len(coll.findall("TRACK"))))

        # Replace this playlist's NODE (remove old if present, append fresh)
        for node in list(root_node.findall("NODE")):
            if node.get("Name") == playlist_name:
                root_node.remove(node)
        pln = ET.SubElement(root_node, "NODE", Type="1", Name=playlist_name,
                            Entries=str(len(pl_ids)), KeyType="0")
        for tid in pl_ids:
            ET.SubElement(pln, "TRACK", Key=tid)
        root_node.set("Count", str(len(root_node.findall("NODE"))))

        ET.indent(root, space="  ")
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
        MASTER_XML.write_text(xml_str, encoding="utf-8")
    except Exception:
        pass


@app.route("/api/set/<int:sid>/download/rekordbox", methods=["POST"])
def api_download_set_rekordbox(sid):
    """Save Rekordbox XML to the MusicRoom export folder and reveal in Finder."""
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404
    tracks = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               f.file_path,f.file_format,f.duration_sec,f.bpm,f.key_note,f.key_scale,
               f.file_size_mb,f.bitrate_kbps,f.sample_rate
        FROM set_tracks st JOIN tracks t ON t.id=st.track_id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE st.set_id=? AND t.is_deleted=0 ORDER BY st.position
    """, (sid,))
    for t in tracks:
        t["notes"] = query("SELECT * FROM notes WHERE track_id=? ORDER BY created_at", (t["id"],))
    xml = _rekordbox_xml(tracks, s["name"])
    filepath = _rb_export_path(s["name"], "set")
    filepath.write_text(xml, encoding="utf-8")
    _update_master_xml(tracks, s["name"])
    _reveal_in_finder(str(filepath))
    return jsonify({"ok": True, "filename": filepath.name, "folder": str(filepath.parent)})


@app.route("/api/place/<int:pid>/download/rekordbox", methods=["POST"])
def api_download_place_rekordbox(pid):
    """Save place Rekordbox XML to the MusicRoom export folder and reveal in Finder."""
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404
    tracks = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,t.track_number,
               f.file_path,f.file_format,f.duration_sec,f.bpm,f.key_note,f.key_scale,
               f.file_size_mb,f.bitrate_kbps,f.sample_rate
        FROM tracks t JOIN place_tracks pt ON pt.track_id=t.id
        LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE pt.place_id=? AND t.is_deleted=0 ORDER BY t.artist_name,t.title
    """, (pid,))
    for t in tracks:
        t["notes"] = query("SELECT * FROM notes WHERE track_id=? ORDER BY created_at", (t["id"],))
    xml = _rekordbox_xml(tracks, place["name"])
    filepath = _rb_export_path(place["name"], "place")
    filepath.write_text(xml, encoding="utf-8")
    _update_master_xml(tracks, place["name"])
    _reveal_in_finder(str(filepath))
    return jsonify({"ok": True, "filename": filepath.name, "folder": str(filepath.parent)})


@app.route("/api/place/<int:pid>/sets")
def api_place_sets(pid):
    """All sets assigned to a place."""
    sets = query("""
        SELECT s.id, s.name, s.set_date, s.set_type,
               COUNT(st.id) as track_count
        FROM sets s
        LEFT JOIN set_tracks st ON st.set_id = s.id
        WHERE s.place_id = ?
        GROUP BY s.id
        ORDER BY s.set_date DESC, s.created_at DESC
    """, (pid,))
    return jsonify(sets)


# =============================================================
# API — ENGINE DJ EXPORT
# Writes a proper Engine DJ Database2/m.db inside a zip.
# Unzip to the root of a FAT32/exFAT USB drive → plug into SC Live 4.
# =============================================================

def _engine_track_rows(track_rows):
    """Enrich track rows with primary file info for the Engine DJ exporter."""
    tracks = []
    for t in track_rows:
        f = query_one("""
            SELECT file_path, file_format, duration_sec, bpm, key_note, key_scale,
                   has_analysis, bitrate_kbps, file_size_mb
            FROM files WHERE track_id=? AND is_primary=1 AND is_deleted=0
        """, (t["id"],))
        row = dict(t)
        if f:
            row.update(f)
        tracks.append(row)
    return tracks


@app.route("/api/place/<int:pid>/export/engine")
def api_export_place_engine(pid):
    place = query_one("SELECT * FROM places WHERE id=?", (pid,))
    if not place: return jsonify({"error": "not found"}), 404

    track_rows = query("""
        SELECT t.id, t.title, t.artist_name, t.album, t.year, t.label
        FROM tracks t JOIN place_tracks pt ON pt.track_id=t.id
        WHERE pt.place_id=? AND t.is_deleted=0 ORDER BY t.artist_name, t.title
    """, (pid,))

    from engine_exporter import build_engine_library
    tracks = _engine_track_rows(track_rows)
    zip_bytes, missing = build_engine_library(tracks, place["name"])

    safe = "".join(c for c in place["name"] if c.isalnum() or c in " -_").strip()
    resp = Response(zip_bytes, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="musicroom_engine_{safe.replace(" ","_")}.zip"'})
    if missing:
        resp.headers["X-Missing-Files"] = str(len(missing))
    return resp


@app.route("/api/set/<int:sid>/export/engine")
def api_export_set_engine(sid):
    s = query_one("SELECT * FROM sets WHERE id=?", (sid,))
    if not s: return jsonify({"error": "not found"}), 404

    track_rows = query("""
        SELECT t.id, t.title, t.artist_name, t.album, t.year, t.label
        FROM set_tracks st JOIN tracks t ON t.id=st.track_id
        WHERE st.set_id=? AND t.is_deleted=0 ORDER BY st.position
    """, (sid,))

    from engine_exporter import build_engine_library
    tracks = _engine_track_rows(track_rows)
    zip_bytes, missing = build_engine_library(tracks, s["name"])

    safe = "".join(c for c in s["name"] if c.isalnum() or c in " -_").strip()
    resp = Response(zip_bytes, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="musicroom_engine_set_{safe.replace(" ","_")}.zip"'})
    if missing:
        resp.headers["X-Missing-Files"] = str(len(missing))
    return resp


# =============================================================
# API — TAGS
# =============================================================

@app.route("/api/tags")
def api_tags():
    return jsonify(query("""
        SELECT t.*, COUNT(tt.track_id) as track_count
        FROM tags t LEFT JOIN track_tags tt ON tt.tag_id=t.id
        GROUP BY t.id ORDER BY t.name ASC
    """))


# =============================================================
# API — LEARN: STATS
# =============================================================

@app.route("/api/learn/stats")
def api_learn_stats():
    total = query_one("SELECT COUNT(*) as n FROM tracks WHERE is_deleted=0")["n"]
    years   = query("SELECT year, COUNT(*) as count FROM tracks WHERE year IS NOT NULL AND year>1900 AND is_deleted=0 GROUP BY year ORDER BY year")
    labels  = query("SELECT label, COUNT(*) as count FROM tracks WHERE label IS NOT NULL AND label!='' AND is_deleted=0 GROUP BY label ORDER BY count DESC LIMIT 20")
    formats = query("SELECT f.file_format, COUNT(DISTINCT f.track_id) as count FROM files f JOIN tracks t ON t.id=f.track_id WHERE f.is_deleted=0 AND t.is_deleted=0 AND f.is_primary=1 GROUP BY f.file_format ORDER BY count DESC")
    analysed = query_one("SELECT COUNT(DISTINCT f.track_id) as n FROM files f JOIN tracks t ON t.id=f.track_id WHERE f.has_analysis=1 AND f.is_primary=1 AND f.is_deleted=0 AND t.is_deleted=0")["n"]
    unique_artists = query_one("SELECT COUNT(DISTINCT artist_name) as n FROM tracks WHERE artist_name IS NOT NULL AND artist_name!='' AND is_deleted=0")["n"]
    unknown_artists = query_one("SELECT COUNT(*) as n FROM tracks WHERE (artist_name IS NULL OR artist_name='') AND is_deleted=0")["n"]

    enriched = 0
    genres_sorted, countries, gender = [], [], []
    if _table_exists("artist_profiles"):
        enriched = query_one("SELECT COUNT(*) as n FROM artist_profiles WHERE enriched_at IS NOT NULL")["n"]
        genres_raw = query("""
            SELECT ap.genres, COUNT(DISTINCT t.id) as count
            FROM artist_profiles ap JOIN tracks t ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE ap.genres IS NOT NULL AND t.is_deleted=0 GROUP BY ap.artist_name
        """)
        genre_counts = {}
        for row in genres_raw:
            if row.get("genres"):
                for g in row["genres"].split(","):
                    g = g.strip().lower()
                    if g: genre_counts[g] = genre_counts.get(g,0) + row["count"]
        genres_sorted = [{"genre": g, "count": c} for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])[:20]]
        countries = query("""
            SELECT ap.country, COUNT(DISTINCT t.id) as count
            FROM artist_profiles ap JOIN tracks t ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE ap.country IS NOT NULL AND t.is_deleted=0
            GROUP BY ap.country ORDER BY count DESC LIMIT 20
        """)
        gender = query("""
            SELECT ap.gender, COUNT(DISTINCT t.id) as count
            FROM artist_profiles ap JOIN tracks t ON LOWER(t.artist_name)=LOWER(ap.artist_name)
            WHERE ap.gender IS NOT NULL AND t.is_deleted=0
            GROUP BY ap.gender ORDER BY count DESC
        """)

    quiz_summary = {"total_sessions": 0, "total_correct": 0, "total_questions": 0}
    if _table_exists("quiz_sessions"):
        qs = query_one("SELECT COUNT(*) as sessions, SUM(score) as correct, SUM(total) as questions FROM quiz_sessions WHERE completed=1")
        if qs:
            quiz_summary = {"total_sessions": qs["sessions"] or 0, "total_correct": qs["correct"] or 0, "total_questions": qs["questions"] or 0}

    return jsonify({
        "total_tracks": total, "total_artists": unique_artists, "unknown_artists": unknown_artists,
        "analysed": analysed, "enriched_artists": enriched,
        "years": years, "labels": labels, "formats": formats,
        "genres": genres_sorted, "countries": countries, "gender": gender,
        "quiz": quiz_summary,
    })


@app.route("/api/learn/artists")
def api_learn_artists():
    if not _table_exists("artist_profiles"): return jsonify([])
    return jsonify(query("""
        SELECT ap.artist_name, ap.country, ap.city, ap.latitude, ap.longitude,
               ap.genres, ap.scene, ap.active_from, ap.active_to,
               ap.gender, ap.bio_summary, COUNT(DISTINCT t.id) as track_count
        FROM artist_profiles ap JOIN tracks t ON LOWER(t.artist_name)=LOWER(ap.artist_name)
        WHERE ap.latitude IS NOT NULL AND ap.longitude IS NOT NULL AND t.is_deleted=0
        GROUP BY ap.artist_name ORDER BY track_count DESC
    """))


@app.route("/api/learn/artist/<path:artist_name>")
def api_learn_artist(artist_name):
    if not _table_exists("artist_profiles"): return jsonify({"error": "not found"}), 404
    profile = query_one("SELECT * FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?)", (artist_name,))
    tracks  = query("""
        SELECT t.id,t.title,t.album,t.year,t.label,
               f.bpm,f.key_note,f.key_scale,f.duration_sec
        FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE LOWER(t.artist_name)=LOWER(?) AND t.is_deleted=0 ORDER BY t.year,t.title
    """, (artist_name,))
    return jsonify({"profile": profile, "tracks": tracks})


# =============================================================
# API — LEARN: ENRICHMENT
# =============================================================

ENRICH_PROMPT = """You are a music encyclopedia. Given an artist name, return a JSON object with factual information.

Artist: {artist_name}

Return ONLY valid JSON, no preamble, no markdown. Use null for anything uncertain.

{{
    "country": "Country of origin. null if unknown.",
    "city": "City most associated with them. null if unknown.",
    "latitude": numeric latitude or null,
    "longitude": numeric longitude or null,
    "active_from": year started as integer or null,
    "active_to": year stopped as integer or null,
    "gender": "male|female|non-binary|duo|group|null",
    "ethnicity_notes": "Brief factual note e.g. 'Nigerian-British'. null if not publicly known.",
    "genres": "comma-separated genres, most specific first",
    "labels": "comma-separated main labels",
    "scene": "One sentence on their scene/era.",
    "influences": "comma-separated artists who influenced them",
    "bio_summary": "2-3 factual sentences. No hype.",
    "notable_works": "comma-separated key releases",
    "wikipedia_note": "Yes|Discogs only|Limited online presence"
}}"""


@app.route("/api/learn/enrich_artist", methods=["POST"])
def api_enrich_artist():
    d           = request.get_json() or {}
    artist_name = d.get("artist_name","").strip()
    force       = d.get("force", False)
    if not artist_name: return jsonify({"error": "artist_name required"}), 400

    if not force and _table_exists("artist_profiles"):
        ex = query_one("SELECT * FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?)", (artist_name,))
        if ex and ex.get("enriched_at"):
            return jsonify({"ok": True, "cached": True, "profile": ex})

    api_key = os.environ.get("ANTHROPIC_API_KEY","") or request.headers.get("X-API-Key","")
    if not api_key: return jsonify({"error": "No API key. Set ANTHROPIC_API_KEY env var."}), 401

    try:
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 800,
            "messages": [{"role": "user", "content": ENRICH_PROMPT.format(artist_name=artist_name)}],
        }).encode("utf-8")
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload,
                                     headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = _json.loads(resp.read())["content"][0]["text"]
        text = _re.sub(r'^```[a-z]*\n?', '', text.strip())
        text = _re.sub(r'\n?```$', '', text.strip())
        pd   = _json.loads(text)
    except Exception as e:
        if _table_exists("artist_profiles"):
            execute("INSERT INTO artist_profiles (artist_name,fetch_error,updated_at) VALUES (?,?,datetime('now')) ON CONFLICT(artist_name) DO UPDATE SET fetch_error=excluded.fetch_error, updated_at=datetime('now')", (artist_name, str(e)))
        return jsonify({"error": str(e)}), 500

    execute("""
        INSERT INTO artist_profiles (
            artist_name,country,city,latitude,longitude,active_from,active_to,
            gender,ethnicity_notes,genres,labels,scene,influences,bio_summary,
            notable_works,wikipedia_note,enriched_at,fetch_error,updated_at
        ) VALUES (
            :artist_name,:country,:city,:latitude,:longitude,:active_from,:active_to,
            :gender,:ethnicity_notes,:genres,:labels,:scene,:influences,:bio_summary,
            :notable_works,:wikipedia_note,datetime('now'),NULL,datetime('now')
        )
        ON CONFLICT(artist_name) DO UPDATE SET
            country=excluded.country, city=excluded.city,
            latitude=excluded.latitude, longitude=excluded.longitude,
            active_from=excluded.active_from, active_to=excluded.active_to,
            gender=excluded.gender, ethnicity_notes=excluded.ethnicity_notes,
            genres=excluded.genres, labels=excluded.labels, scene=excluded.scene,
            influences=excluded.influences, bio_summary=excluded.bio_summary,
            notable_works=excluded.notable_works, wikipedia_note=excluded.wikipedia_note,
            enriched_at=datetime('now'), fetch_error=NULL, updated_at=datetime('now')
    """, {"artist_name":artist_name, "country":pd.get("country"), "city":pd.get("city"),
          "latitude":pd.get("latitude"), "longitude":pd.get("longitude"),
          "active_from":pd.get("active_from"), "active_to":pd.get("active_to"),
          "gender":pd.get("gender"), "ethnicity_notes":pd.get("ethnicity_notes"),
          "genres":pd.get("genres"), "labels":pd.get("labels"), "scene":pd.get("scene"),
          "influences":pd.get("influences"), "bio_summary":pd.get("bio_summary"),
          "notable_works":pd.get("notable_works"), "wikipedia_note":pd.get("wikipedia_note")})

    profile = query_one("SELECT * FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?)", (artist_name,))
    return jsonify({"ok": True, "cached": False, "profile": profile})


# =============================================================
# API — LEARN: QUIZ
# =============================================================

@app.route("/api/learn/quiz/generate", methods=["POST"])
def api_quiz_generate():
    d     = request.get_json() or {}
    mode  = d.get("mode","mixed")
    count = min(int(d.get("count",10)), 20)

    pool = query("""
        SELECT t.id,t.title,t.artist_name,t.album,t.year,t.label,
               f.id as file_id,f.bpm,f.key_note,f.key_scale,f.has_analysis
        FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1
        WHERE t.is_deleted=0 AND t.artist_name IS NOT NULL AND t.artist_name!=''
        ORDER BY RANDOM() LIMIT 200
    """)
    if not pool: return jsonify({"error": "Not enough tracks"}), 400

    type_map = {
        "artist":  ["artist_origin","artist_genre"],
        "context": ["track_label","track_year"],
        "theory":  ["track_bpm","track_key"],
        "ear":     ["ear_artist","ear_title"],
        "notes":   ["notes_recall"],
        "mixed":   ["artist_origin","artist_genre","track_label","track_year","track_bpm","track_key","ear_artist","notes_recall"],
    }
    q_types = type_map.get(mode, type_map["mixed"])

    all_artists = list({t["artist_name"] for t in pool})
    all_labels  = list({t["label"]       for t in pool if t.get("label")})
    all_years   = list({t["year"]        for t in pool if t.get("year")})
    all_bpms    = list({round(t["bpm"])  for t in pool if t.get("bpm")})
    key_opts    = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]

    def choices(correct, pool_vals, n=4):
        d_vals = [x for x in pool_vals if str(x) != str(correct)]
        chosen = random.sample(d_vals, min(n-1, len(d_vals)))
        opts   = [correct] + chosen
        random.shuffle(opts)
        return [str(o) for o in opts]

    questions, used = [], set()

    for _ in range(count * 4):
        if len(questions) >= count: break
        track = random.choice(pool)
        qtype = random.choice(q_types)
        tid   = track["id"]
        if tid in used: continue
        q = None

        if qtype == "artist_origin":
            if not _table_exists("artist_profiles"): continue
            prof = query_one("SELECT country,city,scene,bio_summary FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?) AND enriched_at IS NOT NULL", (track["artist_name"],))
            if not prof or not prof.get("country"): continue
            c    = prof["country"]
            opts = [r["country"] for r in query("SELECT DISTINCT country FROM artist_profiles WHERE country IS NOT NULL ORDER BY RANDOM() LIMIT 20")]
            q = {"type":"artist_origin","track_id":tid,
                 "question":f"Where is {track['artist_name']} from?",
                 "choices":choices(c, opts),
                 "correct_answer":c,
                 "explanation":f"{track['artist_name']} is from {prof.get('city') or c}. {prof.get('scene') or ''} {prof.get('bio_summary') or ''}".strip()}

        elif qtype == "artist_genre":
            if not _table_exists("artist_profiles"): continue
            prof = query_one("SELECT genres,scene FROM artist_profiles WHERE LOWER(artist_name)=LOWER(?) AND enriched_at IS NOT NULL", (track["artist_name"],))
            if not prof or not prof.get("genres"): continue
            primary = prof["genres"].split(",")[0].strip()
            gpool   = []
            for r in query("SELECT genres FROM artist_profiles WHERE genres IS NOT NULL"):
                gpool.extend([g.strip() for g in r["genres"].split(",")])
            q = {"type":"artist_genre","track_id":tid,
                 "question":f"What genre is {track['artist_name']} most associated with?",
                 "choices":choices(primary, list(set(gpool))),
                 "correct_answer":primary,
                 "explanation":f"{track['artist_name']} — {prof.get('genres')}. {prof.get('scene') or ''}".strip()}

        elif qtype == "track_label":
            if not track.get("label"): continue
            q = {"type":"track_label","track_id":tid,
                 "question":f"Which label released '{track['title']}' by {track['artist_name']}?",
                 "choices":choices(track["label"], all_labels),
                 "correct_answer":track["label"],
                 "explanation":f"'{track['title']}' by {track['artist_name']} was released on {track['label']}{', ' + str(track['year']) if track.get('year') else ''}."}

        elif qtype == "track_year":
            if not track.get("year"): continue
            y = track["year"]
            q = {"type":"track_year","track_id":tid,
                 "question":f"What year was '{track['title']}' by {track['artist_name']} released?",
                 "choices":choices(y, [y-3,y-1,y+1,y+2,y+4] + all_years),
                 "correct_answer":str(y),
                 "explanation":f"'{track['title']}' was released in {y}."}

        elif qtype == "track_bpm":
            if not track.get("bpm"): continue
            bpm = round(track["bpm"])
            q = {"type":"track_bpm","track_id":tid,
                 "question":f"What is the BPM of '{track['title']}' by {track['artist_name']}?",
                 "choices":choices(bpm, [bpm+d for d in [-8,-4,4,8,-12,12,-6,6]] + all_bpms),
                 "correct_answer":str(bpm),
                 "explanation":f"'{track['title']}' runs at {track['bpm']} BPM."}

        elif qtype == "track_key":
            if not track.get("key_note"): continue
            k = f"{track['key_note']} {track.get('key_scale','major')}"
            q = {"type":"track_key","track_id":tid,
                 "question":f"What key is '{track['title']}' by {track['artist_name']} in?",
                 "choices":choices(k, [f"{n} {s}" for n in key_opts for s in ["major","minor"]]),
                 "correct_answer":k,
                 "explanation":f"'{track['title']}' is in {k}."}

        elif qtype == "ear_artist":
            if not track.get("file_id"): continue
            q = {"type":"ear_artist","track_id":tid,"file_id":track["file_id"],
                 "question":"Who made this track? Listen to the clip.",
                 "choices":choices(track["artist_name"], all_artists),
                 "correct_answer":track["artist_name"],
                 "explanation":f"That was '{track['title']}' by {track['artist_name']}{', ' + str(track['year']) if track.get('year') else ''}."}

        elif qtype == "ear_title":
            if not track.get("file_id") or not track.get("title"): continue
            all_titles = [t["title"] for t in pool if t.get("title") and t["id"] != tid]
            q = {"type":"ear_title","track_id":tid,"file_id":track["file_id"],
                 "question":f"What is this track by {track['artist_name']} called?",
                 "choices":choices(track["title"], all_titles),
                 "correct_answer":track["title"],
                 "explanation":f"That was '{track['title']}' by {track['artist_name']}."}

        elif qtype == "notes_recall":
            notes = query("SELECT note_text FROM notes WHERE track_id=? AND note_type!='voice' AND note_text!='' ORDER BY RANDOM() LIMIT 1", (tid,))
            if not notes or len(notes[0]["note_text"]) < 10: continue
            note_text = notes[0]["note_text"]
            all_titles = [t["title"] for t in pool if t.get("title") and t["id"] != tid]
            q = {"type":"notes_recall","track_id":tid,
                 "question":f"You wrote: \"{note_text[:120]}{'...' if len(note_text)>120 else ''}\" — which track?",
                 "choices":choices(track["title"] or "Untitled", all_titles),
                 "correct_answer":track["title"] or "Untitled",
                 "explanation":f"That note was about '{track['title']}' by {track['artist_name']}."}

        if q:
            used.add(tid)
            questions.append(q)

    if not questions: return jsonify({"error": "Could not generate questions. Enrich artists first or add notes."}), 400
    return jsonify({"questions": questions, "mode": mode, "count": len(questions)})


@app.route("/api/learn/quiz/save", methods=["POST"])
def api_quiz_save():
    if not _table_exists("quiz_sessions"): return jsonify({"error": "run migrate.py"}), 500
    d       = request.get_json() or {}
    answers = d.get("answers", [])
    score   = sum(1 for a in answers if a.get("is_correct"))
    total   = len(answers)
    sid     = execute("INSERT INTO quiz_sessions (mode,scope,score,total,completed,finished_at) VALUES (?,?,?,?,1,datetime('now'))",
                      (d.get("mode","mixed"), d.get("scope","all"), score, total))
    for a in answers:
        execute("INSERT INTO quiz_questions (session_id,track_id,question_type,question_text,correct_answer,user_answer,is_correct,explanation) VALUES (?,?,?,?,?,?,?,?)",
                (sid, a.get("track_id"), a.get("type"), a.get("question"), a.get("correct_answer"), a.get("user_answer"), 1 if a.get("is_correct") else 0, a.get("explanation")))
    return jsonify({"ok": True, "session_id": sid, "score": score, "total": total})


@app.route("/api/learn/quiz/history")
def api_quiz_history():
    if not _table_exists("quiz_sessions"): return jsonify([])
    return jsonify(query("SELECT id,mode,scope,score,total,started_at,finished_at FROM quiz_sessions WHERE completed=1 ORDER BY finished_at DESC LIMIT 20"))


@app.route("/api/learn/quiz/review/<int:sid>")
def api_quiz_review(sid):
    if not _table_exists("quiz_questions"): return jsonify([])
    return jsonify(query("""
        SELECT qq.*, t.title, t.artist_name
        FROM quiz_questions qq LEFT JOIN tracks t ON t.id=qq.track_id
        WHERE qq.session_id=? ORDER BY qq.id
    """, (sid,)))


# =============================================================
# ANALYSE PAGE + BATCH ANALYSIS API
# =============================================================

_analysis_proc    = None
_analysis_log     = []
_analysis_session = {"done": 0, "failed": 0, "started_at": None}


@app.route("/analyse")
def analyse_page():
    return render_template("analyse.html")


@app.route("/api/analysis/stats")
def api_analysis_stats():
    total     = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0")["n"]
    done      = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND has_analysis=1")["n"]
    remaining = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND has_analysis=0 AND (analysis_status IS NULL OR analysis_status NOT IN ('empty','missing','locked'))")["n"]
    failed    = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND analysis_status='failed'")["n"]
    return jsonify({"total": total, "done": done, "remaining": remaining, "failed": failed})


@app.route("/api/analysis/start", methods=["POST"])
def api_analysis_start():
    global _analysis_proc, _analysis_log, _analysis_session

    if _analysis_proc and _analysis_proc.poll() is None:
        return jsonify({"ok": False, "error": "Analysis already running"})

    import subprocess as _subprocess
    import datetime as _datetime

    cmd = mr_paths.worker_command(["--analyse-missing"], dev_base_dir=BASE_DIR)

    _analysis_log     = []
    _analysis_session = {"done": 0, "failed": 0, "started_at": _datetime.datetime.now().isoformat()}

    try:
        _analysis_proc = _subprocess.Popen(
            cmd,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=BASE_DIR,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    def _reader(proc):
        import re as _re2
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            level = "info"
            llow  = line.lower()
            # Real failure markers from importer.py
            if "analysis failed" in llow or llow.startswith("  failed") or "failed (" in llow:
                level = "err"
            elif "missing:" in llow or "file not on disk" in llow:
                level = "err"
            elif "interrupted" in llow:
                level = "err"
            elif "analysis complete" in llow:
                level = "ok"
            # Update progress counters from "  [123/456] filename" lines
            m = _re2.match(r'\s*\[(\d+)/(\d+)\]', line)
            if m:
                _analysis_session["done"] = int(m.group(1))
            _analysis_log.append({"text": line, "level": level})
            if len(_analysis_log) > 500:
                _analysis_log.pop(0)
        proc.stdout.close()

    threading.Thread(target=_reader, args=(_analysis_proc,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/import/start", methods=["POST"])
def api_import_start():
    """Spawn the importer scoped to a user-supplied folder path. Reuses the
    same subprocess slot + log stream as the analyser (only one heavy CPU
    job at a time makes sense). Returns immediately; poll /api/analysis/status
    for progress and stop via /api/analysis/stop."""
    global _analysis_proc, _analysis_log, _analysis_session

    if _analysis_proc and _analysis_proc.poll() is None:
        return jsonify({"ok": False, "error": "Another job is already running"}), 409

    d = request.get_json() or {}
    path = (d.get("path") or "").strip()
    analyse = bool(d.get("analyse"))
    enrich  = bool(d.get("enrich"))      # Full analysis: artwork + label + culture
    if enrich:
        analyse = True                   # Full always includes audio analysis
    dup_policy = (d.get("dup_policy") or "skip").strip()
    if dup_policy not in ("skip", "import", "delete"):
        dup_policy = "skip"

    # Which formats to import — a subset chosen in the UI, defaulting to all.
    # Sanitised against the supported set; an empty/garbage list falls back to all.
    _ALLOWED_FORMATS = {"mp3", "flac", "wav", "m4a"}
    formats = d.get("formats")
    if isinstance(formats, list):
        formats = [str(f).strip().lower().lstrip(".") for f in formats]
        formats = [f for f in formats if f in _ALLOWED_FORMATS]
    else:
        formats = None
    if not formats:
        formats = None   # None = all supported formats

    # Expand ~ and resolve symlinks; reject anything that isn't a directory.
    if not path:
        return jsonify({"ok": False, "error": "Path required"}), 400
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "error": f"Not a directory: {path}"}), 400

    # Remember this path so the field stays populated next visit.
    try:
        set_setting("last_import_path", path)
    except Exception:
        pass

    import subprocess as _subprocess
    import datetime as _datetime

    worker_args = ["--path", path, "--dup-policy", dup_policy]
    if formats:
        worker_args += ["--formats", ",".join(formats)]
    if analyse:
        worker_args.append("--analyse")
    if enrich:
        worker_args.append("--enrich")
    args = mr_paths.worker_command(worker_args, dev_base_dir=BASE_DIR)

    _analysis_log = []
    _analysis_session = {
        "done": 0, "failed": 0,
        "started_at": _datetime.datetime.now().isoformat(),
        "job": "import",
        "path": path,
        "analyse": analyse,
        "enrich": enrich,
    }

    try:
        _analysis_proc = _subprocess.Popen(
            args,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=BASE_DIR,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    def _reader(proc):
        import re as _re2
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            level = "info"
            llow  = line.lower()
            if "failed" in llow or "error" in llow:
                level = "err"
            elif "import complete" in llow:
                level = "ok"
            m = _re2.match(r'\s*\[(\d+)/(\d+)\]', line)
            if m:
                _analysis_session["done"] = int(m.group(1))
                _analysis_session["total_files"] = int(m.group(2))
            _analysis_log.append({"text": line, "level": level})
            if len(_analysis_log) > 1000:
                _analysis_log.pop(0)
        proc.stdout.close()

    threading.Thread(target=_reader, args=(_analysis_proc,), daemon=True).start()
    return jsonify({"ok": True, "path": path, "analyse": analyse})


@app.route("/api/analysis/stop", methods=["POST"])
def api_analysis_stop():
    """
    Stop the running analyser.
    SIGTERM first (lets importer commit progress), then SIGKILL after a
    short wait if it didn't honor the terminate (librosa C calls don't
    interrupt mid-decode, so a hard kill is sometimes needed).
    """
    global _analysis_proc
    if not _analysis_proc or _analysis_proc.poll() is not None:
        return jsonify({"ok": False, "error": "No analysis running"})

    proc = _analysis_proc

    def _stop():
        try:
            proc.terminate()
        except Exception:
            pass
        # Give the importer ~4s to flush + exit cleanly. If it hasn't
        # exited by then, hard-kill it.
        import time as _t
        for _ in range(40):
            if proc.poll() is not None:
                _analysis_log.append({"text": "  Stopped cleanly.", "level": "ok"})
                return
            _t.sleep(0.1)
        try:
            proc.kill()
            _analysis_log.append({"text": "  Force-killed (didn't respond to terminate).", "level": "err"})
        except Exception as e:
            _analysis_log.append({"text": f"  Could not kill process: {e}", "level": "err"})

    threading.Thread(target=_stop, daemon=True).start()
    _analysis_log.append({"text": "  Stopping analysis...", "level": "info"})
    return jsonify({"ok": True})


@app.route("/api/analysis/status")
def api_analysis_status_batch():
    running = bool(_analysis_proc and _analysis_proc.poll() is None)

    total     = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0")["n"]
    done      = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND has_analysis=1")["n"]
    remaining = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND has_analysis=0 AND (analysis_status IS NULL OR analysis_status NOT IN ('empty','missing','locked'))")["n"]
    failed_db = query_one("SELECT COUNT(*) as n FROM files WHERE is_primary=1 AND is_deleted=0 AND analysis_status='failed'")["n"]

    offset  = int(request.args.get("offset", 0))
    new_log = _analysis_log[offset:]

    return jsonify({
        "running":        running,
        "total":          total,
        "done":           done,
        "remaining":      remaining,
        "failed":         failed_db,
        "log":            new_log,
        "log_total":      len(_analysis_log),
        "session_done":   _analysis_session.get("done", 0),
        "session_failed": _analysis_session.get("failed", 0),
        "started_at":     _analysis_session.get("started_at"),
        "session":        _analysis_session,
    })


# =============================================================
# ARTWORK
# =============================================================

STICKERS_DIR       = Path(mr_paths.STICKERS_DIR)
OVERLAY_IMAGES_DIR = Path(mr_paths.OVERLAY_IMAGES_DIR)
ARTWORK_LOCAL_DIR  = Path(mr_paths.ARTWORK_LOCAL_DIR)
STICKERS_DIR.mkdir(parents=True, exist_ok=True)
OVERLAY_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
ARTWORK_LOCAL_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_artwork_path(stored_path: str) -> str | None:
    """Find artwork file across mounted volumes (same logic as audio path)."""
    if not stored_path:
        return None
    if os.path.exists(stored_path):
        return stored_path
    p = Path(stored_path)
    parts = p.parts
    try:
        vol_idx = parts.index("Volumes")
        rel = Path(*parts[vol_idx + 2:])
    except (ValueError, TypeError):
        return None
    volumes = Path("/Volumes")
    if volumes.exists():
        for vol in volumes.iterdir():
            candidate = vol / rel
            if candidate.exists():
                return str(candidate)
    # Also try local fallback
    local = ARTWORK_LOCAL_DIR / p.name
    if local.exists():
        return str(local)
    return None


def _artwork_version(stored_path):
    """A per-track artwork cache token = the cover file's mtime. It changes the
    instant set-primary repoints artwork_path to a new file, so a ?v=<art_v> on
    the artwork URL busts the browser cache exactly when the cover changes and
    never otherwise. 0 = no / unresolvable art."""
    try:
        fp = _resolve_artwork_path(stored_path)
        return int(os.path.getmtime(fp)) if fp else 0
    except Exception:
        return 0


# Thumbnail cache for artwork: full covers can be multi-MB files; a grid of
# hundreds of them must serve small JPEGs or scrolling janks. Sizes are fixed
# buckets so the cache stays small; generated on first request, then disk-served.
_THUMB_SIZES = (192, 384, 768, 1280)       # 1280: two or three records across a sharp screen (made on request only)
_THUMB_DIR = os.path.join(DATA_DIR, "static", "artwork_thumbs")
# "full" = the laptop's own full-quality copy of the cover (the original bytes
# where they're already a JPEG of sane size, else re-encoded large) — so the
# one-record view and the hero are as sharp with the drive unplugged as with it
_THUMB_FULL = "full"
_THUMB_FULL_MAX = 2000


def _artwork_thumb_cached(tid, size):
    """An existing laptop copy of this track's cover: the requested bucket or
    the nearest one above it (the full copy counts as the biggest bucket), else
    the largest there is. No size = the full copy, else the largest. None if
    nothing was ever made. Never touches the original (it may be unreachable)."""
    ladder = ([b for b in _THUMB_SIZES if b >= size] + [_THUMB_FULL]) if size else [_THUMB_FULL]
    ladder += sorted(_THUMB_SIZES, reverse=True)
    for b in ladder:
        out = os.path.join(_THUMB_DIR, str(b), f"{tid}.jpg")
        if os.path.isfile(out):
            return out
    return None


def _artwork_full(fp, tid):
    """Keep a full-quality copy of the cover on the laptop. A JPEG no bigger
    than _THUMB_FULL_MAX on a side is copied byte for byte; anything else is
    re-encoded large at high quality. Returns the copy's path, or None."""
    out_dir = os.path.join(_THUMB_DIR, _THUMB_FULL)
    out = os.path.join(out_dir, f"{tid}.jpg")
    try:
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(fp):
            return out
        from PIL import Image
        os.makedirs(out_dir, exist_ok=True)
        with Image.open(fp) as im:
            w, h = im.size
            if im.format == "JPEG" and max(w, h) <= _THUMB_FULL_MAX:
                import shutil
                shutil.copyfile(fp, out)
                return out
            im = im.convert("RGB")
            im.thumbnail((_THUMB_FULL_MAX, _THUMB_FULL_MAX), Image.LANCZOS)
            im.save(out, "JPEG", quality=94, subsampling=0)
        return out
    except Exception:
        return None


# Covers live beside the music — usually on the external drive. Whenever the
# drive is reachable, quietly make the laptop's own copies (three thumb sizes +
# a full-quality one) of any cover that doesn't have them yet, so the room still shows every sleeve with the
# drive unplugged (Daithí, 2026-10-22: "I can't see the album covers"). Cheap:
# a path check per cover, PIL work on a thread, and nothing to do once warm.
_artwork_warm_state = {"last": 0.0, "running": False, "done": 0, "total": 0, "vols": None}

@app.before_request
def _artwork_thumbs_warm_maybe():
    now = _time.time()
    if _artwork_warm_state["running"] or now - _artwork_warm_state["last"] < 600:
        return
    _artwork_warm_state["last"] = now
    try:
        rows = query("SELECT id, artwork_path FROM tracks WHERE is_deleted=0 AND artwork_path IS NOT NULL")
        todo = []
        for r in rows:
            fp = _resolve_artwork_path(r["artwork_path"])
            if not fp:
                continue
            if all(os.path.isfile(os.path.join(_THUMB_DIR, str(b), f"{r['id']}.jpg")) for b in (192, 384, 768, _THUMB_FULL)):
                continue
            todo.append((r["id"], fp))
        if not todo:
            return

        def work():
            try:
                for tid, fp in todo:
                    for b in (192, 384, 768):
                        _artwork_thumb(fp, tid, b)
                    _artwork_full(fp, tid)      # the full-quality copy (Daithí, 2026-10-27:
                                                # "full quality like they used to be")
                    _artwork_warm_state["done"] += 1
            finally:
                _artwork_warm_state["running"] = False
        _artwork_warm_state["done"], _artwork_warm_state["total"] = 0, len(todo)
        _artwork_warm_state["running"] = True
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        _artwork_warm_state["running"] = False


def _artwork_thumb(fp, tid, size):
    """Return the path of a cached `size`px JPEG thumb of `fp` (making it if
    needed), or None if the source can't be decoded."""
    out_dir = os.path.join(_THUMB_DIR, str(size))
    out = os.path.join(out_dir, f"{tid}.jpg")
    try:
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(fp):
            return out
        from PIL import Image
        os.makedirs(out_dir, exist_ok=True)
        with Image.open(fp) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            im.save(out, "JPEG", quality=82 if size <= 384 else 90)   # the big ones are looked at closely
        return out
    except Exception:
        return None


@app.route("/api/artwork/<int:tid>")
def api_serve_artwork(tid):
    t = query_one("SELECT artwork_path FROM tracks WHERE id=?", (tid,))
    if not t:
        abort(404)
    fp = _resolve_artwork_path(t["artwork_path"])
    s = request.args.get("s", type=int)
    if not fp:
        # The cover lives on a drive that isn't plugged in right now. The
        # laptop keeps its own copies (made whenever the drive IS here — see
        # _artwork_thumbs_warm_maybe): the thumb buckets and a full-quality
        # copy. A request with no size wants the full one.
        cached = _artwork_thumb_cached(tid, s)
        if cached:
            return send_file(cached, mimetype="image/jpeg", max_age=3600, conditional=True)
        abort(404)
    if s:
        # snap to the nearest bucket at or above the request (caps at the largest)
        size = next((b for b in _THUMB_SIZES if b >= s), _THUMB_SIZES[-1])
        thumb = _artwork_thumb(fp, tid, size)
        if thumb:
            return send_file(thumb, mimetype="image/jpeg", max_age=86400 * 30)
        # fall through to the original if the thumb couldn't be made
    ext = os.path.splitext(fp)[1].lower()
    mime = {"jpg": "image/jpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    # With a ?v=<mtime> token on every cover URL, the bytes for a given URL never
    # change — so it's safe to cache hard, and conditional revalidation closes the
    # heuristic-freshness hole for any un-versioned request.
    return send_file(fp, mimetype=mime, max_age=86400 * 30, conditional=True)


# ── the user's own wall of BLOCKS in a room (static/js/room_canvas.js) ───────
# One JSON document per room: {v, blocks:[{id,type,x,y,w,h,z,data}]}. Stored in
# settings (key room_canvas:<place_id>) — the user's own arrangement, like the
# room view memory, but on the server so it survives the shell's storage wipes.
_ROOM_CANVAS_DIR = os.path.join(_DATA_STATIC, "room_canvas")


def _room_canvas_load(pid):
    import json as _json
    import wall_links as _wl
    raw = get_setting(f"room_canvas:{pid}", "")
    try:
        d = _json.loads(raw) if raw else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("v", 1)
    if not isinstance(d.get("blocks"), list):
        d["blocks"] = []
    d["links"] = _wl.clean_links(d.get("links"), d["blocks"])          # the lines between blocks (see wall_links.py)
    d["next"] = _wl.next_number(d.get("next"), d["blocks"])            # the highest archive number ever given out (numbers are never reused)
    return d


@app.route("/api/room/canvas/<int:pid>")
def api_room_canvas_get(pid):
    return jsonify(_room_canvas_load(pid))


@app.route("/api/room/canvas/<int:pid>", methods=["POST"])
def api_room_canvas_save(pid):
    d = request.get_json(silent=True) or {}
    blocks = d.get("blocks")
    if not isinstance(blocks, list):
        return jsonify({"error": "blocks must be a list"}), 400
    if len(blocks) > 400:
        return jsonify({"error": "too many blocks"}), 400
    import json as _json
    import wall_links as _wl
    # Lines are kept when a caller that only knows about blocks (Explore's "save this moment", Keep) saves without
    # mentioning them: the existing lines stay, minus any whose block is gone.
    links = _wl.clean_links(d["links"] if "links" in d else _room_canvas_load(pid).get("links"), blocks)
    nxt = _wl.next_number(max(int(d["next"]) if isinstance(d.get("next"), (int, float)) and not isinstance(d.get("next"), bool) else 0,
                              _room_canvas_load(pid).get("next", 0)), blocks)             # it only ever goes up
    payload = _json.dumps({"v": 1, "blocks": blocks, "links": links, "next": nxt})
    if len(payload) > 2_000_000:
        return jsonify({"error": "too big"}), 413
    set_setting(f"room_canvas:{pid}", payload)
    return jsonify({"ok": True, "n": len(blocks), "links": len(links), "next": nxt})


@app.route("/api/room/canvas/image", methods=["POST"])
def api_room_canvas_image():
    """An image for a block: re-saved by PIL (never the raw upload), capped at
    1800px, into the app's own static folder. Returns its URL."""
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "no image"}), 400
    import uuid
    try:
        from PIL import Image
        os.makedirs(_ROOM_CANVAS_DIR, exist_ok=True)
        im = Image.open(f.stream)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if max(im.size) > 1800:
            im.thumbnail((1800, 1800), Image.LANCZOS)
        name = uuid.uuid4().hex
        if has_alpha:
            out = os.path.join(_ROOM_CANVAS_DIR, name + ".png")
            im.convert("RGBA").save(out, "PNG", optimize=True)
        else:
            out = os.path.join(_ROOM_CANVAS_DIR, name + ".jpg")
            im.convert("RGB").save(out, "JPEG", quality=90)
    except Exception:
        return jsonify({"error": "that file isn't an image I can read"}), 400
    return jsonify({"ok": True, "url": f"/static/room_canvas/{os.path.basename(out)}"})


def _track_audio_path(tid):
    """The track's resolved audio-file path (for reading embedded cover art)."""
    r = query_one("SELECT file_path FROM files WHERE track_id=? AND is_deleted=0 "
                  "ORDER BY is_primary DESC LIMIT 1", (tid,))
    return _resolve_audio_path(r["file_path"]) if r and r.get("file_path") else None


# Cover options the user can choose between, per source. 'embedded' = the file's
# own art; the rest are online. The CHOSEN one becomes the single artwork_path —
# the grid/cloud never juggle multiple covers per track.
_ART_CAND_SOURCES = (("embedded", "On the file"), ("itunes", "Apple / iTunes"),
                     ("caa", "MusicBrainz"), ("discogs", "Discogs"))

@app.route("/api/artwork/<int:tid>/candidates")
def api_artwork_candidates(tid):
    """The cover options for a track — its embedded art + online matches — so the
    user can pick a primary in the panel. Cached to disk per source (online sources
    only re-fetch with ?refresh=1; embedded always reflects the file)."""
    import io, concurrent.futures as _cf
    t = query_one("SELECT id, title, artist_name, artwork_source FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "not found"}), 404
    import artwork_fetcher as af
    cand_dir = os.path.join(_DATA_STATIC, "artwork_candidates")
    os.makedirs(cand_dir, exist_ok=True)
    refresh = request.args.get("refresh") == "1"
    fpath = _track_audio_path(tid)
    kw_for = {
        "embedded": {"file_path": fpath},
        "itunes":   {"title": t["title"], "artist": t["artist_name"]},
        "caa":      {"title": t["title"], "artist": t["artist_name"]},
        "discogs":  {"title": t["title"], "artist": t["artist_name"]},
    }
    def build(item):
        src, label = item
        out = os.path.join(cand_dir, f"{tid}__{src}.jpg")
        use_cache = os.path.exists(out) and src != "embedded" and not refresh
        if not use_cache:
            data = af.candidate_bytes(src, **kw_for[src])
            if data:
                try:
                    from PIL import Image
                    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=88)
                except Exception:
                    try:
                        with open(out, "wb") as fh:
                            fh.write(data)
                    except Exception:
                        return (src, label, None)
            elif not os.path.exists(out):
                return (src, label, None)
        return (src, label, out if os.path.exists(out) else None)
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        built = list(ex.map(build, _ART_CAND_SOURCES))
    cands = [{"source": s, "label": l,
              "url": f"/static/artwork_candidates/{tid}__{s}.jpg?v={int(os.path.getmtime(p))}",
              "is_current": t["artwork_source"] == s}
             for (s, l, p) in built if p]
    return jsonify({"candidates": cands, "current_source": t["artwork_source"]})

@app.route("/api/artwork/<int:tid>/set-primary", methods=["POST"])
def api_artwork_set_primary(tid):
    """Make a chosen candidate the track's primary cover. Single image — the grid
    and cloud need no change (they serve artwork_path). Busts the thumb cache and
    recomputes the dominant colour so the colour-sort + placeholder match."""
    if not query_one("SELECT id FROM tracks WHERE id=? AND is_deleted=0", (tid,)):
        return jsonify({"error": "not found"}), 404
    src = ((request.get_json() or {}).get("source") or "").strip()
    src = "caa" if src == "musicbrainz" else src
    if src not in ("embedded", "itunes", "caa", "discogs"):
        return jsonify({"error": "unknown source"}), 400
    cand = os.path.join(_DATA_STATIC, "artwork_candidates", f"{tid}__{src}.jpg")
    if not os.path.exists(cand):
        return jsonify({"error": "that cover isn't available — try refreshing"}), 404
    execute("UPDATE tracks SET artwork_path=?, artwork_source=?, artwork_verified=1 WHERE id=?",
            (cand, src, tid))
    for size in _THUMB_SIZES:                      # bust the grid/cloud thumb cache
        thumb = os.path.join(_THUMB_DIR, str(size), f"{tid}.jpg")
        try:
            if os.path.exists(thumb):
                os.remove(thumb)
        except Exception:
            pass
    try:                                           # recompute the cover colour
        from art_colour import dominant_colour
        col = dominant_colour(cand)
        if col:
            execute("UPDATE tracks SET art_colour=? WHERE id=?", (col, tid))
    except Exception:
        pass
    row = query_one("SELECT artwork_path, artwork_source, artwork_verified, art_colour FROM tracks WHERE id=?", (tid,))
    out = dict(row)
    out["art_v"] = _artwork_version(out.get("artwork_path"))   # new token → clients bust the cover URL
    return jsonify({"ok": True, **out})

@app.route("/api/artwork/<int:tid>/fetch", methods=["POST"])
def api_artwork_fetch(tid):
    t = query_one("SELECT id FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "not found"}), 404

    def _do():
        from artwork_fetcher import fetch_for_track
        fetch_for_track(tid)

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "queued": tid})


@app.route("/api/artwork/batch-stop", methods=["POST"])
def api_artwork_batch_stop():
    from artwork_fetcher import request_stop, get_batch_progress
    prog = get_batch_progress()
    if prog.get("running"):
        request_stop()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/artwork/fetch-batch", methods=["POST"])
def api_artwork_fetch_batch():
    from artwork_fetcher import run_batch_fetch, get_batch_progress
    prog = get_batch_progress()
    if prog.get("running"):
        return jsonify({"ok": False, "error": "already running", "progress": prog})
    d = request.get_json() or {}
    phase_only = d.get("phase_only")  # 1 = scan files only, 2 = search online only
    # Setting-driven default: skip tracks we've already searched ("not_found").
    # Caller can force-retry by passing retry_not_found: true.
    setting_default = (get_setting("artwork_retry_not_found", "") or "").strip() == "1"
    retry_not_found = bool(d.get("retry_not_found", setting_default))
    # Pre-flight count using the same exclusion rule the worker will use.
    EXCLUDED_BASE = ('embedded', 'musicbrainz', 'itunes', 'manual', 'spotify', 'discogs')
    excluded = EXCLUDED_BASE if retry_not_found else (EXCLUDED_BASE + ('not_found',))
    placeholders = ",".join("?" * len(excluded))
    eligible = query_one(
        f"SELECT COUNT(*) AS n FROM tracks "
        f"WHERE is_deleted=0 AND artwork_path IS NULL "
        f"  AND (artwork_source IS NULL OR artwork_source NOT IN ({placeholders}))",
        excluded,
    )["n"] or 0
    threading.Thread(
        target=run_batch_fetch,
        kwargs={"phase_only": phase_only, "retry_not_found": retry_not_found},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "retry_not_found": retry_not_found, "eligible": eligible})


# ── v19 perceptual fingerprint batch ────────────────────────────────
_fingerprint_progress = {
    "running":  False,
    "done":     0,
    "total":    0,
    "current":  "",
    "started":  None,
    "finished": None,
    "errors":   0,
}


def _run_fingerprint_batch(only_missing=True):
    import sqlite3 as _sql
    import datetime as _dt
    global _fingerprint_progress
    _fingerprint_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None, "errors": 0,
    }
    try:
        from importer import _compute_band_fingerprint, _load_via_ffmpeg
        import librosa, numpy as np
        conn = _connect_threadsafe()
        cur = conn.cursor()
        where = "WHERE has_analysis = 1"
        if only_missing:
            where += " AND band_presence IS NULL"
        rows = cur.execute(f"SELECT id, file_path FROM files {where} ORDER BY id ASC").fetchall()
        _fingerprint_progress["total"] = len(rows)
        for i, r in enumerate(rows, 1):
            if not _fingerprint_progress["running"]:
                # User clicked Stop. Commit what we've done so the work
                # so far is preserved, then bail.
                _fingerprint_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            path = _resolve_audio_path(r["file_path"])
            _fingerprint_progress["current"] = os.path.basename(r["file_path"])[:80]
            if not path or not os.path.exists(path):
                _fingerprint_progress["errors"] += 1
                _fingerprint_progress["done"] = i
                continue
            try:
                try:
                    y, sr = librosa.load(path, mono=True, sr=22050, duration=120)
                except Exception:
                    y, sr = _load_via_ffmpeg(path)
                stft  = np.abs(librosa.stft(y))
                freqs = librosa.fft_frequencies(sr=sr)
                bands = _compute_band_fingerprint(stft, freqs)
                p, b = bands.get("band_presence"), bands.get("band_brilliance")
                bright = (0.40 * p + 0.60 * b) if (p is not None and b is not None) else None
                cur.execute("""
                    UPDATE files SET
                        band_sub=?, band_bass=?, band_lowmid=?,
                        band_body=?, band_presence=?, band_brilliance=?,
                        brightness_score=?
                    WHERE id=?
                """, (
                    bands.get("band_sub"), bands.get("band_bass"), bands.get("band_lowmid"),
                    bands.get("band_body"), bands.get("band_presence"), bands.get("band_brilliance"),
                    bright,
                    r["id"],
                ))
                # Commit per-track so a Stop or crash preserves all work
                # done so far. WAL makes single-row commits cheap.
                conn.commit()
            except Exception:
                _fingerprint_progress["errors"] += 1
            _fingerprint_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _fingerprint_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _fingerprint_progress["running"] = False
        _fingerprint_progress["finished"] = _dt.datetime.utcnow().isoformat()


# ---- BPM refinement: re-estimate every stored tempo with the fixed analyser
# (the old beat_track snapped a whole library onto ~47 lattice values).
_bpm_refine_progress = {
    "running": False, "done": 0, "total": 0, "current": "",
    "started": None, "finished": None, "errors": 0, "missing": 0,
}


def _run_bpm_refine():
    import datetime as _dt
    global _bpm_refine_progress
    _bpm_refine_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None, "errors": 0, "missing": 0,
    }
    try:
        from importer import estimate_bpm, _load_via_ffmpeg
        import librosa
        conn = _connect_threadsafe()
        cur = conn.cursor()
        # only LIVE, un-refined, un-verified tracks: never touch deleted files
        # (they error and waste the pass) and never clobber a manually-set BPM
        rows = cur.execute("""SELECT id, file_path FROM files
                              WHERE has_analysis=1 AND bpm IS NOT NULL
                                AND bpm_refined_at IS NULL
                                AND is_deleted=0
                                AND COALESCE(bpm_verified,0)=0
                              ORDER BY id ASC""").fetchall()
        _bpm_refine_progress["total"] = len(rows)
        for i, r in enumerate(rows, 1):
            if not _bpm_refine_progress["running"]:
                _bpm_refine_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            path = _resolve_audio_path(r["file_path"])
            _bpm_refine_progress["current"] = os.path.basename(r["file_path"])[:80]
            if not path or not os.path.exists(path):
                # file referenced by the DB isn't on disk (renamed/moved/removed)
                # — count it separately so a broken link isn't read as a crash
                _bpm_refine_progress["missing"] = _bpm_refine_progress.get("missing", 0) + 1
                _bpm_refine_progress["done"] = i
                continue
            try:
                # a 90s window from 20s in is plenty for tempo and much
                # faster to load than the whole file
                try:
                    y, sr = librosa.load(path, mono=True, sr=22050, offset=20, duration=90)
                    if y is None or len(y) < sr * 10:
                        y, sr = librosa.load(path, mono=True, sr=22050, duration=120)
                except Exception:
                    y, sr = _load_via_ffmpeg(path)
                bpm = estimate_bpm(y, sr)
                if bpm:
                    cur.execute("UPDATE files SET bpm=?, bpm_refined_at=datetime('now') WHERE id=?",
                                (bpm, r["id"]))
                else:
                    cur.execute("UPDATE files SET bpm_refined_at=datetime('now') WHERE id=?", (r["id"],))
                conn.commit()        # per-file: Stop/crash keeps everything done so far
            except Exception:
                _bpm_refine_progress["errors"] += 1
            _bpm_refine_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _bpm_refine_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _bpm_refine_progress["running"] = False
        _bpm_refine_progress["finished"] = _dt.datetime.utcnow().isoformat()


@app.route("/api/bpm/refine", methods=["POST"])
def api_bpm_refine():
    if _bpm_refine_progress.get("running"):
        return jsonify({"ok": False, "error": "already running", "progress": _bpm_refine_progress})
    eligible = query_one("""SELECT COUNT(*) AS n FROM files
                            WHERE has_analysis=1 AND bpm IS NOT NULL
                              AND bpm_refined_at IS NULL
                              AND is_deleted=0
                              AND COALESCE(bpm_verified,0)=0""")["n"] or 0
    threading.Thread(target=_run_bpm_refine, daemon=True).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/bpm/refine-stop", methods=["POST"])
def api_bpm_refine_stop():
    if _bpm_refine_progress.get("running"):
        _bpm_refine_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/bpm/refine-status")
def api_bpm_refine_status():
    remaining = query_one("""SELECT COUNT(*) AS n FROM files
                             WHERE has_analysis=1 AND bpm IS NOT NULL
                               AND bpm_refined_at IS NULL
                               AND is_deleted=0
                               AND COALESCE(bpm_verified,0)=0""")["n"] or 0
    refined = query_one("SELECT COUNT(*) AS n FROM files WHERE bpm_refined_at IS NOT NULL AND is_deleted=0")["n"] or 0
    return jsonify({**_bpm_refine_progress, "remaining": remaining, "refined": refined})


@app.route("/api/fingerprint/batch", methods=["POST"])
def api_fingerprint_batch():
    if _fingerprint_progress.get("running"):
        return jsonify({"ok": False, "error": "already running", "progress": _fingerprint_progress})
    d = request.get_json() or {}
    only_missing = bool(d.get("only_missing", True))
    # Pre-flight count so the caller can report "X to do" without waiting.
    where = "WHERE has_analysis = 1"
    if only_missing:
        where += " AND band_presence IS NULL"
    eligible = query_one(f"SELECT COUNT(*) AS n FROM files {where}")["n"] or 0
    threading.Thread(target=_run_fingerprint_batch, args=(only_missing,), daemon=True).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/fingerprint/batch-stop", methods=["POST"])
def api_fingerprint_batch_stop():
    if _fingerprint_progress.get("running"):
        _fingerprint_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/fingerprint/batch-status")
def api_fingerprint_batch_status():
    total      = query_one("SELECT COUNT(*) as n FROM files WHERE has_analysis=1")["n"]
    done       = query_one("SELECT COUNT(*) as n FROM files WHERE has_analysis=1 AND band_presence IS NOT NULL")["n"]
    return jsonify({
        **_fingerprint_progress,
        "tracks_analysed":      total,
        "tracks_fingerprinted": done,
        "tracks_remaining":     total - done,
    })


# ── Record-label lookup (MusicBrainz + Discogs) ─────────────────────
# Per-track on-demand or batched in the background. Discogs token is
# optional — without it we fall back to MB only.
_label_progress = {
    "running": False, "done": 0, "total": 0, "current": "",
    "started": None, "finished": None,
    "found": 0, "not_found": 0, "errors": 0, "skipped": 0,
    "stop_reason": None,
}


# ── "Original Mix" cleanup batch ────────────────────────────────────
# One-shot tool: walks every track with "Original Mix"/"Original Version"
# in its title or file path, strips the tag from the DB title, writes
# the new title to the file's embedded tags, and renames the file on
# disk (collision-safe). Per-track commit so a Stop preserves all work.
_orig_mix_progress = {
    "running": False, "done": 0, "total": 0, "current": "",
    "started": None, "finished": None,
    "titles_changed": 0, "files_renamed": 0, "tags_written": 0,
    "errors": 0, "skipped_no_change": 0, "stop_reason": None,
}


def _rename_file_strip_mix(old_path):
    """Compute the cleaned filename and rename on disk (collision-safe).
    Returns the new path on success, None on no-change, or None if a
    collision/exception prevented the rename."""
    from text_clean import strip_mix_suffix
    if not old_path:
        return None
    d  = os.path.dirname(old_path)
    fn = os.path.basename(old_path)
    base, ext = os.path.splitext(fn)
    new_base = strip_mix_suffix(base)
    if not new_base or new_base == base:
        return None
    new_path = os.path.join(d, new_base + ext)
    if os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(old_path):
        return None  # collision — leave alone
    try:
        os.rename(old_path, new_path)
        return new_path
    except Exception as e:
        print(f"[orig-mix] rename failed: {old_path} -> {new_path}: {e}", flush=True)
        return None


def _run_orig_mix_cleanup():
    import sqlite3 as _sql
    import datetime as _dt
    global _orig_mix_progress
    from text_clean import strip_mix_suffix
    _orig_mix_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None,
        "titles_changed": 0, "files_renamed": 0, "tags_written": 0,
        "errors": 0, "skipped_no_change": 0, "stop_reason": None,
    }
    try:
        conn = _connect_threadsafe()
        cur  = conn.cursor()
        # Anything whose title OR primary file path contains "original mix"
        # (or "original version") is in scope.
        rows = cur.execute("""
            SELECT t.id, t.title, t.artist_name, t.album, t.year, t.track_number
            FROM tracks t
            WHERE t.is_deleted = 0
              AND (LOWER(t.title) LIKE '%original mix%'
                OR LOWER(t.title) LIKE '%original version%'
                OR EXISTS (SELECT 1 FROM files f
                           WHERE f.track_id = t.id AND f.is_deleted = 0
                             AND (LOWER(f.file_path) LIKE '%original mix%'
                               OR LOWER(f.file_path) LIKE '%original version%')))
            ORDER BY t.id ASC
        """).fetchall()
        _orig_mix_progress["total"] = len(rows)

        from tag_writer import write_tags_to_file
        for i, r in enumerate(rows, 1):
            if not _orig_mix_progress["running"]:
                _orig_mix_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            tid       = r["id"]
            old_title = r["title"] or ""
            new_title = strip_mix_suffix(old_title) or old_title
            _orig_mix_progress["current"] = (r["artist_name"] or "?") + " — " + new_title

            title_changed = (new_title != old_title and new_title.strip())
            if title_changed:
                cur.execute(
                    "UPDATE tracks SET title=?, updated_at=datetime('now') WHERE id=?",
                    (new_title, tid)
                )
                _orig_mix_progress["titles_changed"] += 1

            # Rename + tag-write per file. There can be multiple (alt versions).
            files = cur.execute(
                "SELECT id, file_path FROM files WHERE track_id=? AND is_deleted=0"
                " AND COALESCE(analysis_status,'') != 'locked'",   # never a borrow's file
                (tid,)
            ).fetchall()
            for f in files:
                old_path = f["file_path"]
                if not old_path:
                    continue
                resolved = _resolve_audio_path(old_path) or old_path
                # 1. Write tags first — if file exists, push the cleaned title
                #    into its metadata. Failure here is non-fatal.
                if os.path.exists(resolved) and title_changed:
                    try:
                        ok, err = write_tags_to_file(
                            resolved, new_title, r["artist_name"],
                            r["album"], r["year"], r["track_number"]
                        )
                        if ok:
                            _orig_mix_progress["tags_written"] += 1
                        else:
                            print(f"[orig-mix] tag-write failed for {resolved}: {err}", flush=True)
                    except Exception as e:
                        _orig_mix_progress["errors"] += 1
                        print(f"[orig-mix] tag-write crashed for {resolved}: {e}", flush=True)
                # 2. Rename the file on disk if the basename has the tag.
                if os.path.exists(resolved):
                    new_path = _rename_file_strip_mix(resolved)
                    if new_path:
                        cur.execute(
                            "UPDATE files SET file_path=?, updated_at=datetime('now') WHERE id=?",
                            (new_path, f["id"])
                        )
                        _orig_mix_progress["files_renamed"] += 1

            if not title_changed and _orig_mix_progress.get("files_renamed", 0) == 0:
                _orig_mix_progress["skipped_no_change"] += 1
            conn.commit()
            _orig_mix_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _orig_mix_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _orig_mix_progress["running"] = False
        import datetime as _dt2
        _orig_mix_progress["finished"] = _dt2.datetime.utcnow().isoformat()


@app.route("/api/tracks/strip-original-mix", methods=["POST"])
def api_tracks_strip_original_mix():
    if _orig_mix_progress.get("running"):
        return jsonify({"ok": False, "error": "already running",
                        "progress": _orig_mix_progress}), 409
    eligible = query_one("""
        SELECT COUNT(DISTINCT t.id) AS n FROM tracks t
        WHERE t.is_deleted = 0
          AND (LOWER(t.title) LIKE '%original mix%'
            OR LOWER(t.title) LIKE '%original version%'
            OR EXISTS (SELECT 1 FROM files f
                       WHERE f.track_id = t.id AND f.is_deleted = 0
                         AND (LOWER(f.file_path) LIKE '%original mix%'
                           OR LOWER(f.file_path) LIKE '%original version%')))
    """)["n"] or 0
    threading.Thread(target=_run_orig_mix_cleanup, daemon=True).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/tracks/strip-original-mix-stop", methods=["POST"])
def api_tracks_strip_original_mix_stop():
    if _orig_mix_progress.get("running"):
        _orig_mix_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/tracks/strip-original-mix-status")
def api_tracks_strip_original_mix_status():
    return jsonify(_orig_mix_progress)


@app.route("/api/library/analysis-coverage")
def api_analysis_coverage():
    """One-line "where am I" for the Analyse page. Counts how many tracks
    have each enrichment dimension and how many have ALL of them."""
    total = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"] or 0
    with_artwork = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 AND artwork_path IS NOT NULL"
    )["n"] or 0
    with_fingerprint = query_one("""
        SELECT COUNT(DISTINCT t.id) AS n
        FROM tracks t JOIN files f ON f.track_id=t.id
        WHERE t.is_deleted=0 AND f.is_primary=1 AND f.is_deleted=0
          AND f.band_presence IS NOT NULL
    """)["n"] or 0
    dsp_checked = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 "
        "AND dsp_check_state IS NOT NULL AND dsp_check_state != 'untested'"
    )["n"] or 0
    with_label = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 "
        "AND label IS NOT NULL AND TRIM(label) != ''"
    )["n"] or 0
    with_isrc = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 "
        "AND isrc IS NOT NULL AND TRIM(isrc) != ''"
    )["n"] or 0
    fully = query_one("""
        SELECT COUNT(DISTINCT t.id) AS n
        FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 AND f.is_deleted=0
        WHERE t.is_deleted=0
          AND t.artwork_path IS NOT NULL
          AND f.band_presence IS NOT NULL
          AND t.dsp_check_state IS NOT NULL AND t.dsp_check_state != 'untested'
          AND t.label IS NOT NULL AND TRIM(t.label) != ''
    """)["n"] or 0
    return jsonify({
        "total":            total,
        "with_artwork":     with_artwork,
        "with_fingerprint": with_fingerprint,
        "dsp_checked":      dsp_checked,
        "with_label":       with_label,
        "with_isrc":        with_isrc,
        "fully_analysed":   fully,
    })


@app.route("/api/labels")
def api_labels():
    """Distinct record labels in the library with usage counts. Used by
    the track-panel datalist (autocomplete) and any future label filter."""
    rows = query(
        "SELECT label AS name, COUNT(*) AS count FROM tracks "
        "WHERE is_deleted=0 AND label IS NOT NULL AND TRIM(label) != '' "
        "GROUP BY label ORDER BY count DESC, label ASC"
    )
    return jsonify(rows)


@app.route("/api/track/<int:tid>/lookup-label", methods=["POST"])
def api_track_lookup_label(tid):
    """Look up a label for a single track. Returns the proposed label
    + source, but does NOT auto-write it — caller decides whether to
    accept. Pass {apply: true} to also save it onto the track."""
    t = query_one("SELECT id, title, artist_name, label, isrc FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "track not found"}), 404
    d = request.get_json(silent=True) or {}
    from label_lookup import lookup_label
    token = (get_setting("discogs_token", "") or "").strip() or None
    result = lookup_label(t.get("artist_name"), t.get("title"), discogs_token=token, isrc=t.get("isrc"))
    if result.get("error"):
        return jsonify(result), 502
    if d.get("apply") and result.get("label"):
        execute("UPDATE tracks SET label=?, updated_at=datetime('now') WHERE id=?",
                (result["label"], tid))
    return jsonify(result)


def _run_label_lookup(only_missing=True):
    import sqlite3 as _sql
    import datetime as _dt
    global _label_progress
    _label_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None,
        "found": 0, "not_found": 0, "errors": 0, "skipped": 0,
        "stop_reason": None,
    }
    try:
        from label_lookup import lookup_label
        # Thread-safe read — get_setting() needs Flask request context.
        token = (_get_setting_threadsafe("discogs_token", "") or "").strip() or None
        conn = _connect_threadsafe()
        cur  = conn.cursor()
        where = ["is_deleted=0", "title IS NOT NULL", "artist_name IS NOT NULL",
                 "TRIM(title) != ''", "TRIM(artist_name) != ''"]
        if only_missing:
            where.append("(label IS NULL OR TRIM(label) = '')")
        rows = cur.execute(
            f"SELECT id, title, artist_name, isrc FROM tracks WHERE {' AND '.join(where)} ORDER BY id ASC"
        ).fetchall()
        _label_progress["total"] = len(rows)
        consecutive_errors = 0
        for i, r in enumerate(rows, 1):
            if not _label_progress["running"]:
                _label_progress["stop_reason"] = "Cancelled."
                conn.commit()   # save all completed work before bailing
                break
            label_display = (r["artist_name"] or "?") + " — " + (r["title"] or "?")
            _label_progress["current"] = label_display[:80]
            res = lookup_label(r["artist_name"], r["title"], discogs_token=token, isrc=r["isrc"])
            if res.get("skipped"):
                _label_progress["skipped"] += 1
                consecutive_errors = 0
            elif res.get("error"):
                _label_progress["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= 8:
                    _label_progress["stop_reason"] = (
                        f"Stopped after {consecutive_errors} consecutive errors. Last: {res.get('error')}."
                    )
                    break
            elif res.get("label"):
                cur.execute(
                    "UPDATE tracks SET label=?, updated_at=datetime('now') WHERE id=?",
                    (res["label"], r["id"])
                )
                _label_progress["found"] += 1
                consecutive_errors = 0
            else:
                _label_progress["not_found"] += 1
                consecutive_errors = 0
            # Commit per-track so Stop / crash preserves all work done.
            conn.commit()
            _label_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _label_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _label_progress["running"] = False
        import datetime as _dt2
        _label_progress["finished"] = _dt2.datetime.utcnow().isoformat()


@app.route("/api/labels/lookup-batch", methods=["POST"])
def api_labels_lookup_batch():
    if _label_progress.get("running"):
        return jsonify({"ok": False, "error": "already running", "progress": _label_progress}), 409
    d = request.get_json() or {}
    only_missing = bool(d.get("only_missing", True))
    where = ["is_deleted=0", "title IS NOT NULL", "artist_name IS NOT NULL",
             "TRIM(title) != ''", "TRIM(artist_name) != ''"]
    if only_missing:
        where.append("(label IS NULL OR TRIM(label) = '')")
    eligible = query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {' AND '.join(where)}")["n"] or 0
    threading.Thread(target=_run_label_lookup, kwargs={"only_missing": only_missing}, daemon=True).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/labels/lookup-batch-stop", methods=["POST"])
def api_labels_lookup_batch_stop():
    if _label_progress.get("running"):
        _label_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/labels/lookup-batch-status")
def api_labels_lookup_batch_status():
    total      = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 AND title IS NOT NULL AND artist_name IS NOT NULL AND TRIM(title)!='' AND TRIM(artist_name)!=''")["n"] or 0
    with_label = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 AND label IS NOT NULL AND TRIM(label) != ''")["n"] or 0
    return jsonify({
        **_label_progress,
        "eligible_total":   total,
        "with_label":       with_label,
        "without_label":    total - with_label,
        "discogs_configured": bool((get_setting("discogs_token", "") or "").strip()),
    })


# ── DSP availability check (v22) ─────────────────────────────────────
# For each track, ask Spotify "do you have this?". Stores the answer so
# we never have to re-call per page load. Per Daithí: only LABEL tracks
# confirmed not-on-Spotify; absence of label = assumed available.
_dsp_progress = {
    "running":  False,
    "done":     0,
    "total":    0,
    "current":  "",
    "started":  None,
    "finished": None,
    "found":    0,
    "not_found":0,
    "skipped":  0,
    "errors":   0,
    "stop_reason": None,
}


def _run_dsp_check(only_untested=True, max_age_days=None):
    import datetime as _dt
    global _dsp_progress
    from dsp_lookup import check_dsp
    from importer import add_track_isrc

    # Spotify is no longer used for this check (its dev-mode rate limits
    # made per-track /search unusable). We now ask Deezer + iTunes —
    # both free, no-auth, far more generous — with an exact ISRC lookup
    # in front when the track carries one. See dsp_lookup.check_dsp.
    _dsp_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None,
        "found": 0, "not_found": 0, "skipped": 0, "errors": 0,
        "probable": 0,
        "stop_reason": None,
    }
    try:
        conn = _connect_threadsafe()
        cur = conn.cursor()
        where = [
            "is_deleted = 0",
            # Untitled / unknown tracks (samples, field recordings) can't
            # be matched — skip them at SELECT time. They stay 'untested'
            # and get re-evaluated if the user ever fills in metadata.
            "title IS NOT NULL",
            "artist_name IS NOT NULL",
            "TRIM(title) != ''",
            "TRIM(artist_name) != ''",
        ]
        if only_untested:
            # Retry 'error' tracks too — they failed for transient reasons
            # last time (network hiccup) and deserve another shot.
            where.append(
                "(dsp_check_state IS NULL OR dsp_check_state IN ('untested', 'error'))"
            )
        elif max_age_days:
            cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(max_age_days))).isoformat()
            where.append(f"(dsp_checked_at IS NULL OR dsp_checked_at < '{cutoff}')")
        rows = cur.execute(
            "SELECT id, title, artist_name, isrc, artwork_source, spotify_id "
            f"FROM tracks WHERE {' AND '.join(where)} ORDER BY id ASC"
        ).fetchall()
        _dsp_progress["total"] = len(rows)
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        consecutive_errors = 0
        for i, r in enumerate(rows, 1):
            if not _dsp_progress["running"]:
                _dsp_progress["stop_reason"] = "Cancelled."
                conn.commit()   # save all completed work before bailing
                break
            label = (r["artist_name"] or "?") + " — " + (r["title"] or "?")
            _dsp_progress["current"] = label[:80]

            # Free signal — no network call needed. A track with a
            # Spotify id (e.g. matched from a wishlist) or whose artwork
            # came from Apple Music / Spotify is provably on a DSP.
            if r["spotify_id"] or (r["artwork_source"] in ("itunes", "spotify")):
                cur.execute(
                    "UPDATE tracks SET dsp_check_state='on_dsp', dsp_checked_at=? WHERE id=?",
                    (now_iso, r["id"])
                )
                _dsp_progress["found"] += 1
                consecutive_errors = 0
                conn.commit()
                _dsp_progress["done"] = i
                continue

            # ISRC-exact (Deezer) → fuzzy Deezer → fuzzy iTunes.
            res = check_dsp(r["artist_name"], r["title"], isrc=r["isrc"])
            state = res.get("state")
            if state == "skipped":
                cur.execute(
                    "UPDATE tracks SET dsp_check_state='skipped', dsp_checked_at=? WHERE id=?",
                    (now_iso, r["id"])
                )
                _dsp_progress["skipped"] += 1
                consecutive_errors = 0
            elif state == "on_dsp":
                cur.execute(
                    "UPDATE tracks SET dsp_check_state='on_dsp', dsp_probable=NULL, dsp_checked_at=? WHERE id=?",
                    (now_iso, r["id"])
                )
                # Enrichment — we found it on a DSP, so record any ISRC the
                # match exposed for free (Deezer always carries one). Stored
                # in the multi-ISRC table; sets the primary if it had none.
                if res.get("isrc"):
                    add_track_isrc(
                        cur, r["id"], res["isrc"],
                        "deezer" if res.get("source") in ("deezer", "isrc") else "musicbrainz"
                    )
                _dsp_progress["found"] += 1
                consecutive_errors = 0
            elif state == "probable":
                # a close-but-not-exact catalogue match: hold it for the
                # user to confirm on the track panel, never auto-decide
                cur.execute(
                    "UPDATE tracks SET dsp_check_state='probable', dsp_probable=?, dsp_checked_at=? WHERE id=?",
                    (_json.dumps({
                        "artist": res.get("match_artist") or "",
                        "title":  res.get("match_title") or "",
                        "source": res.get("source") or "",
                        "score":  res.get("score"),
                    }), now_iso, r["id"])
                )
                _dsp_progress["probable"] += 1
                consecutive_errors = 0
            elif state == "not_on_dsp":
                cur.execute(
                    "UPDATE tracks SET dsp_check_state='not_on_dsp', dsp_probable=NULL, dsp_checked_at=? WHERE id=?",
                    (now_iso, r["id"])
                )
                _dsp_progress["not_found"] += 1
                consecutive_errors = 0
            else:  # 'error' — transient (Deezer/iTunes unreachable). Leave
                   # the row 'untested' so the next run resumes cleanly.
                _dsp_progress["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= 8:
                    _dsp_progress["stop_reason"] = (
                        "Paused — Deezer/iTunes are rate-limiting (or unreachable). "
                        "Everything checked so far is saved; the unchecked tracks stay "
                        "'untested', so just run this again in a few minutes to resume. "
                        f"Last error: {res.get('message')}."
                    )
                    conn.commit()
                    break
            # Commit per-track so a Stop or crash never loses more than
            # the row we were mid-flight on. WAL makes this cheap.
            conn.commit()
            _dsp_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _dsp_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _dsp_progress["running"] = False
        import datetime as _dt2
        _dsp_progress["finished"] = _dt2.datetime.utcnow().isoformat()


@app.route("/api/dsp/check-batch", methods=["POST"])
def api_dsp_check_batch():
    if _dsp_progress.get("running"):
        return jsonify({"ok": False, "error": "already running",
                        "progress": _dsp_progress}), 409
    d = request.get_json() or {}
    only_untested = bool(d.get("only_untested", True))
    max_age_days  = d.get("max_age_days")
    where = [
        "is_deleted = 0",
        "title IS NOT NULL", "artist_name IS NOT NULL",
        "TRIM(title) != ''", "TRIM(artist_name) != ''",
    ]
    if only_untested:
        where.append("(dsp_check_state IS NULL OR dsp_check_state IN ('untested', 'error'))")
    eligible = query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {' AND '.join(where)}")["n"] or 0
    threading.Thread(
        target=_run_dsp_check,
        kwargs={"only_untested": only_untested, "max_age_days": max_age_days},
        daemon=True
    ).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/track/<int:tid>/dsp-confirm", methods=["POST"])
def api_track_dsp_confirm(tid):
    """The user settles a 'probable' DSP match from the track panel:
    accept → on_dsp, reject → not_on_dsp. Either way the held candidate
    is cleared."""
    d = request.get_json() or {}
    state = "on_dsp" if d.get("accept") else "not_on_dsp"
    import datetime as _dt
    now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    execute(
        "UPDATE tracks SET dsp_check_state=?, dsp_probable=NULL, dsp_checked_at=? WHERE id=?",
        (state, now_iso, tid)
    )
    return jsonify({"ok": True, "state": state})


@app.route("/api/dsp/check-stop", methods=["POST"])
def api_dsp_check_stop():
    if _dsp_progress.get("running"):
        _dsp_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/dsp/check-status")
def api_dsp_check_status():
    counts = {r["dsp_check_state"] or "untested": r["n"] for r in query(
        "SELECT COALESCE(dsp_check_state,'untested') AS dsp_check_state, COUNT(*) AS n "
        "FROM tracks WHERE is_deleted=0 GROUP BY COALESCE(dsp_check_state,'untested')"
    )}
    total = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"] or 0
    return jsonify({
        **_dsp_progress,
        "counts":          counts,
        "total_tracks":    total,
    })


# ── ISRC backfill ───────────────────────────────────────────────────
# Reads the embedded ISRC out of each track's primary file and stores it
# (backend only, never shown). Local mutagen pass — no network, fast.
# ISRC is an exact recording id used to make DSP / label / artwork
# lookups exact instead of fuzzy.
_isrc_progress = {
    "running": False, "done": 0, "total": 0, "current": "",
    "found": 0, "missing": 0, "errors": 0,
    "started": None, "finished": None, "stop_reason": None,
}


def _run_isrc_backfill(only_missing=True):
    import datetime as _dt
    from dsp_lookup import extract_isrc
    from importer import add_track_isrc
    global _isrc_progress
    _isrc_progress = {
        "running": True, "done": 0, "total": 0, "current": "",
        "found": 0, "missing": 0, "errors": 0,
        "started": _dt.datetime.utcnow().isoformat(),
        "finished": None, "stop_reason": None,
    }
    try:
        conn = _connect_threadsafe()
        cur = conn.cursor()
        where = ["t.is_deleted = 0"]
        if only_missing:
            # Skip files we've already probed (whether or not they had one).
            where.append("t.isrc_checked_at IS NULL")
        rows = cur.execute(
            "SELECT t.id, t.artist_name, t.title, f.file_path "
            "FROM tracks t "
            "LEFT JOIN files f ON f.track_id = t.id AND f.is_primary = 1 AND f.is_deleted = 0 "
            f"WHERE {' AND '.join(where)} ORDER BY t.id ASC"
        ).fetchall()
        _isrc_progress["total"] = len(rows)
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        for i, r in enumerate(rows, 1):
            if not _isrc_progress["running"]:
                _isrc_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            _isrc_progress["current"] = ((r["artist_name"] or "?") + " — " + (r["title"] or "?"))[:80]
            rp = _resolve_audio_path(r["file_path"]) if r["file_path"] else None
            if not rp or not os.path.exists(rp):
                # File not reachable (drive unplugged?) — leave isrc_checked_at
                # NULL so a later run retries it.
                _isrc_progress["errors"] += 1
                _isrc_progress["done"] = i
                continue
            try:
                code = extract_isrc(rp)
            except Exception:
                code = None
            if code:
                add_track_isrc(cur, r["id"], code, "embedded")
                _isrc_progress["found"] += 1
            else:
                _isrc_progress["missing"] += 1
            cur.execute(
                "UPDATE tracks SET isrc_checked_at = ? WHERE id = ?",
                (now_iso, r["id"])
            )
            conn.commit()
            _isrc_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _isrc_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _isrc_progress["running"] = False
        import datetime as _dt2
        _isrc_progress["finished"] = _dt2.datetime.utcnow().isoformat()


@app.route("/api/isrc/backfill", methods=["POST"])
def api_isrc_backfill():
    if _isrc_progress.get("running"):
        return jsonify({"ok": False, "error": "already running",
                        "progress": _isrc_progress}), 409
    d = request.get_json() or {}
    only_missing = bool(d.get("only_missing", True))
    where = ["is_deleted = 0"]
    if only_missing:
        where.append("isrc_checked_at IS NULL")
    eligible = query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {' AND '.join(where)}")["n"] or 0
    threading.Thread(target=_run_isrc_backfill,
                     kwargs={"only_missing": only_missing}, daemon=True).start()
    return jsonify({"ok": True, "eligible": eligible})


@app.route("/api/isrc/backfill-stop", methods=["POST"])
def api_isrc_backfill_stop():
    if _isrc_progress.get("running"):
        _isrc_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/isrc/backfill-status")
def api_isrc_backfill_status():
    with_isrc = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 AND isrc IS NOT NULL AND TRIM(isrc) != ''"
    )["n"] or 0
    checked = query_one(
        "SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0 AND isrc_checked_at IS NOT NULL"
    )["n"] or 0
    total = query_one("SELECT COUNT(*) AS n FROM tracks WHERE is_deleted=0")["n"] or 0
    return jsonify({
        **_isrc_progress,
        "with_isrc":    with_isrc,
        "checked":      checked,
        "total_tracks": total,
    })


# ── Cultural enrichment (no AI) — artist + label origins ────────────
# Populates artist_profiles + label_profiles from MusicBrainz + Wikidata +
# Wikipedia (culture.py). ISRC-bridged for exact artist matches. Feeds the
# world map + cultural-mapping views. Commit-per-item, resumable.

def _upsert_profile(cur, table, key_col, key_val, fields, now_iso):
    fields = {**fields, "enriched_at": now_iso, "updated_at": now_iso}
    cols = list(fields.keys())
    vals = [fields[c] for c in cols]
    cur.execute(f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE {key_col}=?",
                vals + [key_val])
    if cur.rowcount == 0:
        cur.execute(
            f"INSERT INTO {table} ({key_col}, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' * len(cols))})", [key_val] + vals)


_artist_culture_progress = {"running": False, "done": 0, "total": 0, "current": "",
                            "found": 0, "errors": 0, "started": None, "finished": None,
                            "stop_reason": None}


def _run_artist_culture(only_missing=True):
    import datetime as _dt
    from culture import enrich_artist
    global _artist_culture_progress
    _artist_culture_progress = {"running": True, "done": 0, "total": 0, "current": "",
                                "found": 0, "errors": 0,
                                "started": _dt.datetime.utcnow().isoformat(),
                                "finished": None, "stop_reason": None}
    try:
        token = (_get_setting_threadsafe("discogs_token", "") or "").strip() or None
        conn = _connect_threadsafe()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT artist_name FROM tracks "
            "WHERE is_deleted=0 AND COALESCE(TRIM(artist_name),'')<>'' "
            "ORDER BY artist_name"
        ).fetchall()
        names = [r["artist_name"] for r in rows]
        if only_missing:
            done = set(r["artist_name"] for r in cur.execute(
                "SELECT artist_name FROM artist_profiles WHERE enriched_at IS NOT NULL").fetchall())
            names = [n for n in names if n not in done]
        _artist_culture_progress["total"] = len(names)
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        consec = 0
        for i, name in enumerate(names, 1):
            if not _artist_culture_progress["running"]:
                _artist_culture_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            _artist_culture_progress["current"] = name[:80]
            ir = cur.execute(
                "SELECT ti.isrc FROM track_isrcs ti JOIN tracks t ON t.id=ti.track_id "
                "WHERE t.artist_name=? AND ti.isrc IS NOT NULL LIMIT 1", (name,)).fetchone()
            try:
                res = enrich_artist(name, isrc=(ir["isrc"] if ir else None), discogs_token=token)
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {e}"}
            _upsert_profile(cur, "artist_profiles", "artist_name", name, {
                "country": res.get("country"), "city": res.get("city"),
                "latitude": res.get("latitude"), "longitude": res.get("longitude"),
                "gender": res.get("gender"), "active_from": res.get("active_from"),
                "active_to": res.get("active_to"), "genres": res.get("genres"),
                "bio_summary": res.get("bio_summary"), "wikipedia_note": res.get("wikipedia_note"),
                "mbid": res.get("mbid"), "wikidata_qid": res.get("wikidata_qid"),
                "artist_type": res.get("artist_type"), "data_source": res.get("data_source"),
                "fetch_error": res.get("error"),
            }, now_iso)
            if res.get("error"):
                _artist_culture_progress["errors"] += 1
                consec += 1
                if consec >= 8:
                    _artist_culture_progress["stop_reason"] = (
                        "Paused — too many consecutive lookup misses/errors. Work so far is "
                        "saved; run again to resume.")
                    conn.commit()
                    break
            else:
                _artist_culture_progress["found"] += 1
                consec = 0
            conn.commit()
            _artist_culture_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _artist_culture_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _artist_culture_progress["running"] = False
        import datetime as _dt2
        _artist_culture_progress["finished"] = _dt2.datetime.utcnow().isoformat()


_label_culture_progress = {"running": False, "done": 0, "total": 0, "current": "",
                           "found": 0, "errors": 0, "started": None, "finished": None,
                           "stop_reason": None}


def _run_label_culture(only_missing=True):
    import datetime as _dt
    from culture import enrich_label
    global _label_culture_progress
    _label_culture_progress = {"running": True, "done": 0, "total": 0, "current": "",
                               "found": 0, "errors": 0,
                               "started": _dt.datetime.utcnow().isoformat(),
                               "finished": None, "stop_reason": None}
    try:
        token = (_get_setting_threadsafe("discogs_token", "") or "").strip() or None
        conn = _connect_threadsafe()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT label FROM tracks "
            "WHERE is_deleted=0 AND COALESCE(TRIM(label),'')<>'' "
            "AND LOWER(TRIM(label))<>'self released' ORDER BY label"
        ).fetchall()
        names = [r["label"] for r in rows]
        if only_missing:
            done = set(r["label_name"] for r in cur.execute(
                "SELECT label_name FROM label_profiles WHERE enriched_at IS NOT NULL").fetchall())
            names = [n for n in names if n not in done]
        _label_culture_progress["total"] = len(names)
        now_iso = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        consec = 0
        for i, name in enumerate(names, 1):
            if not _label_culture_progress["running"]:
                _label_culture_progress["stop_reason"] = "Cancelled."
                conn.commit()
                break
            _label_culture_progress["current"] = name[:80]
            try:
                res = enrich_label(name, discogs_token=token)
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {e}"}
            _upsert_profile(cur, "label_profiles", "label_name", name, {
                "country": res.get("country"), "city": res.get("city"),
                "latitude": res.get("latitude"), "longitude": res.get("longitude"),
                "founded_year": res.get("founded_year"), "founder": res.get("founder"),
                "bio_summary": res.get("bio_summary"), "wikipedia_url": res.get("wikipedia_url"),
                "mbid": res.get("mbid"), "wikidata_qid": res.get("wikidata_qid"),
                "data_source": res.get("data_source"), "fetch_error": res.get("error"),
            }, now_iso)
            if res.get("error"):
                _label_culture_progress["errors"] += 1
                consec += 1
                if consec >= 8:
                    _label_culture_progress["stop_reason"] = (
                        "Paused — too many consecutive misses/errors. Saved; run again to resume.")
                    conn.commit()
                    break
            else:
                _label_culture_progress["found"] += 1
                consec = 0
            conn.commit()
            _label_culture_progress["done"] = i
        conn.commit()
        conn.close()
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _label_culture_progress["stop_reason"] = f"Crashed: {type(e).__name__}: {e}"
    finally:
        _label_culture_progress["running"] = False
        import datetime as _dt2
        _label_culture_progress["finished"] = _dt2.datetime.utcnow().isoformat()


@app.route("/api/culture/artists", methods=["POST"])
def api_culture_artists():
    if _artist_culture_progress.get("running"):
        return jsonify({"ok": False, "error": "already running"}), 409
    only_missing = bool((request.get_json() or {}).get("only_missing", True))
    threading.Thread(target=_run_artist_culture,
                     kwargs={"only_missing": only_missing}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/culture/artists/stop", methods=["POST"])
def api_culture_artists_stop():
    if _artist_culture_progress.get("running"):
        _artist_culture_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/culture/artists/status")
def api_culture_artists_status():
    total = query_one("SELECT COUNT(DISTINCT artist_name) AS n FROM tracks "
                      "WHERE is_deleted=0 AND COALESCE(TRIM(artist_name),'')<>''")["n"] or 0
    placed = query_one("SELECT COUNT(*) AS n FROM artist_profiles "
                       "WHERE country IS NOT NULL AND TRIM(country)<>''")["n"] or 0
    return jsonify({**_artist_culture_progress, "total_artists": total, "with_origin": placed})


@app.route("/api/culture/labels", methods=["POST"])
def api_culture_labels():
    if _label_culture_progress.get("running"):
        return jsonify({"ok": False, "error": "already running"}), 409
    only_missing = bool((request.get_json() or {}).get("only_missing", True))
    threading.Thread(target=_run_label_culture,
                     kwargs={"only_missing": only_missing}, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/culture/labels/stop", methods=["POST"])
def api_culture_labels_stop():
    if _label_culture_progress.get("running"):
        _label_culture_progress["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not running"})


@app.route("/api/culture/labels/status")
def api_culture_labels_status():
    total = query_one("SELECT COUNT(DISTINCT label) AS n FROM tracks "
                      "WHERE is_deleted=0 AND COALESCE(TRIM(label),'')<>'' "
                      "AND LOWER(TRIM(label))<>'self released'")["n"] or 0
    placed = query_one("SELECT COUNT(*) AS n FROM label_profiles "
                       "WHERE country IS NOT NULL AND TRIM(country)<>''")["n"] or 0
    return jsonify({**_label_culture_progress, "total_labels": total, "with_origin": placed})


# ── library-wide stats for corpus-relative UI ───────────────────────
_LIBRARY_STATS_CACHE = {"data": None, "ts": 0}


def _percentile(values, pct):
    """Tiny no-numpy percentile helper (linear interp). values is sorted."""
    if not values:
        return None
    if pct <= 0: return values[0]
    if pct >= 100: return values[-1]
    k = (len(values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


@app.route("/api/library/stats")
def api_library_stats():
    """Return quartile stats across the library for every quantitative file
    attribute. Used by the track panel + Spéaclaí to render corpus-relative
    bars (e.g. 'louder than 73% of your library')."""
    import time as _time
    now = _time.time()
    if _LIBRARY_STATS_CACHE["data"] and (now - _LIBRARY_STATS_CACHE["ts"]) < 300:
        return jsonify(_LIBRARY_STATS_CACHE["data"])

    fields = [
        "loudness_rms", "dynamic_range", "spectral_centroid",
        "brightness_score", "bpm",
        "band_sub", "band_bass", "band_lowmid",
        "band_body", "band_presence", "band_brilliance",
    ]
    stats = {}
    for fld in fields:
        rows = query(
            f"SELECT {fld} as v FROM files "
            f"WHERE {fld} IS NOT NULL AND has_analysis = 1 AND is_primary = 1 "
            f"ORDER BY {fld} ASC"
        )
        vals = [r["v"] for r in rows]
        if not vals:
            stats[fld] = None
            continue
        stats[fld] = {
            "n":      len(vals),
            "min":    vals[0],
            "p10":    _percentile(vals, 10),
            "p25":    _percentile(vals, 25),
            "median": _percentile(vals, 50),
            "p75":    _percentile(vals, 75),
            "p90":    _percentile(vals, 90),
            "max":    vals[-1],
        }
    _LIBRARY_STATS_CACHE["data"] = stats
    _LIBRARY_STATS_CACHE["ts"]   = now
    return jsonify(stats)


@app.route("/api/artwork/batch-status")
def api_artwork_batch_status():
    from artwork_fetcher import get_batch_progress
    embedded_ct  = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_source='embedded'    AND is_deleted=0")["n"]
    itunes_ct    = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_source='itunes'      AND is_deleted=0")["n"]
    mb_ct        = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_source='musicbrainz' AND is_deleted=0")["n"]
    manual_ct    = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_source='manual'      AND is_deleted=0")["n"]
    not_found_ct = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_source='not_found'   AND is_deleted=0")["n"]
    total_tracks = query_one("SELECT COUNT(*) as n FROM tracks WHERE is_deleted=0")["n"]
    never_tried  = query_one("SELECT COUNT(*) as n FROM tracks WHERE artwork_path IS NULL AND (artwork_source IS NULL OR artwork_source NOT IN ('not_found','embedded','musicbrainz','itunes','manual')) AND is_deleted=0")["n"]
    with_art     = embedded_ct + itunes_ct + mb_ct + manual_ct
    return jsonify({
        **get_batch_progress(),
        "with_art":        with_art,
        "not_found":       not_found_ct,
        "never_tried":     never_tried,
        "total_tracks":    total_tracks,
        "without_art":     never_tried,
        "src_embedded":    embedded_ct,
        "src_itunes":      itunes_ct,
        "src_musicbrainz": mb_ct,
        "src_manual":      manual_ct,
    })


@app.route("/api/artwork/<int:tid>/verify", methods=["POST"])
def api_artwork_verify(tid):
    execute("UPDATE tracks SET artwork_verified=1 WHERE id=?", (tid,))
    return jsonify({"ok": True})


@app.route("/api/artwork/<int:tid>/upload", methods=["POST"])
def api_artwork_upload(tid):
    t = query_one("SELECT id FROM tracks WHERE id=? AND is_deleted=0", (tid,))
    if not t:
        return jsonify({"error": "not found"}), 404
    if "image" not in request.files:
        return jsonify({"error": "no image"}), 400
    img = request.files["image"]
    ext = os.path.splitext(img.filename)[1].lower() if img.filename else ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    # Save to artwork dir on drive (or local fallback)
    from artwork_fetcher import get_artwork_dir
    art_dir = get_artwork_dir()
    art_dir.mkdir(parents=True, exist_ok=True)
    dest = art_dir / f"{tid}.jpg"
    img.save(str(dest))
    execute(
        "UPDATE tracks SET artwork_path=?, artwork_source='manual', artwork_verified=1 WHERE id=?",
        (str(dest), tid),
    )
    try:                                           # a manual cover gets its colour too
        from art_colour import dominant_colour
        col = dominant_colour(str(dest))
        if col:
            execute("UPDATE tracks SET art_colour=? WHERE id=?", (col, tid))
    except Exception:
        pass
    return jsonify({"ok": True, "path": str(dest)})


@app.route("/api/artwork/<int:tid>", methods=["DELETE"])
def api_artwork_delete(tid):
    t = query_one("SELECT artwork_path FROM tracks WHERE id=?", (tid,))
    if t and t["artwork_path"]:
        fp = _resolve_artwork_path(t["artwork_path"])
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
    execute("UPDATE tracks SET artwork_path=NULL, artwork_source=NULL, artwork_verified=0 WHERE id=?", (tid,))
    return jsonify({"ok": True})


# =============================================================
# STICKERS (reusable image stamps for Clúdach)
# =============================================================

@app.route("/api/stickers")
def api_stickers_list():
    stickers = query("SELECT * FROM stickers ORDER BY created_at DESC")
    return jsonify({"stickers": stickers})


@app.route("/api/stickers", methods=["POST"])
def api_sticker_add():
    if "image" not in request.files:
        return jsonify({"error": "no image"}), 400
    img  = request.files["image"]
    name = (request.form.get("name") or "").strip() or (img.filename or "sticker")
    ext  = os.path.splitext(img.filename)[1].lower() if img.filename else ".png"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        ext = ".png"
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = STICKERS_DIR / fname
    img.save(str(fpath))
    url = f"/static/stickers/{fname}"
    sid = execute("INSERT INTO stickers (name, image_path) VALUES (?, ?)", (name, url))
    return jsonify({"ok": True, "sticker": {"id": sid, "name": name, "image_path": url}}), 201


@app.route("/api/sticker/<int:sid>", methods=["DELETE"])
def api_sticker_delete(sid):
    s = query_one("SELECT * FROM stickers WHERE id=?", (sid,))
    if not s:
        return jsonify({"error": "not found"}), 404
    path = s["image_path"].lstrip("/")
    full = Path(BASE_DIR) / path
    if full.exists():
        try:
            full.unlink()
        except Exception:
            pass
    execute("DELETE FROM stickers WHERE id=?", (sid,))
    return jsonify({"ok": True})


# =============================================================
# TRACK OVERLAYS (Clúdach canvas — post-its, stickers, images)
# =============================================================

@app.route("/api/track/<int:tid>/overlays")
def api_overlays_get(tid):
    place_id = request.args.get("place_id")
    if place_id:
        # Return global overlays (place_id IS NULL) + this-place overlays
        overlays = query(
            "SELECT * FROM track_overlays WHERE track_id=? AND (place_id IS NULL OR place_id=?) ORDER BY z_index, id",
            (tid, int(place_id)),
        )
    else:
        overlays = query("SELECT * FROM track_overlays WHERE track_id=? AND place_id IS NULL ORDER BY z_index, id", (tid,))
    return jsonify({"overlays": overlays})


@app.route("/api/track/<int:tid>/overlays", methods=["POST"])
def api_overlay_add(tid):
    d = request.get_json() or {}
    ov_type  = d.get("overlay_type", "postit")
    content  = d.get("content", "")
    color    = d.get("color", "#fef08a")
    pos_x    = float(d.get("pos_x_pct", 10))
    pos_y    = float(d.get("pos_y_pct", 10))
    width    = float(d.get("width_pct", 30))
    height   = float(d.get("height_pct", 25))
    rotation = float(d.get("rotation", 0))
    z_index  = int(d.get("z_index", 1))
    # place_id: null = all places, integer = scoped to that place
    raw_pid  = d.get("place_id")
    place_id = int(raw_pid) if raw_pid else None
    oid = execute(
        """INSERT INTO track_overlays
           (track_id, place_id, overlay_type, content, pos_x_pct, pos_y_pct,
            width_pct, height_pct, rotation, z_index, color)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, place_id, ov_type, content, pos_x, pos_y, width, height, rotation, z_index, color),
    )
    return jsonify({"ok": True, "id": oid, "place_id": place_id}), 201


@app.route("/api/overlay/<int:oid>", methods=["PUT"])
def api_overlay_update(oid):
    d = request.get_json() or {}
    fields, vals = [], []
    for key in ("content", "pos_x_pct", "pos_y_pct", "width_pct", "height_pct",
                "rotation", "z_index", "color"):
        if key in d:
            fields.append(f"{key}=?")
            vals.append(d[key])
    if not fields:
        return jsonify({"ok": True})
    vals.append(oid)
    execute(f"UPDATE track_overlays SET {', '.join(fields)} WHERE id=?", vals)
    return jsonify({"ok": True})


@app.route("/api/overlay/<int:oid>", methods=["DELETE"])
def api_overlay_delete(oid):
    ov = query_one("SELECT * FROM track_overlays WHERE id=?", (oid,))
    if ov and ov.get("overlay_type") in ("sticker", "image") and ov.get("content"):
        # Only delete uploaded images (not sticker library refs)
        if ov["content"].startswith("/static/overlay_images/"):
            fp = Path(BASE_DIR) / ov["content"].lstrip("/")
            if fp.exists():
                try:
                    fp.unlink()
                except Exception:
                    pass
    execute("DELETE FROM track_overlays WHERE id=?", (oid,))
    return jsonify({"ok": True})


@app.route("/api/overlay-image-upload", methods=["POST"])
def api_overlay_image_upload():
    if "image" not in request.files:
        return jsonify({"error": "no image"}), 400
    img  = request.files["image"]
    ext  = os.path.splitext(img.filename)[1].lower() if img.filename else ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        ext = ".jpg"
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = OVERLAY_IMAGES_DIR / fname
    img.save(str(fpath))
    return jsonify({"ok": True, "url": f"/static/overlay_images/{fname}"}), 201


# =============================================================
# GREEN ROOM — the borrow side of the GR builds (see green_room.py)
# =============================================================
# The page talks to a campus gr_server through these routes. Borrowed
# tracks never join `tracks` — they live on /greenroom and play through
# the Seinnteoir (cue items carry gr_borrow_id; see _items_with_paths).


def _gr_guard(fn):
    """Run a green_room call; a GRError becomes a readable JSON error."""
    try:
        return jsonify(fn())
    except green_room.GRError as e:
        return jsonify({"error": str(e)}), 502
    except OSError as e:
        # (the Wi-Fi dropping mid-download, a full disk: requests' own errors are OSErrors too.
        #  It used to answer an error PAGE, the button's script choked on it and seemed to do nothing)
        print(f"[green room] {type(e).__name__}: {e}")
        return jsonify({"error": "That was interrupted. Try again."}), 502


def _picture_ready(f, turn=True):
    """An uploaded picture as (filename, bytes, mime), made into one the rooms server takes
    (mr_pictures: a phone's HEIC becomes a JPEG, a huge one is made smaller, one stored on
    its side is turned). → (picture, None), or (None, the words to show the person)."""
    import mr_pictures
    try:
        data, name, mime = mr_pictures.sendable(f.read(), f.filename or "", turn=turn)
    except mr_pictures.NotAPicture as e:
        return None, str(e)
    return (name or "picture", data, mime), None


@app.route("/greenroom")
def greenroom_page():
    # the first pool browser; superseded by Explore → Music rooms
    return redirect("/explore3d?view=rooms")


@app.route("/api/gr/status")
def api_gr_status():
    return jsonify(green_room.status())


@app.route("/api/gr/join", methods=["POST"])
def api_gr_join():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.join(
        d.get("server_url") or "", d.get("invite_code") or "",
        d.get("device_name") or ""))


@app.route("/api/gr/catalogue")
def api_gr_catalogue():
    return _gr_guard(lambda: {"tracks": green_room.catalogue()})


@app.route("/api/gr/art/<int:sid>")
def api_gr_art(sid):
    v = request.args.get("v")
    p = green_room.cover_path(sid, int(v) if v and v.isdigit() else None)
    if not p:
        return jsonify({"error": "no cover"}), 404
    resp = send_file(p, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=2592000" if v else "no-cache"
    return resp


@app.route("/api/gr/borrow", methods=["POST"])
def api_gr_borrow():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.borrow(d.get("track_id")))


@app.route("/api/gr/return", methods=["POST"])
def api_gr_return():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.give_back(d.get("local_id")))


@app.route("/api/gr/offload", methods=["POST"])
def api_gr_offload():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.offload(d.get("local_id")))


@app.route("/api/gr/redownload", methods=["POST"])
def api_gr_redownload():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.redownload(d.get("local_id")))


@app.route("/api/gr/buy", methods=["POST"])
def api_gr_buy():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.buy(d.get("local_id")))


@app.route("/api/gr/buy-track", methods=["POST"])
def api_gr_buy_track():
    """Buy a pool record by its server id — borrowed or not."""
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.buy_track(int(d.get("track_id") or 0)))


# ── other people's rooms, and mine ──

@app.route("/api/gr/rooms")
def api_gr_rooms():
    return _gr_guard(lambda: {"rooms": green_room.rooms(request.args.get("q", ""),
                                                          request.args.get("kind", ""),
                                                          open_only=request.args.get("open") == "1")})


@app.route("/api/gr/rooms/lookup")
def api_gr_rooms_lookup():
    return jsonify({"handle": green_room.room_lookup(request.args.get("name", ""))})


@app.route("/api/gr/rooms/<handle>")
def api_gr_room(handle):
    def go():
        room = green_room.room(handle, request.args.get("design"))
        room["grupai"] = [_gr_grupa_here(c) for c in (room.get("grupai") or [])]
        return room
    return _gr_guard(go)


# ── other people's public grúpaí (and mine, as others see them) ──
# The rooms server hands out its own addresses for a grúpa's small pictures;
# here they become this app's (/api/gr/grupai/<id>/art/<key>), which fetches
# each once and keeps it, like a room's images.
def _gr_grupa_here(card):
    from urllib.parse import quote
    stamp = quote(str(card.get("updated_at") or ""), safe="")

    def here(u, gid):
        m = _re.match(r"^/api/grupai/(\d+)/art/([a-z0-9]{1,12})$", u or "")
        return f"/api/gr/grupai/{m.group(1)}/art/{m.group(2)}?v={stamp}" if m else (u or "")
    cover = card.get("cover") or {}
    if cover.get("image"):
        cover["image"] = here(cover["image"], card.get("id"))
    for it in card.get("items") or []:
        if it.get("image"):
            it["image"] = here(it["image"], card.get("id"))
    return card


def _gr_grupa_items(card):
    """What this listener can do with each element of someone's grúpa: a stream
    plays through their own deck (unless it is marked AI-made); a record that
    lives on the rooms server plays if they hold it (borrowed or bought)."""
    marks = _ai_marks()
    for it in card.get("items") or []:
        it["playable"], it["mine"] = False, None
        if it.get("where") == "stream":
            ai = ai_flags.verdict(it.get("source") or "", it.get("by_ids") or "", it.get("url") or "", *marks)
            it["ai"] = ai or ""
            it["playable"] = (it.get("source") or "") in _GRUPA_DECK_SERVICES and not ai
            it["service"] = _streams.SERVICES.get(it.get("source") or "", it.get("source") or "")
        elif it.get("where") == "campus" and it.get("server_track_id"):
            t = query_one("SELECT t.id, f.id AS file_id FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 "
                          "WHERE t.is_deleted=0 AND t.gr_track_id=? ORDER BY t.id DESC LIMIT 1", (it["server_track_id"],))
            if t:
                it["mine"] = t["id"]
                it["playable"] = bool(t["file_id"])
    return card


@app.route("/api/gr/grupai")
def api_gr_grupai():
    return _gr_guard(lambda: {"grupai": [_gr_grupa_here(c) for c in green_room.public_grupai(
        request.args.get("q", ""), request.args.get("handle", ""))]})


@app.route("/api/gr/grupai/<int:gid>")
def api_gr_grupa(gid):
    return _gr_guard(lambda: _gr_grupa_items(_gr_grupa_here(green_room.public_grupa(gid))))


@app.route("/api/gr/grupai/<int:gid>/queue")
def api_gr_grupa_queue(gid):
    """Someone's public grúpa as a cue for this listener's deck: its streams,
    and any of its records they hold themselves — in its order. ?item=<n>
    (its place in the list) = that one only."""
    def go():
        card = _gr_grupa_items(green_room.public_grupa(gid))
        only = request.args.get("item", type=int)
        out, starts = [], {}
        for n, it in enumerate(card.get("items") or []):
            if not it.get("playable") or (only is not None and n != only):
                continue
            starts[str(n)] = len(out)
            if it.get("where") == "stream":
                out.append({"kind": "embed", "source": it.get("source"), "stream_type": it.get("stream_type") or "",
                            "url": it.get("url"), "title": it.get("title") or "",
                            "artist": it.get("subtitle") or _streams.SERVICES.get(it.get("source"), ""),
                            "image_url": it.get("image") if str(it.get("image") or "").startswith("http") else "",
                            "duration_sec": it.get("duration_sec") or 0})
            else:
                out.extend(query(_GRUPA_TRACK_SQL + "AND t.id=?", (it["mine"],)))
        return {"items": out, "starts": starts}
    return _gr_guard(go)


@app.route("/api/gr/grupai/<int:gid>/art/<key>")
def api_gr_grupa_art(gid, key):
    data = green_room.grupa_art_bytes(gid, key, request.args.get("v", ""))
    if not data:
        return jsonify({"error": "no such picture"}), 404
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/gr/my/room")
def api_gr_my_room():
    return _gr_guard(green_room.my_room)


@app.route("/api/gr/my/room/profile", methods=["POST"])
def api_gr_my_room_profile():
    return _gr_guard(lambda: green_room.my_room_profile(request.get_json(silent=True) or {}))


@app.route("/api/gr/my/room/designs", methods=["POST"])
def api_gr_my_room_designs():
    return _gr_guard(lambda: green_room.save_design(request.get_json(silent=True) or {}))


@app.route("/api/gr/my/room/designs/<int:did>/live", methods=["POST"])
def api_gr_my_room_design_live(did):
    return _gr_guard(lambda: green_room.design_live(did))


@app.route("/api/gr/my/room/designs/<int:did>", methods=["DELETE"])
def api_gr_my_room_design_delete(did):
    return _gr_guard(lambda: green_room.design_delete(did))


# ── room images + an artist's records (uploads go straight through to the server) ──

@app.route("/api/gr/my/assets")
def api_gr_my_assets():
    return _gr_guard(lambda: {"assets": green_room.my_assets()})


@app.route("/api/gr/my/assets", methods=["POST"])
def api_gr_my_assets_upload():
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "no image"}), 400
    pic, why = _picture_ready(f)
    if not pic:
        return jsonify({"error": why}), 400
    name, data, mime = pic
    return _gr_guard(lambda: green_room.upload_asset(
        name, data, mime, request.form.get("label") or "",
        rights=request.form.get("rights") or ""))


@app.route("/api/gr/my/assets/<int:aid>", methods=["DELETE"])
def api_gr_my_assets_delete(aid):
    return _gr_guard(lambda: green_room.delete_asset(aid))


@app.route("/api/gr/asset/<int:aid>")
def api_gr_asset(aid):
    data, mime = green_room.asset_bytes(aid)
    if not data:
        return jsonify({"error": "no such image"}), 404
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/gr/my/records")
def api_gr_my_records():
    return _gr_guard(green_room.my_records)


@app.route("/api/gr/my/records", methods=["POST"])
def api_gr_my_records_upload():
    a = request.files.get("audio")
    if not a or not a.filename:
        return jsonify({"error": "no audio file"}), 400
    audio = (a.filename, a.read(), a.mimetype or "application/octet-stream")
    c = request.files.get("cover")
    cover, why = _picture_ready(c) if c and c.filename else (None, None)
    if why:
        return jsonify({"error": why}), 400
    fields = {k: request.form.get(k) for k in ("title", "artist", "album", "price_cents", "buy_url", "genre", "year", "label", "exclusive",
                                               "rights", "rights_note")
              if request.form.get(k) is not None}
    return _gr_guard(lambda: green_room.upload_record(audio, cover, fields))


@app.route("/api/gr/my/records/<int:tid>", methods=["POST"])
def api_gr_my_records_edit(tid):
    c = request.files.get("cover")
    cover, why = _picture_ready(c) if c and c.filename else (None, None)
    if why:
        return jsonify({"error": why}), 400
    fields = {k: request.form.get(k) for k in ("title", "artist", "album", "price_cents", "buy_url", "genre", "year", "label", "exclusive")
              if request.form.get(k) is not None} if request.form else (request.get_json(silent=True) or {})
    return _gr_guard(lambda: green_room.edit_record(tid, fields, cover))


@app.route("/api/gr/my/records/<int:tid>", methods=["DELETE"])
def api_gr_my_records_delete(tid):
    return _gr_guard(lambda: green_room.delete_record(tid))


@app.route("/api/gr/my/places")
def api_gr_my_places():
    """The user's places, as a snapshot a public room can show (names,
    colours, counts — never audio, never paths)."""
    rows = query("""SELECT p.id, p.name, p.colour_primary, p.illustration_kind,
                           (SELECT COUNT(*) FROM place_tracks pt JOIN tracks t ON t.id = pt.track_id
                             WHERE pt.place_id = p.id AND t.is_deleted = 0) AS n
                    FROM places p WHERE COALESCE(p.is_well, 0) = 0 ORDER BY p.name""")
    return jsonify({"places": rows})


@app.route("/rooms/<handle>")
def public_room_page(handle):
    """Someone's public music room, rendered here from the server's data.
    ?overlay=1 when opened over the 3D room (✕ + 'explore more');
    ?design=<id> lets the owner preview a draft."""
    return render_template("public_room.html", handle=handle,
                           overlay=request.args.get("overlay") == "1",
                           editing=request.args.get("editing") == "1",      # (shown inside its editor: no bar of its own)
                           design_id=request.args.get("design") or "")


@app.route("/myroom")
def my_room_page():
    return render_template("my_room.html")


# ── profiles, plans, the basket ──

@app.route("/api/gr/signup", methods=["POST"])
def api_gr_signup():
    d = request.get_json(silent=True) or {}
    try:
        return jsonify(green_room.signup(d.get("server_url") or "", d.get("name") or "",
                                         d.get("device_name") or "", d.get("kind") or "listener",
                                         agreed=bool(d.get("agreed")), door_code=d.get("door_code") or ""))
    except green_room.GRError as e:
        # (need_code: this server makes no account without its invite code — the front door then asks for it)
        return jsonify({"error": str(e), "need_code": bool(getattr(e, "need_code", False))}), 502
    except OSError as e:
        print(f"[green room] {type(e).__name__}: {e}")
        return jsonify({"error": "That was interrupted. Try again."}), 502


@app.route("/api/gr/plans")
def api_gr_plans():
    return _gr_guard(lambda: {"plans": green_room.plans()})


@app.route("/api/gr/plan", methods=["POST"])
def api_gr_plan():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: green_room.choose_plan(d.get("plan") or ""))


@app.route("/api/gr/basket")
def api_gr_basket():
    return jsonify({"items": green_room.basket()})


@app.route("/api/gr/basket", methods=["POST"])
def api_gr_basket_add():
    d = request.get_json(silent=True) or {}
    return _gr_guard(lambda: {"items": green_room.basket_add(int(d.get("track_id") or 0),
                                                              d.get("mode") or "borrow")})


@app.route("/api/gr/basket/<int:bid>/mode", methods=["POST"])
def api_gr_basket_mode(bid):
    d = request.get_json(silent=True) or {}
    return jsonify({"items": green_room.basket_mode(bid, d.get("mode") or "borrow")})


@app.route("/api/gr/basket/<int:bid>", methods=["DELETE"])
def api_gr_basket_remove(bid):
    return jsonify({"items": green_room.basket_remove(bid)})


@app.route("/api/gr/basket/checkout", methods=["POST"])
def api_gr_basket_checkout():
    return _gr_guard(green_room.basket_checkout)


@app.route("/basket")
def basket_page():
    return render_template("basket.html")


@app.route("/subscribe")
def subscribe_page():
    return render_template("subscribe.html", next_url=request.args.get("next") or "/basket")


# ── the front door (trial builds): no account, no room ──
# Everything redirects to /login until this laptop is signed in. Static
# files, the sign-in calls, the intro and the feedback widget stay open.

_GR_OPEN_PREFIXES = ("/login", "/intro", "/static/", "/api/gr/status", "/api/gr/signup", "/house-rules", "/api/gr/version",
                     "/api/gr/join", "/api/gr/plans", "/api/feedback", "/favicon", "/api/tips")
# the look can be chosen at the front door (its key used to open an empty list): reading the
# themes and picking one, nothing more — importing and deleting wait for sign-in
_GR_OPEN_EXACT = ("/api/themes", "/api/themes/current")


@app.before_request
def _gr_front_door():
    if not IS_GR:
        return
    p = request.path
    if p.startswith(_GR_OPEN_PREFIXES) or p in _GR_OPEN_EXACT:
        return
    if get_setting("gr_token", ""):
        return
    if p.startswith("/api/"):
        return jsonify({"error": "sign in first"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    # ?preview=1 shows the front door while signed in (for looking at it)
    if get_setting("gr_token", "") and request.args.get("preview") != "1":
        return redirect("/room")
    return render_template("login.html")


def _md_page(path):
    """A small Markdown file (headings, lists, paragraphs, **bold**, `code`, fenced
    code, simple tables, links) as HTML — for the house rules and the plug-in guide,
    which live once, in the code's own HOUSE_RULES.md and docs/PLUGINS.md."""
    import html as _h
    import re as _re
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return "<p>This page can't be found.</p>"
    out, in_list, para = [], False, []

    def inline(t):
        t = _h.escape(t)
        t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = _re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        # a link out to the web opens in the browser; one into the code's own folders is just its words
        t = _re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
        return _re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)

    def flush():
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
    fence, table = None, []

    def flush_table():
        nonlocal table
        if table:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table
                    if not _re.fullmatch(r"[\s|:\-]+", r)]
            if rows:
                out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr>"
                           + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:]) + "</table>")
            table = []
    for line in text.splitlines():
        st = line.strip()
        if fence is not None:                       # inside ``` … ```: as it is
            if st.startswith("```"):
                out.append("<pre><code>" + _h.escape("\n".join(fence)) + "</code></pre>"); fence = None
            else:
                fence.append(line)
            continue
        if st.startswith("```"):
            flush(); flush_table()
            if in_list:
                out.append("</ul>"); in_list = False
            fence = []
            continue
        if st.startswith("|"):
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            table.append(st)
            continue
        flush_table()
        if st.startswith("#"):
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            level = min(3, len(st) - len(st.lstrip("#")))
            out.append(f"<h{level}>{inline(st.lstrip('#').strip())}</h{level}>")
        elif st.startswith("- "):
            flush()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append("<li>" + inline(st[2:]))
        elif not st:
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
        elif in_list and line.startswith("  "):
            out[-1] += " " + inline(st)
        else:
            para.append(st)
    flush(); flush_table()
    if fence is not None:
        out.append("<pre><code>" + _h.escape("\n".join(fence)) + "</code></pre>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# ── the master account's page, and what everyone can ask of the master (gr_master.py on the server) ──
import re as _re_master
_MASTER_SUB = _re_master.compile(r"^(overview|accounts(/\d+)?|records(/\d+)?|reports(/\d+)?|feedback(/\d+)?|version|door|grupai/\d+"
                                  r"|sim(/(rules|credits|playlist|activity|sync-note))?|spotify-keys(/assign)?)$")

# the credits trial (gr_sim.py on the server): bring home, buy, take out, the
# Spotify key the master gives each person, the master's playlist reading
import gr_sim_app  # noqa: E402
gr_sim_app.register(app, get_setting=_get_setting_threadsafe, set_setting=_set_setting_threadsafe,
                    spotify=lambda: _get_spotify(), is_gr=IS_GR)


# digital artists (gr_art.py on the server): works, open to work, artwork credits, buying a work
import gr_art_app  # noqa: E402
gr_art_app.register(app)


def _app_version():
    try:
        return open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        return ""


APP_VERSION = _app_version()
_version_seen = {"at": 0.0, "v": None}


@app.route("/master")
def master_page():
    return render_template("master.html", app_version=APP_VERSION)


@app.route("/api/gr/master/<path:sub>", methods=["GET", "POST"])
def api_gr_master(sub):
    """The master page talks to the server's /api/master/… through here (the
    server answers only the master, and only on its own Mac)."""
    if not _MASTER_SUB.match(sub):
        return jsonify({"error": "no such thing"}), 404
    try:
        if request.method == "GET":
            code, d = green_room.shop_call("GET", "/api/master/" + sub, params=dict(request.args))
        else:
            code, d = green_room.shop_call("POST", "/api/master/" + sub, request.get_json(silent=True) or {})
    except green_room.GRError as e:
        return jsonify({"error": str(e)}), 502
    if sub == "version" and code == 200:
        _version_seen["at"] = 0.0
    return jsonify(d), code


@app.route("/api/gr/master/records/<int:tid>/<what>")
def api_gr_master_record_file(tid, what):
    """A waiting record's sound or cover, for the master to hear and see before deciding."""
    if what not in ("audio", "art"):
        return jsonify({"error": "no such thing"}), 404
    try:
        r = green_room.master_file(tid, what, request.headers.get("Range"))
    except green_room.GRError as e:
        return jsonify({"error": str(e)}), 502
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
    return Response(r.iter_content(64 * 1024), status=r.status_code, headers=keep)


@app.route("/api/gr/report", methods=["POST"])
def api_gr_report():
    """Report a room, a record or a public grúpa to the Music Room."""
    d = request.get_json(silent=True) or {}
    try:
        code, out = green_room.shop_call("POST", "/api/report", {k: d.get(k) for k in ("what", "ref", "reason", "note")})
    except green_room.GRError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(out), code


@app.route("/api/gr/version")
def api_gr_version():
    """This app's version, and the one the Music Room says everyone should be on.
    Asked of the server at most every ten minutes."""
    import time as _t
    if _version_seen["v"] is None or _t.time() - _version_seen["at"] > 600:
        try:
            code, d = green_room.shop_call("GET", "/api/version")
            _version_seen["v"] = d if code == 200 else {}
            # the server lives somewhere else now: go there (green_room.follow_move checks it
            # is the same Music Room first), and ask the new address the same question
            if code == 200 and d.get("moved_to") and green_room.follow_move(d["moved_to"]):
                code, d = green_room.shop_call("GET", "/api/version")
                _version_seen["v"] = d if code == 200 else _version_seen["v"]
        except green_room.GRError:
            _version_seen["v"] = _version_seen["v"] or {}
        _version_seen["at"] = _t.time()
    want = _version_seen["v"] or {}
    other = (want.get("version") or "").strip()
    differs = bool(other and APP_VERSION and other.lstrip("v") != APP_VERSION.lstrip("v"))
    can = app_update.where()
    return jsonify({"mine": APP_VERSION, "want": want,
                    # a notice shows when this app can put the new version in by itself, or there is
                    # an address to send the person to
                    "update": differs and (can["can"] or (want.get("url") or "").startswith("https://")),
                    "self": differs and can["can"], "why_not": "" if can["can"] else can["why"],
                    "into": can.get("into", ""),          # (the new version goes somewhere else than where this one runs)
                    "page": app_update.PAGE})


# ── the app updating itself: "Update now" on the new-version notice (app_update.py) ──
try:
    import app_update  # noqa: E402
except Exception as _e:                      # (a packed app missing a piece must still START: it just can't update itself)
    print(f"[update] not available: {_e}")

    class app_update:                        # noqa: N801
        PAGE = "https://github.com/Dbreen101/musicroom/releases/latest"
        where = staticmethod(lambda: {"can": False, "why": "This copy can't update itself.", "app": ""})
        state = staticmethod(lambda: {"step": "error", "error": "This copy can't update itself.", "done": 0, "total": 0, "version": "", "staged": ""})
        start = staticmethod(lambda *a, **k: False)
        apply = staticmethod(lambda: False)


@app.route("/api/gr/update")
def api_gr_update():
    return jsonify(app_update.state())


@app.route("/api/gr/update/start", methods=["POST"])
def api_gr_update_start():
    """Fetch the version the Music Room names — never one the page names."""
    api_gr_version()                                   # (makes sure the server has been asked)
    want = ((_version_seen["v"] or {}).get("version") or "").strip().lstrip("v")
    if not want or want == (APP_VERSION or "").lstrip("v"):
        return jsonify({"error": "This is already the version to be on."}), 409
    app_update.start(want, DATA_DIR)
    return jsonify(app_update.state())


@app.route("/api/gr/update/apply", methods=["POST"])
def api_gr_update_apply():
    ok = app_update.apply()
    try:
        if ok:
            green_room.track("app_update", {"from": APP_VERSION, "to": app_update.state().get("version")})
            green_room.flush_events()
    except Exception:
        pass
    return jsonify(app_update.state()), (200 if ok else 409)


# One Music Room GR on a computer (2026-11-24): what an update can leave behind is cleared a
# while after the app starts; other copies of the app (the one first unzipped in Downloads…) are
# named on the page, and go to the Bin when the person says so.
import sys as _sys_frozen  # noqa: E402
if getattr(_sys_frozen, "frozen", False) and hasattr(app_update, "tidy"):
    threading.Thread(target=lambda: app_update.tidy(DATA_DIR, wait_s=90), daemon=True).start()

_copies_seen = {"at": 0.0, "list": []}


@app.route("/api/gr/update/copies")
def api_gr_update_copies():
    import time as _t
    if not hasattr(app_update, "other_copies"):
        return jsonify({"copies": []})
    if request.args.get("fresh") == "1" or _t.time() - _copies_seen["at"] > 1800:
        _copies_seen.update(at=_t.time(), list=app_update.other_copies())
    home = os.path.expanduser("~")
    return jsonify({"copies": [{"path": p, "where": os.path.dirname(p).replace(home, "~")} for p in _copies_seen["list"]]})


@app.route("/api/gr/update/copies/bin", methods=["POST"])
def api_gr_update_copies_bin():
    if not hasattr(app_update, "bin_copies"):
        return jsonify({"error": "not on this computer"}), 409
    out = app_update.bin_copies()
    _copies_seen.update(at=0.0, list=[])
    if IS_GR:
        try:
            green_room.track("app_copies_binned", {"binned": len(out["binned"]), "left": len(out["left"])})
        except Exception:
            pass
    return jsonify(out)


@app.route("/house-rules")
def house_rules_page():
    return render_template("house_rules.html", body=_md_page(os.path.join(BASE_DIR, "HOUSE_RULES.md")),
                           bare=request.args.get("bare") == "1")


@app.route("/plugins/how")
def plugins_how_page():
    """How to make a plug-in — docs/PLUGINS.md, shown from the app itself (the link used to go
    to the code's page on GitHub, which only invited people can open)."""
    return render_template("house_rules.html", title="Making a plug-in",
                           body=_md_page(os.path.join(BASE_DIR, "docs", "PLUGINS.md")),
                           bare=request.args.get("bare") == "1")


@app.route("/intro")
def intro_page():
    """The first look around, after the account is made: four screens, then the room.
    (Outside the trial it ends in the welcome wizard when the room is empty.)"""
    return render_template("intro.html",
                           next_url="/welcome" if _needs_onboarding() else "/room")


# ── activity: app-side events queue locally, go up in batches ──

@app.route("/api/gr/track", methods=["POST"])
def api_gr_track():
    d = request.get_json(silent=True) or {}
    if d.get("kind"):
        green_room.track(d["kind"], d.get("data"))
    return jsonify({"ok": True})


_gr_flush_last = [0.0]


@app.before_request
def _gr_flush_hook():
    """Send queued activity every ~30s, off the request thread."""
    now = _time.time()
    if now - _gr_flush_last[0] < 30:
        return
    _gr_flush_last[0] = now

    def _flush():
        green_room.flush_events()
        _grupa_public_flush()             # public grúpaí that changed go up with it
    threading.Thread(target=_flush, daemon=True).start()


@app.route("/api/gr/leave", methods=["POST"])
def api_gr_leave():
    """Log out on this laptop: forget who you are, but REMEMBER the server —
    the sign-in page needs its address and nobody should have to type it.
    Borrows stay (they'd need returning first, which needs the server)."""
    for k in ("gr_token", "gr_name"):
        execute("DELETE FROM settings WHERE key=?", (k,))
    return jsonify({"left": True})


# =============================================================
# RUN
# =============================================================

# =============================================================
# GRÚPA — a playlist that holds anything: records from your room, labels,
# artists, places, sets, Spotify / SoundCloud links, other music rooms, any
# link, another grúpa. A description says what the grúpa is; each element can
# carry a note on why it's there. Tables: grupa.py. Streamed elements are
# LINKS (streams.py) — the service itself plays them (the listener's Spotify,
# or the service's player beside the deck), in one cue with owned records.
# A stream marked AI-made (ai_flags.py) is shown but never cued.
# =============================================================
import ai_flags
import grupa as _grupa_mod
import streams as _streams

_grupa_ready = False
_GRUPA_DECK_SERVICES = ("spotify", "soundcloud")     # services the deck can play (Apple Music: off, 2026-11-14)


def _ai_marks():
    """The listener's own marks → (marked AI-made, cleared as not-AI), as sets
    of ai_flags keys."""
    def _set(key):
        try:
            return set(_json.loads(get_setting(key, "") or "[]"))
        except Exception:
            return set()
    return _set("ai_marked"), _set("ai_cleared")


def _ai_verdict(it, marks=None):
    if (it.get("kind") or "") != "stream":
        return None
    marked, cleared = marks if marks is not None else _ai_marks()
    return ai_flags.verdict(it.get("source") or "", it.get("by_ids") or "", it.get("url") or "", marked, cleared)
_GRUPA_NAMES = {"label": "Label", "artist": "Artist", "place": "Place", "set": "Set",
                "room": "Music room", "grupa": "Grúpa"}
_GRUPA_TRACK_SQL = ("SELECT t.id AS track_id, t.title, t.artist_name AS artist, f.id AS file_id, "
                    "f.duration_sec FROM tracks t JOIN files f ON f.track_id=t.id AND f.is_primary=1 "
                    "WHERE t.is_deleted=0 AND COALESCE(f.is_deleted,0)=0 ")


def _grupa_ensure():
    """The tables exist before the first grúpa call (not every launcher runs migrate.py)."""
    global _grupa_ready
    if _grupa_ready:
        return
    db = get_db()
    _grupa_mod.ensure(db.cursor())
    db.commit()
    _grupa_ready = True


def _grupa_items(gid):
    return query("SELECT * FROM grupa_items WHERE grupa_id=? ORDER BY position, id", (gid,))


def _grupa_tracks_for(kind, ref_id, ref_key):
    """The owned records an element stands for, in playing order."""
    if kind == "track":
        return query(_GRUPA_TRACK_SQL + "AND t.id=?", (ref_id,))
    if kind == "label":
        return query(_GRUPA_TRACK_SQL + "AND t.label=? COLLATE NOCASE "
                     "ORDER BY t.album, t.track_number, t.title", (ref_key,))
    if kind == "artist":
        return query(_GRUPA_TRACK_SQL + "AND t.artist_name=? COLLATE NOCASE "
                     "ORDER BY t.year, t.album, t.track_number, t.title", (ref_key,))
    if kind == "place":
        return query(_GRUPA_TRACK_SQL.replace("WHERE", "JOIN place_tracks pt ON pt.track_id=t.id WHERE")
                     + "AND pt.place_id=? ORDER BY t.artist_name, t.title", (ref_id,))
    if kind == "set":
        return query(_GRUPA_TRACK_SQL.replace("WHERE", "JOIN set_tracks st ON st.track_id=t.id WHERE")
                     + "AND st.set_id=? ORDER BY st.position, st.id", (ref_id,))
    return []


def _grupa_hydrate(it, marks=None):
    """An element as it reads NOW — a record renamed since it was added shows
    its new name; one that has gone keeps the name it was added under."""
    out = dict(it)
    out.update(playable=False, count=None, gone=False, href=None, borrowed=False, ai=None)
    k = it["kind"]
    if k == "track":
        t = query_one("SELECT t.id, t.title, t.artist_name, t.gr_borrow_id, f.id AS file_id "
                      "FROM tracks t LEFT JOIN files f ON f.track_id=t.id AND f.is_primary=1 "
                      "WHERE t.id=? AND t.is_deleted=0", (it["ref_id"],))
        if not t:
            out["gone"] = True
        else:
            out.update(title=t["title"] or it["title"] or "Untitled", subtitle=t["artist_name"] or "",
                       image=f"/api/artwork/{t['id']}?s=192", playable=bool(t["file_id"]),
                       href=f"/room?track={t['id']}", borrowed=bool(t["gr_borrow_id"]))
    elif k in ("label", "artist"):
        col = "label" if k == "label" else "artist_name"
        r = query_one(f"SELECT COUNT(*) AS n, MIN(CASE WHEN artwork_path IS NOT NULL THEN id END) AS art "
                      f"FROM tracks WHERE is_deleted=0 AND {col}=? COLLATE NOCASE", (it["ref_key"],))
        out.update(title=it["ref_key"], subtitle=_GRUPA_NAMES[k], count=r["n"], playable=r["n"] > 0,
                   image=f"/api/artwork/{r['art']}?s=192" if r["art"] else (it["image"] or ""))
    elif k in ("place", "set"):
        if k == "place":
            r = query_one("SELECT p.id, p.name, COUNT(pt.track_id) AS n FROM places p "
                          "LEFT JOIN place_tracks pt ON pt.place_id=p.id WHERE p.id=? GROUP BY p.id", (it["ref_id"],))
        else:
            r = query_one("SELECT s.id, s.name, COUNT(st.track_id) AS n FROM sets s "
                          "LEFT JOIN set_tracks st ON st.set_id=s.id WHERE s.id=? GROUP BY s.id", (it["ref_id"],))
        if not r:
            out["gone"] = True
        else:
            out.update(title=r["name"], subtitle=_GRUPA_NAMES[k], count=r["n"], playable=r["n"] > 0,
                       href=(f"/room/{r['id']}" if k == "place" else f"/set/{r['id']}"))
    elif k == "grupa":
        r = query_one("SELECT g.id, g.name, COUNT(i.id) AS n FROM grupa g "
                      "LEFT JOIN grupa_items i ON i.grupa_id=g.id WHERE g.id=? GROUP BY g.id", (it["ref_id"],))
        if not r:
            out["gone"] = True
        else:
            out.update(title=r["name"], subtitle=_GRUPA_NAMES[k], count=r["n"], playable=r["n"] > 0,
                       href=f"/grupa/{r['id']}")
    elif k == "room":
        out.update(title=it["title"] or it["ref_key"], subtitle=it["subtitle"] or _GRUPA_NAMES[k],
                   href=f"/rooms/{it['ref_key']}")
    elif k == "stream":
        ai = _ai_verdict(it, marks)             # marked AI-made: shown, never played
        out.update(playable=(it["source"] or "") in _GRUPA_DECK_SERVICES and not ai, ai=ai,
                   service=_streams.SERVICES.get(it["source"] or "", it["source"] or ""))
    if out["gone"]:
        out["subtitle"] = it["subtitle"] or _GRUPA_NAMES.get(k, "")
    return out


def _grupa_queue(items, depth=0, seen=None, marks=None):
    """Elements → one cue for the deck: owned records by file, streamed ones by
    link (kind 'embed'), in the grúpa's order. A label, artist, place or set
    opens out into its records; a grúpa inside a grúpa opens out too. A stream
    marked AI-made is left out."""
    seen = seen if seen is not None else set()
    marks = marks if marks is not None else _ai_marks()
    out = []
    for it in items:
        k = it["kind"]
        if k in ("track", "label", "artist", "place", "set"):
            out.extend(_grupa_tracks_for(k, it["ref_id"], it["ref_key"]))
        elif k == "stream" and (it["source"] or "") in _GRUPA_DECK_SERVICES and it["url"]:
            if _ai_verdict(it, marks):
                continue
            out.append({"kind": "embed", "source": it["source"], "stream_type": it["stream_type"] or "",
                        "url": it["url"], "title": it["title"] or "",
                        "artist": it["subtitle"] or _streams.SERVICES.get(it["source"], ""),
                        "image_url": it["image"] or "", "duration_sec": it.get("duration_sec") or 0})
        elif k == "grupa" and depth < 3 and it["ref_id"] not in seen:
            seen.add(it["ref_id"])
            out.extend(_grupa_queue(_grupa_items(it["ref_id"]), depth + 1, seen, marks))
    return out


def _grupa_touch(gid):
    """It changed: note when — and, for one that is public, that the rooms
    server's copy is behind (it is sent again shortly, off the request)."""
    execute("UPDATE grupa SET updated_at=datetime('now'), "
            "public_dirty=CASE WHEN COALESCE(is_public,0)=1 THEN 1 ELSE 0 END WHERE id=?", (gid,))


# ── a grúpa's cover: its name in type, or a picture from one of its elements ──
def _grupa_look():
    """The look every grúpa shares until one is given its own."""
    return {"font": _grupa_mod.clean_font(get_setting("grupa_cover_font", "")) or _grupa_mod.FONTS[0],
            "bg": _grupa_mod.clean_hex(get_setting("grupa_cover_bg", ""))}


def _grupa_image_of(it, size=384):
    """The picture an element has, if it has one: a record's cover, a stream's
    or a link's own image, the first cover of a label or an artist."""
    k = it["kind"]
    if k == "track":
        t = query_one("SELECT id FROM tracks WHERE id=? AND is_deleted=0 AND artwork_path IS NOT NULL", (it["ref_id"],))
        return f"/api/artwork/{t['id']}?s={size}" if t else ""
    if k in ("label", "artist"):
        col = "label" if k == "label" else "artist_name"
        r = query_one(f"SELECT MIN(id) AS art FROM tracks WHERE is_deleted=0 AND artwork_path IS NOT NULL "
                      f"AND {col}=? COLLATE NOCASE", (it["ref_key"],))
        return f"/api/artwork/{r['art']}?s={size}" if r and r["art"] else (it["image"] or "")
    return it["image"] or ""


def _grupa_cover(g, look=None):
    def picture(iid):
        it = query_one("SELECT * FROM grupa_items WHERE id=? AND grupa_id=?", (iid, g["id"]))
        return _grupa_image_of(it) if it else ""
    return _grupa_mod.cover_of(g, look or _grupa_look(), picture)


def _grupa_card(g, look=None):
    """A grúpa as the lists show it: its cover resolved, whether it is public."""
    g = dict(g)
    g["cover"] = _grupa_cover(g, look)
    g["is_public"] = bool(g.get("is_public"))
    g["public_error"] = g.get("public_error") or ""
    for k in ("cover_kind", "cover_item", "cover_font", "cover_bg", "public_dirty"):
        g.pop(k, None)
    return g


def _grupa_insert(gid, row):
    pos = query_one("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM grupa_items WHERE grupa_id=?", (gid,))["p"]
    iid = execute(
        "INSERT INTO grupa_items (grupa_id, position, kind, ref_id, ref_key, url, source, stream_type, "
        "title, subtitle, image, note, by_ids, duration_sec) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, pos, row["kind"], row.get("ref_id"), row.get("ref_key"), row.get("url"), row.get("source"),
         row.get("stream_type"), row.get("title"), row.get("subtitle"), row.get("image"),
         (row.get("note") or "").strip(), row.get("by_ids") or "", row.get("duration_sec") or None))
    return iid


@app.route("/grupa")
@app.route("/grupa/<int:gid>")
def grupa_page(gid=None):
    # the trial: grúpaí live inside My Room (their own tab), not on a page of
    # their own — an old address lands there, on the one it named
    if IS_GR:
        return redirect(f"/room?grupa={gid or 'all'}")
    return render_template("grupa.html", gid=gid or 0)


@app.route("/api/grupa")
def api_grupa_list():
    _grupa_ensure()
    rows = query("SELECT g.*, COUNT(i.id) AS count FROM grupa g LEFT JOIN grupa_items i ON i.grupa_id=g.id "
                 "GROUP BY g.id ORDER BY g.updated_at DESC, g.id DESC")
    for g in rows:                       # a few covers, so each one can be told apart at a glance
        covers = []
        for it in query("SELECT kind, ref_id, image FROM grupa_items WHERE grupa_id=? ORDER BY position, id LIMIT 12", (g["id"],)):
            src = f"/api/artwork/{it['ref_id']}?s=96" if it["kind"] == "track" and it["ref_id"] else (it["image"] or "")
            if src and src not in covers:
                covers.append(src)
            if len(covers) >= 4:
                break
        g["covers"] = covers
    look = _grupa_look()
    # the "add to a grúpa" window asks which ones already hold the thing in hand
    has = set()
    tid, url, room = request.args.get("track", type=int), (request.args.get("url") or "").strip(), (request.args.get("room") or "").strip()
    if tid:
        has = {r["grupa_id"] for r in query("SELECT grupa_id FROM grupa_items WHERE kind='track' AND ref_id=?", (tid,))}
    elif url:
        known = _streams.parse(url)
        has = {r["grupa_id"] for r in query("SELECT grupa_id FROM grupa_items WHERE url=?",
                                            (known["url"] if known and not known.get("short") else url,))}
    elif room:
        has = {r["grupa_id"] for r in query("SELECT grupa_id FROM grupa_items WHERE kind='room' AND ref_key=?", (room.lstrip("@"),))}
    out = []
    for g in rows:
        c = _grupa_card(g, look)
        c["has"] = g["id"] in has
        out.append(c)
    return jsonify({"grupa": out, "look": look, "fonts": list(_grupa_mod.FONTS), "swatches": list(_grupa_mod.SWATCHES)})


@app.route("/api/grupa", methods=["POST"])
def api_grupa_create():
    _grupa_ensure()
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "give it a name"}), 400
    gid = execute("INSERT INTO grupa (name, description) VALUES (?,?)", (name[:200], (d.get("description") or "").strip()))
    return jsonify({"ok": True, "id": gid}), 201


@app.route("/api/grupa/<int:gid>")
def api_grupa_get(gid):
    _grupa_ensure()
    g = query_one("SELECT * FROM grupa WHERE id=?", (gid,))
    if not g:
        return jsonify({"error": "not found"}), 404
    ai_flags.refresh()                      # the public list of AI-made artists, kept fresh in the background
    marks = _ai_marks()
    rows = _grupa_items(gid)
    # the pictures its cover could be: one per element that has one (each picture once)
    choices, seen = [], set()
    for it in rows:
        img = _grupa_image_of(it, 192)
        if img and img not in seen:
            seen.add(img)
            choices.append({"item": it["id"], "image": img, "title": it["title"] or ""})
    own = {"kind": g.get("cover_kind") or "", "item": g.get("cover_item"),
           "font": _grupa_mod.clean_font(g.get("cover_font")), "bg": _grupa_mod.clean_hex(g.get("cover_bg")),
           "ink": _grupa_mod.clean_hex(g.get("cover_ink"))}
    return jsonify({"grupa": _grupa_card(g), "items": [_grupa_hydrate(i, marks) for i in rows],
                    "cover_own": own, "cover_choices": choices, "look": _grupa_look(),
                    "fonts": list(_grupa_mod.FONTS), "swatches": list(_grupa_mod.SWATCHES)})


@app.route("/api/grupa/<int:gid>", methods=["PATCH"])
def api_grupa_patch(gid):
    _grupa_ensure()
    if not query_one("SELECT id FROM grupa WHERE id=?", (gid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    if "name" in d:
        name = (d.get("name") or "").strip()
        if not name:
            return jsonify({"error": "it needs a name"}), 400
        execute("UPDATE grupa SET name=? WHERE id=?", (name[:200], gid))
    if "description" in d:
        execute("UPDATE grupa SET description=? WHERE id=?", ((d.get("description") or "").strip(), gid))
    if isinstance(d.get("cover"), dict):
        # {kind: 'shared'} = the look all grúpaí share · {kind: 'text', font, bg} = its own
        # type and colour · {kind: 'item', item} = the picture of one of its elements
        c = d["cover"]
        kind = c.get("kind")
        if kind == "item":
            it = query_one("SELECT * FROM grupa_items WHERE id=? AND grupa_id=?", (c.get("item"), gid))
            if not it or not _grupa_image_of(it):
                return jsonify({"error": "that one has no picture"}), 400
            execute("UPDATE grupa SET cover_kind='item', cover_item=? WHERE id=?", (it["id"], gid))
        elif kind == "text":
            execute("UPDATE grupa SET cover_kind='text', cover_font=?, cover_bg=?, cover_ink=? WHERE id=?",
                    (_grupa_mod.clean_font(c.get("font")), _grupa_mod.clean_hex(c.get("bg")),
                     _grupa_mod.clean_hex(c.get("ink")), gid))
        else:
            execute("UPDATE grupa SET cover_kind='' WHERE id=?", (gid,))
    _grupa_touch(gid)
    return jsonify({"ok": True, "grupa": _grupa_card(query_one("SELECT * FROM grupa WHERE id=?", (gid,)))})


@app.route("/api/grupa/look", methods=["POST"])
def api_grupa_look():
    """The look every grúpa shares: {font, bg}. An empty bg = the page's own paper."""
    _grupa_ensure()
    d = request.get_json(silent=True) or {}
    if "font" in d:
        set_setting("grupa_cover_font", _grupa_mod.clean_font(d.get("font")))
    if "bg" in d:
        set_setting("grupa_cover_bg", _grupa_mod.clean_hex(d.get("bg")))
    # every public grúpa wearing the shared look shows it to others too
    execute("UPDATE grupa SET public_dirty=1 WHERE COALESCE(is_public,0)=1 AND COALESCE(cover_kind,'')=''")
    return jsonify({"ok": True, "look": _grupa_look()})


@app.route("/api/grupa/<int:gid>", methods=["DELETE"])
def api_grupa_delete(gid):
    _grupa_ensure()
    _grupa_delete(gid)
    return jsonify({"ok": True})


def _grupa_delete(gid):
    g = query_one("SELECT * FROM grupa WHERE id=?", (gid,))
    if g and g.get("is_public"):
        _grupa_unpublish(gid)                 # a public one leaves the rooms server too
    if g and g.get("folder_path"):            # its folder of links goes with it (copies in it, if any, are left)
        try:
            _grupa_folder_mod.remove(g["folder_path"], links_only=True)
        except Exception:
            pass
    execute("DELETE FROM grupa_items WHERE grupa_id=?", (gid,))
    execute("DELETE FROM grupa WHERE id=?", (gid,))


# ── public grúpaí ────────────────────────────────────────────────────────────
# Off unless its owner turns it on (Daithí, 2026-11-15: "the option to 'Make
# grúpa public' for each grúpa … off by default"). On, a copy of what it is
# goes to the rooms server — names, notes, links, small covers; never audio,
# never a file path — where others find it in Explore and on the owner's
# public room. It is sent again whenever it changes (see _grupa_public_flush).
def _grupa_thumb_b64(tid, size=192):
    """A small copy of a record's cover, as text, for a public grúpa. '' if none."""
    import base64
    t = query_one("SELECT artwork_path FROM tracks WHERE id=?", (tid,))
    if not t or not t["artwork_path"]:
        return ""
    fp = _resolve_artwork_path(t["artwork_path"])
    path = (_artwork_thumb(fp, tid, size) if fp else None) or os.path.join(_THUMB_DIR, str(size), f"{tid}.jpg")
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= 150_000:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        pass
    return ""


def _grupa_public_payload(g):
    """What others may see of this grúpa. Each element says where it can be
    heard: 'stream' (a link anyone can play through their own Spotify /
    SoundCloud), 'campus' (a record that lives on the rooms server — its
    maker's room is where to borrow or buy it), 'local' (a file on this laptop
    only), 'own' (this person's own work, on this laptop only), 'link', 'room',
    'name' (a label, an artist, a set: a name and no more)."""
    art, items = {}, []
    marks = _ai_marks()
    for n, it in enumerate(_grupa_items(g["id"])):
        h = _grupa_hydrate(it, marks)
        if h.get("gone"):
            continue
        k = it["kind"]
        row = {"kind": k, "title": h.get("title") or "", "subtitle": h.get("subtitle") or "", "note": it["note"] or "", "where": "name"}
        if k == "stream":
            row.update(where="stream", url=it["url"], source=it["source"] or "", stream_type=it["stream_type"] or "",
                       image=it["image"] or "", duration_sec=it.get("duration_sec") or 0, by_ids=it.get("by_ids") or "",
                       ai=h.get("ai") or "")
        elif k == "link":
            row.update(where="link", url=it["url"], image=it["image"] or "")
        elif k == "room":
            row.update(where="room", handle=it["ref_key"])
        elif k == "track":
            t = query_one("SELECT id, album, year, label, gr_track_id, is_own_work FROM tracks WHERE id=?", (it["ref_id"],)) or {}
            row.update(album=t.get("album") or "", year=t.get("year") or None, label=t.get("label") or "")
            if t.get("gr_track_id"):
                row.update(where="campus", server_track_id=t["gr_track_id"])
            else:
                row["where"] = "own" if t.get("is_own_work") else "local"
            b64 = _grupa_thumb_b64(it["ref_id"])
            if b64:
                art[str(n)] = b64
                row["art"] = str(n)
        else:
            row["count"] = h.get("count")
        items.append(row)
    cover = _grupa_cover(g)
    if cover["kind"] == "image":
        it = query_one("SELECT * FROM grupa_items WHERE id=?", (cover.get("item"),)) or {}
        b64 = _grupa_thumb_b64(it.get("ref_id"), 384) if it.get("kind") == "track" else ""
        if b64:
            art["cover"] = b64
            cover = {"kind": "image", "art": "cover"}
        elif (it.get("image") or "").startswith("http"):
            cover = {"kind": "image", "url": it["image"]}
        else:                                       # a picture that can't travel: its name in type instead
            cover = _grupa_mod.cover_of({}, _grupa_look())
    return {"local_id": g["id"], "name": g["name"], "description": g.get("description") or "",
            "cover": cover, "items": items, "art": art}


_grupa_retry_at = {}            # a grúpa that couldn't go up: not before this time again


def _grupa_publish(gid):
    """Send one public grúpa to the rooms server now. → '' or why it didn't go."""
    g = query_one("SELECT * FROM grupa WHERE id=?", (gid,))
    if not g or not g.get("is_public"):
        return ""
    try:
        if not get_setting("gr_token", ""):
            raise green_room.GRError("Sign in first: a public grúpa lives on the rooms server.")
        green_room.grupa_publish(_grupa_public_payload(g))
        execute("UPDATE grupa SET public_dirty=0, public_error='', public_at=datetime('now') WHERE id=?", (gid,))
        _grupa_retry_at.pop(gid, None)
        return ""
    except green_room.GRError as e:
        execute("UPDATE grupa SET public_dirty=1, public_error=? WHERE id=?", (str(e), gid))
        _grupa_retry_at[gid] = _time.time() + 600
        return str(e)


def _grupa_unpublish(gid):
    """Take one down. If the rooms server can't be reached now, it is taken
    down the next time it can."""
    try:
        green_room.grupa_unpublish(gid)
        ok = True
    except green_room.GRError:
        ok = False
    try:
        pend = set(_json.loads(get_setting("grupa_unpublish_pending", "") or "[]"))
    except Exception:
        pend = set()
    before = set(pend)
    pend.discard(gid) if ok else pend.add(gid)
    if pend != before:
        set_setting("grupa_unpublish_pending", _json.dumps(sorted(pend)))
    return ok


def _grupa_set_public(gid, on):
    if on:
        execute("UPDATE grupa SET is_public=1, public_dirty=1, public_error='' WHERE id=?", (gid,))
        return _grupa_publish(gid)
    execute("UPDATE grupa SET is_public=0, public_dirty=0, public_error='', public_at=NULL WHERE id=?", (gid,))
    _grupa_unpublish(gid)
    return ""


def _grupa_public_flush():
    """Off the request thread, every half minute or so: send any public grúpa
    that changed, and finish any taking-down that couldn't be done at the time."""
    with app.app_context():
        try:
            _grupa_ensure()
            if not get_setting("gr_token", ""):
                return
            now = _time.time()
            for g in query("SELECT id FROM grupa WHERE COALESCE(is_public,0)=1 AND COALESCE(public_dirty,0)=1"):
                if _grupa_retry_at.get(g["id"], 0) <= now:
                    _grupa_publish(g["id"])
            try:
                pend = _json.loads(get_setting("grupa_unpublish_pending", "") or "[]")
            except Exception:
                pend = []
            for gid in pend:
                if not query_one("SELECT id FROM grupa WHERE id=? AND COALESCE(is_public,0)=1", (gid,)):
                    _grupa_unpublish(gid)
        except Exception:
            pass


@app.route("/api/grupa/<int:gid>/public", methods=["POST"])
def api_grupa_public(gid):
    """{on: true | false}. Off by default; only its owner turns it on."""
    _grupa_ensure()
    if not query_one("SELECT id FROM grupa WHERE id=?", (gid,)):
        return jsonify({"error": "not found"}), 404
    on = bool((request.get_json(silent=True) or {}).get("on"))
    why = _grupa_set_public(gid, on)
    try:
        green_room.track("grupa_public" if on else "grupa_private", {})
    except Exception:
        pass
    return jsonify({"ok": True, "is_public": on, "public_error": why})


@app.route("/api/grupa/bulk", methods=["POST"])
def api_grupa_bulk():
    """One thing done to several grúpaí at once: {ids: […], action: 'public' |
    'private' | 'delete'} — so nobody has to go through them one by one."""
    _grupa_ensure()
    d = request.get_json(silent=True) or {}
    ids = [int(i) for i in (d.get("ids") or []) if str(i).isdigit()][:500]
    ids = [r["id"] for r in query(f"SELECT id FROM grupa WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    action = d.get("action")
    if action not in ("public", "private", "delete"):
        return jsonify({"error": "what should be done to them?"}), 400
    why = ""
    for gid in ids:
        if action == "delete":
            _grupa_delete(gid)
        else:
            why = _grupa_set_public(gid, action == "public") or why
    try:
        green_room.track("grupa_bulk", {"action": action, "n": len(ids)})
    except Exception:
        pass
    return jsonify({"ok": True, "n": len(ids), "public_error": why})


@app.route("/api/grupa/<int:gid>/remove", methods=["POST"])
def api_grupa_remove(gid):
    """Take a thing out of a grúpa by what it is ({kind:'track', ref_id} ·
    {url} · {kind:'room', ref_key}) — the "add to a grúpa" window unticking one."""
    _grupa_ensure()
    d = request.get_json(silent=True) or {}
    n = 0
    if d.get("kind") == "track" and d.get("ref_id"):
        n = execute("DELETE FROM grupa_items WHERE grupa_id=? AND kind='track' AND ref_id=?", (gid, d["ref_id"]))
    elif d.get("kind") == "room" and d.get("ref_key"):
        n = execute("DELETE FROM grupa_items WHERE grupa_id=? AND kind='room' AND ref_key=?", (gid, str(d["ref_key"]).lstrip("@")))
    elif (d.get("url") or "").strip():
        known = _streams.parse(d["url"].strip())
        n = execute("DELETE FROM grupa_items WHERE grupa_id=? AND url=?",
                    (gid, known["url"] if known and not known.get("short") else d["url"].strip()))
    _grupa_touch(gid)
    return jsonify({"ok": True})


@app.route("/api/grupa/<int:gid>/items", methods=["POST"])
def api_grupa_add(gid):
    """Add one element. {url} alone = a pasted link (recognised here);
    otherwise {kind, ref_id | ref_key, …}. An optional note rides along."""
    _grupa_ensure()
    if not query_one("SELECT id FROM grupa WHERE id=?", (gid,)):
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    kind = (d.get("kind") or "").strip()
    url = (d.get("url") or "").strip()
    row = {"note": d.get("note") or ""}
    if url and kind in ("", "stream", "link"):
        known = _streams.parse(url)
        if known and known["source"] == "spotify":
            ai_flags.refresh(wait=not ai_flags.listed())    # the very first Spotify link waits for the list
        if known and not known.get("short") and (d.get("title") or "").strip():
            # already described (a search result, a playlist row, what Spotify is playing): no lookup
            try:
                secs = float(d.get("duration_sec") or 0) or None
            except (TypeError, ValueError):
                secs = None
            row.update(kind="stream", source=known["source"], stream_type=known["type"], url=known["url"],
                       title=d["title"].strip(), subtitle=(d.get("subtitle") or "").strip(), image=d.get("image") or "",
                       by_ids=(d.get("by_ids") or "").strip() or _streams.by_ids_of(known["source"], known["url"]),
                       duration_sec=secs)
        else:
            # a link this room already holds somewhere (in another grúpa) needs no second lookup
            prior = (query_one("SELECT * FROM grupa_items WHERE kind='stream' AND url=? AND COALESCE(title,'') != '' "
                               "ORDER BY id DESC LIMIT 1", (known["url"],))
                     if known and not known.get("short") else None)
            info = None if prior else (_streams.resolve(url) if known else None)
            if prior:
                row.update(kind="stream", source=prior["source"], stream_type=prior["stream_type"], url=prior["url"],
                           title=prior["title"], subtitle=prior["subtitle"], image=prior["image"],
                           by_ids=prior.get("by_ids") or "", duration_sec=prior.get("duration_sec"))
            elif info:
                row.update(kind="stream", source=info["source"], stream_type=info["type"], url=info["url"],
                           title=info["title"] or info["url"], subtitle=info["subtitle"], image=info["image"],
                           by_ids=info.get("by_ids") or "", duration_sec=info.get("duration_sec") or None)
            else:
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                given = (d.get("title") or "").strip()
                if given:
                    # already described (something from Explore, a link out of someone's grúpa): kept as it reads there
                    row.update(kind="link", source=_detect_source(url), url=url, title=given[:300],
                               subtitle=(d.get("subtitle") or "").strip()[:300], image=(d.get("image") or "").strip())
                else:
                    try:
                        r = _resolve_link(url)
                    except Exception:
                        r = {}
                    row.update(kind="link", source=r.get("source") or _detect_source(url), url=r.get("url") or url,
                               title=r.get("title") or url, subtitle=r.get("artist") or "", image=r.get("image") or "")
    elif kind == "track":
        t = query_one("SELECT id, title, artist_name FROM tracks WHERE id=? AND is_deleted=0", (d.get("ref_id"),))
        if not t:
            return jsonify({"error": "that record isn't in your room"}), 404
        row.update(kind="track", ref_id=t["id"], title=t["title"], subtitle=t["artist_name"])
    elif kind in ("label", "artist"):
        name = (d.get("ref_key") or "").strip()
        if not name:
            return jsonify({"error": "it needs a name"}), 400
        row.update(kind=kind, ref_key=name[:200], title=name[:200], subtitle=_GRUPA_NAMES[kind])
    elif kind in ("place", "set", "grupa"):
        table = {"place": "places", "set": "sets", "grupa": "grupa"}[kind]
        r = query_one(f"SELECT id, name FROM {table} WHERE id=?", (d.get("ref_id"),))
        if not r:
            return jsonify({"error": "not found"}), 404
        if kind == "grupa" and r["id"] == gid:
            return jsonify({"error": "a grúpa can't hold itself"}), 400
        row.update(kind=kind, ref_id=r["id"], title=r["name"], subtitle=_GRUPA_NAMES[kind])
    elif kind == "room":
        handle = (d.get("ref_key") or "").strip().lstrip("@")
        if not handle:
            return jsonify({"error": "which room?"}), 400
        row.update(kind="room", ref_key=handle, title=(d.get("title") or handle).strip(),
                   subtitle=(d.get("subtitle") or "").strip() or _GRUPA_NAMES["room"])
    else:
        return jsonify({"error": "nothing to add"}), 400
    if d.get("once"):                       # "keep what's playing": pressing twice doesn't add it twice
        ex = None
        if row["kind"] == "track":
            ex = query_one("SELECT * FROM grupa_items WHERE grupa_id=? AND kind='track' AND ref_id=?", (gid, row["ref_id"]))
        elif row.get("url"):
            ex = query_one("SELECT * FROM grupa_items WHERE grupa_id=? AND url=?", (gid, row["url"]))
        if ex:
            return jsonify({"ok": True, "already": True, "item": _grupa_hydrate(ex)})
    iid = _grupa_insert(gid, row)
    _grupa_touch(gid)
    try:
        green_room.track("grupa_add", {"kind": row["kind"], "source": row.get("source") or None})
    except Exception:
        pass
    return jsonify({"ok": True, "item": _grupa_hydrate(query_one("SELECT * FROM grupa_items WHERE id=?", (iid,)))}), 201


@app.route("/api/grupa/<int:gid>/tracks", methods=["POST"])
def api_grupa_add_tracks(gid):
    """Many records from the room at once ({ids:[…]}, in that order) — "add
    the records in hand". One already in the grúpa isn't added twice."""
    _grupa_ensure()
    if not query_one("SELECT id FROM grupa WHERE id=?", (gid,)):
        return jsonify({"error": "not found"}), 404
    ids = [int(i) for i in ((request.get_json(silent=True) or {}).get("ids") or []) if str(i).lstrip("-").isdigit()][:2000]
    have = {r["ref_id"] for r in query("SELECT ref_id FROM grupa_items WHERE grupa_id=? AND kind='track'", (gid,))}
    added = 0
    for tid in ids:
        if tid in have:
            continue
        t = query_one("SELECT id, title, artist_name FROM tracks WHERE id=? AND is_deleted=0", (tid,))
        if not t:
            continue
        _grupa_insert(gid, {"kind": "track", "ref_id": t["id"], "title": t["title"], "subtitle": t["artist_name"]})
        have.add(tid)
        added += 1
    if added:
        _grupa_touch(gid)
        try:
            green_room.track("grupa_add", {"kind": "track", "n": added})
        except Exception:
            pass
    return jsonify({"ok": True, "added": added, "already": len(ids) - added})


@app.route("/api/grupa/<int:gid>/streams", methods=["POST"])
def api_grupa_add_streams(gid):
    """Many streamed tracks at once, already described ({items:[{url, title,
    subtitle, image, by_ids, duration_sec}]}, in that order) — "add all" from an
    opened Spotify playlist. One whose link is already in the grúpa isn't added
    twice; anything that isn't a recognised stream link is left out."""
    _grupa_ensure()
    if not query_one("SELECT id FROM grupa WHERE id=?", (gid,)):
        return jsonify({"error": "not found"}), 404
    rows = ((request.get_json(silent=True) or {}).get("items") or [])[:2000]
    have = {r["url"] for r in query("SELECT url FROM grupa_items WHERE grupa_id=? AND url IS NOT NULL", (gid,))}
    added = already = 0
    for d in rows:
        known = _streams.parse((d.get("url") or "").strip()) if isinstance(d, dict) else None
        if not known or known.get("short") or not (d.get("title") or "").strip():
            continue
        if known["url"] in have:
            already += 1
            continue
        try:
            secs = float(d.get("duration_sec") or 0) or None
        except (TypeError, ValueError):
            secs = None
        _grupa_insert(gid, {"kind": "stream", "source": known["source"], "stream_type": known["type"], "url": known["url"],
                            "title": d["title"].strip(), "subtitle": (d.get("subtitle") or "").strip(), "image": d.get("image") or "",
                            "by_ids": (d.get("by_ids") or "").strip() or _streams.by_ids_of(known["source"], known["url"]),
                            "duration_sec": secs})
        have.add(known["url"])
        added += 1
    if added:
        _grupa_touch(gid)
        try:
            green_room.track("grupa_add", {"kind": "stream", "n": added})
        except Exception:
            pass
    return jsonify({"ok": True, "added": added, "already": already})


@app.route("/api/grupa/item/<int:iid>/ai", methods=["POST"])
def api_grupa_item_ai(iid):
    """The listener's own mark on a stream: {ai: true} = AI-made (it won't be
    played here), {ai: false} = not AI (lifts a mark, the public list's
    included). It hangs on who the stream is by, so it covers their other
    tracks too."""
    _grupa_ensure()
    it = query_one("SELECT * FROM grupa_items WHERE id=?", (iid,))
    if not it or it["kind"] != "stream":
        return jsonify({"error": "not found"}), 404
    if not (it.get("by_ids") or "") and (it.get("source") or "") == "spotify":
        try:                                    # added before the app kept who it is by: ask once
            info = _streams.resolve(it["url"]) or {}
            if info.get("by_ids"):
                execute("UPDATE grupa_items SET by_ids=? WHERE id=?", (info["by_ids"], iid))
                it["by_ids"] = info["by_ids"]
        except Exception:
            pass
    keys = ai_flags.keys_for(it.get("source") or "", it.get("by_ids") or "", it.get("url") or "")
    want = bool((request.get_json(silent=True) or {}).get("ai"))
    marked, cleared = _ai_marks()
    if want:
        marked |= set(keys); cleared -= set(keys)
    else:
        marked -= set(keys); cleared |= set(keys)
    set_setting("ai_marked", _json.dumps(sorted(marked)))
    set_setting("ai_cleared", _json.dumps(sorted(cleared)))
    try:
        green_room.track("ai_mark" if want else "ai_cleared",
                         {"source": it.get("source"), "by": it.get("by_ids") or None, "url": it.get("url"),
                          "title": it.get("title"), "artist": it.get("subtitle")})
    except Exception:
        pass
    return jsonify({"ok": True, "item": _grupa_hydrate(it)})


@app.route("/api/ai-list")
def api_ai_list():
    """What the app knows about AI-made music: the public list it checks (how
    many accounts, when it was fetched) and how many marks are the listener's."""
    marked, cleared = _ai_marks()
    return jsonify(dict(ai_flags.status(), marked=len(marked), cleared=len(cleared)))


@app.route("/api/grupa/item/<int:iid>", methods=["PATCH"])
def api_grupa_item_patch(iid):
    _grupa_ensure()
    it = query_one("SELECT id, grupa_id FROM grupa_items WHERE id=?", (iid,))
    if not it:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    if "note" in d:
        execute("UPDATE grupa_items SET note=? WHERE id=?", ((d.get("note") or "").strip(), iid))
    if "title" in d and (d.get("title") or "").strip():
        execute("UPDATE grupa_items SET title=? WHERE id=?", (d["title"].strip()[:300], iid))
    _grupa_touch(it["grupa_id"])
    return jsonify({"ok": True})


@app.route("/api/grupa/item/<int:iid>", methods=["DELETE"])
def api_grupa_item_delete(iid):
    _grupa_ensure()
    it = query_one("SELECT grupa_id FROM grupa_items WHERE id=?", (iid,))
    if it:
        execute("DELETE FROM grupa_items WHERE id=?", (iid,))
        _grupa_touch(it["grupa_id"])
    return jsonify({"ok": True})


@app.route("/api/grupa/<int:gid>/order", methods=["POST"])
def api_grupa_order(gid):
    _grupa_ensure()
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    db = get_db()
    for pos, iid in enumerate(ids):
        db.execute("UPDATE grupa_items SET position=? WHERE id=? AND grupa_id=?", (pos, iid, gid))
    db.commit()
    _grupa_touch(gid)
    return jsonify({"ok": True})


# ── a grúpa as a FOLDER of files, to drag into Ableton, Serato, rekordbox… ──
# (Daithí, 2026-11-16.) grupa_folder.py does the work and explains it: the
# grúpa's own records, in its order, put in one folder WITHOUT copying them
# where the disk allows (the same file listed twice, else a link to it), and
# never moved. Here: which files a grúpa stands for, where its folder lives,
# and the three things the page asks — what would happen, do it, show it.
import grupa_folder as _grupa_folder_mod


def _grupa_folder_tracks(gid):
    """A grúpa's own records as files on this machine, in its order →
    (tracks, left_out). Labels, artists, places, sets and grúpaí inside it open
    out as they do when it is played. Left out, and counted: streamed things,
    borrowed records (locked: they play only here), files that can't be found."""
    tracks, left = [], {"streams": 0, "borrowed": 0, "missing": 0, "other": 0}
    seen = {gid}
    for it in _grupa_items(gid):
        k = it["kind"]
        if k == "stream":
            left["streams"] += 1
            continue
        if k in ("room", "link"):
            left["other"] += 1
            continue
        for r in _grupa_queue([it], seen=seen):
            if r.get("kind") == "embed":
                left["streams"] += 1
                continue
            f = query_one("SELECT file_path, analysis_status, duration_sec FROM files WHERE id=?", (r["file_id"],))
            if not f:
                left["missing"] += 1
                continue
            if (f.get("analysis_status") or "") == "locked" or str(f.get("file_path") or "").endswith(".mrb"):
                left["borrowed"] += 1
                continue
            path = _resolve_audio_path(f.get("file_path") or "")
            if not path:
                left["missing"] += 1
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                left["missing"] += 1
                continue
            tracks.append({"track_id": r["track_id"], "title": r.get("title") or "", "artist": r.get("artist") or "",
                           "path": path, "duration_sec": f.get("duration_sec") or r.get("duration_sec") or 0, "size": size})
    return tracks, left


def _grupa_folder_place(g):
    """→ (the folder this grúpa's records would go in, "library" | "own")."""
    try:
        lib = get_library_dest()
    except Exception:
        lib = ""
    root, where = _grupa_folder_mod.root_for(lib, get_export_dir())
    return root / _grupa_folder_mod.clean(g["name"]), where


@app.route("/api/grupa/<int:gid>/folder")
def api_grupa_folder(gid):
    """What a grúpa's folder is now, and what making it (again) would do."""
    _grupa_ensure()
    g = query_one("SELECT * FROM grupa WHERE id=?", (gid,))
    if not g:
        return jsonify({"error": "no such grúpa"}), 404
    tracks, left = _grupa_folder_tracks(gid)
    target, where = _grupa_folder_place(g)
    st = (g.get("folder_path") and _grupa_folder_mod.status(g["folder_path"])) or _grupa_folder_mod.status(target)
    if st:
        m = _grupa_folder_mod.read_manifest(st["folder"]) or {}
        now = [(e.get("file"), e.get("src")) for e in m.get("files") or []]
        want = list(zip(_grupa_folder_mod.names_for(tracks), [t["path"] for t in tracks]))
        st["stale"] = now != want or os.path.basename(st["folder"]) != target.name
        st["bytes"] = sum(t["size"] for t in tracks) if st["how"].get("copy") else 0
    return jsonify({"folder": st,
                    "plan": {"count": len(tracks), "seconds": round(sum(t["duration_sec"] or 0 for t in tracks)),
                             "bytes": sum(t["size"] for t in tracks), "left_out": left, "where": where,
                             "would_be": str(target)}})


@app.route("/api/grupa/<int:gid>/folder", methods=["POST"])
def api_grupa_folder_make(gid):
    """Make the grúpa's folder, or bring it in line with the grúpa. {mode: "link"
    (default: nothing is copied) | "copy"}. When the disk can't hold a record
    without copying it, nothing is done and the answer says so (needs_copy),
    with how much room copies would take: the page asks before copying."""
    _grupa_ensure()
    g = query_one("SELECT * FROM grupa WHERE id=?", (gid,))
    if not g:
        return jsonify({"error": "no such grúpa"}), 404
    mode = "copy" if (request.get_json(silent=True) or {}).get("mode") == "copy" else "link"
    tracks, left = _grupa_folder_tracks(gid)
    if not tracks:
        return jsonify({"error": "Nothing in this grúpa is a file on this machine.", "left_out": left}), 400
    target, where = _grupa_folder_place(g)
    known = g.get("folder_path") or ""
    try:
        # the grúpa was renamed since its folder was made: the folder follows it
        if (known and os.path.isdir(known) and os.path.abspath(known) != os.path.abspath(str(target))
                and os.path.dirname(os.path.abspath(known)) == os.path.abspath(str(target.parent))
                and _grupa_folder_mod.read_manifest(known) and not target.exists()):
            os.rename(known, str(target))
        # a folder of that name that this app didn't make: stay out of it
        if target.is_dir() and not _grupa_folder_mod.read_manifest(target) and any(n != ".DS_Store" for n in os.listdir(target)):
            return jsonify({"error": f"There is already a folder called “{target.name}” there, and this app didn't make it. "
                                     "Rename the grúpa, or move that folder away."}), 409
        if mode == "link" and not _grupa_folder_mod.can_link(target, tracks[0]["path"]):
            try:
                target.rmdir()
            except OSError:
                pass
            return jsonify({"needs_copy": True, "count": len(tracks), "bytes": sum(t["size"] for t in tracks),
                            "error": "This disk can't hold your records without copying them."}), 409
        out = _grupa_folder_mod.build(target, g["name"], tracks, mode=mode, grupa_id=gid)
    except OSError as e:
        return jsonify({"error": f"The folder couldn't be made: {e.strerror or e}"}), 500
    execute("UPDATE grupa SET folder_path=?, folder_at=datetime('now') WHERE id=?", (out["folder"], gid))
    if IS_GR:
        green_room.track("grupa_folder", {"mode": mode, "files": out["files"], "same": out["same"],
                                          "linked": out["linked"], "copied": out["copied"]})
    return jsonify(dict(out, left_out=left, where=where, mode=mode))


@app.route("/api/grupa/<int:gid>/folder/show", methods=["POST"])
def api_grupa_folder_show(gid):
    """Open the grúpa's folder in Finder."""
    _grupa_ensure()
    g = query_one("SELECT folder_path FROM grupa WHERE id=?", (gid,))
    path = (g or {}).get("folder_path") or ""
    if not path or not os.path.isdir(path):
        return jsonify({"error": "The folder isn't there. Is its drive plugged in?"}), 404
    try:
        mr_os.open_target(path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "folder": path})


@app.route("/api/grupa/<int:gid>/queue")
def api_grupa_queue(gid):
    """What the deck should cue: the whole grúpa, or one element (?item=<id>)."""
    _grupa_ensure()
    iid = request.args.get("item", type=int)
    items = (query("SELECT * FROM grupa_items WHERE id=? AND grupa_id=?", (iid, gid)) if iid else _grupa_items(gid))
    out, starts, seen = [], {}, {gid}
    for it in items:                     # `starts` = where each element begins in the cue
        sub = _grupa_queue([it], seen=seen)
        if sub:
            starts[str(it["id"])] = len(out)
            out.extend(sub)
    return jsonify({"items": out, "starts": starts})


@app.route("/api/grupa/search")
def api_grupa_search():
    """Type to find something to add: records, artists and labels in your room,
    places, sets, other grúpa, public music rooms. A name that matches nothing
    can still be added as a label or an artist."""
    _grupa_ensure()
    q = (request.args.get("q") or "").strip()
    gid = request.args.get("gid", type=int) or 0
    if len(q) < 2:
        return jsonify({"results": []})
    # words in any order, each found in the title, the artist, the album or the
    # label ("stott luxury" finds Luxury Problems by Andy Stott)
    typed, q = q, mr_text.fold(q)         # "roisin" finds Róisín; "Björk" finds Bjork
    like = f"%{q}%"
    toks = [t for t in q.split() if t][:6]
    each = "(mrfold(t.title) LIKE ? OR mrfold(t.artist_name) LIKE ? OR mrfold(t.album) LIKE ? OR mrfold(t.label) LIKE ?)"
    have = {r["ref_id"] for r in query("SELECT ref_id FROM grupa_items WHERE grupa_id=? AND kind='track'", (gid,))} if gid else set()
    res = []
    for t in query("SELECT t.id, t.title, t.artist_name, t.album, t.year, t.artwork_path FROM tracks t "
                   "WHERE t.is_deleted=0 AND COALESCE(t.is_recording,0)=0 AND " + " AND ".join(each for _ in toks) +
                   " ORDER BY (mrfold(t.title) LIKE ?) DESC, (mrfold(t.artist_name) LIKE ?) DESC, t.artist_name, t.album, t.track_number, t.title LIMIT 10",
                   [f"%{tok}%" for tok in toks for _ in range(4)] + [q + "%", q + "%"]):
        res.append({"kind": "track", "ref_id": t["id"], "title": t["title"] or "Untitled",
                    "subtitle": t["artist_name"] or "", "album": t["album"] or "", "year": t["year"] or "",
                    "image": f"/api/artwork/{t['id']}?s=96" if t["artwork_path"] else "", "have": t["id"] in have})
    names = {"artist": set(), "label": set()}
    for kind, col in (("artist", "artist_name"), ("label", "label")):
        for r in query(f"SELECT {col} AS name, COUNT(*) AS n, MIN(CASE WHEN artwork_path IS NOT NULL THEN id END) AS art "
                       f"FROM tracks WHERE is_deleted=0 AND {col} IS NOT NULL AND " + " AND ".join(f"mrfold({col}) LIKE ?" for _ in toks) +
                       f" GROUP BY {col} COLLATE NOCASE ORDER BY (mrfold({col}) LIKE ?) DESC, n DESC LIMIT 4",
                       [f"%{tok}%" for tok in toks] + [q + "%"]):
            names[kind].add(mr_text.fold(r["name"]))
            res.append({"kind": kind, "ref_key": r["name"], "title": r["name"], "subtitle": _GRUPA_NAMES[kind], "count": r["n"],
                        "image": f"/api/artwork/{r['art']}?s=96" if r["art"] else ""})
    # (places belong to the forest, which the trial doesn't have)
    for kind, table in ((("set", "sets"), ("grupa", "grupa")) if IS_GR else (("place", "places"), ("set", "sets"), ("grupa", "grupa"))):
        for r in query(f"SELECT id, name FROM {table} WHERE mrfold(name) LIKE ? AND NOT (? = 'grupa' AND id = ?) LIMIT 4",
                       (like, kind, gid)):
            res.append({"kind": kind, "ref_id": r["id"], "title": r["name"], "subtitle": _GRUPA_NAMES[kind]})
    if get_setting("gr_token", ""):
        try:
            for r in (green_room.rooms(typed) or [])[:4]:
                res.append({"kind": "room", "ref_key": r.get("handle"), "title": r.get("name") or r.get("handle"),
                            "subtitle": (r.get("kind") or "").capitalize() + " · music room" if r.get("kind") else _GRUPA_NAMES["room"]})
        except Exception:
            pass
    for kind in ("label", "artist"):                 # not in your room? it can still go in, by name
        if q not in names[kind]:             # (as it was typed: the accents are theirs to keep)
            res.append({"kind": kind, "ref_key": typed, "title": typed, "subtitle": _GRUPA_NAMES[kind], "free": True})
    return jsonify({"results": res})


# Where the sound goes — the deck's speaker list — keeps its routes in sound_out.py.
try:
    import sound_out as _sound_out
    _sound_out.register(app, track=lambda name, data: green_room.track(name, data))
except ImportError:
    pass

# The globe in Explore keeps its own routes in explore_globe.py (register(app)).
try:
    import explore_globe as _explore_globe
    _explore_globe.register(app)
except ImportError:
    pass

# How the Map in Explore is dressed (its skin, which way is up) is kept by map_look.py.
try:
    import map_look as _map_look
    _map_look.register(app, get_setting, set_setting, track=lambda name, data: green_room.track(name, data))
except ImportError:
    pass

# Plug-ins for exploring and discovering (Explore → Plug-ins) keep their routes in plugins.py.
try:
    import plugins as _plugins
    _plugins.register(app, BASE_DIR, DATA_DIR, get_setting, set_setting, query, green_room,
                      track=lambda name, data: green_room.track(name, data))
except ImportError:
    pass

# Record shopping (Explore: crates, the counter, blind) keeps its routes in record_shop.py.
try:
    import record_shop as _record_shop
    _record_shop.register(app)
except ImportError:
    pass

# Find it (paste a YouTube link, say album / mix / talk, bring the music home) lives in find_music.py.
try:
    import find_music as _find_music
    _find_music.register(app, get_spotify=lambda: _get_spotify())
except ImportError:
    pass

# The Void (the wall as a real 3D space, time as depth) keeps its own store and serves its pictures: void_store.py.
try:
    import void_store as _void_store
    _void_store.register(app, get_setting, set_setting)

    @app.route("/api/void/mine")
    def api_void_mine():
        """Which place holds my Void (the Well, as for the room page), so My Public Room can publish a copy of it."""
        w = query_one("SELECT id FROM places WHERE is_well=1")
        return jsonify({"pid": w["id"] if w else 1})
except ImportError:
    pass

# Connections (the neighbourhood of a record: label, genres, other records, similar artists) lives in connections.py.
try:
    import connections as _connections
    _connections.register(app)
except ImportError:
    pass


if __name__ == "__main__":
    _port = int(os.environ.get("MUSICROOM_PORT", "5177"))
    print("\n" + "="*60)
    print(f"  Music Room{' 3D' if IS_3D else ''} — starting")
    print(f"  http://127.0.0.1:{_port}")
    print("="*60 + "\n")
    # Kick off a background backup to the hard drive if it's mounted
    backup_to_drive(block=False)
    app.run(debug=True, port=_port, threaded=True)
