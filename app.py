import base64
import io
import math
import os
import re
from datetime import date
from functools import wraps

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
MIN_PASSWORD_LENGTH = 6


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
db_pool = MySQLConnectionPool(
    pool_name="fuel_pool",
    pool_size=5,
    pool_reset_session=True,
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "fuel_consumption_db"),
    ssl_disabled=False,
)

def get_db_connection():
    conn = db_pool.get_connection()
    try:
        conn.ping(reconnect=True, attempts=1, delay=0)
    except Exception:
        pass
    return conn


def query(sql, params=()):
    connection = get_db_connection()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        connection.close()


def execute(sql, params=()):
    """Run one write statement. Returns (rowcount, lastrowid)."""
    connection = get_db_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        connection.commit()
        return cursor.rowcount, cursor.lastrowid
    finally:
        if cursor:
            cursor.close()
        connection.close()


def run_transaction(statements):
    """Run several write statements atomically."""
    connection = get_db_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        for sql, params in statements:
            cursor.execute(sql, params)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Authentication and roles
# ---------------------------------------------------------------------------
def get_user(user_id):
    rows = query("SELECT user_id, username, role FROM users WHERE user_id = %s", (user_id,))
    return rows[0] if rows else None


def current_user():
    user_id = session.get("user_id")
    return get_user(user_id) if user_id else None


def start_session(user_id):
    session.clear()
    session["user_id"] = user_id


def login_required(admin_only=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify({"error": "Please sign in first."}), 401
            if admin_only and user["role"] != "admin":
                return jsonify({"error": "Admin access only."}), 403
            g.user = user
            return view(*args, **kwargs)
        return wrapper
    return decorator


def is_admin():
    return g.user["role"] == "admin"


def can_manage(owner_id):
    """Admins can edit anyone's data. Users can edit only what they own."""
    return is_admin() or owner_id == g.user["user_id"]


def scope_user_id():
    """Whose data this request covers.
    Users always get their own data. Admins get one user's data when user_id is
    passed, or None (all users) when it is not."""
    if is_admin():
        raw = request.args.get("user_id", "")
        return int(raw) if raw.isdigit() else None
    return g.user["user_id"]


def vehicle_owner(vehicle_id):
    rows = query("SELECT owner_id FROM vehicles WHERE vehicle_id = %s", (vehicle_id,))
    return rows[0]["owner_id"] if rows else None


def record_owner(record_id):
    rows = query("""SELECT v.owner_id FROM fuel_records fr
                    JOIN vehicles v ON v.vehicle_id = fr.vehicle_id
                    WHERE fr.record_id = %s""", (record_id,))
    return rows[0]["owner_id"] if rows else None


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def get_vehicles(owner_id=None):
    sql = """SELECT v.vehicle_id, v.owner_id, u.username AS owner_name,
                    v.vehicle_code, v.vehicle_type, v.fuel_type,
                    (SELECT odometer_km FROM fuel_records fr
                     WHERE fr.vehicle_id = v.vehicle_id
                     ORDER BY fill_date DESC, record_id DESC LIMIT 1) AS last_odometer,
                    (SELECT fill_date FROM fuel_records fr
                     WHERE fr.vehicle_id = v.vehicle_id
                     ORDER BY fill_date DESC, record_id DESC LIMIT 1) AS last_fill_date
             FROM vehicles v JOIN users u ON u.user_id = v.owner_id"""
    params = ()
    if owner_id is not None:
        sql += " WHERE v.owner_id = %s"
        params = (owner_id,)
    rows = query(sql + " ORDER BY v.vehicle_id", params)
    for r in rows:
        if r.get("last_odometer") is not None:
            r["last_odometer"] = float(r["last_odometer"])
        if r.get("last_fill_date") is not None:
            r["last_fill_date"] = str(r["last_fill_date"])
    return rows


def get_fuel_data(owner_id=None):
    sql = """SELECT fr.record_id, fr.vehicle_id, v.owner_id, u.username AS owner_name,
                    v.vehicle_code, v.vehicle_type, v.fuel_type, fr.fill_date,
                    fr.odometer_km, fr.litres, fr.total_cost, fr.full_tank_flag
             FROM fuel_records fr
             JOIN vehicles v ON fr.vehicle_id = v.vehicle_id
             JOIN users u ON u.user_id = v.owner_id"""
    params = ()
    if owner_id is not None:
        sql += " WHERE v.owner_id = %s"
        params = (owner_id,)
    return query(sql + " ORDER BY fr.vehicle_id, fr.fill_date, fr.record_id", params)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------
def num(value):
    """Float or None (NaN / inf / non-numeric become None) so JSON stays valid."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def ratio(numerator, denominator, digits=2):
    """numerator / denominator, or None when it cannot be calculated."""
    a, b = num(numerator), num(denominator)
    if a is None or b is None or b <= 0:
        return None
    return round(float(np.divide(a, b)), digits)


def rounded(value, digits=2):
    value = num(value)
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Analytics engine (formulas unchanged from the original prototype)
# ---------------------------------------------------------------------------
def validate_row(row, last_valid_odometer):
    litres, cost, odometer = row["litres"], row["total_cost"], row["odometer_km"]

    if pd.isna(litres) or litres <= 0:
        return "Litres must be greater than zero"
    if pd.isna(odometer) or odometer < 0:
        return "Odometer reading is missing or negative"
    if pd.isna(cost) or cost < 0:
        return "Total cost is missing or negative"
    if last_valid_odometer is not None and odometer <= last_valid_odometer:
        return (f"Odometer {odometer:g} km is not greater than the previous "
                f"valid reading ({last_valid_odometer:g} km)")
    return None


PAIR_COLUMNS = ["vehicle_id", "vehicle_code", "month", "distance", "litres", "cost"]
SEGMENT_COLUMNS = ["vehicle_id", "vehicle_code", "month", "start_date", "end_date",
                   "distance", "litres", "cost", "partial_fills"]


def analyze(owner_id=None):
    rows = get_fuel_data(owner_id)
    vehicles = get_vehicles(owner_id)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["fill_date"] = pd.to_datetime(df["fill_date"])
        for column in ("odometer_km", "litres", "total_cost"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["full_tank_flag"] = pd.to_numeric(df["full_tank_flag"], errors="coerce").fillna(0).astype(int)
        df["month"] = df["fill_date"].dt.strftime("%Y-%m")
        df = df.sort_values(["vehicle_id", "fill_date", "record_id"]).reset_index(drop=True)

    annotations = {}
    invalid = []
    pairs = []
    segments = []

    for _, group in (df.groupby("vehicle_id", sort=False) if not df.empty else []):
        last_valid_odometer = None
        previous = None
        anchor = None
        fuel_since = cost_since = 0.0
        partial_fills = 0

        for _, r in group.iterrows():
            record_id = int(r["record_id"])
            reason = validate_row(r, last_valid_odometer)

            if reason:
                annotations[record_id] = {"status": "invalid", "reason": reason,
                                          "distance": None, "mileage": None, "basis": None}
                invalid.append({
                    "record_id": record_id,
                    "owner": r["owner_name"],
                    "vehicle_code": r["vehicle_code"],
                    "fill_date": r["fill_date"].strftime("%Y-%m-%d"),
                    "odometer_km": rounded(r["odometer_km"]),
                    "litres": rounded(r["litres"], 3),
                    "total_cost": rounded(r["total_cost"]),
                    "full_tank": bool(r["full_tank_flag"]),
                    "reason": reason,
                })
                previous = anchor = None
                fuel_since = cost_since = 0.0
                partial_fills = 0
                continue

            odometer = float(r["odometer_km"])
            litres = float(r["litres"])
            cost = float(r["total_cost"])
            last_valid_odometer = odometer
            note = {"status": "valid", "reason": None,
                    "distance": None, "mileage": None, "basis": None}

            if previous is not None:
                distance = odometer - previous["odometer"]
                note["distance"] = round(distance, 2)
                note["basis"] = "Approximate"
                note["mileage"] = ratio(distance, litres)
                pairs.append({"vehicle_id": int(r["vehicle_id"]), "vehicle_code": r["vehicle_code"],
                              "month": r["month"], "distance": distance,
                              "litres": litres, "cost": cost})

            if r["full_tank_flag"] == 1:
                if anchor is not None:
                    distance = odometer - anchor["odometer"]
                    seg_litres = fuel_since + litres
                    segments.append({
                        "vehicle_id": int(r["vehicle_id"]), "vehicle_code": r["vehicle_code"],
                        "month": r["month"],
                        "start_date": anchor["date"].strftime("%Y-%m-%d"),
                        "end_date": r["fill_date"].strftime("%Y-%m-%d"),
                        "distance": distance, "litres": seg_litres,
                        "cost": cost_since + cost, "partial_fills": partial_fills,
                    })
                    note["basis"] = "Full-tank"
                    note["distance"] = round(distance, 2)
                    note["mileage"] = ratio(distance, seg_litres)
                anchor = {"odometer": odometer, "date": r["fill_date"]}
                fuel_since = cost_since = 0.0
                partial_fills = 0
            elif anchor is not None:
                fuel_since += litres
                cost_since += cost
                partial_fills += 1

            annotations[record_id] = note
            previous = {"odometer": odometer}

    pairs_df = pd.DataFrame(pairs, columns=PAIR_COLUMNS)
    segments_df = pd.DataFrame(segments, columns=SEGMENT_COLUMNS)
    for frame in (pairs_df, segments_df):
        for column in ("distance", "litres", "cost"):
            frame[column] = frame[column].astype(float)

    if not segments_df.empty:
        distance_arr = segments_df["distance"].to_numpy(dtype=float)
        litres_arr = segments_df["litres"].to_numpy(dtype=float)
        cost_arr = segments_df["cost"].to_numpy(dtype=float)
        segments_df["mileage"] = np.divide(distance_arr, litres_arr,
                                           out=np.full(len(segments_df), np.nan), where=litres_arr > 0)
        segments_df["cost_per_km"] = np.divide(cost_arr, distance_arr,
                                               out=np.full(len(segments_df), np.nan), where=distance_arr > 0)
    else:
        segments_df["mileage"] = pd.Series(dtype=float)
        segments_df["cost_per_km"] = pd.Series(dtype=float)

    return df, vehicles, annotations, invalid, pairs_df, segments_df


def efficiency(seg, pr):
    """Full-tank and approximate metrics for a subset of segments / pairs."""
    ft_distance = float(seg["distance"].sum()) if len(seg) else 0.0
    ft_litres = float(seg["litres"].sum()) if len(seg) else 0.0
    ft_cost = float(seg["cost"].sum()) if len(seg) else 0.0
    ap_distance = float(pr["distance"].sum()) if len(pr) else 0.0
    ap_litres = float(pr["litres"].sum()) if len(pr) else 0.0
    ap_cost = float(pr["cost"].sum()) if len(pr) else 0.0

    return {
        "fulltank_segments": int(len(seg)),
        "fulltank_distance": round(ft_distance, 2),
        "fulltank_litres": round(ft_litres, 2),
        "fulltank_mileage": ratio(ft_distance, ft_litres),
        "fulltank_cost_per_km": ratio(ft_cost, ft_distance),
        "approx_pairs": int(len(pr)),
        "approx_distance": round(ap_distance, 2),
        "approx_mileage": ratio(ap_distance, ap_litres),
        "approx_cost_per_km": ratio(ap_cost, ap_distance),
    }


def spend_rows(frame):
    """Rows that represent a real purchase (used for expense totals)."""
    return frame[(frame["litres"] > 0) & (frame["total_cost"] >= 0)]


def calculate_analytics(owner_id=None):
    df, vehicles, annotations, invalid, pairs_df, segments_df = analyze(owner_id)

    empty_eff = efficiency(segments_df, pairs_df)
    if df.empty:
        return {
            "vehicles": [], "monthly": [], "segments": [], "invalid_readings": [],
            "monthly_vehicle_mileage": {"months": [], "vehicles": [], "values": []},
            "totals": {**empty_eff, "total_litres": 0, "total_cost": 0,
                       "avg_price_per_litre": None, "total_records": 0,
                       "valid_records": 0, "invalid_records": 0,
                       "total_vehicles": len(vehicles),
                       "total_distance": 0, "average_mileage": None, "cost_per_km": None},
        }

    spend = spend_rows(df)
    total_litres = float(spend["litres"].sum())
    total_cost = float(spend["total_cost"].sum())

    vehicle_results = []
    for vehicle in vehicles:
        vid = vehicle["vehicle_id"]
        sub = df[df["vehicle_id"] == vid]
        sub_spend = spend_rows(sub)
        eff = efficiency(segments_df[segments_df["vehicle_id"] == vid],
                         pairs_df[pairs_df["vehicle_id"] == vid])
        invalid_count = sum(1 for item in invalid if item["vehicle_code"] == vehicle["vehicle_code"]
                            and item["owner"] == vehicle["owner_name"])
        litres = float(sub_spend["litres"].sum())
        cost = float(sub_spend["total_cost"].sum())

        vehicle_results.append({
            "vehicle_id": int(vid),
            "vehicle_code": vehicle["vehicle_code"],
            "vehicle_type": vehicle["vehicle_type"],
            "fuel_type": vehicle["fuel_type"],
            "records": int(len(sub)),
            "invalid_records": invalid_count,
            "total_litres": round(litres, 2),
            "total_cost": round(cost, 2),
            "avg_price_per_litre": ratio(cost, litres),
            **eff,
            "total_distance": eff["approx_distance"],
            "average_mileage": eff["fulltank_mileage"],
        })

    ranked = sorted((v for v in vehicle_results if v["fulltank_mileage"] is not None),
                    key=lambda v: v["fulltank_mileage"], reverse=True)
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position
    for item in vehicle_results:
        item.setdefault("rank", None)

    months = sorted(set(df["month"]) | set(segments_df["month"]) | set(pairs_df["month"]))
    monthly_results = []
    for month in months:
        month_spend = spend_rows(df[df["month"] == month])
        eff = efficiency(segments_df[segments_df["month"] == month],
                         pairs_df[pairs_df["month"] == month])
        litres = float(month_spend["litres"].sum())
        cost = float(month_spend["total_cost"].sum())
        monthly_results.append({
            "month": month,
            "records": int((df["month"] == month).sum()),
            "total_litres": round(litres, 2),
            "total_cost": round(cost, 2),
            "avg_price_per_litre": ratio(cost, litres),
            **eff,
        })

    codes = [v["vehicle_code"] for v in vehicles]
    matrix = []
    for month in months:
        row = []
        for code in codes:
            seg = segments_df[(segments_df["month"] == month) & (segments_df["vehicle_code"] == code)]
            row.append(ratio(seg["distance"].sum(), seg["litres"].sum()) if len(seg) else None)
        matrix.append(row)

    segment_rows = [
        {**{k: rounded(s[k], 3) if k in ("distance", "litres", "cost") else s[k]
            for k in ("vehicle_code", "start_date", "end_date", "distance",
                      "litres", "cost", "partial_fills")},
         "mileage": rounded(s["mileage"]), "cost_per_km": rounded(s["cost_per_km"])}
        for _, s in segments_df.sort_values("end_date", ascending=False).iterrows()
    ]

    totals_eff = efficiency(segments_df, pairs_df)
    totals = {
        **totals_eff,
        "total_litres": round(total_litres, 2),
        "total_cost": round(total_cost, 2),
        "avg_price_per_litre": ratio(total_cost, total_litres),
        "total_records": int(len(df)),
        "valid_records": int(len(df) - len(invalid)),
        "invalid_records": len(invalid),
        "total_vehicles": len(vehicles),
        "total_distance": totals_eff["approx_distance"],
        "average_mileage": totals_eff["fulltank_mileage"],
        "cost_per_km": totals_eff["fulltank_cost_per_km"],
    }

    return {
        "vehicles": vehicle_results,
        "monthly": monthly_results,
        "monthly_vehicle_mileage": {"months": months, "vehicles": codes, "values": matrix},
        "segments": segment_rows,
        "invalid_readings": invalid,
        "totals": totals,
    }


def user_overview():
    """Return a lightweight summary of all user accounts."""
    users = query("""
        SELECT
            u.user_id,
            u.username,
            u.role,
            u.created_at,
            COUNT(DISTINCT v.vehicle_id) AS total_vehicles,
            COUNT(DISTINCT fr.record_id) AS total_records,
            COALESCE(SUM(fr.litres), 0) AS total_litres,
            COALESCE(SUM(fr.total_cost), 0) AS total_cost
        FROM users u
        LEFT JOIN vehicles v ON v.owner_id = u.user_id
        LEFT JOIN fuel_records fr ON fr.vehicle_id = v.vehicle_id
        GROUP BY u.user_id, u.username, u.role, u.created_at
        ORDER BY u.user_id
    """)

    result = []
    for user in users:
        result.append({
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "created_at": str(user["created_at"]),
            "total_vehicles": user["total_vehicles"],
            "total_records": user["total_records"],
            "total_litres": float(user["total_litres"]),
            "total_cost": float(user["total_cost"]),
            "fulltank_mileage": 0,
            "invalid_records": 0,
        })

    return result


# ---------------------------------------------------------------------------
# Validation helpers for fuel records
# ---------------------------------------------------------------------------
def parse_record(data):
    try:
        fill_date = date.fromisoformat(str(data["fill_date"]))
        odometer_km = float(data["odometer_km"])
        litres = float(data["litres"])
        total_cost = float(data["total_cost"])
    except (KeyError, TypeError, ValueError):
        return None, "Enter valid values in all required fields."

    full_tank_flag = 1 if data.get("full_tank_flag", True) else 0
    if not all(math.isfinite(x) for x in (odometer_km, litres, total_cost)):
        return None, "Enter valid numeric values."
    if litres <= 0:
        return None, "Fuel quantity must be greater than zero."
    if odometer_km < 0 or total_cost < 0:
        return None, "Odometer reading and cost cannot be negative."
    if fill_date > date.today():
        return None, "Fill date cannot be in the future."

    return {"fill_date": fill_date, "odometer_km": odometer_km, "litres": litres,
            "total_cost": total_cost, "full_tank_flag": full_tank_flag}, None


def odometer_conflict(cursor, vehicle_id, fill_date, odometer_km, exclude_record_id=0):
    """Checks the odometer against the neighbouring readings of the same vehicle."""
    if exclude_record_id:
        cursor.execute("""
            SELECT fill_date, odometer_km FROM fuel_records
            WHERE vehicle_id = %s AND record_id <> %s
              AND (fill_date < %s OR (fill_date = %s AND record_id < %s))
            ORDER BY fill_date DESC, record_id DESC LIMIT 1
        """, (vehicle_id, exclude_record_id, fill_date, fill_date, exclude_record_id))
        previous = cursor.fetchone()

        cursor.execute("""
            SELECT fill_date, odometer_km FROM fuel_records
            WHERE vehicle_id = %s AND record_id <> %s
              AND (fill_date > %s OR (fill_date = %s AND record_id > %s))
            ORDER BY fill_date ASC, record_id ASC LIMIT 1
        """, (vehicle_id, exclude_record_id, fill_date, fill_date, exclude_record_id))
        following = cursor.fetchone()
    else:
        cursor.execute("""
            SELECT fill_date, odometer_km FROM fuel_records
            WHERE vehicle_id = %s AND fill_date <= %s
            ORDER BY fill_date DESC, record_id DESC LIMIT 1
        """, (vehicle_id, fill_date))
        previous = cursor.fetchone()

        cursor.execute("""
            SELECT fill_date, odometer_km FROM fuel_records
            WHERE vehicle_id = %s AND fill_date > %s
            ORDER BY fill_date ASC, record_id ASC LIMIT 1
        """, (vehicle_id, fill_date))
        following = cursor.fetchone()

    if previous and odometer_km <= float(previous["odometer_km"]):
        return (f"Odometer must be greater than the previous reading "
                f"({float(previous['odometer_km']):g} km on {previous['fill_date']}). "
                "If that earlier reading is wrong, correct it first.")
    if following and odometer_km >= float(following["odometer_km"]):
        return (f"Odometer must be less than the next reading "
                f"({float(following['odometer_km']):g} km on {following['fill_date']}). "
                "Readings must follow date order.")
    return None


def scoped_analytics_or_error():
    owner = scope_user_id()
    if owner is None:
        return None, (jsonify({"error": "Choose a user to view analytics."}), 400)
    return owner, None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/login")
@app.route("/signup")
def home():
    return render_template("index.html")


@app.route("/api/health")
def health():
    try:
        connection = get_db_connection()
        connection.close()
        return jsonify({"status": "connected"})
    except mysql.connector.Error as error:
        return jsonify({"error": str(error)}), 500


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
@app.route("/api/auth/me")
def me():
    return jsonify({"user": current_user()})


@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not USERNAME_PATTERN.match(username):
        return jsonify({"error": "Username must be 3-50 characters: letters, numbers, dot, dash or underscore."}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."}), 400

    already = "Username already exists. Please choose another."
    if query("SELECT 1 FROM users WHERE username = %s", (username,)):
        return jsonify({"error": already}), 409

    try:
        _, user_id = execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'user')",
            (username, generate_password_hash(password)))
    except mysql.connector.IntegrityError:
        return jsonify({"error": already}), 409

    start_session(user_id)
    return jsonify({"user": get_user(user_id)}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    rows = query("SELECT user_id, password_hash FROM users WHERE username = %s", (username,))
    if not rows or not check_password_hash(rows[0]["password_hash"], password):
        return jsonify({"error": "Incorrect username or password."}), 401

    start_session(rows[0]["user_id"])
    return jsonify({"user": get_user(rows[0]["user_id"])})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Signed out."})


# ---------------------------------------------------------------------------
# Users API
# ---------------------------------------------------------------------------
@app.route("/api/users", methods=["GET"])
@login_required(admin_only=True)
def list_users():
    return jsonify(user_overview())


@app.route("/api/users/<int:user_id>", methods=["PUT", "DELETE"])
@login_required()
def manage_user(user_id):
    if request.method == "DELETE":
        if not is_admin():
            return jsonify({"error": "Admin access only."}), 403
        if user_id == g.user["user_id"]:
            return jsonify({"error": "You cannot delete your own admin account."}), 400
        if not query("SELECT 1 FROM users WHERE user_id = %s", (user_id,)):
            return jsonify({"error": "User not found."}), 404
        run_transaction([
            ("""DELETE fr FROM fuel_records fr
                JOIN vehicles v ON v.vehicle_id = fr.vehicle_id
                WHERE v.owner_id = %s""", (user_id,)),
            ("DELETE FROM vehicles WHERE owner_id = %s", (user_id,)),
            ("DELETE FROM users WHERE user_id = %s", (user_id,)),
        ])
        return jsonify({"message": "User account and all its data deleted."})

    # PUT: users can edit themselves; admins can edit anyone
    is_self = user_id == g.user["user_id"]
    if not is_self and not is_admin():
        return jsonify({"error": "You can only edit your own account."}), 403

    rows = query("SELECT user_id, username, password_hash, role FROM users WHERE user_id = %s", (user_id,))
    if not rows:
        return jsonify({"error": "User not found."}), 404
    target = rows[0]

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", target["username"])).strip()
    if not USERNAME_PATTERN.match(username):
        return jsonify({"error": "Username must be 3-50 characters: letters, numbers, dot, dash or underscore."}), 400

    updates = {"username": username}

    new_password = str(data.get("new_password") or "")
    if new_password:
        if len(new_password) < MIN_PASSWORD_LENGTH:
            return jsonify({"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."}), 400
        if is_self and not check_password_hash(target["password_hash"], str(data.get("current_password") or "")):
            return jsonify({"error": "Current password is incorrect."}), 400
        updates["password_hash"] = generate_password_hash(new_password)

    if "role" in data and is_admin():
        new_role = data["role"]
        if new_role not in ("admin", "user"):
            return jsonify({"error": "Role must be admin or user."}), 400
        if is_self and new_role != target["role"]:
            return jsonify({"error": "You cannot change your own role."}), 400
        updates["role"] = new_role

    if username != target["username"] and query(
            "SELECT 1 FROM users WHERE username = %s AND user_id <> %s", (username, user_id)):
        return jsonify({"error": "Username already exists. Please choose another."}), 409

    set_clause = ", ".join(f"{column} = %s" for column in updates)  # column names are fixed above
    try:
        execute(f"UPDATE users SET {set_clause} WHERE user_id = %s", (*updates.values(), user_id))
    except mysql.connector.IntegrityError:
        return jsonify({"error": "Username already exists. Please choose another."}), 409

    return jsonify({"message": "Account updated."})


# ---------------------------------------------------------------------------
# Vehicles API
# ---------------------------------------------------------------------------
@app.route("/api/vehicles", methods=["GET", "POST"])
@login_required()
def vehicles():
    if request.method == "GET":
        return jsonify(get_vehicles(scope_user_id()))

    data = request.get_json(silent=True) or {}
    vehicle_code = str(data.get("vehicle_code", "")).strip()
    vehicle_type = str(data.get("vehicle_type", "")).strip()
    fuel_type = str(data.get("fuel_type", "")).strip()
    if not vehicle_code or not vehicle_type or not fuel_type:
        return jsonify({"error": "All vehicle fields are required."}), 400
    if len(vehicle_code) > 30:
        return jsonify({"error": "Vehicle code must be 30 characters or fewer."}), 400

    owner_id = g.user["user_id"]
    if is_admin() and data.get("owner_id") not in (None, ""):
        try:
            owner_id = int(data["owner_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "Choose a valid owner."}), 400
    if not query("SELECT 1 FROM users WHERE user_id = %s", (owner_id,)):
        return jsonify({"error": "Selected owner does not exist."}), 400

    try:
        execute(
            "INSERT INTO vehicles (owner_id, vehicle_code, vehicle_type, fuel_type) VALUES (%s, %s, %s, %s)",
            (owner_id, vehicle_code, vehicle_type, fuel_type))
        return jsonify({"message": "Vehicle added successfully."}), 201
    except mysql.connector.IntegrityError:
        return jsonify({"error": "This owner already has a vehicle with that code."}), 409


@app.route("/api/vehicles/<int:vehicle_id>", methods=["PUT", "DELETE"])
@login_required()
def manage_vehicle(vehicle_id):
    owner_id = vehicle_owner(vehicle_id)
    if owner_id is None:
        return jsonify({"error": "Vehicle not found."}), 404
    if not can_manage(owner_id):
        return jsonify({"error": "You can only change your own vehicles."}), 403

    if request.method == "DELETE":
        count = query("SELECT COUNT(*) AS n FROM fuel_records WHERE vehicle_id = %s", (vehicle_id,))[0]["n"]
        if count:
            return jsonify({"error": f"This vehicle has {count} fuel record(s). Delete them first."}), 409
        execute("DELETE FROM vehicles WHERE vehicle_id = %s", (vehicle_id,))
        return jsonify({"message": "Vehicle deleted."})

    data = request.get_json(silent=True) or {}
    vehicle_code = str(data.get("vehicle_code", "")).strip()
    vehicle_type = str(data.get("vehicle_type", "")).strip()
    fuel_type = str(data.get("fuel_type", "")).strip()
    if not vehicle_code or not vehicle_type or not fuel_type:
        return jsonify({"error": "All vehicle fields are required."}), 400
    if len(vehicle_code) > 30:
        return jsonify({"error": "Vehicle code must be 30 characters or fewer."}), 400

    try:
        execute("""UPDATE vehicles SET vehicle_code = %s, vehicle_type = %s, fuel_type = %s
                   WHERE vehicle_id = %s""", (vehicle_code, vehicle_type, fuel_type, vehicle_id))
    except mysql.connector.IntegrityError:
        return jsonify({"error": "This owner already has a vehicle with that code."}), 409
    return jsonify({"message": "Vehicle updated."})


# ---------------------------------------------------------------------------
# Fuel records API
# ---------------------------------------------------------------------------
@app.route("/api/fuel-records", methods=["GET", "POST"])
@login_required()
def fuel_records():
    if request.method == "GET":
        owner = scope_user_id()
        df, _, annotations, _, _, _ = analyze(owner)
        rows = df.to_dict("records")
        rows.sort(key=lambda r: (r["fill_date"], r["record_id"]), reverse=True)
        for row in rows:
            note = annotations.get(row["record_id"], {})
            row["fill_date"] = row["fill_date"].strftime("%Y-%m-%d")
            row["odometer_km"] = float(row["odometer_km"])
            row["litres"] = float(row["litres"])
            row["total_cost"] = float(row["total_cost"])
            row["full_tank_flag"] = int(row["full_tank_flag"])
            row["status"] = note.get("status")
            row["reason"] = note.get("reason")
            row["distance"] = note.get("distance")
            row["mileage"] = note.get("mileage")
            row["basis"] = note.get("basis")
        return jsonify(rows)

    data = request.get_json(silent=True) or {}
    try:
        vehicle_id = int(data["vehicle_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Choose a vehicle."}), 400

    record, error = parse_record(data)
    if error:
        return jsonify({"error": error}), 400

    owner_id = vehicle_owner(vehicle_id)
    if owner_id is None:
        return jsonify({"error": "Selected vehicle does not exist."}), 400
    if not can_manage(owner_id):
        return jsonify({"error": "You can only add records to your own vehicles."}), 403

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        conflict = odometer_conflict(cursor, vehicle_id, record["fill_date"], record["odometer_km"])
        if conflict:
            return jsonify({"error": conflict}), 400
        cursor.execute("""INSERT INTO fuel_records
                          (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
                          VALUES (%s, %s, %s, %s, %s, %s)""",
                       (vehicle_id, record["fill_date"], record["odometer_km"], record["litres"],
                        record["total_cost"], record["full_tank_flag"]))
        connection.commit()
        return jsonify({"message": "Fuel record added successfully."}), 201
    finally:
        cursor.close()
        connection.close()


@app.route("/api/fuel-records/<int:record_id>", methods=["PUT", "DELETE"])
@login_required()
def manage_fuel_record(record_id):
    owner_id = record_owner(record_id)
    if owner_id is None:
        return jsonify({"error": "Record not found."}), 404
    if not can_manage(owner_id):
        return jsonify({"error": "You can only change your own records."}), 403

    if request.method == "DELETE":
        execute("DELETE FROM fuel_records WHERE record_id = %s", (record_id,))
        return jsonify({"message": "Fuel record deleted."})

    data = request.get_json(silent=True) or {}
    record, error = parse_record(data)
    if error:
        return jsonify({"error": error}), 400

    vehicle_id = query("SELECT vehicle_id FROM fuel_records WHERE record_id = %s", (record_id,))[0]["vehicle_id"]

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        conflict = odometer_conflict(cursor, vehicle_id, record["fill_date"], record["odometer_km"],
                                     exclude_record_id=record_id)
        if conflict:
            return jsonify({"error": conflict}), 400
        cursor.execute("""UPDATE fuel_records
                          SET fill_date = %s, odometer_km = %s, litres = %s,
                              total_cost = %s, full_tank_flag = %s
                          WHERE record_id = %s""",
                       (record["fill_date"], record["odometer_km"], record["litres"],
                        record["total_cost"], record["full_tank_flag"], record_id))
        connection.commit()
        return jsonify({"message": "Fuel record updated."})
    finally:
        cursor.close()
        connection.close()


# ---------------------------------------------------------------------------
# Analytics, invalid readings and exports
# ---------------------------------------------------------------------------
@app.route("/api/analytics")
@login_required()
def analytics():
    owner, error = scoped_analytics_or_error()
    if error:
        return error

    try:
        data = calculate_analytics(owner)

        if request.args.get("include_images") == "1":
            monthly = data["monthly"]
            vehicle_rows = data["vehicles"]
            pivot = data["monthly_vehicle_mileage"]

            month_labels = [row["month"] for row in monthly]
            codes = [row["vehicle_code"] for row in vehicle_rows]

            data["charts"] = {
                "monthly_cost": bar_chart(
                    month_labels,
                    [r["total_cost"] for r in monthly],
                    "Monthly Fuel Cost", "Cost (₹)", decimals=0
                ),
                "monthly_fuel": bar_chart(
                    month_labels,
                    [r["total_litres"] for r in monthly],
                    "Monthly Fuel Consumption", "Fuel (litres)", decimals=1
                ),
                "monthly_mileage": line_chart(
                    pivot["months"], pivot["vehicles"], pivot["values"],
                    "Monthly Mileage Trend (full-tank)", "km per litre"
                ),
                "vehicle_mileage": grouped_bar_chart(
                    codes,
                    {
                        "Full-tank (accurate)": [
                            r["fulltank_mileage"] for r in vehicle_rows
                        ],
                        "Approximate": [
                            r["approx_mileage"] for r in vehicle_rows
                        ],
                    },
                    "Vehicle Mileage Comparison", "km per litre", decimals=1
                ),
                "vehicle_cost_per_km": bar_chart(
                    codes,
                    [r["fulltank_cost_per_km"] for r in vehicle_rows],
                    "Fuel Cost per km (full-tank)", "₹ per km", decimals=2
                ),
                "vehicle_cost": bar_chart(
                    codes,
                    [r["total_cost"] for r in vehicle_rows],
                    "Vehicle-wise Fuel Cost", "Cost (₹)", decimals=0
                ),
            }
        else:
            data["charts"] = {}

        return jsonify(data)

    except Exception as exc:
        app.logger.exception("Analytics error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/invalid-readings")
@login_required()
def invalid_readings():
    _, _, _, invalid, _, _ = analyze(scope_user_id())
    return jsonify(invalid)


EXPORTS = {
    "vehicle-mileage": ("vehicles", "vehicle_mileage_report.csv"),
    "monthly-summary": ("monthly", "monthly_fuel_summary.csv"),
    "invalid-readings": ("invalid_readings", "invalid_readings_report.csv"),
    "full-tank-segments": ("segments", "full_tank_segments.csv"),
}


@app.route("/api/export/<name>.csv")
@login_required()
def export_csv(name):
    if name not in EXPORTS:
        return jsonify({"error": "Unknown report."}), 404
    key, filename = EXPORTS[name]
    owner = scope_user_id()
    try:
        if key == "invalid_readings":
            rows = analyze(owner)[3]
        else:
            if owner is None:
                return jsonify({"error": "Choose a user to export."}), 400
            rows = calculate_analytics(owner)[key]
        frame = pd.DataFrame(rows)
        return Response(frame.to_csv(index=False), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as exc:
        app.logger.exception("Export error")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Matplotlib charts (used on the Analytics page)
# ---------------------------------------------------------------------------
def figure_to_base64(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120, bbox_inches="tight",
                pad_inches=0.25, facecolor="white")
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


CHART_COLORS = {"navy": "#102a43", "muted": "#526575", "amber": "#e0a030"}
SERIES_COLORS = ["#146b67", "#e0a030", "#2e86ab", "#c0392b", "#7d5ba6", "#5b8c3a", "#d35400"]
TEAL_SCALE = LinearSegmentedColormap.from_list("teal_scale", ["#8bd3c7", "#146b67", "#0d514e"])

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#d5dee5",
    "axes.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlecolor": CHART_COLORS["navy"],
    "axes.titlepad": 14,
    "axes.labelcolor": CHART_COLORS["muted"],
    "axes.labelsize": 10,
    "grid.color": "#e6edf2",
    "grid.linewidth": 0.8,
    "xtick.color": CHART_COLORS["muted"],
    "ytick.color": CHART_COLORS["muted"],
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.frameon": False,
    "legend.fontsize": 9,
})


def to_array(values):
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def style_axes(ax, title, ylabel, decimals=0):
    ax.set_title(title, loc="left")
    ax.set_ylabel(ylabel)
    ax.xaxis.grid(False)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.{decimals}f}"))


def empty_state(ax, message):
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=11,
            color=CHART_COLORS["muted"], transform=ax.transAxes)


def bar_chart(labels, values, title, ylabel, decimals=0):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    data = to_array(values)

    if len(labels) and np.isfinite(data).any():
        x = np.arange(len(labels))
        heights = np.where(np.isfinite(data), data, 0.0)
        colors = TEAL_SCALE(np.linspace(0.2, 0.9, len(labels)))
        bars = ax.bar(x, heights, width=0.62, color=colors, zorder=3)

        top = float(np.nanmax(data))
        ax.set_ylim(0, (top if top > 0 else 1) * 1.3)
        ax.bar_label(bars, labels=[f"{v:,.{decimals}f}" if np.isfinite(v) else "" for v in data],
                     padding=3, fontsize=8.5, color=CHART_COLORS["navy"], fontweight="semibold")

        if len(labels) > 1:
            average = float(np.nanmean(data))
            ax.axhline(average, color=CHART_COLORS["amber"], linestyle=(0, (4, 3)),
                       linewidth=1.4, zorder=4)
            ax.legend(handles=[Line2D([], [], color=CHART_COLORS["amber"], linestyle="--",
                                      label=f"Average: {average:,.{decimals}f}")],
                      loc="upper right")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right")
    else:
        empty_state(ax, "No data available")

    style_axes(ax, title, ylabel, decimals)
    fig.tight_layout()
    return figure_to_base64(fig)


def grouped_bar_chart(labels, series, title, ylabel, decimals=1):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    arrays = {name: to_array(vals) for name, vals in series.items()}

    if len(labels) and any(np.isfinite(a).any() for a in arrays.values()):
        x = np.arange(len(labels))
        width = 0.8 / len(arrays)
        top = max(float(np.nanmax(a)) for a in arrays.values() if np.isfinite(a).any())
        ax.set_ylim(0, (top if top > 0 else 1) * 1.3)

        for i, (name, data) in enumerate(arrays.items()):
            offset = x + i * width - 0.4 + width / 2
            bars = ax.bar(offset, np.where(np.isfinite(data), data, 0.0), width * 0.92,
                          label=name, color=SERIES_COLORS[i % len(SERIES_COLORS)], zorder=3)
            ax.bar_label(bars, labels=[f"{v:,.{decimals}f}" if np.isfinite(v) else "" for v in data],
                         padding=2, fontsize=8, color=CHART_COLORS["navy"], fontweight="semibold")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.legend(loc="upper right", ncol=len(arrays))
    else:
        empty_state(ax, "No data available")

    style_axes(ax, title, ylabel, decimals)
    fig.tight_layout()
    return figure_to_base64(fig)


def line_chart(months, codes, values, title, ylabel):
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    matrix = np.array([[np.nan if v is None else v for v in row] for row in values], dtype=float)

    if matrix.size and np.isfinite(matrix).any():
        x = np.arange(len(months))
        for index, code in enumerate(codes):
            column = matrix[:, index]
            if np.isfinite(column).any():
                ax.plot(x, column, color=SERIES_COLORS[index % len(SERIES_COLORS)],
                        linewidth=2.4, marker="o", markersize=7, markerfacecolor="white",
                        markeredgewidth=2, label=code, zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(months, rotation=30, ha="right")
        ax.legend(title="Vehicle", loc="upper left", bbox_to_anchor=(1.01, 1))
    else:
        empty_state(ax, "No full-tank mileage available yet")

    style_axes(ax, title, ylabel, decimals=1)
    fig.tight_layout()
    return figure_to_base64(fig)


@app.route("/api/analytics/charts")
@login_required()
def analytics_charts():
    owner, error = scoped_analytics_or_error()
    if error:
        return error
    try:
        data = calculate_analytics(owner)
        monthly, vehicle_rows = data["monthly"], data["vehicles"]
        pivot = data["monthly_vehicle_mileage"]
        month_labels = [row["month"] for row in monthly]
        codes = [row["vehicle_code"] for row in vehicle_rows]

        charts = {
            "monthly_cost": bar_chart(month_labels, [r["total_cost"] for r in monthly],
                                      "Monthly Fuel Cost", "Cost (₹)", decimals=0),
            "monthly_fuel": bar_chart(month_labels, [r["total_litres"] for r in monthly],
                                      "Monthly Fuel Consumption", "Fuel (litres)", decimals=1),
            "monthly_mileage": line_chart(pivot["months"], pivot["vehicles"], pivot["values"],
                                          "Monthly Mileage Trend (full-tank)", "km per litre"),
            "vehicle_mileage": grouped_bar_chart(
                codes,
                {"Full-tank (accurate)": [r["fulltank_mileage"] for r in vehicle_rows],
                 "Approximate": [r["approx_mileage"] for r in vehicle_rows]},
                "Vehicle Mileage Comparison", "km per litre", decimals=1),
            "vehicle_cost_per_km": bar_chart(codes, [r["fulltank_cost_per_km"] for r in vehicle_rows],
                                             "Fuel Cost per km (full-tank)", "₹ per km", decimals=2),
            "vehicle_cost": bar_chart(codes, [r["total_cost"] for r in vehicle_rows],
                                      "Vehicle-wise Fuel Cost", "Cost (₹)", decimals=0),
        }
        return jsonify(charts)
    except Exception as exc:
        app.logger.exception("Chart generation error")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------
def ensure_admin():
    """Creates the first admin from ADMIN_USERNAME / ADMIN_PASSWORD in .env
    if no admin exists yet. Safe to run on every start."""
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        print("No ADMIN_USERNAME/ADMIN_PASSWORD in .env; skipping admin creation.")
        return
    if query("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"):
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters; admin not created.")
        return
    try:
        execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                (username, generate_password_hash(password)))
        print(f"Admin account '{username}' created.")
    except mysql.connector.IntegrityError:
        print(f"'{username}' already exists as a normal user. Choose another ADMIN_USERNAME.")


if __name__ == "__main__":
    try:
        ensure_admin()
    except mysql.connector.Error as error:
        print(f"Could not reach the database: {error}")
    app.run(debug=True, port=5001)