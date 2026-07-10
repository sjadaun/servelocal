"""
app.py
Flask HTTP server for the meal planner.

Serves:
  - the web UI at "/"
  - a versioned JSON REST API under /api/v1/*, designed to be dropped straight
    into an Android/iOS app later (consistent response envelope, typed
    fields, CORS enabled).

Run with: python3 app.py   (defaults to 0.0.0.0:8080)
"""

import calendar as pycalendar
import datetime
import pathlib
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

import database as db

app = Flask(__name__)
CORS(app)
db.init_db()

VERSION_FILE = pathlib.Path(__file__).parent / "version.txt"


def read_version():
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "unknown"


APP_VERSION = read_version()

CATEGORIES = [
    {"id": "breakfast", "label": "Breakfast"},
    {"id": "lunch", "label": "Lunch"},
    {"id": "dinner", "label": "Dinner"},
    {"id": "snack", "label": "Snack"},
    {"id": "other", "label": "Other"},
]
VALID_CATEGORY_IDS = {c["id"] for c in CATEGORIES}
VALID_REPEAT_TYPES = {"once", "daily", "weekly", "monthly"}


# ------------------------------------------------------------- helpers -----

def ok(data=None, status=200):
    return jsonify({"success": True, "data": data}), status


def err(message, status=400):
    return jsonify({"success": False, "error": message}), status


def serialize_meal(meal: dict) -> dict:
    """Turn a raw meal_slots DB row into clean, typed JSON."""
    if meal is None:
        return None
    return {
        "id": meal["id"],
        "name": meal["name"],
        "category": meal["category"],
        "notes": meal["notes"],
        "scheduled_time": meal["scheduled_time"],
        "prep_minutes": meal["prep_minutes"],
        "going_out": bool(meal["going_out"]),
        "going_out_place": meal["going_out_place"],
        "repeat_type": meal["repeat_type"],
        "repeat_days": [int(d) for d in meal["repeat_days"].split(",") if d != ""],
        "start_date": meal["start_date"],
        "end_date": meal["end_date"] or None,
        "active": bool(meal["active"]),
        "created_at": meal["created_at"],
    }


def serialize_occurrence(occ: dict) -> dict:
    """Turn an _effective_occurrence()/get_day_occurrences() dict into JSON.
    `occ["meal"]` may be a merged (possibly overridden) view, so this does
    NOT go through serialize_meal (which expects a raw DB row)."""
    m = occ["meal"]
    data = {
        "slot_id": occ["slot_id"],
        "exception_id": occ.get("exception_id"),
        "has_override": occ.get("has_override", False),
        "cancelled": occ.get("cancelled", False),
        "completed": occ.get("completed", False),
        "when": occ["when"].isoformat(),
        "name": m["name"],
        "category": m["category"],
        "notes": m["notes"],
        "scheduled_time": m["scheduled_time"],
        "prep_minutes": m["prep_minutes"],
        "going_out": bool(m["going_out"]),
        "going_out_place": m["going_out_place"],
    }
    if "done" in occ:
        data["done"] = occ["done"]
    return data


def validate_payload(data: dict, partial=False):
    required = ["name", "category", "scheduled_time", "start_date"]
    if not partial:
        for field in required:
            if not data.get(field):
                return f"'{field}' is required"

    if "category" in data and data["category"] not in VALID_CATEGORY_IDS:
        return f"'category' must be one of {sorted(VALID_CATEGORY_IDS)}"

    if "repeat_type" in data and data["repeat_type"] not in VALID_REPEAT_TYPES:
        return f"'repeat_type' must be one of {sorted(VALID_REPEAT_TYPES)}"

    if data.get("scheduled_time"):
        try:
            hh, mm = data["scheduled_time"].split(":")
            assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        except Exception:
            return "'scheduled_time' must be in 'HH:MM' 24h format"

    for field in ("start_date", "end_date"):
        if data.get(field):
            try:
                datetime.date.fromisoformat(data[field])
            except ValueError:
                return f"'{field}' must be 'YYYY-MM-DD'"

    if data.get("repeat_type") == "weekly" and not data.get("repeat_days"):
        return "'repeat_days' is required when repeat_type is 'weekly'"

    if data.get("going_out") and not data.get("going_out_place"):
        return "'going_out_place' is required when 'going_out' is true"

    return None


def parse_date_param(s):
    try:
        return datetime.date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------- UI -----

@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


# ------------------------------------------------------------ API v1 -----

@app.route("/api/v1/health", methods=["GET"])
def api_health():
    return ok({"status": "ok", "server_time": datetime.datetime.now().isoformat(), "version": APP_VERSION})


@app.route("/api/v1/categories", methods=["GET"])
def api_categories():
    return ok(CATEGORIES)


@app.route("/api/v1/meals", methods=["GET"])
def api_list_meals():
    return ok([serialize_meal(m) for m in db.list_meals()])


@app.route("/api/v1/meals", methods=["POST"])
def api_create_meal():
    data = request.get_json(force=True, silent=True) or {}
    error = validate_payload(data)
    if error:
        return err(error, 422)
    meal_id = db.create_meal(data)
    return ok(serialize_meal(db.get_meal(meal_id)), 201)


@app.route("/api/v1/meals/<int:meal_id>", methods=["GET"])
def api_get_meal(meal_id):
    meal = db.get_meal(meal_id)
    if not meal:
        return err("meal not found", 404)
    return ok(serialize_meal(meal))


@app.route("/api/v1/meals/<int:meal_id>", methods=["PUT", "PATCH"])
def api_update_meal(meal_id):
    existing = db.get_meal(meal_id)
    if not existing:
        return err("meal not found", 404)
    data = request.get_json(force=True, silent=True) or {}
    partial = request.method == "PATCH"
    error = validate_payload(data, partial=partial)
    if error:
        return err(error, 422)
    merged = {**serialize_meal(existing), **data}
    merged["end_date"] = merged.get("end_date") or ""
    db.update_meal(meal_id, merged)
    return ok(serialize_meal(db.get_meal(meal_id)))


@app.route("/api/v1/meals/<int:meal_id>", methods=["DELETE"])
def api_delete_meal(meal_id):
    if not db.get_meal(meal_id):
        return err("meal not found", 404)
    db.delete_meal(meal_id)
    return ok({"deleted": meal_id})


@app.route("/api/v1/today", methods=["GET"])
def api_today():
    return ok([serialize_occurrence(o) for o in db.get_today_meals()])


@app.route("/api/v1/next", methods=["GET"])
def api_next():
    occ = db.get_next_meal()
    return ok(serialize_occurrence(occ) if occ else None)


# ---------------------------------------------------- calendar / days -----

@app.route("/api/v1/calendar", methods=["GET"])
def api_calendar_month():
    """?year=2026&month=7 -> {'YYYY-MM-DD': count} for days with meals."""
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
        assert 1 <= month <= 12
    except (TypeError, ValueError, AssertionError):
        return err("valid 'year' and 'month' query params are required", 422)
    return ok(db.get_month_counts(year, month))


@app.route("/api/v1/calendar/<date_str>", methods=["GET"])
def api_calendar_day(date_str):
    day = parse_date_param(date_str)
    if not day:
        return err("date must be 'YYYY-MM-DD'", 422)
    return ok([serialize_occurrence(o) for o in db.get_day_occurrences(day)])


# ------------------------------------------------- per-occurrence edits ---

@app.route("/api/v1/meals/<int:meal_id>/occurrences/<date_str>", methods=["PUT"])
def api_set_occurrence(meal_id, date_str):
    """Override (or cancel) a single occurrence of a recurring meal, without
    touching the rest of the series. Body may include `cancelled: true`, or
    any subset of: name, category, notes, scheduled_time, prep_minutes,
    going_out, going_out_place."""
    if not db.get_meal(meal_id):
        return err("meal not found", 404)
    day = parse_date_param(date_str)
    if not day:
        return err("date must be 'YYYY-MM-DD'", 422)
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("cancelled"):
        if "category" in data and data["category"] not in VALID_CATEGORY_IDS:
            return err(f"'category' must be one of {sorted(VALID_CATEGORY_IDS)}", 422)
        if data.get("scheduled_time"):
            try:
                hh, mm = data["scheduled_time"].split(":")
                assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
            except Exception:
                return err("'scheduled_time' must be in 'HH:MM' 24h format", 422)
    db.upsert_exception(meal_id, day.isoformat(), data)
    occ = next((o for o in db.get_day_occurrences(day) if o["slot_id"] == meal_id), None)
    return ok(serialize_occurrence(occ) if occ else None)


@app.route("/api/v1/meals/<int:meal_id>/occurrences/<date_str>", methods=["DELETE"])
def api_clear_occurrence(meal_id, date_str):
    """Revert a single day back to the recurring series default."""
    day = parse_date_param(date_str)
    if not day:
        return err("date must be 'YYYY-MM-DD'", 422)
    db.delete_exception(meal_id, day.isoformat())
    occ = next((o for o in db.get_day_occurrences(day) if o["slot_id"] == meal_id), None)
    return ok(serialize_occurrence(occ) if occ else None)


@app.route("/api/v1/meals/<int:meal_id>/occurrences/<date_str>/complete", methods=["POST"])
def api_mark_complete(meal_id, date_str):
    """Manually mark (or, with {"completed": false}, unmark) one occurrence
    as done. Used by the 'Mark as done' button on the Next card -- lets you
    advance past a meal as soon as it's actually made, rather than waiting
    for its scheduled time to pass."""
    if not db.get_meal(meal_id):
        return err("meal not found", 404)
    day = parse_date_param(date_str)
    if not day:
        return err("date must be 'YYYY-MM-DD'", 422)
    data = request.get_json(force=True, silent=True) or {}
    completed = data.get("completed", True)
    db.set_completed(meal_id, day.isoformat(), bool(completed))
    occ = next((o for o in db.get_day_occurrences(day) if o["slot_id"] == meal_id), None)
    return ok(serialize_occurrence(occ) if occ else None)


@app.route("/api/v1/theme", methods=["GET"])
def api_get_theme():
    mode = db.get_theme_mode()
    return ok({"mode": mode, "resolved": db.resolve_theme(mode)})


@app.route("/api/v1/theme", methods=["PUT"])
def api_set_theme():
    """Sets the theme for BOTH the web UI and the physical display -- this
    is one shared setting, not a per-browser preference."""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    if mode not in db.VALID_THEME_MODES:
        return err(f"'mode' must be one of {sorted(db.VALID_THEME_MODES)}", 422)
    db.set_theme_mode(mode)
    return ok({"mode": mode, "resolved": db.resolve_theme(mode)})


@app.errorhandler(404)
def not_found(_e):
    return err("not found", 404)


@app.errorhandler(500)
def server_error(_e):
    return err("internal server error", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
