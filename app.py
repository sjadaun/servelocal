"""
app.py
Flask HTTP server for the meal planner.

Serves:
  - the web UI at "/"
  - a versioned JSON REST API under /api/v1/*, designed to be dropped straight
    into an Android/iOS app later (consistent response envelope, typed
    fields, CORS enabled, no server-rendered HTML mixed into responses).

Run with: python3 app.py   (defaults to 0.0.0.0:8080)
"""

import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

import database as db

app = Flask(__name__)
CORS(app)  # allow calls from a future mobile app / different origin webview
db.init_db()

CATEGORIES = [
    {"id": "breakfast", "label": "Breakfast"},
    {"id": "lunch", "label": "Lunch"},
    {"id": "dinner", "label": "Dinner"},
    {"id": "school", "label": "School"},
    {"id": "snack", "label": "Snack"},
    {"id": "other", "label": "Other"},
]
VALID_CATEGORY_IDS = {c["id"] for c in CATEGORIES}
VALID_REPEAT_TYPES = {"once", "daily", "weekly"}


# ------------------------------------------------------------- helpers -----

def ok(data=None, status=200):
    return jsonify({"success": True, "data": data}), status


def err(message, status=400):
    return jsonify({"success": False, "error": message}), status


def serialize_meal(meal: dict) -> dict:
    """Turn a raw DB row into clean, typed JSON for API consumers."""
    if meal is None:
        return None
    return {
        "id": meal["id"],
        "name": meal["name"],
        "category": meal["category"],
        "notes": meal["notes"],
        "scheduled_time": meal["scheduled_time"],
        "prep_minutes": meal["prep_minutes"],
        "temperature": meal["temperature"],
        "repeat_type": meal["repeat_type"],
        "repeat_days": [int(d) for d in meal["repeat_days"].split(",") if d != ""],
        "start_date": meal["start_date"],
        "end_date": meal["end_date"] or None,
        "active": bool(meal["active"]),
        "created_at": meal["created_at"],
    }


def validate_payload(data: dict, partial=False):
    """Returns an error string, or None if the payload is valid."""
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

    return None


# ----------------------------------------------------------------- UI -----

@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------ API v1 -----

@app.route("/api/v1/health", methods=["GET"])
def api_health():
    return ok({"status": "ok", "server_time": datetime.datetime.now().isoformat()})


@app.route("/api/v1/categories", methods=["GET"])
def api_categories():
    return ok(CATEGORIES)


@app.route("/api/v1/meals", methods=["GET"])
def api_list_meals():
    meals = [serialize_meal(m) for m in db.list_meals()]
    return ok(meals)


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
    today = db.get_today_meals()
    return ok([
        {**serialize_meal(item["slot"]), "when": item["when"].isoformat(), "done": item["done"]}
        for item in today
    ])


@app.route("/api/v1/next", methods=["GET"])
def api_next():
    nxt = db.get_next_meal()
    if not nxt:
        return ok(None)
    return ok({**serialize_meal(nxt["slot"]), "when": nxt["when"].isoformat()})


@app.errorhandler(404)
def not_found(_e):
    return err("not found", 404)


@app.errorhandler(500)
def server_error(_e):
    return err("internal server error", 500)


if __name__ == "__main__":
    # host 0.0.0.0 so it's reachable from other devices on your LAN
    app.run(host="0.0.0.0", port=8080, debug=False)
