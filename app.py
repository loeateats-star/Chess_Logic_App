import sqlite3
import os
from flask import Flask, render_template, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-before-deploying')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chess_data.db')

# ── Space-count expression reused across tier queries ─────────────────────────
# N UCI moves separated by spaces → N-1 spaces in the string.
_SPACES = "LENGTH(engine_best_move) - LENGTH(REPLACE(engine_best_move, ' ', ''))"


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create application tables if they do not already exist."""
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id             INTEGER PRIMARY KEY,
                username       TEXT    UNIQUE NOT NULL,
                password_hash  TEXT    NOT NULL,
                current_streak INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS student_analytics (
                id            INTEGER  PRIMARY KEY,
                user_id       INTEGER  NOT NULL,
                puzzle_id     INTEGER  NOT NULL,
                time_spent_ms INTEGER,
                is_correct    INTEGER,
                timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
    finally:
        conn.close()


init_db()


# ── Tier helper ───────────────────────────────────────────────────────────────

def _tier_filter(streak: int) -> str:
    """Return a SQL WHERE fragment that matches puzzles for the given streak."""
    if streak < 5:
        return f'{_SPACES} BETWEEN 0 AND 1'   # 1–2-move  (⭐)
    if streak < 10:
        return f'{_SPACES} BETWEEN 2 AND 3'   # 3–4-move  (⭐⭐)
    return f'{_SPACES} >= 4'                   # 5+-move   (⭐⭐⭐)


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/curriculum')
def curriculum():
    return render_template('curriculum.html')


@app.route('/worksheets')
def worksheets():
    return render_template('worksheets.html')


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/register', methods=['POST'])
def register():
    data     = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({'error': 'Username and password are required.'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash) VALUES (?, ?)',
            (username, generate_password_hash(password))
        )
        conn.commit()
        return jsonify({'message': f'User "{username}" registered successfully.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username already taken.'}), 409
    finally:
        conn.close()


@app.route('/login', methods=['POST'])
def login():
    data     = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    conn = get_db()
    try:
        row = conn.execute(
            'SELECT id, password_hash, current_streak FROM users WHERE username = ?',
            (username,)
        ).fetchone()
    finally:
        conn.close()

    if row is None or not check_password_hash(row['password_hash'], password):
        return jsonify({'error': 'Invalid username or password.'}), 401

    session['user_id']  = row['id']
    session['username'] = username
    return jsonify({
        'message': f'Welcome back, {username}!',
        'streak':  row['current_streak']
    })


@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'message': 'Logged out.'})


@app.route('/get-analytics')
def get_analytics():
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT
                COUNT(*)                                    AS total_attempts,
                COALESCE(AVG(CAST(is_correct AS REAL)), 0)  AS accuracy_raw,
                COALESCE(AVG(time_spent_ms), 0)             AS avg_time_ms
               FROM student_analytics
               WHERE user_id = ?''',
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    total    = row['total_attempts']
    accuracy = round(row['accuracy_raw'] * 100, 1) if total > 0 else 0
    avg_time = round(row['avg_time_ms'])            if total > 0 else 0

    return jsonify({
        'total_attempts': total,
        'accuracy':       accuracy,
        'avg_time_ms':    avg_time,
        'history':        []
    })


# ── Telemetry ─────────────────────────────────────────────────────────────────

@app.route('/log-attempt', methods=['POST'])
def log_attempt():
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    data          = request.get_json(force=True) or {}
    puzzle_id     = data.get('puzzle_id')
    time_spent_ms = data.get('time_spent_ms')
    is_correct    = int(bool(data.get('is_correct')))

    if puzzle_id is None:
        return jsonify({'error': 'puzzle_id is required.'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO student_analytics (user_id, puzzle_id, time_spent_ms, is_correct) '
            'VALUES (?, ?, ?, ?)',
            (user_id, puzzle_id, time_spent_ms, is_correct)
        )
        if is_correct:
            conn.execute(
                'UPDATE users SET current_streak = current_streak + 1 WHERE id = ?',
                (user_id,)
            )
        else:
            conn.execute(
                'UPDATE users SET current_streak = 0 WHERE id = ?',
                (user_id,)
            )
        conn.commit()
        return jsonify({'message': 'Attempt logged.'})
    finally:
        conn.close()


# ── Puzzle serving ────────────────────────────────────────────────────────────

@app.route('/get-puzzle', methods=['GET'])
def get_puzzle():
    user_id = session.get('user_id')

    if user_id is not None:
        # Authenticated: read streak from the database
        conn = get_db()
        try:
            user_row = conn.execute(
                'SELECT current_streak FROM users WHERE id = ?', (user_id,)
            ).fetchone()
            streak = user_row['current_streak'] if user_row else 0
        finally:
            conn.close()
    else:
        # Anonymous: honour the query-param streak the frontend sends
        streak = request.args.get('streak', 0, type=int)

    where = _tier_filter(streak)

    conn = get_db()
    try:
        # Primary: tier-matched puzzle
        row = conn.execute(
            f'SELECT id, fen, engine_best_move FROM personal_blunders '
            f'WHERE {where} ORDER BY RANDOM() LIMIT 1'
        ).fetchone()

        # Fallback: any puzzle so the frontend never crashes
        if row is None:
            row = conn.execute(
                'SELECT id, fen, engine_best_move FROM personal_blunders '
                'ORDER BY RANDOM() LIMIT 1'
            ).fetchone()

        if row is None:
            return jsonify({'error': 'No puzzles found.'}), 404

        return jsonify({
            'puzzle_id':       row['id'],
            'fen':             row['fen'],
            'engine_best_move': row['engine_best_move']
        })
    finally:
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
