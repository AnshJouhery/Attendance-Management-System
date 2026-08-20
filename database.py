"""SQLite persistence for multi-class, lecture-wise attendance and unknown-face alerts."""

import sqlite3
import re
from difflib import SequenceMatcher
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "attendance.db"
DEFAULT_CLASS_ID = "SEM7_IT_A1"


def _canonical_name(name):
    """Normalize a student name for a single-roster project while keeping normal capitalization."""
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    if not cleaned:
        return ""
    return cleaned.title()


def _name_quality(name):
    text = str(name or "")
    return (sum(1 for c in text if c.islower()), sum(1 for c in text if c.isupper()), -len(text))


def _normalize_student_data(conn):
    """Merge duplicate student names that differ only by case/spacing and fix attendance names."""
    if not _table_exists(conn, "students"):
        return

    rows = conn.execute("SELECT id, class_id, name FROM students ORDER BY id").fetchall()
    groups = {}
    for row in rows:
        cleaned = re.sub(r"\s+", " ", str(row["name"] or "").strip())
        if not cleaned:
            continue
        key = (row["class_id"], cleaned.casefold())
        groups.setdefault(key, []).append(row)

    # Make sure every attendance row points at the canonical spelling.
    # When case-only duplicates exist for the same lecture, keep the strongest status.
    status_rank = {"absent": 0, "late": 1, "present": 2}
    for (_, _), group in groups.items():
        canonical = max((str(r["name"]).strip() for r in group), key=_name_quality)
        canonical = _canonical_name(canonical)
        class_id = group[0]["class_id"]
        names = [str(r["name"]).strip() for r in group]
        if _table_exists(conn, "attendance"):
            ph = ",".join("?" for _ in names)
            att_rows = conn.execute(
                f"SELECT * FROM attendance WHERE class_id=? AND name IN ({ph}) ORDER BY id",
                [class_id, *names],
            ).fetchall()
            seen = {}
            for a in att_rows:
                key = (a["date"], a["lecture_no"])
                existing = seen.get(key)
                if existing is None:
                    seen[key] = a
                    continue
                # Merge duplicates created by different casing.
                new_rank = status_rank.get(str(a["status"]).lower(), -1)
                old_rank = status_rank.get(str(existing["status"]).lower(), -1)
                if new_rank > old_rank:
                    conn.execute("UPDATE attendance SET status=?, time=? WHERE id=?", (a["status"], a["time"], existing["id"]))
                conn.execute("DELETE FROM attendance WHERE id=?", (a["id"],))
            conn.execute(
                f"UPDATE attendance SET name=? WHERE class_id=? AND name IN ({ph})",
                [canonical, class_id, *names],
            )

        # Rebuild the student entry set for this case-insensitive group.
        keeper_id = min(int(r["id"]) for r in group)
        conn.execute("UPDATE students SET name=? WHERE id=?", (canonical, keeper_id))
        dup_ids = [int(r["id"]) for r in group if int(r["id"]) != keeper_id]
        if dup_ids:
            ph2 = ",".join("?" for _ in dup_ids)
            conn.execute(f"DELETE FROM students WHERE id IN ({ph2})", dup_ids)



def _normalize_sem7_attendance_names(conn):
    """Repair case/spacing variants in Semester 7 attendance safely.

    Existing records such as ADITYA and Aditya are merged into the enrolled
    roster name. For the same student/date/lecture, the strongest status is
    retained: PRESENT > LATE > ABSENT.
    """
    class_id = DEFAULT_CLASS_ID
    if not _table_exists(conn, "students") or not _table_exists(conn, "attendance"):
        return

    students = conn.execute(
        "SELECT id, name FROM students WHERE class_id=? ORDER BY id",
        (class_id,),
    ).fetchall()
    canonical = {}
    for row in students:
        name = _canonical_name(row["name"])
        if name:
            canonical[re.sub(r"\s+", " ", name).casefold()] = name
    if not canonical:
        return

    def resolve_name(raw_name):
        cleaned = re.sub(r"\s+", " ", str(raw_name or "").strip())
        key = cleaned.casefold()
        if key in canonical:
            return canonical[key]
        # Repair minor spelling/casing variants against the actual enrolled roster.
        compact_raw = re.sub(r"[^a-z0-9]", "", cleaned.casefold())
        raw_parts = cleaned.split()
        best = None
        best_ratio = 0.0
        for candidate_key, candidate_name in canonical.items():
            candidate_parts = candidate_name.split()
            if raw_parts and candidate_parts and raw_parts[0].casefold() != candidate_parts[0].casefold():
                continue
            compact_candidate = re.sub(r"[^a-z0-9]", "", candidate_name.casefold())
            ratio = SequenceMatcher(None, compact_raw, compact_candidate).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, candidate_name
        return best if best is not None and best_ratio >= 0.90 else _canonical_name(cleaned)

    status_rank = {"absent": 0, "late": 1, "present": 2}
    rows = conn.execute(
        "SELECT * FROM attendance WHERE class_id=? ORDER BY id",
        (class_id,),
    ).fetchall()

    # Group by canonical student + lecture identity first, before changing any
    # names, so the UNIQUE(class_id,name,date,lecture_no) constraint is never
    # violated by an intermediate UPDATE.
    groups = {}
    for row in rows:
        raw = re.sub(r"\s+", " ", str(row["name"] or "").strip())
        canonical_name = resolve_name(raw)
        if not canonical_name:
            continue
        key = (canonical_name, row["date"], row["lecture_no"])
        groups.setdefault(key, []).append(row)

    for (canonical_name, _, _), group in groups.items():
        # Keep an already-canonical row when one exists; otherwise keep the first row.
        keeper = next((r for r in group if r["name"] == canonical_name), group[0])
        best = max(group, key=lambda r: status_rank.get(str(r["status"]).lower(), -1))

        # Delete all competing rows before renaming/updating the keeper so the
        # composite UNIQUE constraint can never be violated.
        for row in group:
            if row["id"] != keeper["id"]:
                conn.execute("DELETE FROM attendance WHERE id=?", (row["id"],))

        conn.execute(
            "UPDATE attendance SET name=?, status=?, time=? WHERE id=?",
            (canonical_name, best["status"], best["time"], keeper["id"]),
        )

    # Canonicalize any remaining Semester 7 attendance names whose exact
    # lecture identity is unique.
    rows = conn.execute(
        "SELECT id,name FROM attendance WHERE class_id=?",
        (class_id,),
    ).fetchall()
    for row in rows:
        raw = re.sub(r"\s+", " ", str(row["name"] or "").strip())
        canonical_name = resolve_name(raw)
        if canonical_name and raw != canonical_name:
            # Safe because duplicate groups have already been merged above.
            conn.execute("UPDATE attendance SET name=? WHERE id=?", (canonical_name, row["id"]))


def _merge_roster_aliases(conn, class_id, canonical_names):
    """Merge very-close attendance/student name variants into the actual image-roster name."""
    canonical_names = [_canonical_name(n) for n in canonical_names if _canonical_name(n)]
    if not canonical_names or not _table_exists(conn, "students"):
        return

    def compact(value):
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())

    existing = conn.execute("SELECT id,name FROM students WHERE class_id=? ORDER BY id", (class_id,)).fetchall()
    for row in existing:
        current = _canonical_name(row["name"])
        exact = next((c for c in canonical_names if current.casefold() == c.casefold()), None)
        if exact:
            if current != exact:
                conn.execute("UPDATE students SET name=? WHERE id=?", (exact, row["id"]))
                conn.execute("UPDATE attendance SET name=? WHERE class_id=? AND name=?", (exact, class_id, row["name"]))
            continue

        best=None; best_ratio=0.0
        current_parts=current.split()
        for candidate in canonical_names:
            candidate_parts=candidate.split()
            if current_parts and candidate_parts and current_parts[0].casefold() != candidate_parts[0].casefold():
                continue
            ratio=SequenceMatcher(None, compact(current), compact(candidate)).ratio()
            if ratio>best_ratio:
                best_ratio=ratio; best=candidate
        if not best or best_ratio < 0.90 or current.casefold() == best.casefold():
            continue

        # Merge duplicate attendance rows if alias and canonical both have the same lecture.
        aliases=[str(row["name"])]
        ph=','.join('?' for _ in aliases)
        att_rows=conn.execute(
            f"SELECT * FROM attendance WHERE class_id=? AND name IN ({ph}) ORDER BY id",
            [class_id,*aliases],
        ).fetchall()
        status_rank={"absent":0,"late":1,"present":2}
        for a in att_rows:
            existing_canon=conn.execute(
                "SELECT id,status FROM attendance WHERE class_id=? AND name=? AND date=? AND lecture_no=? AND id<>?",
                (class_id,best,a["date"],a["lecture_no"],a["id"]),
            ).fetchone()
            if existing_canon:
                if status_rank.get(str(a["status"]).lower(),-1)>status_rank.get(str(existing_canon["status"]).lower(),-1):
                    conn.execute("UPDATE attendance SET status=?,time=? WHERE id=?", (a["status"],a["time"],existing_canon["id"]))
                conn.execute("DELETE FROM attendance WHERE id=?", (a["id"],))
            else:
                conn.execute("UPDATE attendance SET name=? WHERE id=?", (best,a["id"]))

        # If the canonical student row already exists, remove this alias row.
        canon_row=conn.execute("SELECT id FROM students WHERE class_id=? AND name=?", (class_id,best)).fetchone()
        if canon_row and int(canon_row["id"]) != int(row["id"]):
            conn.execute("DELETE FROM students WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE students SET name=? WHERE id=?", (best,row["id"]))


def _ensure_holidays_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
            date TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(class_id, date)
        )
    """)


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def get_conn():
    return _conn_context()


@contextmanager
def _conn_context():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _create_attendance_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'present',
            lecture_no INTEGER NOT NULL,
            lecture_label TEXT NOT NULL,
            lecture_start TEXT,
            lecture_end TEXT,
            subject TEXT,
            weekday TEXT,
            period_no INTEGER,
            UNIQUE(class_id, name, date, lecture_no)
        )
    """)


def _ensure_attendance_schema(conn):
    canonical = {
        "class_id", "name", "date", "time", "status", "lecture_no",
        "lecture_label", "lecture_start", "lecture_end", "subject",
        "weekday", "period_no"
    }
    if not _table_exists(conn, "attendance"):
        _create_attendance_table(conn)
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(attendance)").fetchall()}
    if canonical.issubset(cols):
        if _table_exists(conn, "attendance_legacy"):
            conn.execute("DROP TABLE attendance_legacy")
        conn.execute("UPDATE attendance SET class_id=COALESCE(NULLIF(class_id,''),?)", (DEFAULT_CLASS_ID,))
        conn.execute("UPDATE attendance SET subject=COALESCE(NULLIF(subject,''),'Legacy')")
        conn.execute("UPDATE attendance SET weekday=COALESCE(weekday,'')")
        return

    conn.execute("DROP TABLE IF EXISTS attendance_new")
    _create_attendance_table(conn.__class__ and conn)  # create original table if absent is harmless
    # The preceding call is intentionally not used for the new table; create explicitly.
    conn.execute("DROP TABLE IF EXISTS attendance_new")
    conn.execute("""
        CREATE TABLE attendance_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'present',
            lecture_no INTEGER NOT NULL,
            lecture_label TEXT NOT NULL,
            lecture_start TEXT,
            lecture_end TEXT,
            subject TEXT,
            weekday TEXT,
            period_no INTEGER,
            UNIQUE(class_id, name, date, lecture_no)
        )
    """)

    source_tables = ["attendance"]
    if _table_exists(conn, "attendance_legacy"):
        source_tables.append("attendance_legacy")

    for table in source_tables:
        tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not {"name", "date", "time", "status"}.issubset(tcols):
            continue
        class_expr = "class_id" if "class_id" in tcols else f"'{DEFAULT_CLASS_ID}'"
        lecture_no_expr = "lecture_no" if "lecture_no" in tcols else "0"
        lecture_label_expr = "lecture_label" if "lecture_label" in tcols else "'Legacy'"
        lecture_start_expr = "lecture_start" if "lecture_start" in tcols else "''"
        lecture_end_expr = "lecture_end" if "lecture_end" in tcols else "''"
        subject_expr = "subject" if "subject" in tcols else "'Legacy'"
        weekday_expr = "weekday" if "weekday" in tcols else "''"
        period_expr = "period_no" if "period_no" in tcols else lecture_no_expr
        conn.execute(
            f"""
            INSERT OR IGNORE INTO attendance_new
            (class_id,name,date,time,status,lecture_no,lecture_label,lecture_start,lecture_end,subject,weekday,period_no)
            SELECT {class_expr},name,date,time,status,{lecture_no_expr},{lecture_label_expr},
                   {lecture_start_expr},{lecture_end_expr},{subject_expr},{weekday_expr},{period_expr}
            FROM {table}
            ORDER BY id
            """
        )

    conn.execute("DROP TABLE attendance")
    if _table_exists(conn, "attendance_legacy"):
        conn.execute("DROP TABLE attendance_legacy")
    conn.execute("ALTER TABLE attendance_new RENAME TO attendance")


def _ensure_students_schema(conn):
    """Migrate the old global student list into class-specific rosters."""
    if not _table_exists(conn, "students"):
        conn.execute("""
            CREATE TABLE students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
                name TEXT NOT NULL,
                UNIQUE(class_id, name)
            )
        """)
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
    # Existing old schema: id, name with UNIQUE(name).
    if "class_id" not in cols:
        conn.execute("DROP TABLE IF EXISTS students_new")
        conn.execute("""
            CREATE TABLE students_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
                name TEXT NOT NULL,
                UNIQUE(class_id, name)
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO students_new (class_id,name) SELECT ?,name FROM students ORDER BY id",
            (DEFAULT_CLASS_ID,),
        )
        conn.execute("DROP TABLE students")
        conn.execute("ALTER TABLE students_new RENAME TO students")
        return

    # Class-aware schema is present. Ensure the desired composite uniqueness by
    # rebuilding only when needed; duplicates across classes are intentionally allowed.
    index_rows = conn.execute("PRAGMA index_list(students)").fetchall()
    has_composite_unique = False
    for idx in index_rows:
        if not idx[2]:
            continue
        idx_name = idx[1]
        index_cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()]
        if index_cols == ["class_id", "name"]:
            has_composite_unique = True
            break
    if not has_composite_unique:
        conn.execute("DROP TABLE IF EXISTS students_new")
        conn.execute("""
            CREATE TABLE students_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
                name TEXT NOT NULL,
                UNIQUE(class_id, name)
            )
        """)
        conn.execute("INSERT OR IGNORE INTO students_new (class_id,name) SELECT COALESCE(NULLIF(class_id,''),?),name FROM students ORDER BY id", (DEFAULT_CLASS_ID,))
        conn.execute("DROP TABLE students")
        conn.execute("ALTER TABLE students_new RENAME TO students")


def _ensure_unknown_schema(conn):
    if not _table_exists(conn, "unknown_alerts"):
        conn.execute("""
            CREATE TABLE unknown_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
                filename TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                reviewed INTEGER NOT NULL DEFAULT 0
            )
        """)
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(unknown_alerts)").fetchall()}
    if "class_id" in cols:
        conn.execute("UPDATE unknown_alerts SET class_id=COALESCE(NULLIF(class_id,''),?)", (DEFAULT_CLASS_ID,))
        return
    conn.execute("DROP TABLE IF EXISTS unknown_alerts_new")
    conn.execute("""
        CREATE TABLE unknown_alerts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id TEXT NOT NULL DEFAULT 'SEM7_IT_A1',
            filename TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("INSERT INTO unknown_alerts_new (id,class_id,filename,timestamp,reviewed) SELECT id,?,filename,timestamp,reviewed FROM unknown_alerts", (DEFAULT_CLASS_ID,))
    conn.execute("DROP TABLE unknown_alerts")
    conn.execute("ALTER TABLE unknown_alerts_new RENAME TO unknown_alerts")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS classes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                semester INTEGER NOT NULL,
                section TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL
            )
        """)
        _ensure_students_schema(conn)
        _ensure_attendance_schema(conn)
        _ensure_unknown_schema(conn)
        _ensure_holidays_schema(conn)
        _normalize_student_data(conn)
        _normalize_sem7_attendance_names(conn)


def ensure_classes(classes):
    with get_conn() as conn:
        for c in classes:
            conn.execute(
                "INSERT OR IGNORE INTO classes (id,name,semester,section,start_date,end_date) VALUES (?,?,?,?,?,?)",
                (c["id"], c["name"], c["semester"], c["section"], c["start_date"], c["end_date"]),
            )


def get_classes():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM classes ORDER BY semester DESC, section").fetchall()]


def sync_students(class_id, names):
    canonical_names=[]
    with get_conn() as conn:
        for name in names:
            canonical = _canonical_name(name)
            if canonical:
                canonical_names.append(canonical)
                conn.execute("INSERT OR IGNORE INTO students (class_id,name) VALUES (?,?)", (class_id, canonical))
        _normalize_student_data(conn)
        _merge_roster_aliases(conn, class_id, canonical_names)


def get_student_names(class_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM students WHERE class_id=? ORDER BY name COLLATE NOCASE", (class_id,)).fetchall()
        return [r["name"] for r in rows]


def get_total_students(class_id):
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM students WHERE class_id=?", (class_id,)).fetchone()["c"]


def mark_attendance(class_id, name, date_str, time_str, status, lecture_no, lecture_label,
                    lecture_start, lecture_end, weekday=None, period_no=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO attendance "
            "(class_id,name,date,time,status,lecture_no,lecture_label,lecture_start,lecture_end,subject,weekday,period_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (class_id, name, date_str, time_str, status, lecture_no, lecture_label, lecture_start,
             lecture_end, lecture_label, weekday, period_no if period_no is not None else lecture_no),
        )
        return cur.rowcount > 0


def get_present_today(class_id, date_str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT name FROM attendance WHERE class_id=? AND date=? AND status IN ('present','late')",
            (class_id, date_str),
        ).fetchall()
        return [r["name"] for r in rows]


def finalize_lecture_absences(class_id, date_str, weekday, period_no, subject, lecture_start, lecture_end):
    with get_conn() as conn:
        students = conn.execute("SELECT DISTINCT name FROM students WHERE class_id=? ORDER BY name COLLATE NOCASE", (class_id,)).fetchall()
        count = 0
        for row in students:
            cur = conn.execute(
                "INSERT OR IGNORE INTO attendance "
                "(class_id,name,date,time,status,lecture_no,lecture_label,lecture_start,lecture_end,subject,weekday,period_no) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (class_id, row["name"], date_str, lecture_end + ":00", "absent", period_no,
                 subject, lecture_start, lecture_end, subject, weekday, period_no)
            )
            count += cur.rowcount
        return count


def _iso_date_expr(column):
    return f"substr({column},7,4)||'-'||substr({column},4,2)||'-'||substr({column},1,2)"


def get_semester_student_summary(class_id, start_iso, end_iso):
    date_expr = _iso_date_expr("a.date")
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT s.name,
                   COUNT(a.id) AS total_lectures,
                   COALESCE(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END),0) AS present_count,
                   COALESCE(SUM(CASE WHEN a.status='late' THEN 1 ELSE 0 END),0) AS late_count,
                   COALESCE(SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END),0) AS absent_count
            FROM students s
            LEFT JOIN attendance a
              ON a.class_id=s.class_id AND a.name=s.name AND {date_expr} BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
            WHERE s.class_id=?
            GROUP BY s.name ORDER BY s.name COLLATE NOCASE
            """, (start_iso, end_iso, class_id)
        ).fetchall()
        out=[]
        for r in rows:
            item=dict(r); total=item["total_lectures"] or 0
            item["attendance_percentage"] = round(((item["present_count"] or 0)+(item["late_count"] or 0))*100/total,2) if total else 0.0
            out.append(item)
        return out


def get_student_subject_summary(class_id, student_name, start_iso, end_iso):
    """Return subject-wise attendance for one specific student in a date range."""
    date_expr = _iso_date_expr("a.date")
    subjects = ("AI", "SPM", "STQA", "RS", "DWM")
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT a.subject,
                   COUNT(DISTINCT a.date || '-' || a.period_no) AS held_lectures,
                   COALESCE(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END),0) AS present_count,
                   COALESCE(SUM(CASE WHEN a.status='late' THEN 1 ELSE 0 END),0) AS late_count,
                   COALESCE(SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END),0) AS absent_count
            FROM attendance a
            WHERE a.class_id=? AND a.name=?
              AND a.subject IN ('AI','SPM','STQA','RS','DWM')
              AND {date_expr} BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
            GROUP BY a.subject
            ORDER BY a.subject
            """, (class_id, student_name, start_iso, end_iso)
        ).fetchall()
        by_subject = {r["subject"]: dict(r) for r in rows}
        out = []
        for subject in subjects:
            item = by_subject.get(subject, {
                "subject": subject, "held_lectures": 0,
                "present_count": 0, "late_count": 0, "absent_count": 0
            })
            held = item.get("held_lectures") or 0
            item["attendance_percentage"] = round(((item.get("present_count") or 0) + (item.get("late_count") or 0)) * 100 / held, 2) if held else 0.0
            out.append(item)
        return out


def get_student_attendance_detail(class_id, student_name, start_iso, end_iso, subject=None):
    """Return lecture-wise rows for one specific student."""
    date_expr = _iso_date_expr("a.date")
    clauses = ["a.class_id=?", "a.name=?", f"{date_expr} BETWEEN ? AND ?", "a.subject IN ('AI','SPM','STQA','RS','DWM')", f"NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})"]
    params = [class_id, student_name, start_iso, end_iso]
    if subject:
        clauses.append("a.subject=?")
        params.append(subject)
    where = " AND ".join(clauses)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT a.date, a.weekday, a.subject, a.period_no, a.lecture_no,
                   a.lecture_start, a.lecture_end, a.status, a.time
            FROM attendance a
            WHERE {where}
            ORDER BY {date_expr} DESC, a.period_no ASC
            """, params
        ).fetchall()
        return [dict(r) for r in rows]


def get_semester_subject_summary(class_id, start_iso, end_iso):
    date_expr = _iso_date_expr("a.date")
    with get_conn() as conn:
        student_count = conn.execute("SELECT COUNT(*) AS c FROM students WHERE class_id=?", (class_id,)).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT a.subject,
                   COUNT(DISTINCT a.date || '-' || a.period_no) AS held_lectures,
                   COALESCE(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END),0) AS present_count,
                   COALESCE(SUM(CASE WHEN a.status='late' THEN 1 ELSE 0 END),0) AS late_count,
                   COALESCE(SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END),0) AS absent_count
            FROM attendance a
            WHERE a.class_id=? AND a.subject IN ('AI','SPM','STQA','RS','DWM') AND {date_expr} BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
            GROUP BY a.subject ORDER BY a.subject
            """, (class_id,start_iso,end_iso)
        ).fetchall()
        out=[]
        for r in rows:
            item=dict(r); held=item["held_lectures"] or 0; expected=held*student_count
            item["expected_records"]=expected; item["student_count"]=student_count
            item["attendance_percentage"] = round(((item["present_count"] or 0)+(item["late_count"] or 0))*100/expected,2) if expected else 0.0
            out.append(item)
        return out


def get_semester_overview(class_id, start_iso, end_iso, through_iso):
    date_expr = _iso_date_expr("date"); end_for_query=min(end_iso,through_iso)
    with get_conn() as conn:
        held=conn.execute(f"SELECT COUNT(*) AS c FROM (SELECT DISTINCT date, period_no FROM attendance a WHERE class_id=? AND {date_expr} BETWEEN ? AND ? AND subject IS NOT NULL AND subject!='' AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr}))", (class_id,start_iso,end_for_query)).fetchone()["c"]
        present=conn.execute(f"SELECT COUNT(*) AS c FROM attendance a WHERE class_id=? AND {date_expr} BETWEEN ? AND ? AND status='present' AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})", (class_id,start_iso,end_for_query)).fetchone()["c"]
        late=conn.execute(f"SELECT COUNT(*) AS c FROM attendance a WHERE class_id=? AND {date_expr} BETWEEN ? AND ? AND status='late' AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})", (class_id,start_iso,end_for_query)).fetchone()["c"]
        absent=conn.execute(f"SELECT COUNT(*) AS c FROM attendance a WHERE class_id=? AND {date_expr} BETWEEN ? AND ? AND status='absent' AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})", (class_id,start_iso,end_for_query)).fetchone()["c"]
        return {"held_lecture_sessions":held,"present_records":present,"late_records":late,"absent_records":absent}


def get_lecture_summary(class_id, start_iso, end_iso):
    date_expr=_iso_date_expr("a.date")
    with get_conn() as conn:
        rows=conn.execute(
            f"""
            SELECT a.date,a.weekday,a.period_no,a.lecture_no,a.subject,a.lecture_start,a.lecture_end,
                   COUNT(*) AS student_count,
                   GROUP_CONCAT(a.name || ' · ' || UPPER(a.status), '||') AS student_names,
                   SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) AS present_count,
                   SUM(CASE WHEN a.status='late' THEN 1 ELSE 0 END) AS late_count,
                   SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) AS absent_count
            FROM attendance a
            WHERE a.class_id=? AND a.subject IN ('AI','SPM','STQA','RS','DWM') AND {date_expr} BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date={date_expr})
            GROUP BY a.date,a.weekday,a.period_no,a.lecture_no,a.subject,a.lecture_start,a.lecture_end
            ORDER BY {date_expr} DESC, a.period_no ASC
            """, (class_id,start_iso,end_iso)).fetchall()
        return [dict(r) for r in rows]


def get_subject_date_attendance(class_id, date_str, subject, period_no):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.name,
                   a.status, a.time, a.date, a.subject, a.period_no,
                   a.lecture_no, a.lecture_start, a.lecture_end, a.weekday
            FROM students s
            LEFT JOIN attendance a
              ON a.class_id=s.class_id
             AND a.name=s.name
             AND a.date=?
             AND a.subject=?
             AND a.period_no=?
            WHERE s.class_id=?
            ORDER BY s.name COLLATE NOCASE
            """,
            (date_str, subject, period_no, class_id),
        ).fetchall()
        return [dict(r) | {"status": (r["status"] or "absent")} for r in rows]


def get_records(class_id, limit=5000, start_iso=None, end_iso=None, status=None, subject=None):
    date_expr = _iso_date_expr("date")
    clauses = ["class_id=?", "NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=attendance.class_id AND h.date=" + date_expr + ")"]
    params = [class_id]
    if start_iso:
        clauses.append(f"{date_expr} >= ?"); params.append(start_iso)
    if end_iso:
        clauses.append(f"{date_expr} <= ?"); params.append(end_iso)
    if status and status != "all":
        clauses.append("status=?"); params.append(status)
    if subject:
        clauses.append("subject=?"); params.append(subject)
    where = " AND ".join(clauses)
    with get_conn() as conn:
        rows=conn.execute(f"SELECT * FROM attendance WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def get_weekly_counts(class_id,last_n_days=7):
    with get_conn() as conn:
        rows=conn.execute("""
            SELECT a.date, COUNT(DISTINCT a.name) AS c
            FROM attendance a
            WHERE a.class_id=?
              AND a.status IN ('present','late')
              AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date=substr(a.date,7,4)||'-'||substr(a.date,4,2)||'-'||substr(a.date,1,2))
            GROUP BY a.date
            ORDER BY MAX(a.id) DESC
            LIMIT ?
        """, (class_id,last_n_days)).fetchall()
        return {r["date"]:r["c"] for r in rows}


def get_subject_summary(class_id):
    with get_conn() as conn:
        rows=conn.execute("SELECT a.subject, COUNT(*) AS attendance_count, COUNT(DISTINCT a.name) AS students_seen, SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) AS present_count, SUM(CASE WHEN a.status='late' THEN 1 ELSE 0 END) AS late_count FROM attendance a WHERE a.class_id=? AND a.subject IS NOT NULL AND a.subject!='' AND NOT EXISTS (SELECT 1 FROM holidays h WHERE h.class_id=a.class_id AND h.date=substr(a.date,7,4)||'-'||substr(a.date,4,2)||'-'||substr(a.date,1,2)) GROUP BY a.subject ORDER BY a.subject", (class_id,)).fetchall()
        return [dict(r) for r in rows]


def add_holiday(class_id, date_iso, reason=""):
    """Declare a teacher-created holiday and remove any attendance rows for that date."""
    datetime.strptime(date_iso, "%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO holidays (class_id,date,reason,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(class_id,date) DO UPDATE SET reason=excluded.reason",
            (class_id, date_iso, str(reason or "").strip(), datetime.now().strftime("%d-%m-%Y %H:%M:%S")),
        )
        conn.execute(
            "DELETE FROM attendance WHERE class_id=? AND "
            "substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2)=?",
            (class_id, date_iso),
        )


def delete_holiday(class_id, date_iso):
    with get_conn() as conn:
        cur=conn.execute("DELETE FROM holidays WHERE class_id=? AND date=?", (class_id,date_iso))
        return cur.rowcount > 0


def is_holiday(class_id, date_iso):
    with get_conn() as conn:
        row=conn.execute("SELECT 1 FROM holidays WHERE class_id=? AND date=?", (class_id,date_iso)).fetchone()
        return row is not None


def get_holidays(class_id, start_iso=None, end_iso=None):
    clauses=["class_id=?"]
    params=[class_id]
    if start_iso:
        clauses.append("date>=?"); params.append(start_iso)
    if end_iso:
        clauses.append("date<=?"); params.append(end_iso)
    where=" AND ".join(clauses)
    with get_conn() as conn:
        rows=conn.execute(f"SELECT id,date,reason,created_at FROM holidays WHERE {where} ORDER BY date", params).fetchall()
        return [dict(r) for r in rows]


def add_unknown_alert(class_id, filename, timestamp):
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO unknown_alerts (class_id,filename,timestamp) VALUES (?,?,?)", (class_id, filename, timestamp))
        return cur.lastrowid


def mark_alert_reviewed(alert_id, class_id=None):
    with get_conn() as conn:
        if class_id:
            conn.execute("UPDATE unknown_alerts SET reviewed=1 WHERE id=? AND class_id=?", (alert_id, class_id))
        else:
            conn.execute("UPDATE unknown_alerts SET reviewed=1 WHERE id=?", (alert_id,))


def get_alerts(class_id, limit=100):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id,filename,timestamp,reviewed FROM unknown_alerts WHERE class_id=? ORDER BY id DESC LIMIT ?", (class_id,limit)).fetchall()]


def get_unreviewed_alert_count(class_id):
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM unknown_alerts WHERE class_id=? AND reviewed=0", (class_id,)).fetchone()["c"]


def delete_alert(alert_id, class_id=None):
    with get_conn() as conn:
        if class_id:
            row=conn.execute("SELECT filename FROM unknown_alerts WHERE id=? AND class_id=?",(alert_id,class_id)).fetchone()
            if row is None:return None
            conn.execute("DELETE FROM unknown_alerts WHERE id=? AND class_id=?",(alert_id,class_id))
        else:
            row=conn.execute("SELECT filename FROM unknown_alerts WHERE id=?",(alert_id,)).fetchone()
            if row is None:return None
            conn.execute("DELETE FROM unknown_alerts WHERE id=?",(alert_id))
        return row["filename"]


def delete_alerts(class_id, alert_ids=None, delete_all=False):
    with get_conn() as conn:
        if delete_all:
            rows=conn.execute("SELECT filename FROM unknown_alerts WHERE class_id=?",(class_id,)).fetchall()
            conn.execute("DELETE FROM unknown_alerts WHERE class_id=?",(class_id,))
        else:
            ids=[int(x) for x in (alert_ids or []) if str(x).isdigit()]
            if not ids:return []
            ph=','.join('?' for _ in ids)
            rows=conn.execute(f"SELECT filename FROM unknown_alerts WHERE class_id=? AND id IN ({ph})",[class_id,*ids]).fetchall()
            conn.execute(f"DELETE FROM unknown_alerts WHERE class_id=? AND id IN ({ph})",[class_id,*ids])
        return [r["filename"] for r in rows]
