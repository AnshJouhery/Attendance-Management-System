"""Camera capture and on-demand face-recognition engine."""

import os
import time
import threading
from datetime import datetime, timedelta, date

import cv2
import numpy as np
import face_recognition

import database as db

IMAGES_PATH = "images"
UNKNOWN_FACES_PATH = "unknown_faces"
MATCH_TOLERANCE = 0.50

# Seven 55-minute periods, Monday-Friday. Four subjects have exactly
# three lectures each week; remaining lecture slots are intentionally free.
PERIODS = [
    {"period": 1, "start": "09:15", "end": "10:10"},
    {"period": 2, "start": "10:10", "end": "11:05"},
    {"period": 3, "start": "11:05", "end": "12:00"},
    {"period": 4, "start": "12:00", "end": "12:55"},
    {"period": 5, "start": "13:50", "end": "14:45"},
    {"period": 6, "start": "14:45", "end": "15:40"},
    {"period": 7, "start": "15:40", "end": "16:35"},
]

LUNCH = {"label": "Lunch", "start": "12:55", "end": "13:50"}
SUBJECTS = ["AI", "SPM", "STQA", "RS", "DWM"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

CLASS_OPTIONS = [
    {"id": "SEM7_IT_A1", "name": "Semester 7 · IT A1", "semester": 7, "section": "A1", "start_date": "2026-08-01", "end_date": "2026-12-01"},
]


# Student photos are isolated by class/semester. The old top-level images/
# directory is used only as a compatibility fallback for Semester 7 · IT A1.
CLASS_IMAGE_DIRS = {
    "SEM7_IT_A1": IMAGES_PATH,
}


SEMESTER_START = date(2026, 8, 1)
SEMESTER_END = date(2026, 12, 1)

# Each subject appears exactly 3 times in the week.
WEEKLY_SUBJECTS = {
    "Monday":    {1: "AI",   3: "SPM", 5: "STQA"},
    "Tuesday":   {2: "DWM",  4: "AI",  6: "RS"},
    "Wednesday": {1: "SPM",  4: "STQA", 7: "DWM"},
    "Thursday":  {2: "RS",   5: "AI",  7: "SPM"},
    "Friday":    {1: "STQA", 4: "DWM",  6: "RS"},
}


class AttendanceSystem:
    def __init__(self):
        self.known_encodings = []
        self.known_names = []
        self.lock = threading.Lock()
        self.latest_jpeg = None
        self.latest_frame = None
        self.running = False
        self.camera_error = None
        self._thread = None
        self._capture_lock = threading.Lock()
        self._last_finalize_key = None
        self.class_id = "SEM7_IT_A1"

    # ---------------------------------------------------------------
    # Setup
    # ---------------------------------------------------------------
    def _class_image_root(self, class_id):
        # This version intentionally supports only Semester 7 · IT A1.
        # The existing images/<student>/ roster is the single source of truth.
        os.makedirs(IMAGES_PATH, exist_ok=True)
        return IMAGES_PATH

    def load_known_faces(self, class_id=None):
        class_id = class_id or self.class_id
        root = self._class_image_root(class_id)
        os.makedirs(UNKNOWN_FACES_PATH, exist_ok=True)

        encodings = []
        names = []
        for entry in sorted(os.listdir(root)):
            # Skip other class-specific roster folders if the legacy root is used.
            if entry in CLASS_IMAGE_DIRS or False:
                continue
            full_path = os.path.join(root, entry)
            if os.path.isdir(full_path):
                person_encodings = []
                for fname in sorted(os.listdir(full_path)):
                    enc = self._encode_file(os.path.join(full_path, fname))
                    if enc is not None:
                        person_encodings.append(enc)
                if person_encodings:
                    person_encoding = np.mean(person_encodings, axis=0)
                    encodings.append(person_encoding)
                    names.append(db._canonical_name(entry) if hasattr(db, "_canonical_name") else entry.strip().title())
            elif os.path.isfile(full_path):
                enc = self._encode_file(full_path)
                if enc is not None:
                    encodings.append(enc)
                    names.append(db._canonical_name(os.path.splitext(entry)[0]) if hasattr(db, "_canonical_name") else os.path.splitext(entry)[0].strip().title())

        self.known_encodings = encodings
        self.known_names = names
        db.sync_students(class_id, names)
        return names

    @staticmethod
    def _encode_file(path):
        img = cv2.imread(path)
        if img is None:
            return None
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        found = face_recognition.face_encodings(img)
        return found[0] if found else None

    # ---------------------------------------------------------------
    # Background camera loop
    # ---------------------------------------------------------------
    def start(self, camera_device="/dev/video0"):
        if self.running:
            return
        self.running = True
        self.camera_error = None
        self._thread = threading.Thread(target=self._run, args=(camera_device,), daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _run(self, camera_device):
        cap = cv2.VideoCapture(camera_device, cv2.CAP_V4L2) if isinstance(camera_device, str) and camera_device.startswith("/dev/video") else cv2.VideoCapture(camera_device)
        if not cap.isOpened():
            self.camera_error = f"Could not open camera {camera_device}. Check that the webcam is connected and not in use by another application."
            self.running = False
            return

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        while self.running:
            success, img = cap.read()
            if not success:
                time.sleep(0.05)
                continue
            with self.lock:
                self.latest_frame = img.copy()
            ok, buf = cv2.imencode(".jpg", img)
            if ok:
                with self.lock:
                    self.latest_jpeg = buf.tobytes()
        cap.release()

    # ---------------------------------------------------------------
    # Timetable / attendance windows
    # ---------------------------------------------------------------
    @staticmethod
    def _clock(value):
        return datetime.strptime(value, "%H:%M").time()

    def _day_schedule(self, day_name):
        assignments = WEEKLY_SUBJECTS.get(day_name, {})
        result = []
        for p in PERIODS:
            subject = assignments.get(p["period"])
            result.append({
                "period": p["period"],
                "lecture_no": p["period"],
                "start": p["start"],
                "end": p["end"],
                "subject": subject,
                "label": subject if subject else "Free Lecture",
                "kind": "lecture" if subject else "free",
            })
        result.insert(4, {"period": None, "lecture_no": None, "start": LUNCH["start"], "end": LUNCH["end"], "subject": None, "label": "Lunch", "kind": "lunch"})
        return result

    def weekly_timetable(self):
        return {day: self._day_schedule(day) for day in DAY_NAMES}

    def subject_sessions(self, subject, start_iso, end_iso, completed_only=True, now=None):
        """Return scheduled sessions for a subject in a date range."""
        subject_key = str(subject or "").strip().upper()
        start = date.fromisoformat(start_iso)
        end = date.fromisoformat(end_iso)
        now = now or datetime.now()
        rows = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5 and not db.is_holiday(self.class_id, cursor.isoformat()):
                day_name = DAY_NAMES[cursor.weekday()]
                for slot in self._day_schedule(day_name):
                    if slot["kind"] != "lecture" or str(slot.get("subject") or "").strip().upper() != subject_key:
                        continue
                    end_dt = datetime.combine(cursor, self._clock(slot["end"]))
                    if completed_only and end_dt > now:
                        continue
                    rows.append({
                        "date": cursor.strftime("%d-%m-%Y"),
                        "date_iso": cursor.isoformat(),
                        "weekday": day_name,
                        "period_no": slot["period"],
                        "lecture_no": slot["lecture_no"],
                        "subject": slot["subject"],
                        "lecture_start": slot["start"],
                        "lecture_end": slot["end"],
                    })
            cursor += timedelta(days=1)
        return rows

    def semester_window(self):
        selected = next((c for c in CLASS_OPTIONS if c["id"] == self.class_id), CLASS_OPTIONS[0])
        start = date.fromisoformat(selected["start_date"])
        end = date.fromisoformat(selected["end_date"])
        return {
            "name": selected["name"],
            "semester": selected["semester"],
            "section": selected["section"],
            "class_id": selected["id"],
            "start": start.strftime("%d-%m-%Y"),
            "end": end.strftime("%d-%m-%Y"),
            "start_iso": start.isoformat(),
            "end_iso": end.isoformat(),
        }

    def finalize_past_lectures(self, now=None):
        """Backfill ABSENT rows for every completed scheduled lecture in the semester."""
        now = now or datetime.now()
        finalize_key = (now.date().isoformat(), now.hour, now.minute)
        if finalize_key == self._last_finalize_key:
            return 0
        self._last_finalize_key = finalize_key
        win = self.semester_window()
        sem_start = date.fromisoformat(win["start_iso"])
        sem_end = date.fromisoformat(win["end_iso"])
        today = now.date()
        if today < sem_start:
            return 0
        through = min(today, sem_end)
        cursor = sem_start
        created = 0
        while cursor <= through:
            if cursor.weekday() < 5:
                day_name = DAY_NAMES[cursor.weekday()]
                for slot in self._day_schedule(day_name):
                    if slot["kind"] != "lecture":
                        continue
                    end_t = self._clock(slot["end"])
                    end_dt = datetime.combine(cursor, end_t)
                    if end_dt <= now:
                        created += db.finalize_lecture_absences(
                            self.class_id,
                            cursor.strftime("%d-%m-%Y"),
                            day_name,
                            slot["period"],
                            slot["subject"],
                            slot["start"],
                            slot["end"],
                        )
            cursor += timedelta(days=1)
        return created

    def timetable_state(self, now=None):
        now = now or datetime.now()
        window = self.semester_window()
        self.finalize_past_lectures(now)
        day_name = DAY_NAMES[now.weekday()] if now.weekday() < len(DAY_NAMES) else "Weekend"
        current = None
        phase = "closed"
        if not (date.fromisoformat(window["start_iso"]) <= now.date() <= date.fromisoformat(window["end_iso"])):
            return {
                "class_id": self.class_id,
                "class_name": window["name"],
                "semester_number": window["semester"],
                "now": now.strftime("%H:%M:%S"),
                "date": now.strftime("%d-%m-%Y"),
                "day": DAY_NAMES[now.weekday()] if now.weekday() < len(DAY_NAMES) else "Weekend",
                "phase": "closed",
                "message": f"Semester is closed. {window['start']} → {window['end']}.",
                "current": None, "next_lecture": None, "lunch": dict(LUNCH),
                "today_schedule": [], "weekly_schedule": self.weekly_timetable(),
                "subjects": SUBJECTS, "semester": window,
            }
        if db.is_holiday(self.class_id, now.date().isoformat()):
            holiday = next((h for h in db.get_holidays(self.class_id, now.date().isoformat(), now.date().isoformat()) if h["date"] == now.date().isoformat()), None)
            reason = (holiday or {}).get("reason") or "Declared holiday"
            return {
                "class_id": self.class_id,
                "class_name": window["name"],
                "semester_number": window["semester"],
                "now": now.strftime("%H:%M:%S"),
                "date": now.strftime("%d-%m-%Y"),
                "day": day_name,
                "phase": "holiday",
                "message": f"Holiday: {reason}",
                "current": None, "next_lecture": None, "lunch": dict(LUNCH),
                "today_schedule": [], "weekly_schedule": self.weekly_timetable(),
                "subjects": SUBJECTS, "semester": window, "holiday": holiday,
            }

        message = "No classes scheduled for today."
        next_lecture = None
        today_schedule = self._day_schedule(day_name) if day_name in DAY_NAMES else []

        for slot in today_schedule:
            if slot["kind"] != "lecture":
                if slot["kind"] == "free":
                    start = self._clock(slot["start"])
                    end = self._clock(slot["end"])
                    start_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
                    end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
                    if start_dt <= now < end_dt:
                        phase = "free"
                        message = f"Period {slot['period']} is a free lecture. Attendance is not scheduled."
                        current = dict(slot)
                        break
                elif slot["kind"] == "lunch":
                    start = self._clock(slot["start"]); end = self._clock(slot["end"])
                    start_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
                    end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
                    if start_dt <= now < end_dt:
                        phase = "lunch"; message = "Lunch break. Attendance is closed."; current = dict(slot); break
                continue

            start = self._clock(slot["start"])
            end = self._clock(slot["end"])
            start_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            late_dt = start_dt + timedelta(minutes=10)
            if start_dt <= now < end_dt:
                current = dict(slot)
                current["late_start"] = late_dt.strftime("%H:%M")
                current["present_window_end"] = late_dt.strftime("%H:%M")
                if now < late_dt:
                    phase = "present"
                    message = f"{slot['subject']}: Present window (first 10 minutes)."
                else:
                    phase = "late"
                    message = f"{slot['subject']}: Late window until {slot['end']}."
                break
            if now < start_dt and slot["subject"] and next_lecture is None:
                next_lecture = dict(slot)

        if phase == "closed" and day_name in DAY_NAMES:
            message = "No active lecture right now."

        return {
            "class_id": self.class_id,
            "class_name": self.semester_window()["name"],
            "semester_number": self.semester_window()["semester"],
            "now": now.strftime("%H:%M:%S"),
            "date": now.strftime("%d-%m-%Y"),
            "day": day_name,
            "phase": phase,
            "message": message,
            "current": current,
            "next_lecture": next_lecture,
            "lunch": dict(LUNCH),
            "today_schedule": today_schedule,
            "weekly_schedule": self.weekly_timetable(),
            "subjects": SUBJECTS,
            "semester": self.semester_window(),
        }


    def get_classes(self):
        return CLASS_OPTIONS

    def set_class(self, class_id):
        # Semester switching is intentionally disabled for now.
        return class_id == "SEM7_IT_A1"

    # ---------------------------------------------------------------
    # On-demand attendance capture
    # ---------------------------------------------------------------
    def capture_and_mark_attendance(self):
        state = self.timetable_state()
        if state["phase"] not in ("present", "late") or not state.get("current", {}).get("subject"):
            return {"ok": False, "error": state["message"], "phase": state["phase"], "timetable": state}

        slot = state["current"]
        with self._capture_lock:
            with self.lock:
                if self.latest_frame is None:
                    return {"ok": False, "error": "No camera frame is available yet."}
                img = self.latest_frame.copy()

            img_small = cv2.resize(img, (0, 0), None, 0.25, 0.25)
            img_small = cv2.cvtColor(img_small, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(img_small)
            face_encodings = face_recognition.face_encodings(img_small, face_locations)

            marked, already_marked = [], []
            unknown_faces = 0
            seen_names = set()
            stored_unknowns = []
            now = datetime.now()
            date_str = now.strftime("%d-%m-%Y")
            time_str = now.strftime("%H:%M:%S")
            status = "present" if state["phase"] == "present" else "late"

            for idx, (encoding, location) in enumerate(zip(face_encodings, face_locations), start=1):
                name = self._match(encoding)
                if name is None:
                    unknown_faces += 1
                    saved = self._store_unknown_face(img, location, idx)
                    if saved:
                        stored_unknowns.append(saved)
                    continue
                if name in seen_names:
                    continue
                seen_names.add(name)
                inserted = db.mark_attendance(
                    self.class_id, name, date_str, time_str, status,
                    slot["period"], slot["subject"], slot["start"], slot["end"],
                    state["day"], slot["period"],
                )
                (marked if inserted else already_marked).append(name)

            return {
                "ok": True,
                "faces_found": len(face_encodings),
                "marked": marked,
                "already_marked": already_marked,
                "unknown_faces": unknown_faces,
                "stored_unknowns": stored_unknowns,
                "subject": slot["subject"],
                "lecture": f"Period {slot['period']}",
                "lecture_no": slot["period"],
                "lecture_start": slot["start"],
                "lecture_end": slot["end"],
                "status": status,
            }

    def _store_unknown_face(self, img, location, index):
        try:
            top, right, bottom, left = location
            scale = 4
            top, right, bottom, left = [int(v * scale) for v in (top, right, bottom, left)]
            h, w = img.shape[:2]
            pad_x = max(int((right - left) * 0.25), 16)
            pad_y = max(int((bottom - top) * 0.25), 16)
            left = max(0, left - pad_x); right = min(w, right + pad_x)
            top = max(0, top - pad_y); bottom = min(h, bottom + pad_y)
            crop = img[top:bottom, left:right]
            if crop.size == 0:
                return None
            os.makedirs(UNKNOWN_FACES_PATH, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"unknown_{stamp}_{index}.jpg"
            path = os.path.join(UNKNOWN_FACES_PATH, filename)
            if not cv2.imwrite(path, crop, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                return None
            db.add_unknown_alert(self.class_id, filename, datetime.now().strftime("%d-%m-%Y %H:%M:%S"))
            return filename
        except Exception:
            return None

    def _match(self, encoding):
        if not self.known_encodings:
            return None
        distances = face_recognition.face_distance(self.known_encodings, encoding)
        best_index = int(np.argmin(distances))
        return self.known_names[best_index] if distances[best_index] <= MATCH_TOLERANCE else None

    def get_jpeg(self):
        with self.lock:
            return self.latest_jpeg

    def mjpeg_generator(self):
        boundary = b"--frame"
        while True:
            frame = self.get_jpeg()
            if frame is not None:
                yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.05)
