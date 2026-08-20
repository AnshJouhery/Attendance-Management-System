# ATTEND//OS — Classroom Attendance System

A face-recognition attendance system with a live web dashboard: teacher
login, live camera feed, per-student attendance, a weekly trend chart,
and unknown-face alerts.

This is a rebuild of the original `attendence.py` script, split into a
Flask backend + browser dashboard instead of a `cv2.imshow` window.

## What changed vs. the original script

| Issue in the original | Fix here |
|---|---|
| CSV parsed by hand, no locking | SQLite (`database.py`) with proper tables and `UNIQUE` constraints |
| `argmin` accepted the "least bad" match even if it was far away | Explicit `MATCH_TOLERANCE` distance threshold in `attendance_engine.py` |
| One unknown-face image saved almost every frame a stranger was in view | Cooldown + encoding-similarity de-duplication (`UNKNOWN_DEDUP_SECONDS`) |
| `unknown_faces/` assumed to exist | Created automatically on startup |
| Only the first face in one reference photo per person | `images/<Name>/` folders with multiple photos are averaged |
| Blocking `while True` + `cv2.imshow` loop, can't serve a web page | Camera runs in a background thread; Flask routes just read shared state |
| No camera-open check | `cap.isOpened()` checked, surfaced to the dashboard as "camera unavailable" |
| No teacher login / access control | Flask session-based login (see note on credentials below) |

Not fixed (out of scope, worth knowing about): there's still no liveness
/ anti-spoofing check, so a photo or video of someone can be matched.
Face encodings also aren't encrypted at rest — treat `attendance.db`
and `images/` as sensitive biometric data.

## Project structure

```
attendance_project/
├── app.py                 # Flask app: routes, login, API
├── attendance_engine.py   # camera capture + recognition (background thread)
├── database.py             # SQLite schema + queries
├── requirements.txt
├── static/
│   └── dashboard.html      # the whole frontend (single file)
├── images/                 # put known faces here (see below)
└── unknown_faces/          # unrecognized-face snapshots land here
```

## 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`face_recognition` depends on `dlib`, which needs CMake and a C++
compiler to build. On Windows, installing the prebuilt wheel or using
WSL is usually easiest; on macOS, `brew install cmake` first; on
Ubuntu/Debian, `sudo apt install cmake build-essential`.

## 2. Add known faces

Either:

- **One photo per person:** `images/Aarav Sharma.jpg` — the filename
  (minus extension) becomes the student's name.
- **Multiple photos per person (recommended, more accurate):**
  ```
  images/
    Aarav Sharma/
      photo1.jpg
      photo2.jpg
    Priya Nair/
      photo1.jpg
  ```
  All photos in a folder are encoded and averaged into one reference.

Use clear, front-facing, well-lit photos — recognition quality depends
entirely on these.

## 3. Run it

```bash
python app.py
```

Open **http://localhost:5000**. Log in with:

- Teacher ID: `teacher`
- Password: `demo123`

(Change these in the `TEACHERS` dict in `app.py`. For anything beyond
a local demo, replace it with a real user table and hashed passwords
— `werkzeug.security.generate_password_hash` — rather than plaintext.)

If no webcam is available, the dashboard still loads; the camera panel
shows "camera unavailable" and everything else (records, stats,
alerts) still works against whatever is already in the database.

## 4. Using the dashboard

- **Dashboard** — live feed, today's present/absent counts, unknown-face
  count, a recognition log, a 7-day attendance chart, and a preview of
  recent unknown-face alerts.
- **Attendance Records** — full searchable/filterable log of every
  attendance entry (present vs. late).
- **Unknown Alerts** — every unidentified-face snapshot, with a "mark
  reviewed" action.

Everything on the dashboard is read from the database and refreshed
every 3 seconds — it's not a simulation, it reflects what the camera
thread actually recognized.

## Notes on running this for a real class

- `app.run(debug=True)` is a local dev server — don't expose it to the
  internet as-is. Run it behind a real WSGI server (gunicorn/waitress)
  and HTTPS if it needs to be reachable beyond one machine.
- `MATCH_TOLERANCE` and `UNKNOWN_DEDUP_SECONDS` in
  `attendance_engine.py` are tunable — lower `MATCH_TOLERANCE` (e.g.
  0.45) for stricter matching if you're seeing false positives.
