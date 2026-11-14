import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "QUEUELESS_SECRET_KEY", "development-only-change-this-secret-before-deployment"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
DATABASE = Path(__file__).resolve().parent / "queueless.db"
ADMIN_PASSWORD_HASH = os.environ.get("QUEUELESS_ADMIN_PASSWORD_HASH")
ADMIN_PASSWORD = os.environ.get("QUEUELESS_ADMIN_PASSWORD", "admin123")

OFFICES = [
    {"name": "Examination Cell", "description": "Results, marksheets and examination queries.", "icon": "🎓",
     "services": ["Result / marksheet", "Rechecking / revaluation", "Examination query"]},
    {"name": "Accounts Office", "description": "Fee payments and account-related queries.", "icon": "💳",
     "services": ["Fee payment query", "Fee receipt", "Scholarship / account query"]},
    {"name": "Academic Office", "description": "Academic records and course-related support.", "icon": "📚",
     "services": ["Academic record", "Course / subject query", "Attendance query"]},
    {"name": "Administration", "description": "General university service and support.", "icon": "🏢",
     "services": ["General enquiry", "Document request", "Other assistance"]},
]
DEFAULT_SERVICE_MINUTES = 4


def get_db_connection():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def init_db():
    with get_db_connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                student_id TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS queue_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                office TEXT NOT NULL,
                service TEXT NOT NULL,
                token_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # Safe, additive migration: existing users and tokens are retained.
        existing = columns(db, "queue_tokens")
        for name, sql_type in (
            ("started_at", "TIMESTAMP"),
            ("completed_at", "TIMESTAMP"),
            ("duration_seconds", "INTEGER"),
            ("updated_at", "TIMESTAMP"),
        ):
            if name not in existing:
                db.execute(f"ALTER TABLE queue_tokens ADD COLUMN {name} {sql_type}")
        db.execute("""CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '🏢',
            services TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""")
        db.execute("""CREATE TABLE IF NOT EXISTS counters (
            id INTEGER PRIMARY KEY AUTOINCREMENT, department_id INTEGER NOT NULL,
            name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(department_id) REFERENCES departments(id), UNIQUE(department_id,name))""")
        db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_id INTEGER,
            kind TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(token_id) REFERENCES queue_tokens(id))""")
        db.execute("CREATE TABLE IF NOT EXISTS university_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        token_cols = columns(db, 'queue_tokens')
        if 'counter_id' not in token_cols:
            db.execute('ALTER TABLE queue_tokens ADD COLUMN counter_id INTEGER')
        for office in OFFICES:
            db.execute('INSERT OR IGNORE INTO departments(name,description,icon,services) VALUES(?,?,?,?)',
                       (office['name'], office['description'], office['icon'], '\n'.join(office['services'])))
        for dept in db.execute('SELECT id,name FROM departments').fetchall():
            db.execute('INSERT OR IGNORE INTO counters(department_id,name) VALUES(?,?)', (dept['id'], 'Counter 1'))
            db.execute('UPDATE queue_tokens SET counter_id=(SELECT id FROM counters WHERE department_id=? ORDER BY id LIMIT 1) WHERE office=? AND counter_id IS NULL', (dept['id'],dept['name']))
        db.execute("INSERT OR IGNORE INTO university_settings(key,value) VALUES('university_name','QueueLess University')")
        db.execute('CREATE INDEX IF NOT EXISTS idx_queue_counter_status ON queue_tokens(counter_id,status,token_number)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,is_read,created_at)')
        db.execute("CREATE INDEX IF NOT EXISTS idx_queue_office_status ON queue_tokens(office, status, token_number)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_queue_user_status ON queue_tokens(user_id, status)")
        db.commit()


def student_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify(error="Please sign in."), 401
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify(error="Admin login required."), 401
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def get_offices():
    with get_db_connection() as db:
        rows = db.execute('SELECT * FROM departments WHERE active=1 ORDER BY name').fetchall()
        items=[]
        for r in rows:
            counters=db.execute('SELECT id,name FROM counters WHERE department_id=? AND active=1 ORDER BY id',(r['id'],)).fetchall()
            items.append({'id':r['id'],'name':r['name'],'description':r['description'],'icon':r['icon'],
                          'services':[x.strip() for x in r['services'].splitlines() if x.strip()],
                          'counters':[dict(c) for c in counters]})
        return items

def university_name():
    with get_db_connection() as db:
        r=db.execute("SELECT value FROM university_settings WHERE key='university_name'").fetchone()
        return r['value'] if r else 'QueueLess University'

def office_info(name):
    return next((item for item in get_offices() if item['name']==name),None)

def create_notification(db, user_id, token_id, kind, title, message):
    if not db.execute('SELECT id FROM notifications WHERE user_id=? AND token_id=? AND kind=?',(user_id,token_id,kind)).fetchone():
        db.execute('INSERT INTO notifications(user_id,token_id,kind,title,message) VALUES(?,?,?,?,?)',
                   (user_id,token_id,kind,title,message))


def average_service_seconds(db, office):
    row = db.execute("""
        SELECT AVG(duration_seconds) AS avg_seconds, COUNT(*) AS samples
        FROM queue_tokens
        WHERE office = ? AND status = 'completed'
          AND duration_seconds IS NOT NULL AND duration_seconds > 0
          AND completed_at >= datetime('now', '-90 days')
    """, (office,)).fetchone()
    if row and row["samples"] and row["avg_seconds"]:
        # Smooth early data toward the four-minute prior so one sample doesn't swing estimates wildly.
        sample_count = min(row["samples"], 10)
        observed = float(row["avg_seconds"])
        baseline = DEFAULT_SERVICE_MINUTES * 60
        return ((observed * sample_count) + (baseline * (10 - sample_count))) / 10
    return DEFAULT_SERVICE_MINUTES * 60


def token_snapshot(db, token):
    office = token["office"]
    counter_row = db.execute("SELECT name FROM counters WHERE id=?", (token["counter_id"],)).fetchone() if token["counter_id"] else None
    avg_seconds = average_service_seconds(db, office)
    ahead = db.execute("""
        SELECT COUNT(*) FROM queue_tokens
        WHERE office = ? AND counter_id IS ? AND status = 'waiting'
          AND date(created_at, 'localtime') = date(?, 'localtime')
          AND token_number < ?
    """, (office, token["counter_id"], token["created_at"], token["token_number"])).fetchone()[0]
    serving = db.execute("""
        SELECT * FROM queue_tokens
        WHERE office = ? AND counter_id IS ? AND status = 'serving'
          AND date(created_at, 'localtime') = date(?, 'localtime')
        ORDER BY started_at LIMIT 1
    """, (office, token["counter_id"], token["created_at"])).fetchone()

    remaining_current = 0
    if serving and serving["id"] != token["id"]:
        if serving["started_at"]:
            try:
                started = datetime.fromisoformat(serving["started_at"])
                elapsed = max(0, (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds())
            except (ValueError, TypeError):
                elapsed = 0
            remaining_current = max(0, avg_seconds - elapsed)
        else:
            remaining_current = avg_seconds

    if token["status"] == "serving":
        position = 0
        wait_minutes = 0
    else:
        position = ahead
        wait_minutes = round((remaining_current + ahead * avg_seconds) / 60)
    return {
        "token_id": token["id"],
        "token_number": token["token_number"],
        "office": office,
        "counter": counter_row["name"] if counter_row else "Counter to be assigned",
        "service": token["service"],
        "status": token["status"],
        "position": position,
        "estimated_wait": max(0, wait_minutes),
        "average_service_minutes": round(avg_seconds / 60, 1),
        "updated_at": token["updated_at"] or token["created_at"],
    }


def get_active_token(db, user_id):
    return db.execute("""
        SELECT * FROM queue_tokens
        WHERE user_id = ? AND status IN ('waiting', 'serving')
        ORDER BY id DESC LIMIT 1
    """, (user_id,)).fetchone()


@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    student_id = request.form.get("student_id", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    if not all([name, email, student_id, password, confirm]):
        flash("Please fill in every field.", "error")
        return redirect(url_for("home") + "?form=register")
    if len(password) < 8:
        flash("Password must contain at least 8 characters.", "error")
        return redirect(url_for("home") + "?form=register")
    if password != confirm:
        flash("The passwords do not match.", "error")
        return redirect(url_for("home") + "?form=register")
    try:
        with get_db_connection() as db:
            cursor = db.execute(
                "INSERT INTO users (full_name, email, student_id, password_hash) VALUES (?, ?, ?, ?)",
                (name, email, student_id, generate_password_hash(password))
            )
            db.commit()
        session.clear()
        session["user_id"] = cursor.lastrowid
        session["user_name"] = name
        return redirect(url_for("dashboard"))
    except sqlite3.IntegrityError:
        flash("That email or student account is already registered. Please sign in.", "error")
        return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    with get_db_connection() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        flash("Incorrect email or password.", "error")
        return redirect(url_for("home"))
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["full_name"]
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@student_required
def dashboard():
    with get_db_connection() as db:
        active_token = get_active_token(db, session["user_id"])
        snapshot = token_snapshot(db, active_token) if active_token else None
    return render_template(
        "dashboard.html", user_name=session.get("user_name"),
        offices=get_offices(), active_token=active_token, snapshot=snapshot,
        university_name=university_name()
    )


@app.route("/join-queue", methods=["POST"])
@student_required
def join_queue():
    office = request.form.get("office", "").strip()
    service = request.form.get("service", "").strip()
    selected = office_info(office)
    if not selected or service not in selected["services"]:
        flash("Please select a valid office and service.", "error")
        return redirect(url_for("dashboard"))
    with get_db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if get_active_token(db, session["user_id"]):
            flash("You already have an active token. Complete or cancel it before joining another queue.", "error")
            return redirect(url_for("dashboard"))
        last = db.execute("""
            SELECT COALESCE(MAX(token_number), 0) FROM queue_tokens
            WHERE office = ? AND date(created_at, 'localtime') = date('now', 'localtime')
        """, (office,)).fetchone()[0]
        number = last + 1
        counter = db.execute("""SELECT c.id FROM counters c JOIN departments d ON d.id=c.department_id
            WHERE d.name=? AND c.active=1 ORDER BY (SELECT COUNT(*) FROM queue_tokens q
            WHERE q.counter_id=c.id AND q.status IN ('waiting','serving')) ASC, c.id LIMIT 1""",(office,)).fetchone()
        if not counter:
            flash('No active counter is available for this department.','error')
            return redirect(url_for('dashboard'))
        db.execute("""INSERT INTO queue_tokens(user_id,office,service,token_number,counter_id,status,updated_at)
            VALUES(?,?,?,?,?,'waiting',CURRENT_TIMESTAMP)""",(session['user_id'],office,service,number,counter['id']))
        db.commit()
    flash(f"Your token {office[:2].upper()}-{number:03d} has been generated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/leave-queue", methods=["POST"])
@student_required
def leave_queue():
    with get_db_connection() as db:
        db.execute("""
            UPDATE queue_tokens SET status='cancelled', updated_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND status='waiting'
        """, (session["user_id"],))
        db.commit()
    flash("Your waiting token has been cancelled.", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/my-queue")
@student_required
def my_queue_api():
    with get_db_connection() as db:
        token = get_active_token(db, session["user_id"])
        if not token:
            return jsonify(active=False)
        snap=token_snapshot(db,token)
        if token['status']=='serving':
            create_notification(db,session['user_id'],token['id'],'serving',"It's your turn!",'Please proceed to your assigned counter.')
        elif snap['position']<=2:
            create_notification(db,session['user_id'],token['id'],'approaching','Your turn is approaching','You are among the next two students. Please get ready.')
        db.commit()
        return jsonify(active=True, **snap)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        password = request.form.get("password", "")
        valid = check_password_hash(ADMIN_PASSWORD_HASH, password) if ADMIN_PASSWORD_HASH else password == ADMIN_PASSWORD
        if valid:
            session.clear()
            session["is_admin"] = True
            session["admin_name"] = "QueueLess Administrator"
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect admin password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def admin_queue_data(db):
    result = []
    for office in get_offices():
        rows = db.execute("""
            SELECT qt.*, u.full_name, u.student_id, c.name AS counter_name
            FROM queue_tokens qt JOIN users u ON u.id=qt.user_id LEFT JOIN counters c ON c.id=qt.counter_id
            WHERE qt.office = ?
              AND date(qt.created_at, 'localtime') = date('now', 'localtime')
              AND qt.status IN ('waiting', 'serving')
            ORDER BY CASE qt.status WHEN 'serving' THEN 0 ELSE 1 END, qt.token_number
        """, (office["name"],)).fetchall()
        result.append({
            "office": office,
            "tokens": [dict(row) for row in rows],
            "waiting": sum(1 for row in rows if row["status"] == "waiting"),
            "serving": next((dict(row) for row in rows if row["status"] == "serving"), None),
            "average_minutes": round(average_service_seconds(db, office["name"]) / 60, 1)
        })
    return result


@app.route("/admin")
@admin_required
def admin_dashboard():
    with get_db_connection() as db:
        queues = admin_queue_data(db)
        stats = {
            "waiting": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='waiting' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "serving": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='serving' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "completed": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='completed' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "cancelled": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='cancelled' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "students": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        }
    return render_template("admin.html", queues=queues, stats=stats,
                           offices=get_offices(), university_name=university_name())


@app.route("/api/admin/queues")
@admin_required
def admin_queues_api():
    with get_db_connection() as db:
        queues = admin_queue_data(db)
        stats = {
            "waiting": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='waiting' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "serving": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='serving' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "completed": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='completed' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
            "cancelled": db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='cancelled' AND date(created_at,'localtime')=date('now','localtime')").fetchone()[0],
        }
    serial = []
    for q in queues:
        serial.append({
            "office": q["office"], "waiting": q["waiting"], "average_minutes": q["average_minutes"],
            "tokens": [{k: t[k] for k in ("id","full_name","student_id","service","token_number","status","created_at","started_at","counter_name")} for t in q["tokens"]]
        })
    return jsonify(queues=serial, stats=stats)


@app.route("/admin/call-next", methods=["POST"])
@admin_required
def admin_call_next():
    office = request.form.get("office", "").strip()
    counter_id = request.form.get("counter_id", type=int)
    if not office_info(office):
        abort(400)
    with get_db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("""
            SELECT * FROM queue_tokens WHERE office=? AND status='serving'
              AND (? IS NULL OR counter_id=?) ORDER BY started_at LIMIT 1
        """, (office,counter_id,counter_id)).fetchone()
        if current:
            started = current["started_at"]
            seconds = None
            if started:
                try:
                    seconds = max(1, int((datetime.now() - datetime.fromisoformat(started)).total_seconds()))
                except (ValueError, TypeError):
                    seconds = None
            db.execute("""
                UPDATE queue_tokens SET status='completed', completed_at=CURRENT_TIMESTAMP,
                duration_seconds=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (seconds, current["id"]))
        next_token = db.execute("""
            SELECT id FROM queue_tokens WHERE office=? AND status='waiting'
              AND date(created_at,'localtime')=date('now','localtime')
              AND (? IS NULL OR counter_id=?) ORDER BY token_number LIMIT 1
        """, (office,counter_id,counter_id)).fetchone()
        if next_token:
            db.execute("""
                UPDATE queue_tokens SET status='serving', started_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (next_token["id"],))
        db.commit()
    flash("Next token called." if next_token else "No waiting tokens in this department.", "success" if next_token else "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/complete/<int:token_id>", methods=["POST"])
@admin_required
def admin_complete(token_id):
    with get_db_connection() as db:
        token = db.execute("SELECT * FROM queue_tokens WHERE id=? AND status='serving'", (token_id,)).fetchone()
        if not token:
            abort(404)
        seconds = None
        if token["started_at"]:
            try:
                seconds = max(1, int((datetime.now() - datetime.fromisoformat(token["started_at"])).total_seconds()))
            except (ValueError, TypeError):
                pass
        db.execute("""
            UPDATE queue_tokens SET status='completed', completed_at=CURRENT_TIMESTAMP,
            duration_seconds=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (seconds, token_id))
        db.commit()
    flash("Token marked as completed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/cancel/<int:token_id>", methods=["POST"])
@admin_required
def admin_cancel(token_id):
    with get_db_connection() as db:
        cur = db.execute("""
            UPDATE queue_tokens SET status='cancelled', updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('waiting','serving')
        """, (token_id,))
        db.commit()
        if cur.rowcount == 0:
            abort(404)
    flash("Token cancelled.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route('/api/notifications')
@student_required
def notifications_api():
    with get_db_connection() as db:
        rows=db.execute('SELECT id,title,message,kind,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 8',(session['user_id'],)).fetchall()
        db.execute('UPDATE notifications SET is_read=1 WHERE user_id=?',(session['user_id'],)); db.commit()
    return jsonify(items=[dict(r) for r in rows])

@app.route('/admin/analytics')
@admin_required
def admin_analytics():
    with get_db_connection() as db:
        daily=db.execute("SELECT date(created_at,'localtime') day,COUNT(*) total FROM queue_tokens WHERE created_at>=datetime('now','-6 days') GROUP BY day ORDER BY day").fetchall()
        departments=db.execute("SELECT office,COUNT(*) total,SUM(status='completed') completed,ROUND(AVG(duration_seconds)/60.0,1) avg_minutes FROM queue_tokens WHERE created_at>=datetime('now','-30 days') GROUP BY office ORDER BY total DESC").fetchall()
        totals={'tokens':db.execute("SELECT COUNT(*) FROM queue_tokens WHERE created_at>=datetime('now','-30 days')").fetchone()[0],
                'completed':db.execute("SELECT COUNT(*) FROM queue_tokens WHERE status='completed' AND created_at>=datetime('now','-30 days')").fetchone()[0],
                'students':db.execute('SELECT COUNT(*) FROM users').fetchone()[0]}
    return render_template('analytics.html',daily=[dict(x) for x in daily],departments=[dict(x) for x in departments],totals=totals)

@app.route('/admin/settings',methods=['GET','POST'])
@admin_required
def admin_settings():
    if request.method=='POST':
        action=request.form.get('action')
        with get_db_connection() as db:
            if action=='university':
                name=request.form.get('university_name','').strip()
                if name: db.execute("INSERT INTO university_settings(key,value) VALUES('university_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(name,))
            elif action=='department':
                name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); icon=request.form.get('icon','🏢').strip() or '🏢'
                services='\n'.join(x.strip() for x in request.form.get('services','').splitlines() if x.strip())
                if name and services:
                    cur=db.execute('INSERT OR IGNORE INTO departments(name,description,icon,services) VALUES(?,?,?,?)',(name,desc,icon,services))
                    if cur.rowcount: db.execute('INSERT INTO counters(department_id,name) VALUES(?,?)',(cur.lastrowid,'Counter 1'))
            elif action=='counter':
                dept=request.form.get('department_id',type=int); name=request.form.get('counter_name','').strip()
                if dept and name: db.execute('INSERT OR IGNORE INTO counters(department_id,name) VALUES(?,?)',(dept,name))
            elif action=='toggle_department':
                db.execute('UPDATE departments SET active=1-active WHERE id=?',(request.form.get('department_id',type=int),))
            elif action=='toggle_counter':
                db.execute('UPDATE counters SET active=1-active WHERE id=?',(request.form.get('counter_id',type=int),))
            db.commit()
        flash('Settings updated successfully.','success'); return redirect(url_for('admin_settings'))
    with get_db_connection() as db:
        depts=db.execute('SELECT * FROM departments ORDER BY name').fetchall()
        counters=db.execute('SELECT c.*,d.name department FROM counters c JOIN departments d ON d.id=c.department_id ORDER BY d.name,c.id').fetchall()
    return render_template('settings.html',departments=depts,counters=counters,university_name=university_name())

@app.route("/logout", methods=["POST"])
@student_required
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.context_processor
def inject_now():
    return {"current_year": datetime.now().year, "university_name": university_name()}


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
