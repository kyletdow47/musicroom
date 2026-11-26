"""Proves SAVED ARTICLES join the research substrate as reference notes:
an Explore article/video saved via POST /api/listen/reference becomes a
listening_note with kind='reference' and no track — and threads, projects,
note_links and custom fields all work on it unchanged (the polyrhythm-video
→ polyrhythmic-techno-moments connection). Also proves the v53 table
rebuild keeps child FKs pointing at listening_notes (the rename trap).

Run:  venv/bin/python test_reference_notes.py
"""
import os, sys, sqlite3, tempfile, subprocess

PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mr_refs_")
os.environ["MUSICROOM_DATA"] = TMP
sys.path.insert(0, PROJ); os.chdir(PROJ)
PY = sys.executable
fails = []
def chk(c, m): print(("  PASS  " if c else "  FAIL  ") + m); (None if c else fails.append(m))

subprocess.run([PY, "schema.py"], env=os.environ, capture_output=True)
subprocess.run([PY, "migrate.py"], env=os.environ, capture_output=True)
DB = os.path.join(TMP, "library.db")

# the v53 rebuild must leave every child table referencing listening_notes,
# never the temp rebuild name
db = sqlite3.connect(DB)
bad = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%listening_notes_new%'")]
chk(not bad, f"v53 rebuild leaves no dangling child references ({bad or 'clean'})")
chk(db.execute("PRAGMA foreign_key_check").fetchall() == [], "foreign_key_check clean after rebuild")

# seed one real track + one real listening moment beside the article
db.execute("INSERT INTO tracks(id,title,artist_name,year,is_deleted) VALUES(1,'Spastik','Plastikman',1993,0)")
db.execute("INSERT INTO files(id,track_id,file_path,is_primary,is_deleted,duration_sec) VALUES(1,1,'/s.mp3',1,0,300)")
db.execute("INSERT INTO listening_sessions(id,track_id,started_at) VALUES(1,1,datetime('now'))")
db.execute("INSERT INTO listening_notes(id,session_id,track_id,timestamp_sec,note_text) VALUES(1,1,1,61,'three over four in the hats')")
db.commit(); db.close()

import app
c = app.app.test_client()

# save an article (the YouTube video that sparked the listening)
r = c.post("/api/listen/reference", json={
    "url": "https://youtube.com/watch?v=poly123",
    "title": "Polyrhythms in techno, explained",
    "source": "YouTube",
    "image_url": "https://img.youtube.com/poly123.jpg",
    "summary": "Why 3:2 grooves pull a dancefloor sideways.",
}).get_json()
ref_id = r.get("id")
chk(r.get("created") and ref_id and r.get("kind") == "reference", "article saved as a reference note")
chk(r.get("track_id") is None and r.get("session_id") is None, "reference carries no track/session")

# idempotent: saving the same URL again returns the same note
r2 = c.post("/api/listen/reference", json={
    "url": "https://youtube.com/watch?v=poly123", "title": "dup"}).get_json()
chk(r2.get("created") is False and r2.get("id") == ref_id, "second save of same URL returns existing note")

# it appears in the notes browser with its ref fields, beside the moment
notes = c.get("/api/listen/notes").get_json()
ref = next((n for n in notes if n["id"] == ref_id), None)
chk(ref and ref["ref_title"] == "Polyrhythms in techno, explained"
        and ref["ref_source"] == "YouTube" and ref["kind"] == "reference",
    "notes API carries the reference (LEFT JOIN survives missing track)")
chk(any(n["id"] == 1 and n["kind"] == "moment" for n in notes), "real moments unharmed beside it")

# it can START a thread, and the listening moment joins it
t = c.post("/api/threads", json={"name": "polyrhythmic techno"}).get_json()
tid = t["id"]
c.post(f"/api/thread/{tid}/moments", json={"note_id": ref_id})
c.post(f"/api/thread/{tid}/moments", json={"note_id": 1})
th = c.get(f"/api/thread/{tid}").get_json()
kinds = [m["kind"] for m in th["moments"]]
chk(len(th["moments"]) == 2 and "reference" in kinds and "moment" in kinds,
    "thread holds the article AND the listening moment")

# …and a LINK records WHY they're connected
l = c.post("/api/note-links", json={"from_note_id": ref_id, "to_note_id": 1,
                                    "label": "sparked this listening"}).get_json()
chk(l.get("ok") or l.get("id"), "article linked to the moment it sparked")

# it can join a project (or arrive already inside one)
p = c.post("/api/listen/projects", json={"name": "polyrhythm research"}).get_json()
pid = p["id"]
r3 = c.post("/api/listen/reference", json={
    "url": "https://example.com/burundi", "title": "Burundi drumming primer",
    "project_id": pid, "thread_id": tid}).get_json()
proj_notes = c.get(f"/api/listen/notes?project_id={pid}").get_json()
chk(any(n["id"] == r3["id"] for n in proj_notes), "save can land straight inside a project")
th2 = c.get(f"/api/thread/{tid}").get_json()
chk(len(th2["moments"]) == 3, "save can land straight onto a thread")

# custom fields work on references like any atom
pr = c.post(f"/api/note/{ref_id}/property", json={"name": "medium", "value": "video"}).get_json()
chk(pr.get("ok"), "custom field attaches to a reference")

# thread → set ignores the article and builds from real tracks only
s = c.post(f"/api/thread/{tid}/to-set").get_json()
chk(s.get("set_id") is not None, "thread→set still works with references aboard")


# a video saved AT A MOMENT: the time is kept, each moment of one video is its own note, and bad times are ignored
vid = "https://youtu.be/abcdefghijk"
m1 = c.post("/api/listen/reference", json={"url": vid + "?t=559", "title": "A talk", "source": "YouTube",
                                           "summary": "the line about borrowing", "timestamp_sec": 559}).get_json()
m2 = c.post("/api/listen/reference", json={"url": vid + "?t=1200", "title": "A talk", "timestamp_sec": 1200.5}).get_json()
m1b = c.post("/api/listen/reference", json={"url": vid + "?t=559", "title": "A talk", "timestamp_sec": 5}).get_json()
chk(m1["created"] and m2["created"] and m1["id"] != m2["id"], "two moments of one video are two notes")
chk(m1["timestamp_sec"] == 559 and m2["timestamp_sec"] == 1200.5, "the time in the video is kept on the note")
chk(m1["note_text"] == "the line about borrowing" and m1["ref_url"] == vid + "?t=559", "the person's own note and the timed link are kept")
chk(m1b["created"] is False and m1b["id"] == m1["id"] and m1b["timestamp_sec"] == 559, "saving the same moment again returns it and changes nothing")
for label, val in (("negative", -5), ("text", "559"), ("true", True), ("huge", 10**9), ("none", None)):
    mm = c.post("/api/listen/reference", json={"url": f"https://youtu.be/zzzzzzzzzzz?x={label}", "title": "T", "timestamp_sec": val}).get_json()
    chk(mm["created"] and mm["timestamp_sec"] == 0, f"a {label} time is ignored, not stored")
old = c.post("/api/listen/reference", json={"url": "https://example.com/no-time", "title": "No time"}).get_json()
chk(old["timestamp_sec"] == 0, "an article saved the old way still has no time")

# validation
bad = c.post("/api/listen/reference", json={"title": "no url"})
chk(bad.status_code == 400, "missing url rejected")

print("")
print("  ALL PASS" if not fails else f"  {len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
