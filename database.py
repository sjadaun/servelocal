"""
database.py
SQLite storage + scheduling logic for the meal planner.

A "meal slot" is one row: a meal, a category (breakfast/lunch/dinner/school/...),
a time of day it should be ready, and a repeat rule:
  - once     -> happens only on start_date
  - daily    -> happens every day from start_date (until end_date, if set)
  - weekly   -> happens on specific weekdays every week from start_date

Weekdays are stored 0=Monday .. 6=Sunday (Python's date.weekday() convention).
"""

import sqlite3
import datetime
from contextlib import contextmanager

DB_PATH = "mealplanner.db"  # adjust if you install elsewhere

SCHEMA = """
CREATE TABLE IF NOT EXISTS meal_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,          -- breakfast / lunch / dinner / school / custom
    notes           TEXT DEFAULT '',
    scheduled_time  TEXT NOT NULL,           -- 'HH:MM', 24h, meal should be READY by this time
    prep_minutes    INTEGER DEFAULT 0,       -- how long before scheduled_time to start prepping
    temperature     TEXT DEFAULT '',         -- free text e.g. "180C" or "350F" (blank = n/a)
    repeat_type     TEXT NOT NULL DEFAULT 'once',  -- once / daily / weekly
    repeat_days     TEXT DEFAULT '',         -- comma-separated weekday ints, only for weekly
    start_date      TEXT NOT NULL,           -- 'YYYY-MM-DD'
    end_date        TEXT DEFAULT '',         -- optional 'YYYY-MM-DD', blank = no end
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(SCHEMA)


# ---------------------------------------------------------------- CRUD -----

def create_meal(data):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO meal_slots
               (name, category, notes, scheduled_time, prep_minutes, temperature,
                repeat_type, repeat_days, start_date, end_date, active, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"],
                data["category"],
                data.get("notes", ""),
                data["scheduled_time"],
                int(data.get("prep_minutes", 0) or 0),
                data.get("temperature", ""),
                data.get("repeat_type", "once"),
                ",".join(str(d) for d in data.get("repeat_days", [])),
                data["start_date"],
                data.get("end_date", ""),
                1,
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def update_meal(meal_id, data):
    with get_db() as conn:
        conn.execute(
            """UPDATE meal_slots SET
                 name=?, category=?, notes=?, scheduled_time=?, prep_minutes=?,
                 temperature=?, repeat_type=?, repeat_days=?, start_date=?,
                 end_date=?, active=?
               WHERE id=?""",
            (
                data["name"],
                data["category"],
                data.get("notes", ""),
                data["scheduled_time"],
                int(data.get("prep_minutes", 0) or 0),
                data.get("temperature", ""),
                data.get("repeat_type", "once"),
                ",".join(str(d) for d in data.get("repeat_days", [])),
                data["start_date"],
                data.get("end_date", ""),
                1 if data.get("active", True) else 0,
                meal_id,
            ),
        )


def delete_meal(meal_id):
    with get_db() as conn:
        conn.execute("DELETE FROM meal_slots WHERE id=?", (meal_id,))


def list_meals():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM meal_slots ORDER BY scheduled_time"
        ).fetchall()
        return [dict(r) for r in rows]


def get_meal(meal_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM meal_slots WHERE id=?", (meal_id,)
        ).fetchone()
        return dict(row) if row else None


# --------------------------------------------------------- scheduling -----

def _occurs_on(slot, day: datetime.date) -> bool:
    start = datetime.date.fromisoformat(slot["start_date"])
    if day < start:
        return False
    if slot["end_date"]:
        end = datetime.date.fromisoformat(slot["end_date"])
        if day > end:
            return False

    rtype = slot["repeat_type"]
    if rtype == "once":
        return day == start
    if rtype == "daily":
        return True
    if rtype == "weekly":
        days = {int(d) for d in slot["repeat_days"].split(",") if d != ""}
        return day.weekday() in days
    return False


def _next_occurrence(slot, now: datetime.datetime, horizon_days=14):
    """Return the next datetime this slot is due at or after `now`, or None."""
    hh, mm = (int(x) for x in slot["scheduled_time"].split(":"))
    for offset in range(0, horizon_days):
        day = now.date() + datetime.timedelta(days=offset)
        if not _occurs_on(slot, day):
            continue
        occ = datetime.datetime.combine(day, datetime.time(hh, mm))
        if occ >= now:
            return occ
    return None


def get_next_meal(now=None):
    """The single soonest upcoming meal slot across all active meals."""
    now = now or datetime.datetime.now()
    best = None
    best_dt = None
    for slot in list_meals():
        if not slot["active"]:
            continue
        occ = _next_occurrence(slot, now)
        if occ and (best_dt is None or occ < best_dt):
            best, best_dt = slot, occ
    if best is None:
        return None
    return {"slot": best, "when": best_dt}


def get_today_meals(now=None):
    """All meal slots scheduled for today, in time order, with a 'done' flag
    for anything already past."""
    now = now or datetime.datetime.now()
    today = now.date()
    out = []
    for slot in list_meals():
        if not slot["active"] or not _occurs_on(slot, today):
            continue
        hh, mm = (int(x) for x in slot["scheduled_time"].split(":"))
        occ = datetime.datetime.combine(today, datetime.time(hh, mm))
        out.append({"slot": slot, "when": occ, "done": occ < now})
    out.sort(key=lambda x: x["when"])
    return out
