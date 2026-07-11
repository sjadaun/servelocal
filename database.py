"""
database.py
SQLite storage + scheduling logic for the meal planner.

Two tables:

  meal_slots      -- the recurring definition ("Family dinner every Sunday
                      at 20:00"). Repeat rules:
                        once    -> happens only on start_date
                        daily   -> happens every day from start_date
                                   (until end_date, if set)
                        weekly  -> happens on specific weekdays every week
                        monthly -> happens once a month, on the same
                                   day-of-month as start_date (clamped to
                                   the last day of shorter months, e.g. a
                                   31st start still fires on Feb 28/29)
                      Weekdays are 0=Monday .. 6=Sunday.

  meal_exceptions -- an optional per-date override for one occurrence of a
                      slot, so you can edit or cancel a single day without
                      touching the recurring series. Keyed by
                      (meal_slot_id, exception_date). If `cancelled` is set,
                      that occurrence simply doesn't happen. Otherwise any
                      non-NULL override_* column replaces the base slot's
                      value for that date only.

A meal can optionally be marked "going out" (going_out=1, going_out_place
set) instead of home-cooked -- this can be overridden per-occurrence too
(e.g. "normally home dinner, but eating out this one Friday").

  app_settings    -- small key/value store for shared settings, e.g. the
                      "theme_mode" the web UI *and* the physical display
                      both read, so they always agree (light / dark / auto,
                      where auto resolves based on the server's clock --
                      there's no browser involved on the device side).
"""

import sqlite3
import datetime
import calendar
from contextlib import contextmanager

DB_PATH = "/var/lib/servelocal/db/mealplanner.db"  # adjust if you install elsewhere

SCHEMA = """
CREATE TABLE IF NOT EXISTS meal_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,          -- breakfast / lunch / dinner / snack / other
    notes           TEXT DEFAULT '',
    scheduled_time  TEXT NOT NULL,           -- 'HH:MM', 24h, meal should be READY by this time
    prep_minutes    INTEGER DEFAULT 0,       -- minutes before scheduled_time to start prepping
                                              -- (or, if going_out, minutes before to leave home)
    going_out       INTEGER NOT NULL DEFAULT 0,
    going_out_place TEXT DEFAULT '',
    repeat_type     TEXT NOT NULL DEFAULT 'once',  -- once / daily / weekly
    repeat_days     TEXT DEFAULT '',         -- comma-separated weekday ints, only for weekly
    start_date      TEXT NOT NULL,           -- 'YYYY-MM-DD'
    end_date        TEXT DEFAULT '',         -- optional 'YYYY-MM-DD', blank = no end
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meal_exceptions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_slot_id            INTEGER NOT NULL REFERENCES meal_slots(id) ON DELETE CASCADE,
    exception_date          TEXT NOT NULL,   -- 'YYYY-MM-DD'
    cancelled               INTEGER NOT NULL DEFAULT 0,
    completed               INTEGER NOT NULL DEFAULT 0,  -- manually marked done via the Next card
    override_name           TEXT,
    override_category       TEXT,
    override_notes          TEXT,
    override_scheduled_time TEXT,
    override_prep_minutes   INTEGER,
    override_going_out      INTEGER,
    override_going_out_place TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE(meal_slot_id, exception_date)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # lightweight migration for DBs created before `completed` existed
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(meal_exceptions)").fetchall()]
        if "completed" not in cols:
            conn.execute(
                "ALTER TABLE meal_exceptions ADD COLUMN completed INTEGER NOT NULL DEFAULT 0"
            )


# ---------------------------------------------------------- slot CRUD -----

def create_meal(data):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO meal_slots
               (name, category, notes, scheduled_time, prep_minutes,
                going_out, going_out_place, repeat_type, repeat_days,
                start_date, end_date, active, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"],
                data["category"],
                data.get("notes", ""),
                data["scheduled_time"],
                int(data.get("prep_minutes", 0) or 0),
                1 if data.get("going_out") else 0,
                data.get("going_out_place", ""),
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
                 going_out=?, going_out_place=?, repeat_type=?, repeat_days=?,Revert / Restore — remove the override or cancellation and fall back to the series default
                 start_date=?, end_date=?, active=?
               WHERE id=?""",
            (
                data["name"],
                data["category"],
                data.get("notes", ""),
                data["scheduled_time"],
                int(data.get("prep_minutes", 0) or 0),
                1 if data.get("going_out") else 0,
                data.get("going_out_place", ""),
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


# ------------------------------------------------------ exception CRUD -----

_OVERRIDE_FIELDS = {
    # payload key       -> (db column,                  base slot key)
    "name":              ("override_name",               "name"),
    "category":           ("override_category",          "category"),
    "notes":              ("override_notes",              "notes"),
    "scheduled_time":     ("override_scheduled_time",     "scheduled_time"),
    "prep_minutes":       ("override_prep_minutes",       "prep_minutes"),
    "going_out":          ("override_going_out",          "going_out"),
    "going_out_place":    ("override_going_out_place",    "going_out_place"),
}


def get_exception(meal_slot_id, date_str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM meal_exceptions WHERE meal_slot_id=? AND exception_date=?",
            (meal_slot_id, date_str),
        ).fetchone()
        return dict(row) if row else None


def upsert_exception(meal_slot_id, date_str, data):
    """data may include `cancelled` (bool) and/or any of _OVERRIDE_FIELDS keys.
    If cancelled is true, override fields are ignored (the occurrence just
    doesn't happen, nothing to display)."""
    cancelled = 1 if data.get("cancelled") else 0
    cols = {
        "override_name": None, "override_category": None, "override_notes": None,
        "override_scheduled_time": None, "override_prep_minutes": None,
        "override_going_out": None, "override_going_out_place": None,
    }
    if not cancelled:
        for payload_key, (db_col, _) in _OVERRIDE_FIELDS.items():
            if payload_key in data and data[payload_key] is not None:
                val = data[payload_key]
                if payload_key == "going_out":
                    val = 1 if val else 0
                cols[db_col] = val

    with get_db() as conn:
        conn.execute(
            f"""INSERT INTO meal_exceptions
                (meal_slot_id, exception_date, cancelled, {", ".join(cols.keys())}, created_at)
                VALUES (?,?,?,{", ".join("?" for _ in cols)},?)
                ON CONFLICT(meal_slot_id, exception_date) DO UPDATE SET
                    cancelled=excluded.cancelled,
                    {", ".join(f"{c}=excluded.{c}" for c in cols.keys())}
            """,
            (meal_slot_id, date_str, cancelled, *cols.values(),
             datetime.datetime.now().isoformat(timespec="seconds")),
        )


def delete_exception(meal_slot_id, date_str):
    """Revert a single day back to the recurring series default."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM meal_exceptions WHERE meal_slot_id=? AND exception_date=?",
            (meal_slot_id, date_str),
        )


def set_completed(meal_slot_id, date_str, completed: bool):
    """Manually mark (or unmark) a single occurrence as done -- e.g. from the
    'Mark as done' button on the Next card. Deliberately separate from
    upsert_exception() so this never touches any existing override/cancel
    data for that day."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM meal_exceptions WHERE meal_slot_id=? AND exception_date=?",
            (meal_slot_id, date_str),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE meal_exceptions SET completed=? WHERE id=?",
                (1 if completed else 0, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO meal_exceptions
                   (meal_slot_id, exception_date, cancelled, completed, created_at)
                   VALUES (?,?,0,?,?)""",
                (meal_slot_id, date_str, 1 if completed else 0,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )


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
    if rtype == "monthly":
        last_day = calendar.monthrange(day.year, day.month)[1]
        target_day = min(start.day, last_day)
        return day.day == target_day
    return False


def _apply_exception(slot: dict, exc: dict | None) -> dict:
    merged = dict(slot)
    if exc:
        for payload_key, (db_col, slot_key) in _OVERRIDE_FIELDS.items():
            val = exc[db_col]
            if val is not None:
                merged[slot_key] = val
    return merged


def _has_real_override(exc: dict | None) -> bool:
    """True only if an exception row carries an actual field override --
    not just bookkeeping like a 'completed' flag with nothing else changed."""
    if not exc:
        return False
    return any(exc[db_col] is not None for db_col, _ in _OVERRIDE_FIELDS.values())


def _effective_occurrence(slot, day: datetime.date):
    """Returns None if this slot doesn't run on `day` or was cancelled for
    that date. Otherwise returns a dict describing the occurrence."""
    if not _occurs_on(slot, day):
        return None
    exc = get_exception(slot["id"], day.isoformat())
    if exc and exc["cancelled"]:
        return None
    merged = _apply_exception(slot, exc)
    hh, mm = (int(x) for x in merged["scheduled_time"].split(":"))
    when = datetime.datetime.combine(day, datetime.time(hh, mm))
    return {
        "meal": merged,
        "when": when,
        "slot_id": slot["id"],
        "exception_id": exc["id"] if exc else None,
        "has_override": _has_real_override(exc),
        "completed": bool(exc["completed"]) if exc else False,
    }


def get_next_meal(now=None, horizon_days=35):
    """The single soonest upcoming, not-yet-completed occurrence across all
    active slots."""
    now = now or datetime.datetime.now()
    best = None
    for slot in list_meals():
        if not slot["active"]:
            continue
        for offset in range(0, horizon_days):
            day = now.date() + datetime.timedelta(days=offset)
            occ = _effective_occurrence(slot, day)
            if occ and not occ["completed"] and occ["when"] >= now:
                if best is None or occ["when"] < best["when"]:
                    best = occ
                break  # this slot's next hit is found, no need to scan further days for it
    return best


def get_today_meals(now=None):
    """All occurrences for today, in time order, each with a 'done' flag
    (true if manually marked done, or if its time has already passed)."""
    now = now or datetime.datetime.now()
    today = now.date()
    out = []
    for slot in list_meals():
        if not slot["active"]:
            continue
        occ = _effective_occurrence(slot, today)
        if occ:
            occ["done"] = occ["completed"] or occ["when"] < now
            out.append(occ)
    out.sort(key=lambda o: o["when"])
    return out


def get_day_occurrences(day: datetime.date):
    """Everything relevant to `day` for the calendar tab: every slot that
    would run that day per its base rule, INCLUDING cancelled ones (so the
    UI can show 'skipped, tap to restore')."""
    out = []
    for slot in list_meals():
        if not slot["active"] or not _occurs_on(slot, day):
            continue
        exc = get_exception(slot["id"], day.isoformat())
        cancelled = bool(exc and exc["cancelled"])
        merged = slot if cancelled else _apply_exception(slot, exc)
        hh, mm = (int(x) for x in merged["scheduled_time"].split(":"))
        when = datetime.datetime.combine(day, datetime.time(hh, mm))
        out.append({
            "meal": merged,
            "when": when,
            "slot_id": slot["id"],
            "exception_id": exc["id"] if exc else None,
            "has_override": _has_real_override(exc) and not cancelled,
            "cancelled": cancelled,
            "completed": bool(exc["completed"]) if exc else False,
        })
    out.sort(key=lambda o: o["when"])
    return out


def get_month_counts(year: int, month: int):
    """{'YYYY-MM-DD': count} of non-cancelled occurrences, for drawing dots
    on the calendar grid without one request per day."""
    import calendar as _cal
    _, last_day = _cal.monthrange(year, month)
    counts = {}
    for day_num in range(1, last_day + 1):
        day = datetime.date(year, month, day_num)
        occs = [o for o in get_day_occurrences(day) if not o["cancelled"]]
        if occs:
            counts[day.isoformat()] = len(occs)
    return counts


# ----------------------------------------------------------- settings -----

def get_setting(key: str, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )


# --------------------------------------------------------------- theme -----
# A single shared setting drives BOTH the web UI and the physical display,
# so they always show the same theme rather than each picking independently.
# "auto" has no browser/OS dark-mode concept to lean on for the physical
# screen, so it's resolved here from the server's own clock -- the same
# resolution the web UI asks for too, keeping everything in sync.

VALID_THEME_MODES = {"light", "dark", "auto"}
LIGHT_MODE_START_HOUR = 6   # 6:00 AM
LIGHT_MODE_END_HOUR = 19    # 7:00 PM -- dark outside [start, end)


def get_theme_mode() -> str:
    return get_setting("theme_mode", "auto")


def set_theme_mode(mode: str):
    if mode not in VALID_THEME_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_THEME_MODES)}")
    set_setting("theme_mode", mode)


def resolve_theme(mode: str, now=None) -> str:
    """Turn a mode ('light' / 'dark' / 'auto') into an actual 'light' or
    'dark' to render, using the server's local time for 'auto'."""
    if mode in ("light", "dark"):
        return mode
    now = now or datetime.datetime.now()
    return "light" if LIGHT_MODE_START_HOUR <= now.hour < LIGHT_MODE_END_HOUR else "dark"
