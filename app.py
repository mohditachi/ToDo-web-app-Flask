from flask import Flask, render_template, request, redirect, url_for, session, g, flash
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from functools import wraps
import atexit
import os
from dotenv import load_dotenv


load_dotenv() 

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "default_secret_key")

DATABASE = 'todo.db'

# ----------------- DB HELPERS ------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db:
        db.close()

# ----------------- AUTH HELPER ------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

# ----------------- MAIL CONFIG ------------------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")
mail = Mail(app)

# ----------------- SMALL UTILITIES ------------------
TZ = pytz.timezone('Asia/Kolkata')

def cast_bool(v):
    # Handles 0/1 int or "0"/"1" str safely
    try:
        return bool(int(v))
    except Exception:
        return bool(v)

def parse_due(d):
    if not d:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            continue
    return None

# ----------------- LOGIN SYSTEM ------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password']
        hashed = generate_password_hash(password)

        db = get_db()
        try:
            db.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                       (username, email, hashed))
            db.commit()

            # Send email
            msg = Message("Welcome to ToDo App!",
                          sender=app.config['MAIL_USERNAME'],
                          recipients=[email])
            msg.body = f"Hi {username},\n\nThanks for registering on our ToDo App!"
            mail.send(msg)

            flash("Registration successful! Check your email.", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Username or email already exists.", "danger")

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
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

# Smart home: send to dashboard if logged in, else login
@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# ----------------- TODO ROUTES -------------------
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user_id = session['user_id']

    tasks_raw = db.execute("""
        SELECT id, description, completed, due_date
        FROM tasks
        WHERE user_id = ?
        ORDER BY due_date IS NULL, due_date
    """, (user_id,)).fetchall()

    tasks = []
    total_tasks = len(tasks_raw)
    completed_tasks = 0
    overdue_count = 0
    due_soon_count = 0

    now = datetime.now(TZ)
    soon_threshold = now + timedelta(days=2)

    for row in tasks_raw:
        completed_bool = cast_bool(row['completed'])  # ✅ Fix for checkbox truthiness

        if completed_bool:
            completed_tasks += 1

        overdue = False
        due_soon = False

        if row['due_date'] and not completed_bool:
            due_dt_naive = parse_due(row['due_date'])
            if due_dt_naive:
                # localize to IST for fair compare
                due_dt = TZ.localize(due_dt_naive)
                if due_dt < now:
                    overdue = True
                    overdue_count += 1
                elif due_dt <= soon_threshold:
                    due_soon = True
                    due_soon_count += 1

        tasks.append({
            'id': row['id'],
            'description': row['description'],
            'completed': completed_bool,
            'due_date': row['due_date'],
            'overdue': overdue,
            'due_soon': due_soon
        })

    stats = {
        "total": total_tasks,
        "completed": completed_tasks,
        "pending": max(0, total_tasks - completed_tasks),
        "overdue": overdue_count,
        "due_soon": due_soon_count
    }

    return render_template('index.html', tasks=tasks, stats=stats)

@app.route('/stats')
@login_required
def stats_dashboard():
    db = get_db()
    user_id = session['user_id']

    total = db.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ?', (user_id,)).fetchone()[0]
    completed = db.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ? AND completed = 1', (user_id,)).fetchone()[0]
    pending = db.execute('SELECT COUNT(*) FROM tasks WHERE user_id = ? AND completed = 0', (user_id,)).fetchone()[0]

    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    overdue = db.execute('''
        SELECT COUNT(*) FROM tasks
        WHERE user_id = ? AND completed = 0 AND due_date IS NOT NULL AND due_date < ?
    ''', (user_id, now.strftime("%Y-%m-%dT%H:%M"))).fetchone()[0]

    due_today = db.execute('''
        SELECT COUNT(*) FROM tasks
        WHERE user_id = ? AND completed = 0 AND due_date >= ? AND due_date < ?
    ''', (
        user_id,
        today_start.strftime("%Y-%m-%dT%H:%M"),
        today_end.strftime("%Y-%m-%dT%H:%M")
    )).fetchone()[0]

    return render_template('stats.html',
                           total=total, completed=completed,
                           pending=pending, overdue=overdue, due_today=due_today)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_task():
    if request.method == 'POST':
        task = request.form.get('task', '').strip()
        due_date = request.form.get('due_date')  # may be None/''

        if task:
            db = get_db()
            db.execute(
                'INSERT INTO tasks (description, due_date, user_id) VALUES (?, ?, ?)',
                (task, due_date if due_date else None, session['user_id'])
            )
            db.commit()
            flash('Task added successfully!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('add_task.html')

@app.route('/edit/<int:task_id>', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    db = get_db()
    task = db.execute(
        'SELECT * FROM tasks WHERE id = ? AND user_id = ?',
        (task_id, session['user_id'])
    ).fetchone()

    if not task:
        flash('Task not found or unauthorized.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        updated_task = request.form.get('task', '').strip()
        updated_due_date = request.form.get('due_date')

        if updated_task:
            db.execute(
                'UPDATE tasks SET description = ?, due_date = ? WHERE id = ? AND user_id = ?',
                (updated_task, updated_due_date if updated_due_date else None, task_id, session['user_id'])
            )
            db.commit()
            flash('Task updated successfully!', 'success')
            return redirect(url_for('dashboard'))

    return render_template('edit_task.html', task=task, index=task_id)

@app.route('/delete', methods=['POST'])
@login_required
def delete_task():
    task_id = request.form.get('task_id')
    if task_id:
        db = get_db()
        db.execute('DELETE FROM tasks WHERE id = ? AND user_id = ?', (int(task_id), session['user_id']))
        db.commit()
        flash('Task deleted successfully!', 'success')
    else:
        flash('Task ID not provided.', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/toggle/<int:task_id>', methods=['POST'])
@login_required
def toggle_task(task_id):
    db = get_db()
    task = db.execute('SELECT completed FROM tasks WHERE id = ? AND user_id = ?', (task_id, session['user_id'])).fetchone()
    if task:
        new_status = 0 if cast_bool(task['completed']) else 1
        db.execute('UPDATE tasks SET completed = ? WHERE id = ? AND user_id = ?', (new_status, task_id, session['user_id']))
        db.commit()
    return redirect(url_for('dashboard'))

@app.template_filter('todatetime')
def to_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M')
    except Exception:
        return datetime.now()

# ----------------- REMINDERS ------------------
def send_reminder_email(to_email, subject, body_text):
    msg = Message(subject=subject,
                  sender=app.config['MAIL_USERNAME'],
                  recipients=[to_email])
    msg.body = body_text
    print(f"📧 Sending '{subject}' to {to_email}")
    mail.send(msg)

def send_reminders():
    with app.app_context():
        print("✅ Scheduler triggered send_reminders()")

        db = get_db()
        now = datetime.now()

        # join with users to get email
        rows = db.execute('''
            SELECT tasks.id, tasks.description, tasks.due_date, users.email, tasks.reminder_sent, tasks.completed
            FROM tasks
            JOIN users ON tasks.user_id = users.id
            WHERE tasks.due_date IS NOT NULL
        ''').fetchall()

        for r in rows:
            task_id = r['id']
            description = r['description']
            due_str = r['due_date']
            email = r['email']
            completed = cast_bool(r['completed'])

            try:
                due_date = parse_due(due_str)
                if not due_date:
                    continue

                delta = due_date - now

                if not completed and timedelta(days=0) <= delta <= timedelta(days=1):
                    send_reminder_email(
                        email,
                        f"⏰ Reminder: '{description}' is due soon!",
                        f"Your task '{description}' is due on {due_date.strftime('%b %d, %Y %I:%M %p')}."
                    )
                elif not completed and due_date < now:
                    send_reminder_email(
                        email,
                        f"⚠️ Overdue Task: '{description}'",
                        f"Your task '{description}' was due on {due_date.strftime('%b %d, %Y %I:%M %p')} and is now overdue!"
                    )
            except Exception as e:
                print(f"❌ Date parsing error for task ID {task_id}: {e}")

@app.route('/test-reminder')
def test_reminder():
    send_reminders()
    return 'Reminder emails sent!'

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