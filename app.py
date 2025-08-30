from flask import Flask, render_template, request, redirect, url_for, session, flash
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from datetime import datetime, timedelta, date
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from functools import wraps
import atexit
import os
from dotenv import load_dotenv

# ----------------- BOOT ------------------
load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "default_secret_key")

# ----------------- DB CONFIG ------------------
DATABASE_URL = os.environ.get("DATABASE_URL")  # Render ka External Database URL

def get_conn():
    # DictCursor -> rows ko dict jaisa access: row['column']
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)

# ----------------- INIT DB (tables if missing) ------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    # tasks (note: we use description; title optional/NULL)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            due_date TIMESTAMP,
            completed BOOLEAN DEFAULT FALSE
        )
    """)

    conn.commit()
    cur.close()
    conn.close()

init_db()

# ----------------- MAIL CONFIG ------------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")
mail = Mail(app)

# ----------------- UTIL ------------------
TZ = pytz.timezone('Asia/Kolkata')

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

def cast_bool(v):
    try:
        return bool(int(v))
    except Exception:
        return bool(v)

# ----------------- AUTH ------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return render_template('register.html')

        hashed = generate_password_hash(password)

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, email, password) VALUES (%s, %s, %s)',
                (username, email, hashed)
            )
            conn.commit()

            # welcome mail
            if app.config['MAIL_USERNAME'] and app.config['MAIL_PASSWORD']:
                msg = Message("Welcome to ToDo App!",
                              sender=app.config['MAIL_USERNAME'],
                              recipients=[email])
                msg.body = f"Hi {username},\n\nThanks for registering on our ToDo App!"
                mail.send(msg)

            flash("Registration successful! Check your email.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            conn.rollback()
            flash("Username or email already exists.", "danger")
        finally:
            cur.close()
            conn.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE username = %s', (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password!', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully!', 'info')
    return redirect(url_for('login'))

@app.route('/')
def home():
    return redirect(url_for('dashboard') if 'user_id' in session else 'login')

# ----------------- TASKS ------------------
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_conn()
    cur = conn.cursor()
    user_id = session['user_id']

    cur.execute("""
        SELECT id, description, completed, due_date
        FROM tasks
        WHERE user_id = %s
        ORDER BY due_date IS NULL, due_date
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    tasks = []
    total = len(rows)
    completed_cnt = 0
    overdue_cnt = 0
    due_soon_cnt = 0

    now = datetime.now(TZ)
    soon_threshold = now + timedelta(days=2)

    for r in rows:
        completed = cast_bool(r['completed'])
        if completed:
            completed_cnt += 1

        overdue = False
        due_soon = False

        if r['due_date'] and not completed:
            # Postgres returns aware/naive? treat as naive UTC-ish; compare in local TZ
            due_dt = r['due_date']
            if due_dt.tzinfo is None:
                # assume UTC then convert to IST for fair compare
                due_dt = pytz.utc.localize(due_dt).astimezone(TZ)
            else:
                due_dt = r['due_date'].astimezone(TZ)

            if due_dt < now:
                overdue = True
                overdue_cnt += 1
            elif due_dt <= soon_threshold:
                due_soon = True
                due_soon_cnt += 1

        tasks.append({
            'id': r['id'],
            'description': r['description'],
            'completed': completed,
            'due_date': r['due_date'],
            'overdue': overdue,
            'due_soon': due_soon
        })

    stats = {
        "total": total,
        "completed": completed_cnt,
        "pending": max(0, total - completed_cnt),
        "overdue": overdue_cnt,
        "due_soon": due_soon_cnt
    }

    return render_template('index.html', tasks=tasks, stats=stats)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_task():
    if request.method == 'POST':
        desc = request.form.get('task', '').strip()
        due  = request.form.get('due_date')  # HTML datetime-local -> "YYYY-MM-DDTHH:MM"
        
        if not desc:
            flash("Task description is required.", "danger")
            return redirect(url_for('dashboard'))

        due_ts = None
        if due:
            try:
                due_ts = datetime.strptime(due, "%Y-%m-%dT%H:%M")
            except ValueError:
                due_ts = None

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO tasks (title, description, due_date, user_id, completed) VALUES (%s, %s, %s, %s, %s)',
            (desc, desc, due_ts, session['user_id'], False)
        )
        conn.commit()
        cur.close()
        conn.close()

        flash('Task added successfully!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('add_task.html')

@app.route('/edit/<int:task_id>', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT * FROM tasks WHERE id = %s AND user_id = %s', (task_id, session['user_id']))
    task = cur.fetchone()

    if not task:
        cur.close()
        conn.close()
        flash('Task not found or unauthorized.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        desc = request.form.get('task', '').strip()
        due  = request.form.get('due_date')
        if not desc:
            flash("Task description is required.", "danger")
            cur.close()
            conn.close()
            return redirect(url_for('dashboard'))

        due_ts = None
        if due:
            try:
                due_ts = datetime.strptime(due, "%Y-%m-%dT%H:%M")
            except ValueError:
                due_ts = None

        cur.execute(
            'UPDATE tasks SET description = %s, due_date = %s WHERE id = %s AND user_id = %s',
            (desc, due_ts, task_id, session['user_id'])
        )
        conn.commit()
        cur.close()
        conn.close()
        flash('Task updated successfully!', 'success')
        return redirect(url_for('dashboard'))

    cur.close()
    conn.close()
    return render_template('edit_task.html', task=task, index=task_id)

@app.route('/delete', methods=['POST'])
@login_required
def delete_task():
    task_id = request.form.get('task_id')
    if not task_id:
        flash('Task ID not provided.', 'danger')
        return redirect(url_for('dashboard'))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM tasks WHERE id = %s AND user_id = %s', (int(task_id), session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Task deleted successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/toggle/<int:task_id>', methods=['POST'])
@login_required
def toggle_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT completed FROM tasks WHERE id = %s AND user_id = %s', (task_id, session['user_id']))
    row = cur.fetchone()
    if row:
        new_status = not cast_bool(row['completed'])
        cur.execute('UPDATE tasks SET completed = %s WHERE id = %s AND user_id = %s',
                    (new_status, task_id, session['user_id']))
        conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('dashboard'))

# ----------------- 📊 STATS DASHBOARD ------------------
@app.route('/stats')
@login_required
def stats_dashboard():
    conn = get_conn()
    cur = conn.cursor()
    uid = session['user_id']

    cur.execute("SELECT id, completed, due_date FROM tasks WHERE user_id = %s", (uid,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    completed = sum(1 for r in rows if cast_bool(r['completed']))
    pending   = total - completed
    now = datetime.now()
    overdue   = sum(1 for r in rows if (r['due_date'] and r['due_date'] < now and not cast_bool(r['completed'])))
    due_today = sum(1 for r in rows if (r['due_date'] and r['due_date'].date() == date.today()))

    return render_template(
        "stats.html",
        total=total, pending=pending, completed=completed, overdue=overdue, due_today=due_today
    )

# ----------------- REMINDERS ------------------
def send_reminder_email(to_email, subject, body_text):
    if not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
        print("Mail creds missing; skipping email send.")
        return
    msg = Message(subject=subject, sender=app.config['MAIL_USERNAME'], recipients=[to_email])
    msg.body = body_text
    print(f"📧 Sending '{subject}' to {to_email}")
    mail.send(msg)

def send_reminders():
    with app.app_context():
        print("✅ Scheduler triggered send_reminders()")
        conn = get_conn()
        cur = conn.cursor()
        # pending tasks with a due_date
        cur.execute("""
            SELECT t.id, t.description, t.due_date, t.completed, u.email, u.username
            FROM tasks t
            JOIN users u ON t.user_id = u.id
            WHERE t.due_date IS NOT NULL AND t.completed = FALSE
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        now = datetime.utcnow()
        for r in rows:
            due = r['due_date']
            # treat due as UTC-naive if tzinfo absent
            due_utc = due if due.tzinfo else pytz.utc.localize(due)
            delta = due_utc - pytz.utc.localize(now) if now.tzinfo is None else due_utc - now

            if timedelta(0) <= delta <= timedelta(days=1):
                send_reminder_email(
                    r['email'],
                    f"⏰ Reminder: '{r['description']}' is due soon!",
                    f"Hi {r['username']},\n\nYour task '{r['description']}' is due on {due.strftime('%b %d, %Y %I:%M %p')}."
                )
            elif due_utc < (pytz.utc.localize(now) if now.tzinfo is None else now):
                send_reminder_email(
                    r['email'],
                    f"⚠️ Overdue Task: '{r['description']}'",
                    f"Hi {r['username']},\n\nYour task '{r['description']}' was due on {due.strftime('%b %d, %Y %I:%M %p')} and is now overdue!"
                )

@app.route('/test-reminder')
def test_reminder():
    send_reminders()
    return 'Reminder emails attempted (check server logs / mailbox).'

# ----------------- SCHEDULER ------------------
scheduler = BackgroundScheduler()
scheduler.start()
scheduler.add_job(
    func=send_reminders,
    trigger=IntervalTrigger(hours=1),
    id='reminder_job',
    name='Send email reminders every hour',
    replace_existing=True
)
atexit.register(lambda: scheduler.shutdown())

# ----------------- MAIN ------------------
if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
