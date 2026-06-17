import sqlite3
import os
from datetime import datetime, timedelta, timezone
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

            CREATE TABLE IF NOT EXISTS user_puzzles_state (
                user_id     INTEGER NOT NULL,
                puzzle_id   INTEGER NOT NULL,
                ease_factor REAL    DEFAULT 2.5,
                interval    INTEGER DEFAULT 0,
                repetitions INTEGER DEFAULT 0,
                next_review DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, puzzle_id)
            );
        ''')
        conn.commit()
    finally:
        conn.close()


init_db()


# ── SM-2 spaced-repetition ────────────────────────────────────────────────────

def calculate_sm2(
    quality: int,
    ease_factor: float,
    interval: int,
    repetitions: int,
) -> tuple:
    """
    SuperMemo-2 algorithm.  Returns (new_interval_days, new_ease_factor, new_repetitions).

    quality 0-5:
        5 = perfect, instant recall
        4 = correct after brief hesitation
        3 = correct but recalled with difficulty
        2 = incorrect; correct answer felt easy in hindsight  ← resets schedule
        1 = incorrect; correct answer vaguely remembered      ← resets schedule
        0 = complete blackout                                 ← resets schedule
    """
    if quality >= 3:
        if repetitions == 0:
            new_interval = 1
        elif repetitions == 1:
            new_interval = 6
        else:
            new_interval = max(1, round(interval * ease_factor))
        new_repetitions = repetitions + 1
    else:
        # Failed recall — restart the schedule from scratch
        new_interval    = 1
        new_repetitions = 0

    # EF update applies on every attempt (SM-2 spec)
    new_ef = ease_factor + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    new_ef = max(1.3, round(new_ef, 4))

    return new_interval, new_ef, new_repetitions


def _quality_from_attempt(is_correct: bool, time_ms) -> int:
    """Map correctness + solve time to an SM-2 quality score (0–5)."""
    if not is_correct:
        return 1          # wrong answer — near-failure (not a total blackout)
    if time_ms is None:
        return 3          # no timing data — minimum passing grade
    secs = time_ms / 1000
    if secs <= 3:
        return 5          # instant — perfect recall
    if secs <= 10:
        return 4          # fast — correct with brief thought
    return 3              # slow but correct — recalled with effort


# ── Tier helper ───────────────────────────────────────────────────────────────

def _tier_filter(streak: int) -> str:
    """Return a SQL WHERE fragment that matches puzzles for the given streak."""
    if streak < 5:
        return f'{_SPACES} BETWEEN 0 AND 1'   # 1–2-move  (⭐)
    if streak < 10:
        return f'{_SPACES} BETWEEN 2 AND 3'   # 3–4-move  (⭐⭐)
    return f'{_SPACES} >= 4'                   # 5+-move   (⭐⭐⭐)


def _level_filter(level: int) -> str:
    """Return a SQL WHERE fragment for an explicit level (1, 2, or 3)."""
    if level == 2:
        return f'{_SPACES} BETWEEN 2 AND 3'
    if level == 3:
        return f'{_SPACES} >= 4'
    return f'{_SPACES} BETWEEN 0 AND 1'   # level 1 (default)


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


@app.route('/basics')
def basics():
    return render_template('basics.html')


@app.route('/trainer')
def trainer():
    level = request.args.get('level', 1, type=int)
    level = max(1, min(3, level))
    return render_template('trainer.html', level=level)


@app.route('/me')
def me():
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'logged_in': False})
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT username, current_streak FROM users WHERE id = ?', (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        session.clear()
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'username': row['username'], 'streak': row['current_streak']})


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
        # ── 1. Raw attempt log ────────────────────────────────────────────
        conn.execute(
            'INSERT INTO student_analytics (user_id, puzzle_id, time_spent_ms, is_correct) '
            'VALUES (?, ?, ?, ?)',
            (user_id, puzzle_id, time_spent_ms, is_correct)
        )

        # ── 2. Streak bookkeeping ─────────────────────────────────────────
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

        # ── 3. SM-2 update ────────────────────────────────────────────────
        state = conn.execute(
            'SELECT ease_factor, interval, repetitions FROM user_puzzles_state '
            'WHERE user_id = ? AND puzzle_id = ?',
            (user_id, puzzle_id)
        ).fetchone()

        ef, iv, reps = (
            (state['ease_factor'], state['interval'], state['repetitions'])
            if state else (2.5, 0, 0)
        )

        quality                          = _quality_from_attempt(bool(is_correct), time_spent_ms)
        new_interval, new_ef, new_reps   = calculate_sm2(quality, ef, iv, reps)
        next_review                      = (
            datetime.now(timezone.utc) + timedelta(days=new_interval)
        ).strftime('%Y-%m-%d %H:%M:%S')

        conn.execute(
            '''INSERT INTO user_puzzles_state
                   (user_id, puzzle_id, ease_factor, interval, repetitions, next_review)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, puzzle_id) DO UPDATE SET
                   ease_factor = excluded.ease_factor,
                   interval    = excluded.interval,
                   repetitions = excluded.repetitions,
                   next_review = excluded.next_review''',
            (user_id, puzzle_id, new_ef, new_interval, new_reps, next_review)
        )

        conn.commit()
        return jsonify({'message': 'Attempt logged.', 'next_review': next_review})
    finally:
        conn.close()


# ── Puzzle serving ────────────────────────────────────────────────────────────

@app.route('/get-puzzle', methods=['GET'])
def get_puzzle():
    user_id = session.get('user_id')

    level_param = request.args.get('level', type=int)   # explicit level override

    # ── Anonymous: tier-based random delivery ─────────────────────────────────
    if user_id is None:
        streak = request.args.get('streak', 0, type=int)
        where  = _level_filter(level_param) if level_param in (1, 2, 3) else _tier_filter(streak)
        conn   = get_db()
        try:
            row = conn.execute(
                f'SELECT id, fen, engine_best_move FROM personal_blunders '
                f'WHERE {where} ORDER BY RANDOM() LIMIT 1'
            ).fetchone() or conn.execute(
                'SELECT id, fen, engine_best_move FROM personal_blunders '
                'ORDER BY RANDOM() LIMIT 1'
            ).fetchone()
            if row is None:
                return jsonify({'error': 'No puzzles found.'}), 404
            return jsonify({
                'puzzle_id':        row['id'],
                'fen':              row['fen'],
                'engine_best_move': row['engine_best_move'],
            })
        finally:
            conn.close()

    # ── Authenticated: SRS priority queue ─────────────────────────────────────
    conn = get_db()
    try:
        user_row = conn.execute(
            'SELECT current_streak FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        streak = user_row['current_streak'] if user_row else 0
        where  = _level_filter(level_param) if level_param in (1, 2, 3) else _tier_filter(streak)

        # P1 — overdue SRS review, matching the current difficulty tier
        row = conn.execute(
            f'''SELECT pb.id, pb.fen, pb.engine_best_move
                FROM personal_blunders pb
                JOIN user_puzzles_state ups
                  ON ups.puzzle_id = pb.id AND ups.user_id = ?
                WHERE ups.next_review <= datetime('now')
                  AND {where}
                ORDER BY ups.next_review ASC
                LIMIT 1''',
            (user_id,)
        ).fetchone()

        # P2 — new (never-seen) puzzle in the current difficulty tier
        if row is None:
            row = conn.execute(
                f'''SELECT id, fen, engine_best_move
                    FROM personal_blunders
                    WHERE {where}
                      AND id NOT IN (
                          SELECT puzzle_id FROM user_puzzles_state WHERE user_id = ?
                      )
                    ORDER BY RANDOM()
                    LIMIT 1''',
                (user_id,)
            ).fetchone()

        # P3 — any overdue SRS review (tier-agnostic)
        if row is None:
            row = conn.execute(
                '''SELECT pb.id, pb.fen, pb.engine_best_move
                   FROM personal_blunders pb
                   JOIN user_puzzles_state ups
                     ON ups.puzzle_id = pb.id AND ups.user_id = ?
                   WHERE ups.next_review <= datetime('now')
                   ORDER BY ups.next_review ASC
                   LIMIT 1''',
                (user_id,)
            ).fetchone()

        # P4 — any unseen puzzle (tier-agnostic)
        if row is None:
            row = conn.execute(
                '''SELECT id, fen, engine_best_move
                   FROM personal_blunders
                   WHERE id NOT IN (
                       SELECT puzzle_id FROM user_puzzles_state WHERE user_id = ?
                   )
                   ORDER BY RANDOM()
                   LIMIT 1''',
                (user_id,)
            ).fetchone()

        # P5 — absolute fallback: any puzzle at all
        if row is None:
            row = conn.execute(
                'SELECT id, fen, engine_best_move FROM personal_blunders '
                'ORDER BY RANDOM() LIMIT 1'
            ).fetchone()

        if row is None:
            return jsonify({'error': 'No puzzles found.'}), 404

        return jsonify({
            'puzzle_id':        row['id'],
            'fen':              row['fen'],
            'engine_best_move': row['engine_best_move'],
        })
    finally:
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
