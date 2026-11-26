"""
spotify.py — thin Spotify Web API client with PKCE OAuth.

Why this exists: Music Room's vision is owning music. But most users are
arriving from DSPs with their library on Spotify. This module is the
bridge that lets us *see* that library, recognise which tracks the user
already owns locally, and help them find the rest on Bandcamp/Discogs/etc.

No Spotify Client Secret is used — PKCE flow only. The user registers
their own Spotify app, pastes the Client ID into Music Room settings,
and adds http://127.0.0.1:5177/api/spotify/callback as a redirect URI.

Tokens live in the `settings` table (per-machine, never synced).
"""

import base64
import datetime as _dt
import hashlib
import json
import os
import secrets
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request


# =============================================================
# APP-WIDE RATE LIMITER — every Spotify call goes through this.
# Shared across all SpotifyClient instances + every code path that
# touches the Spotify API (DSP check, artwork fetcher, /spotify
# pages). Designed to be conservative enough that a personal
# dev-mode app (post-Feb-2026 Spotify rules) won't accumulate
# 429s and trigger progressive shadow-throttling.
# =============================================================

_RATE_LOCK = threading.Lock()
_LAST_CALL_TS         = 0.0
_DEFAULT_GAP_S        = 2.5   # ~24 calls/min, well under the documented 180/min
_PENALISED_GAP_S      = 5.0   # ~12 calls/min — applied after a 429
_RECENT_429_FLOOR_S   = 600   # how long the penalty lasts (10 min)
_RECENT_429_UNTIL_TS  = 0.0

# Daily soft cap. Safer than no cap at all — Spotify doesn't publish a
# daily quota, but apps that 429 a lot get progressively throttled.
# 2,000 is enough to fully classify a ~2k-track library in one day plus
# headroom for retries; well under any realistic ban threshold.
SPOTIFY_DAILY_CAP = 2000


def _acquire(settings_io):
    """Block until we're allowed to make the next Spotify call.

    Two gates: time-based (min gap since last call, raised after a 429)
    and a daily soft-cap (refuses if today's count has reached
    SPOTIFY_DAILY_CAP). Returns True if cleared, False if cap hit.
    """
    global _LAST_CALL_TS

    # Daily cap check — fast, no lock needed beyond settings_io's own.
    today = _dt.date.today().isoformat()
    stored_date = (settings_io.get("spotify_calls_today_date", "") or "").strip()
    if stored_date != today:
        settings_io.set("spotify_calls_today_date",  today)
        settings_io.set("spotify_calls_today_count", "0")
        count = 0
    else:
        try:    count = int(settings_io.get("spotify_calls_today_count", "0") or 0)
        except Exception: count = 0
    if count >= SPOTIFY_DAILY_CAP:
        return False

    # Time-based gate. _LAST_CALL_TS is module-shared so concurrent
    # callers (e.g. DSP + artwork batch) serialise through the same
    # bottleneck rather than each running at their own rate.
    with _RATE_LOCK:
        now = time.time()
        gap = _PENALISED_GAP_S if now < _RECENT_429_UNTIL_TS else _DEFAULT_GAP_S
        wait = (_LAST_CALL_TS + gap) - now
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL_TS = time.time()

    # Increment daily counter for this attempt (counts both successes
    # and rate-limited tries — Spotify counts the latter against budget too).
    try:
        settings_io.set("spotify_calls_today_count", str(count + 1))
    except Exception:
        pass
    return True


def _penalise():
    """A 429 was just received — raise the minimum-gap floor for the
    next 10 minutes."""
    global _RECENT_429_UNTIL_TS
    with _RATE_LOCK:
        _RECENT_429_UNTIL_TS = max(_RECENT_429_UNTIL_TS, time.time() + _RECENT_429_FLOOR_S)


def _rate_state(settings_io):
    """Snapshot of current rate state — used by the UI to show
    "X / 2000 today" and "throttled until …"."""
    today = _dt.date.today().isoformat()
    stored_date = (settings_io.get("spotify_calls_today_date", "") or "").strip()
    if stored_date != today:
        used = 0
    else:
        try:    used = int(settings_io.get("spotify_calls_today_count", "0") or 0)
        except Exception: used = 0
    penalised = time.time() < _RECENT_429_UNTIL_TS
    return {
        "used_today":   used,
        "daily_cap":    SPOTIFY_DAILY_CAP,
        "remaining_today": max(0, SPOTIFY_DAILY_CAP - used),
        "current_gap_s":   _PENALISED_GAP_S if penalised else _DEFAULT_GAP_S,
        "penalised":       penalised,
        "penalty_until":   _RECENT_429_UNTIL_TS if penalised else None,
    }


AUTH_BASE   = "https://accounts.spotify.com"
API_BASE    = "https://api.spotify.com/v1"

# Scopes we ask for.
#   user-library-read         — saved tracks
#   playlist-read-private     — user's private playlists
#   playlist-read-collaborative — collab playlists
#   user-read-private         — display name / profile
#   user-read-playback-state / user-read-currently-playing — see what the
#                               listener's Spotify is playing, and on what
#   user-modify-playback-state — play / pause / skip it from the deck
#                               (Spotify only lets Premium accounts do this)
SCOPES = ("user-library-read playlist-read-private playlist-read-collaborative user-read-private "
          "user-read-playback-state user-read-currently-playing user-modify-playback-state")
SCOPE_SEE     = ("user-read-playback-state", "user-read-currently-playing")
SCOPE_CONTROL = "user-modify-playback-state"

# Setting keys. Centralised so callers don't typo.
KEY_CLIENT_ID        = "spotify_client_id"
KEY_REDIRECT_URI     = "spotify_redirect_uri"   # rarely overridden
KEY_ACCESS_TOKEN     = "spotify_access_token"
KEY_REFRESH_TOKEN    = "spotify_refresh_token"
KEY_TOKEN_EXPIRES_AT = "spotify_token_expires_at"
KEY_USER_ID          = "spotify_user_id"
KEY_USER_NAME        = "spotify_user_name"
KEY_PKCE_VERIFIER    = "spotify_pkce_verifier"     # transient, cleared after callback
KEY_LAST_SYNCED_AT   = "spotify_last_synced_at"
KEY_SCOPES           = "spotify_scopes"            # what the sign-in actually granted
KEY_NO_PREMIUM       = "spotify_no_premium"        # "1" once Spotify refused control for want of Premium

# What checkup() found, in words for the person: (what it is, what to do). {name} = their Spotify name.
CHECKUP_WORDS = {
    "ready":      ("Whole tracks play through your own Spotify.", ""),
    "no_key":     ("No Spotify key is set in this app.", "Paste the Client ID you were given, above, then sign in."),
    "signed_out": ("You are not signed in to Spotify here.", "Press Sign in, and finish in your browser."),
    "network":    ("Spotify could not be reached just now.", "Check your internet and press Check again."),
    "busy":       ("Spotify asked for a moment.", "Wait a minute and press Check again."),
    "unlisted":   ("Spotify does not know you on this key yet, so it only gives previews.",
                   "Whoever gave you the Client ID has to add you: developer.spotify.com → Dashboard → their app → "
                   "User Management, with your name and the EMAIL OF YOUR SPOTIFY ACCOUNT (not another address of yours). "
                   "Then press Check again. If it still says this, sign out here and sign in again."),
    "reconnect":  ("This sign-in is missing the permission to play.", "Sign out here and sign in again, and say yes to everything Spotify asks."),
    "free":       ("Your Spotify account is not Premium.",
                   "Spotify only lets Premium accounts be played from another app. Until then you hear its 30-second previews."),
    "no_app":     ("The Spotify app is not on this computer.",
                   "Install it from spotify.com/download, open it, and sign in as {name}. Then press Check again."),
    "no_device":  ("No Spotify app is open on a computer for this account.",
                   "Open the Spotify app on this computer and make sure it is signed in as {name} — the same account as here. Then press Check again."),
}

# THE one address the listener registers on their Spotify app, port included.
# Every copy of Music Room signs in with exactly this, whatever port the copy
# itself runs on: when that isn't 5177, app.py opens 5177 just long enough to
# take Spotify's answer (_spotify_callback_listener).
#   Why not "one address per port" or a port-less address: Spotify's docs say
#   a loopback address may be registered without a port and given one at
#   sign-in, but its developer dashboard REFUSES that form ("This redirect URI
#   is not secure") — Daithí hit it on 2026-11-14, as have others on Spotify's
#   forum. An explicit port is the only form the dashboard reliably takes.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:5177/api/spotify/callback"


# ─── the player lane ────────────────────────────────────────────────
# Remote-control calls (what is playing, play, pause, skip, search-as-you-type)
# are the listener's own gestures and the deck's clock: they can't wait behind
# the 2.5-second library gate above, and they don't count against its daily
# cap. They get a light gate of their own, and stand down when Spotify says
# to slow (429 + Retry-After).
_PLAYER_LOCK          = threading.Lock()
_PLAYER_LAST_TS       = 0.0
_PLAYER_GAP_S         = 0.12
_PLAYER_BACKOFF_UNTIL = 0.0


def _player_gate():
    """True when a player-lane call may go now (after a short wait if needed)."""
    global _PLAYER_LAST_TS
    if time.time() < _PLAYER_BACKOFF_UNTIL:
        return False
    with _PLAYER_LOCK:
        wait = (_PLAYER_LAST_TS + _PLAYER_GAP_S) - time.time()
        if wait > 0:
            time.sleep(wait)
        _PLAYER_LAST_TS = time.time()
    return True


def _player_backoff(seconds):
    global _PLAYER_BACKOFF_UNTIL
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 5.0
    _PLAYER_BACKOFF_UNTIL = max(_PLAYER_BACKOFF_UNTIL, time.time() + max(1.0, min(60.0, seconds)))


# ─── helpers ────────────────────────────────────────────────────────

def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_pkce_pair():
    """Generate a PKCE code_verifier + code_challenge pair."""
    verifier  = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(client_id, redirect_uri, code_challenge, state):
    qs = urllib.parse.urlencode({
        "client_id":             client_id,
        "response_type":         "code",
        "redirect_uri":          redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge":        code_challenge,
        "scope":                 SCOPES,
        "state":                 state,
        "show_dialog":           "false",
    })
    return f"{AUTH_BASE}/authorize?{qs}"


def _http_json(method, url, headers=None, data=None, timeout=20):
    """Minimal HTTP wrapper that returns parsed JSON + status."""
    if data is not None and not isinstance(data, (bytes, str)):
        data = urllib.parse.urlencode(data).encode("utf-8")
    elif isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore") or "{}"
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"raw": body}      # the player endpoints sometimes answer 200 with a bare string
            return r.status, (parsed if isinstance(parsed, dict) else {"raw": parsed})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore") or "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        try:
            ra = e.headers.get("Retry-After")
            if ra:
                payload["_retry_after"] = ra
        except Exception:
            pass
        # Log raw Spotify error responses to stderr so they're visible when
        # the app is launched from a terminal (./start.sh). This is the best
        # way to see Spotify's own error message ("Insufficient client scope",
        # "Forbidden", etc.) when the UI just shows a generic 403.
        try:
            short_url = url.split("?", 1)[0]
            print(f"[spotify] {method} {short_url} -> {e.code}: {payload!r}", flush=True)
        except Exception:
            pass
        return e.code, payload
    except Exception as e:                  # no network, a timeout, DNS
        return 0, {"error": "network", "detail": str(e)[:200]}


def exchange_code(client_id, redirect_uri, code, code_verifier):
    """Exchange the auth code for tokens."""
    status, data = _http_json(
        "POST", f"{AUTH_BASE}/api/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  redirect_uri,
            "client_id":     client_id,
            "code_verifier": code_verifier,
        },
    )
    return status, data


def refresh_token(client_id, refresh_token_value):
    status, data = _http_json(
        "POST", f"{AUTH_BASE}/api/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token_value,
            "client_id":     client_id,
        },
    )
    return status, data


# ─── token / settings bridge ─────────────────────────────────────────
# Callers pass a `settings_io` object exposing get(key, default='') and
# set(key, value) so we don't depend on app.py's exact helper names.

class SpotifyClient:
    def __init__(self, settings_io):
        self.s = settings_io

    # ─── identity / status ──────────────────────────────────────────
    @property
    def client_id(self):
        return (self.s.get(KEY_CLIENT_ID, "") or "").strip()

    @property
    def redirect_uri(self):
        return (self.s.get(KEY_REDIRECT_URI, "") or "").strip() or DEFAULT_REDIRECT_URI

    def is_connected(self):
        return bool(self.s.get(KEY_REFRESH_TOKEN, "").strip())

    def scopes(self):
        return set((self.s.get(KEY_SCOPES, "") or "").split())

    def can_see_playing(self):
        return self.is_connected() and any(x in self.scopes() for x in SCOPE_SEE)

    def can_control(self):
        """May the deck drive this listener's Spotify? Needs the playback
        permission (a sign-in from before 2026-11-14 doesn't have it) and a
        Premium account (Spotify refuses the rest)."""
        return (self.is_connected() and SCOPE_CONTROL in self.scopes()
                and (self.s.get(KEY_NO_PREMIUM, "") or "") != "1")

    def status(self):
        connected = self.is_connected()
        return {
            "connected":      connected,
            "can_see_playing": self.can_see_playing(),
            "can_control":    self.can_control(),
            # signed in before the deck could drive Spotify → one fresh sign-in adds it
            "needs_reconnect": connected and SCOPE_CONTROL not in self.scopes(),
            "no_premium":     (self.s.get(KEY_NO_PREMIUM, "") or "") == "1",
            "client_id_set":  bool(self.client_id),
            "redirect_uri":   self.redirect_uri,      # the address to register on the Spotify app
            "user_id":        self.s.get(KEY_USER_ID, "") or None,
            "user_name":      self.s.get(KEY_USER_NAME, "") or None,
            "last_synced_at": self.s.get(KEY_LAST_SYNCED_AT, "") or None,
            "rate":           _rate_state(self.s),
        }

    # ─── "why does it only play previews?" ──────────────────────────
    def checkup(self, app_there=None):
        """What stands between this listener and WHOLE tracks in their own Spotify, asked of
        Spotify itself, one thing after another → {"step", "ok", "name", "product", "devices"}.

        step: no_key · signed_out · network · busy · unlisted · reconnect · free · no_app ·
        no_device · ready.   `app_there`: is the Spotify app on this computer (None = can't tell).

        Why it exists (25 Nov 2026): a fellow was given the key, signed in, and still heard
        previews — and the app could not say which of three things it was. A key in development
        mode only works for the people its owner has listed (Spotify answers everyone else 403,
        even after a sign-in that looked fine); playing from another app needs Premium; and
        there must be a Spotify app open on a computer, signed in to the SAME account."""
        out = {"step": "", "ok": False, "name": self.s.get(KEY_USER_NAME, "") or "", "product": "", "devices": []}
        if not self.client_id:
            return dict(out, step="no_key")
        if not self.is_connected():
            return dict(out, step="signed_out")
        status, me = self._player_call("GET", "/me")
        if status in (0, 429, 401):
            return dict(out, step={0: "network", 429: "busy", 401: "signed_out"}[status])
        if status == 403:
            why = (self._why(me) or str(me.get("raw") or "")).upper()
            return dict(out, step="reconnect" if "SCOPE" in why else "unlisted")
        if status != 200 or not isinstance(me, dict) or not me.get("id"):
            return dict(out, step="network")
        out["name"] = me.get("display_name") or me.get("id") or out["name"]
        out["product"] = (me.get("product") or "").lower()
        if not self.s.get(KEY_USER_ID, ""):                 # (a sign-in made before the owner listed them has no name yet)
            self.s.set(KEY_USER_ID, me.get("id") or "")
            self.s.set(KEY_USER_NAME, out["name"])
        if SCOPE_CONTROL not in self.scopes():
            return dict(out, step="reconnect")
        if out["product"] and out["product"] != "premium":
            return dict(out, step="free")
        if out["product"] == "premium" and (self.s.get(KEY_NO_PREMIUM, "") or "") == "1":
            self.s.set(KEY_NO_PREMIUM, "")                  # (they have Premium now: let the deck try again)
        status, d = self._player_call("GET", "/me/player/devices")
        if status in (0, 429):
            return dict(out, step={0: "network", 429: "busy"}[status])
        if status == 403:
            return dict(out, step="unlisted")
        devs = (d.get("devices") or []) if status == 200 and isinstance(d, dict) else []
        out["devices"] = [x.get("name") or "a computer" for x in devs
                          if x.get("id") and not x.get("is_restricted") and x.get("type") == "Computer"]
        if not out["devices"]:
            return dict(out, step="no_app" if app_there is False else "no_device")
        return dict(out, step="ready", ok=True)

    # ─── OAuth flow ─────────────────────────────────────────────────
    def begin_auth(self, redirect_uri=None):
        """Start the PKCE flow. Returns (auth_url, state) so the caller
        can open the URL externally. The redirect used is remembered with the
        sign-in, so the exchange sends exactly the same one."""
        if not self.client_id:
            return None, None
        verifier, challenge = make_pkce_pair()
        state = _b64url(secrets.token_bytes(16))
        ru = redirect_uri or self.redirect_uri
        self.s.set(KEY_PKCE_VERIFIER, verifier + "|" + state + "|" + ru)
        return authorize_url(self.client_id, ru, challenge, state), state

    def complete_auth(self, code, state):
        """Finish the PKCE flow. Returns (ok, error_or_user_dict)."""
        stash = self.s.get(KEY_PKCE_VERIFIER, "") or ""
        if not stash or "|" not in stash:
            return False, "no PKCE verifier in flight"
        bits = stash.split("|", 2)
        verifier, want_state = bits[0], bits[1]
        ru = bits[2] if len(bits) > 2 and bits[2] else self.redirect_uri
        if state != want_state:
            return False, "state mismatch"
        if not self.client_id:
            return False, "client_id missing"
        status, data = exchange_code(self.client_id, ru, code, verifier)
        if status != 200 or "access_token" not in data:
            return False, data.get("error_description") or data.get("error") or f"HTTP {status}"
        self._save_token_response(data)
        self.s.set(KEY_PKCE_VERIFIER, "")
        self.s.set(KEY_NO_PREMIUM, "")          # a fresh sign-in gets a fresh try
        # Pull profile so we can display "connected as …"
        me = self._api_get("/me", _retry=False)
        if isinstance(me, dict) and me.get("id"):
            self.s.set(KEY_USER_ID,   me.get("id") or "")
            self.s.set(KEY_USER_NAME, me.get("display_name") or me.get("id") or "")
        return True, {"user_id": self.s.get(KEY_USER_ID, ""), "user_name": self.s.get(KEY_USER_NAME, "")}

    def disconnect(self):
        for k in (KEY_ACCESS_TOKEN, KEY_REFRESH_TOKEN, KEY_TOKEN_EXPIRES_AT,
                  KEY_USER_ID, KEY_USER_NAME, KEY_PKCE_VERIFIER, KEY_LAST_SYNCED_AT,
                  KEY_SCOPES, KEY_NO_PREMIUM):
            self.s.set(k, "")

    # ─── token plumbing ─────────────────────────────────────────────
    def _save_token_response(self, data):
        self.s.set(KEY_ACCESS_TOKEN, data.get("access_token") or "")
        if data.get("refresh_token"):
            self.s.set(KEY_REFRESH_TOKEN, data["refresh_token"])
        if data.get("scope"):
            self.s.set(KEY_SCOPES, data["scope"])
        expires_in = int(data.get("expires_in") or 3600)
        self.s.set(KEY_TOKEN_EXPIRES_AT, str(int(time.time()) + expires_in - 30))  # 30s safety

    def _ensure_access_token(self):
        access = (self.s.get(KEY_ACCESS_TOKEN, "") or "").strip()
        expires = int(self.s.get(KEY_TOKEN_EXPIRES_AT, "0") or 0)
        if access and time.time() < expires:
            return access
        rt = (self.s.get(KEY_REFRESH_TOKEN, "") or "").strip()
        if not rt or not self.client_id:
            return None
        status, data = refresh_token(self.client_id, rt)
        if status != 200 or "access_token" not in data:
            return None
        self._save_token_response(data)
        return self.s.get(KEY_ACCESS_TOKEN, "") or None

    def _api_get(self, path, params=None, _retry=True):
        token = self._ensure_access_token()
        if not token:
            return {"error": "not connected", "status": 401}

        # Daily soft-cap + shared-throttle gate. _acquire blocks until
        # we're allowed to call, or returns False if today's cap is hit.
        if not _acquire(self.s):
            return {
                "error": (f"Daily Spotify call cap ({SPOTIFY_DAILY_CAP}) reached. "
                          f"Resets at UTC midnight — try again tomorrow."),
                "status": 429,
                "daily_cap_reached": True,
            }

        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, data = _http_json("GET", url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        if status == 401 and _retry:
            # Force refresh and retry once
            self.s.set(KEY_TOKEN_EXPIRES_AT, "0")
            return self._api_get(path, params, _retry=False)
        if status == 429:
            # Raise the floor for the next 10 min so subsequent callers
            # (including parallel batches) automatically back off.
            _penalise()
            return {"error": "rate-limited", "retry_after": data.get("retry_after", 1), "status": 429}
        if status >= 400:
            msg = (data.get("error", {}).get("message")
                   if isinstance(data.get("error"), dict)
                   else str(data.get("error") or f"HTTP {status}"))
            return {"error": msg, "status": status}
        return data

    # ─── library reads ──────────────────────────────────────────────
    def saved_tracks(self, limit=50, offset=0):
        return self._api_get("/me/tracks", {"limit": min(50, limit), "offset": offset})

    def playlists(self, limit=50, offset=0):
        return self._api_get("/me/playlists", {"limit": min(50, limit), "offset": offset})

    def playlist_tracks(self, playlist_id, limit=100, offset=0):
        # Spotify deprecated `/playlists/{id}/tracks` in Feb 2026 — that path
        # now 403s for any app in Development Mode. The replacement is
        # `/playlists/{id}/items` with the per-row field renamed from
        # `track` to `item`. /me/tracks (saved-tracks) is unaffected — it
        # still uses the `track` field.
        return self._api_get(
            f"/playlists/{playlist_id}/items",
            {"limit": min(100, limit), "offset": offset,
             "fields": "items(added_at,item(id,name,artists(id,name),album(name,images),duration_ms,external_urls)),total,next"}
        )

    def me(self):
        return self._api_get("/me")

    # ─── the player lane: the listener's own Spotify, seen and driven ─────
    def _player_call(self, method, path, params=None, body=None, timeout=8, _retry=True):
        """One call on the player lane → (status, data). status 0 = no network,
        401 = not signed in, 429 = Spotify asked us to slow down."""
        token = self._ensure_access_token()
        if not token:
            return 401, {"error": "not connected"}
        if not _player_gate():
            return 429, {"error": "rate-limited"}
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        elif method in ("PUT", "POST"):
            data = b""                              # an empty body still needs its Content-Length
        status, out = _http_json(method, url, headers=headers, data=data, timeout=timeout)
        if status == 401 and _retry:
            self.s.set(KEY_TOKEN_EXPIRES_AT, "0")   # force a refresh, try once more
            return self._player_call(method, path, params, body, timeout, _retry=False)
        if status == 429:
            _player_backoff(out.get("_retry_after") or 5)
        return status, out

    # Spotify now and then answers 503 ("temporarily unavailable"), or drops the connection, for a moment: the same
    # search a second later works. (Seen on 2026-11-26: 6 of 20 searches failed this way, none for rate limits.)
    # A person typing a search should not have to press again, so these are tried again, shortly. Never a 429
    # (Spotify asking us to slow down: _player_call already stands down for it) and never a 4xx the person has to fix.
    TRANSIENT = (500, 502, 503, 504)

    def _player_call_retry(self, method, path, params=None, body=None, timeout=8, pauses=(0.4, 1.0), sleep=time.sleep):
        status, out = self._player_call(method, path, params, body, timeout)
        dropped = 0
        for pause in pauses:
            if status == 0:
                dropped += 1
                if dropped > 1:                     # a dropped connection waits out the timeout: try that once only
                    break
            elif status not in self.TRANSIENT:
                break
            sleep(pause)
            status, out = self._player_call(method, path, params, body, timeout)
        return status, out

    @staticmethod
    def _why(data):
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return (err.get("reason") or err.get("message") or "").strip()
        return str(err or "")

    def player_state(self):
        """What the listener's Spotify is doing → (status, data). 204 = nothing."""
        return self._player_call("GET", "/me/player", {"additional_types": "track,episode"})

    def player_devices(self):
        status, data = self._player_call("GET", "/me/player/devices")
        return (data.get("devices") or []) if status == 200 else []

    def player_play(self, uris=None, context_uri=None, position_ms=None, device_id=None):
        body = {}
        if uris:
            body["uris"] = list(uris)
            # ⚠️ Spotify quirk, found against the real desktop app 2026-11-14:
            # a bare {"uris": [x]} is answered 204 and then IGNORED — whatever
            # was already loaded just starts again from 0 (with or without
            # position_ms). Saying where in the list to start makes Spotify
            # take the list. Tried nine ways; this one is obeyed every time.
            body["offset"] = {"position": 0}
        if context_uri: body["context_uri"] = context_uri
        if position_ms: body["position_ms"] = int(position_ms)
        return self._player_call("PUT", "/me/player/play", {"device_id": device_id}, body or None)

    def player_pause(self, device_id=None):
        return self._player_call("PUT", "/me/player/pause", {"device_id": device_id})

    def player_next(self):
        return self._player_call("POST", "/me/player/next")

    def player_previous(self):
        return self._player_call("POST", "/me/player/previous")

    def player_seek(self, position_ms):
        return self._player_call("PUT", "/me/player/seek", {"position_ms": int(max(0, position_ms))})

    def player_volume(self, percent):
        return self._player_call("PUT", "/me/player/volume",
                                 {"volume_percent": int(max(0, min(100, round(percent))))})

    def search(self, q, limit=10):
        """What the listener typed, looked up on Spotify in ONE call: tracks,
        albums and artists. Their own gesture, one call per pause in typing —
        not the batch use the note below ruled out. (Since Feb 2026 a search
        returns at most 10 of each kind.) → (status, {'tracks','albums','artists'})"""
        status, data = self._player_call_retry("GET", "/search", {"q": q, "type": "track,album,artist",
                                                                  "limit": max(1, min(10, int(limit)))})
        if status != 200:
            return status, {"tracks": [], "albums": [], "artists": []}
        items = lambda k: [x for x in ((data.get(k) or {}).get("items") or []) if isinstance(x, dict) and x.get("id")]
        albums = []
        for a in items("albums"):
            arts = a.get("artists") or []
            albums.append({
                "spotify_id": a["id"], "title": a.get("name") or "",
                "artist": ", ".join(x["name"] for x in arts if x.get("name")),
                "artist_ids": ",".join(x["id"] for x in arts if x.get("id")),
                "image": _image(a.get("images"), 300), "thumb": _image(a.get("images"), 64),
                "year": (a.get("release_date") or "")[:4], "tracks": a.get("total_tracks"),
                "album_type": a.get("album_type") or "album",
                "spotify_url": (a.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/album/{a['id']}",
            })
        artists = [{
            "spotify_id": a["id"], "title": a.get("name") or "",
            "image": _image(a.get("images"), 300), "thumb": _image(a.get("images"), 64),
            "spotify_url": (a.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/artist/{a['id']}",
        } for a in items("artists")]
        return status, {"tracks": [f for f in (flatten_track(t) for t in items("tracks")) if f],
                        "albums": albums, "artists": artists}

    # NOTE: the batch search_track() was removed and stays removed: the
    # Spotify API is never used to look tracks up for DSP/artwork checks (use
    # dsp_lookup — Deezer + iTunes — for "is this on a DSP" and label/artwork).
    # search() above is a different thing: the listener's own typed search,
    # when adding to a grúpa.


# ─── normalising helpers (Spotify track JSON → flat dict) ────────────

def flatten_track(t, added_at=None):
    """Pull the fields we care about out of a Spotify track object.
    Tolerates three wrappers:
      • /me/tracks rows                  → {added_at, track:{}}
      • /playlists/{id}/items (Feb 2026) → {added_at, item:{}}
      • bare track objects                → {id, name, artists, …}
    """
    if t is None:
        return None
    if isinstance(t, dict):
        for key in ("track", "item"):  # new "item" first if both present; either works
            if isinstance(t.get(key), dict):
                return _flatten_track_inner(t[key], added_at or t.get("added_at"))
    return _flatten_track_inner(t, added_at)


def _flatten_track_inner(t, added_at):
    if not isinstance(t, dict) or not t.get("id"):
        return None
    artists = t.get("artists") or []
    artist_name = ", ".join(a.get("name") for a in artists if isinstance(a, dict) and a.get("name"))
    album       = t.get("album") or {}
    album_name  = album.get("name")
    images      = album.get("images") or []
    image_url   = (images[0].get("url") if images else None)
    return {
        "album_image_mid":   _image(images, 300) or image_url,     # what a grúpa element keeps
        "album_image_small": _image(images, 64) or image_url,      # a row in a list
        "album_year":        (album.get("release_date") or "")[:4],
        "spotify_id":      t["id"],
        "title":           t.get("name"),
        "artist":          artist_name or None,
        "artist_ids":      ",".join(a["id"] for a in artists if isinstance(a, dict) and a.get("id")),
        "album":           album_name,
        "album_image_url": image_url,
        "duration_ms":     t.get("duration_ms"),
        "added_at":        added_at,
        "spotify_url":     (t.get("external_urls") or {}).get("spotify"),
    }


def _image(images, want):
    """The smallest of Spotify's cover sizes that is at least `want` px wide
    (they come largest first: 640 · 300 · 64), else the largest there is."""
    imgs = [i for i in (images or []) if isinstance(i, dict) and i.get("url")]
    if not imgs:
        return ""
    ok = [i for i in imgs if (i.get("width") or 0) >= want]
    return (min(ok, key=lambda i: i.get("width") or 0) if ok else max(imgs, key=lambda i: i.get("width") or 0))["url"]


# ─── a tidy search: Spotify's own matching is loose ("loraine james" also
# brings "Lorraine", "Rosie"…). Keep what really holds the words typed; only
# when nothing does (a misspelling) fall back to Spotify's own guesses. ──────
_LETTERS = str.maketrans({"æ": "ae", "Æ": "ae", "ø": "o", "Ø": "o", "ß": "ss", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l", "œ": "oe", "Œ": "oe", "þ": "th", "Þ": "th", "ð": "d", "Ð": "d"})


def fold(text):
    """Lower-case, accents off, punctuation to spaces — for comparing words."""
    t = (text or "").translate(_LETTERS)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def holds(tokens, text):
    """Does `text` hold every word typed? A word counts when it starts one of
    the text's words (so "lor ja" finds Loraine James as it is typed)."""
    words = fold(text).split()
    return all(any(w.startswith(t) for w in words) for t in tokens)


def tidy_search(q, found, n_tracks=6, n_albums=4, n_artists=2):
    """Spotify's three lists → ONE ordered list for the add line. Each row:
    {'type': 'track'|'album'|'artist', …the flat fields}.
      · Only rows that really hold the words typed. When NOTHING does (a
        misspelling), Spotify's own guesses stand in, marked 'loose'.
      · Order: the artist whose name IS what was typed; then anything whose
        own name was typed in full ("untrue burial" → the track Untrue and the
        album Untrue before the album's other tracks); then tracks, albums,
        artists as Spotify ranked them."""
    tokens = [t for t in fold(q).split() if len(t) >= 2][:6]
    typed, whole = set(fold(q).split()), fold(q)
    text_of = {"tracks": lambda r: f"{r.get('title')} {r.get('artist')} {r.get('album')}",
               "albums": lambda r: f"{r.get('title')} {r.get('artist')}",
               "artists": lambda r: r.get("title")}
    strict = {k: [r for r in (found.get(k) or []) if tokens and holds(tokens, text_of[k](r))] for k in text_of}
    if any(strict.values()):
        got = strict
    else:                                           # nothing holds the words: Spotify's guesses, a few of each
        got = {"tracks": [dict(r, loose=True) for r in (found.get("tracks") or [])[:4]],
               "albums": [dict(r, loose=True) for r in (found.get("albums") or [])[:2]],
               "artists": [dict(r, loose=True) for r in (found.get("artists") or [])[:1]]}
    tracks, seen = [], set()
    for t in got["tracks"]:
        key = (fold(t.get("title")), fold(t.get("artist")))           # the same recording on a single and an album: once
        if key not in seen:
            seen.add(key)
            tracks.append(dict(t, type="track"))
    albums = [dict(a, type="album") for a in got["albums"]][:n_albums]
    artists = [dict(a, type="artist") for a in got["artists"]]
    exact = [a for a in artists if fold(a.get("title")) == whole][:1]
    rest = [a for a in artists if a not in exact][:max(0, n_artists - len(exact))]
    named = lambda r: bool(fold(r.get("title"))) and set(fold(r.get("title")).split()) <= typed    # its own name, typed in full
    body = tracks[:n_tracks] + albums
    return exact + [r for r in body if named(r)] + [r for r in body if not named(r)] + rest


# ─── "find this elsewhere" search-link helper ────────────────────────
# These are search URLs — not deep links to specific releases. Music
# Room's role is to point the user *toward* a place where they can
# actually buy the music; verifying the match is theirs to do.

def buy_links(artist, title):
    q = urllib.parse.quote_plus(f"{(artist or '').strip()} {(title or '').strip()}".strip())
    if not q:
        return []
    return [
        {"label": "Bandcamp", "url": f"https://bandcamp.com/search?q={q}"},
        {"label": "Discogs",  "url": f"https://www.discogs.com/search/?type=release&q={q}"},
        {"label": "Boomkat",  "url": f"https://boomkat.com/search?term={q}"},
        {"label": "Beatport", "url": f"https://www.beatport.com/search?q={q}"},
    ]
