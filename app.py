"""
app.py

Flask backend for the classroom attendance dashboard.

Run with:
    python app.py

Then open http://localhost:5000 in a browser. A webcam must be
connected for the live feed / recognition to work; the dashboard
still loads and shows a "camera unavailable" state without one.

Demo login: teacher / demo123  (see TEACHERS below - replace with a
real user store + hashed passwords before using this anywhere real).
"""

import os
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, Response, jsonify, request, session, send_from_directory
)

import database as db
from attendance_engine import AttendanceSystem, CLASS_OPTIONS

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("ATTENDANCE_SECRET_KEY", "dev-secret-change-me")

# NOTE: hardcoded demo credentials. Swap this for a real user table with
# hashed passwords (e.g. werkzeug.security.generate_password_hash) before
# deploying this anywhere beyond a local demo.
TEACHERS = {
    "teacher": "demo123",
}

engine = AttendanceSystem()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("teacher"):
            return jsonify({"error": "not authenticated"}), 401
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    teacher_id = (data.get("id") or "").strip()
    password = (data.get("password") or "").strip()

    if TEACHERS.get(teacher_id) == password:
        session["teacher"] = teacher_id
        return jsonify({"ok": True, "name": teacher_id})

    return jsonify({"ok": False, "error": "Invalid credentials"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("teacher", None)
    return jsonify({"ok": True})


@app.route("/api/session")
def check_session():
    return jsonify({"authenticated": bool(session.get("teacher")),
                     "name": session.get("teacher")})


# ---------------------------------------------------------------------
# Live video
# ---------------------------------------------------------------------
@app.route("/video_feed")
@login_required
def video_feed():
    if engine.camera_error:
        return jsonify({"error": engine.camera_error}), 503
    return Response(
        engine.mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/camera_status")
@login_required
def camera_status():
    return jsonify({
        "running": engine.running,
        "error": engine.camera_error,
    })


@app.route("/api/camera/stop", methods=["POST"])
@login_required
def stop_camera():
    engine.stop()
    return jsonify({"ok": True, "running": False, "error": None})


@app.route("/api/camera/start", methods=["POST"])
@login_required
def start_camera():
    if engine.running:
        return jsonify({"ok": True, "running": True, "error": None})
    engine.start(camera_device="/dev/video0")
    # Give the background thread a brief moment to open the V4L2 device.
    import time as _time
    _time.sleep(0.15)
    if not engine.running and engine.camera_error:
        return jsonify({"ok": False, "running": False, "error": engine.camera_error}), 503
    return jsonify({"ok": True, "running": engine.running, "error": engine.camera_error})


@app.route("/api/mark_attendance", methods=["POST"])
@login_required
def mark_attendance():
    """Capture the current in-memory camera frame and mark matched students.

    No image is saved. Only the attendance record is written to SQLite.
    """
    result = engine.capture_and_mark_attendance()
    status = 200 if result.get("ok") else 503
    return jsonify(result), status


@app.route("/api/classes")
@login_required
def classes():
    db.ensure_classes(CLASS_OPTIONS)
    active = engine.class_id
    items = [dict(c) for c in CLASS_OPTIONS]
    for item in items:
        item["active"] = item["id"] == active
        item["student_count"] = db.get_total_students(item["id"])
    return jsonify(items)


@app.route("/api/classes/select", methods=["POST"])
@login_required
def select_class():
    # Kept only for backward compatibility with older browser caches.
    return jsonify({"ok": False, "error": "Semester switching is disabled; only Semester 7 · IT A1 is active."}), 409


# ---------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------
@app.route("/api/stats")
@login_required
def stats():
    today = datetime.now().strftime("%d-%m-%Y")
    total = db.get_total_students(engine.class_id)
    if db.is_holiday(engine.class_id, datetime.now().date().isoformat()):
        present_count = 0
        absent_count = 0
    else:
        present = db.get_present_today(engine.class_id, today)
        present_count = len(present)
        absent_count = max(total - present_count, 0)
    return jsonify({
        "total_students": total,
        "present_today": present_count,
        "absent_today": absent_count,
        "unknown_unreviewed": db.get_unreviewed_alert_count(engine.class_id),
        "known_names": engine.known_names,
    })


@app.route("/api/subject-dates")
@login_required
def subject_dates():
    subject = (request.args.get("subject") or "").strip()
    start_iso = (request.args.get("start") or engine.semester_window()["start_iso"]).strip()
    end_iso = (request.args.get("end") or engine.semester_window()["end_iso"]).strip()
    if not subject:
        return jsonify({"error": "Subject is required"}), 400
    try:
        datetime.strptime(start_iso, "%Y-%m-%d")
        datetime.strptime(end_iso, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date range"}), 400
    if start_iso > end_iso:
        return jsonify({"error": "Start date cannot be after end date"}), 400
    return jsonify(engine.subject_sessions(subject, start_iso, end_iso, completed_only=True))


@app.route("/api/today-subjects")
@login_required
def today_subjects():
    """Return subject-wise attendance for a teacher-selected day. Defaults to today."""
    requested = (request.args.get("date") or "").strip()
    if requested:
        try:
            selected_date = datetime.strptime(requested, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    else:
        selected_date = datetime.now().date()

    window = engine.semester_window()
    if not (window["start_iso"] <= selected_date.isoformat() <= window["end_iso"]):
        return jsonify({"error": "Date is outside the active semester."}), 400
    holiday_list = db.get_holidays(engine.class_id, selected_date.isoformat(), selected_date.isoformat())
    if holiday_list:
        return jsonify({"date": selected_date.strftime("%d-%m-%Y"), "date_iso": selected_date.isoformat(), "holiday": holiday_list[0], "subjects": []})

    # Past dates are fully completed. For today, use the live clock so the
    # current/future lectures remain pending. Future dates are also pending.
    now = datetime.now()
    sessions = []
    for subject in ("AI", "SPM", "STQA", "RS", "DWM"):
        sessions.extend(engine.subject_sessions(subject, selected_date.isoformat(), selected_date.isoformat(), completed_only=False, now=now))

    groups = []
    for session_info in sessions:
        end_dt = datetime.combine(selected_date, engine._clock(session_info["lecture_end"]))
        start_dt = datetime.combine(selected_date, engine._clock(session_info["lecture_start"]))
        if selected_date < now.date():
            completed = True
        elif selected_date > now.date():
            completed = False
        else:
            completed = end_dt <= now

        students = db.get_subject_date_attendance(
            engine.class_id,
            session_info["date"],
            session_info["subject"],
            session_info["period_no"],
        )
        for row in students:
            if not row.get("status"):
                row["status"] = "absent" if completed else "pending"

        groups.append({
            "date": session_info["date"],
            "date_iso": session_info["date_iso"],
            "weekday": session_info["weekday"],
            "period_no": session_info["period_no"],
            "lecture_start": session_info["lecture_start"],
            "lecture_end": session_info["lecture_end"],
            "subject": session_info["subject"],
            "completed": completed,
            "students": students,
        })

    groups.sort(key=lambda x: (x["period_no"], x["subject"]))
    return jsonify({
        "date": selected_date.strftime("%d-%m-%Y"),
        "date_iso": selected_date.isoformat(),
        "subjects": groups,
    })


@app.route("/api/subject-date")
@login_required
def subject_date():
    subject = (request.args.get("subject") or "").strip()
    date_iso = (request.args.get("date") or "").strip()
    try:
        requested = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    sessions = [x for x in engine.subject_sessions(subject, date_iso, date_iso, completed_only=True) if x["date_iso"] == date_iso]
    if not sessions:
        return jsonify({"error": "No completed lecture scheduled for this subject/date."}), 404
    session_info = sessions[0]
    students = db.get_subject_date_attendance(engine.class_id, session_info["date"], session_info["subject"], session_info["period_no"])
    for row in students:
        row["date_iso"] = date_iso
        row["date"] = session_info["date"]
        row["weekday"] = session_info["weekday"]
        row["period_no"] = session_info["period_no"]
        row["lecture_no"] = session_info["lecture_no"]
        row["lecture_start"] = session_info["lecture_start"]
        row["lecture_end"] = session_info["lecture_end"]
        row["subject"] = session_info["subject"]
        if row.get("status") not in {"present", "late", "absent"}:
            row["status"] = "absent"
    return jsonify({
        "session": session_info,
        "students": students,
    })


@app.route("/api/records")
@login_required
def records():
    """Return attendance records, optionally filtered by a teacher-selected date range."""
    start_iso = (request.args.get("start") or "").strip() or None
    end_iso = (request.args.get("end") or "").strip() or None
    status = (request.args.get("status") or "all").strip().lower()
    subject = (request.args.get("subject") or "").strip() or None

    # HTML date inputs already use ISO format; validate them before querying.
    for value in (start_iso, end_iso):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400

    if start_iso and end_iso and start_iso > end_iso:
        return jsonify({"error": "Start date cannot be after end date."}), 400

    return jsonify(db.get_records(
        engine.class_id,
        limit=5000,
        start_iso=start_iso,
        end_iso=end_iso,
        status=status,
        subject=subject,
    ))


@app.route("/api/timetable")
@login_required
def timetable():
    return jsonify(engine.timetable_state())


@app.route("/api/holidays", methods=["GET", "POST"])
@login_required
def holidays():
    if request.method == "GET":
        window = engine.semester_window()
        return jsonify(db.get_holidays(engine.class_id, window["start_iso"], window["end_iso"]))

    data = request.get_json(silent=True) or {}
    date_iso = (data.get("date") or "").strip()
    reason = (data.get("reason") or "").strip()
    if not date_iso:
        return jsonify({"error": "Holiday date is required."}), 400
    try:
        datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid holiday date. Use YYYY-MM-DD."}), 400
    window = engine.semester_window()
    if not (window["start_iso"] <= date_iso <= window["end_iso"]):
        return jsonify({"error": "Holiday date must be inside the active semester."}), 400
    db.add_holiday(engine.class_id, date_iso, reason)
    return jsonify({"ok": True, "holidays": db.get_holidays(engine.class_id, window["start_iso"], window["end_iso"])})


@app.route("/api/holidays/<date_iso>", methods=["DELETE"])
@login_required
def remove_holiday(date_iso):
    try:
        datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid holiday date."}), 400
    return jsonify({"ok": db.delete_holiday(engine.class_id, date_iso)})


@app.route("/api/student-summary")
@login_required
def student_summary():
    engine.finalize_past_lectures()
    window = engine.semester_window()
    return jsonify(db.get_semester_student_summary(engine.class_id, window["start_iso"], window["end_iso"]))


@app.route("/api/lecture-summary")
@login_required
def lecture_summary():
    """Return lecture-wise attendance for the selected date/range.

    The UI's lecture-date filter sends ?date=YYYY-MM-DD. When no date is
    supplied, the active semester range is returned for compatibility.
    """
    engine.finalize_past_lectures()
    window = engine.semester_window()
    selected = (request.args.get("date") or "").strip()
    start_iso = (request.args.get("start") or "").strip()
    end_iso = (request.args.get("end") or "").strip()

    if selected:
        try:
            datetime.strptime(selected, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
        start_iso = end_iso = selected
    else:
        start_iso = start_iso or window["start_iso"]
        end_iso = end_iso or window["end_iso"]

    if start_iso > end_iso:
        return jsonify({"error": "Start date cannot be after end date."}), 400
    if start_iso < window["start_iso"] or end_iso > window["end_iso"]:
        return jsonify({"error": "Date range is outside the active semester."}), 400

    return jsonify(db.get_lecture_summary(engine.class_id, start_iso, end_iso))


@app.route("/api/subject-summary")
@login_required
def subject_summary():
    engine.finalize_past_lectures()
    window = engine.semester_window()
    return jsonify(db.get_semester_subject_summary(engine.class_id, window["start_iso"], window["end_iso"]))


@app.route("/api/student-detail")
@login_required
def student_detail():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Student name is required."}), 400
    start_iso = (request.args.get("start") or engine.semester_window()["start_iso"]).strip()
    end_iso = (request.args.get("end") or engine.semester_window()["end_iso"]).strip()
    try:
        datetime.strptime(start_iso, "%Y-%m-%d")
        datetime.strptime(end_iso, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date range."}), 400
    if start_iso > end_iso:
        return jsonify({"error": "Start date cannot be after end date."}), 400
    names = db.get_student_names(engine.class_id)
    # Match case-insensitively, but return the canonical roster spelling.
    canonical = next((n for n in names if n.casefold() == name.casefold()), None)
    if canonical is None:
        return jsonify({"error": "Student not found."}), 404
    return jsonify({
        "student": canonical,
        "start": start_iso,
        "end": end_iso,
        "subjects": db.get_student_subject_summary(engine.class_id, canonical, start_iso, end_iso),
        "records": db.get_student_attendance_detail(engine.class_id, canonical, start_iso, end_iso),
    })


@app.route("/api/semester-summary")
@login_required
def semester_summary():
    engine.finalize_past_lectures()
    window = engine.semester_window()
    return jsonify({
        "semester": window,
        "students": db.get_semester_student_summary(engine.class_id, window["start_iso"], window["end_iso"]),
        "subjects": db.get_semester_subject_summary(engine.class_id, window["start_iso"], window["end_iso"]),
        "overview": db.get_semester_overview(engine.class_id, window["start_iso"], window["end_iso"], datetime.now().date().isoformat())
    })


@app.route("/api/weekly")
@login_required
def weekly():
    counts = db.get_weekly_counts(engine.class_id, last_n_days=7)
    # Build the last 7 calendar days (oldest -> newest) so the chart
    # always has 7 points even on days with zero attendance.
    days = []
    for i in range(6, -1, -1):
        d = datetime.now() - timedelta(days=i)
        key = d.strftime("%d-%m-%Y")
        days.append({
            "label": d.strftime("%a"),
            "date": key,
            "count": counts.get(key, 0),
        })
    return jsonify(days)


@app.route("/api/alerts")
@login_required
def alerts():
    return jsonify(db.get_alerts(engine.class_id))


@app.route("/api/alerts/<int:alert_id>/review", methods=["POST"])
@login_required
def review_alert(alert_id):
    db.mark_alert_reviewed(alert_id, engine.class_id)
    return jsonify({"ok": True})


@app.route("/unknown_faces/<path:filename>")
@login_required
def unknown_face_image(filename):
    return send_from_directory("unknown_faces", filename)


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@login_required
def delete_alert(alert_id):
    """Delete an unknown-face alert and its stored image from disk."""
    filename = db.delete_alert(alert_id, engine.class_id)
    if filename is None:
        return jsonify({"ok": False, "error": "Alert not found"}), 404

    # Database rows may come from older project versions with a prefixed path.
    safe_name = os.path.basename(filename)
    image_path = os.path.join("unknown_faces", safe_name)
    try:
        if os.path.isfile(image_path):
            os.remove(image_path)
    except OSError as exc:
        # Keep the database deletion successful, but tell the UI the file cleanup failed.
        return jsonify({"ok": True, "warning": f"Alert deleted, but image cleanup failed: {exc}"})

    return jsonify({"ok": True})


@app.route("/api/alerts/bulk", methods=["DELETE"])
@login_required
def delete_alerts_bulk():
    """Delete selected or all unknown-face alerts and their stored images."""
    data = request.get_json(silent=True) or {}
    delete_all = bool(data.get("all"))
    alert_ids = data.get("ids", [])

    filenames = db.delete_alerts(engine.class_id, alert_ids=alert_ids, delete_all=delete_all)
    warnings = []
    for filename in filenames:
        safe_name = os.path.basename(filename)
        image_path = os.path.join("unknown_faces", safe_name)
        try:
            if os.path.isfile(image_path):
                os.remove(image_path)
        except OSError as exc:
            warnings.append(f"{safe_name}: {exc}")

    return jsonify({
        "ok": True,
        "deleted": len(filenames),
        "warnings": warnings,
    })


# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------
def bootstrap():
    db.init_db()
    db.ensure_classes(CLASS_OPTIONS)
    engine.load_known_faces()
    engine.start(camera_device="/dev/video0")


if __name__ == "__main__":
    bootstrap()
    app.run(debug=True, use_reloader=False, threaded=True, port=5000)
