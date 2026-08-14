import os
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

import db
from curriculum_data import get_section, ordered_sections, format_duration

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-before-deploying')

# Cross-origin isolation (needed for SharedArrayBuffer, which the in-browser
# WASM Stockfish engine in static/js/engine-client.js requires). Scoped to
# the analysis page and everything under static/js/ (the worker shim, the
# self-hosted stockfish-wasm/ bundle, and the nested pthread worker it
# spawns all need a matching COEP header or the browser refuses to start
# them). 'credentialless' (vs. 'require-corp') avoids having to add
# Cross-Origin-Resource-Policy headers to every CDN asset these pages load —
# cdn.tailwindcss.com in particular doesn't send one. Left off every other
# route so it doesn't affect the YouTube embeds on the curriculum pages.
@app.after_request
def _cross_origin_isolation_headers(response):
    if request.path == '/analysis' or request.path.startswith('/static/js/'):
        response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
        response.headers['Cross-Origin-Embedder-Policy'] = 'credentialless'
    return response

# ── Space-count expression reused across tier queries ─────────────────────────
# N UCI moves separated by spaces → N-1 spaces in the string.
_SPACES = "LENGTH(engine_best_move) - LENGTH(REPLACE(engine_best_move, ' ', ''))"


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    return db.connect()


def init_db():
    """Create application tables if they do not already exist."""
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id             SERIAL  PRIMARY KEY,
                username       TEXT    UNIQUE NOT NULL,
                password_hash  TEXT    NOT NULL,
                current_streak INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS student_analytics (
                id            SERIAL    PRIMARY KEY,
                user_id       INTEGER   NOT NULL,
                puzzle_id     INTEGER   NOT NULL,
                time_spent_ms INTEGER,
                is_correct    INTEGER,
                timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_puzzles_state (
                user_id     INTEGER   NOT NULL,
                puzzle_id   INTEGER   NOT NULL,
                ease_factor REAL      DEFAULT 2.5,
                interval    INTEGER   DEFAULT 0,
                repetitions INTEGER   DEFAULT 0,
                next_review TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, puzzle_id)
            );

            CREATE TABLE IF NOT EXISTS user_video_progress (
                user_id      INTEGER   NOT NULL,
                section      TEXT      NOT NULL,
                video_id     TEXT      NOT NULL,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, video_id)
            );

            CREATE TABLE IF NOT EXISTS personal_blunders (
                id               SERIAL    PRIMARY KEY,
                fen              TEXT,
                engine_best_move TEXT,
                timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
        # Backwards-compatible schema migrations. `IF NOT EXISTS` (not
        # try/except) because a failed statement aborts the whole Postgres
        # transaction — every statement after the first failure would
        # otherwise be silently skipped too.
        for stmt in [
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS experience_level  TEXT',
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_time_budget INTEGER DEFAULT 30',
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS training_goal     TEXT',
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS diagnostic_done   INTEGER DEFAULT 0',
        ]:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def seed_puzzles_if_empty():
    """Populate personal_blunders from puzzles.pgn on first boot.

    chess_data.db is gitignored (it also holds user password hashes), so a
    fresh deploy starts with an empty database. puzzles.pgn IS tracked in
    git, so we rebuild the puzzle table from it here instead of relying on
    a database file that never leaves this machine.
    """
    conn = get_db()
    try:
        count = conn.execute('SELECT COUNT(*) AS count FROM personal_blunders').fetchone()['count']
        if count > 0:
            return
        pgn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'puzzles.pgn')
        if not os.path.exists(pgn_path):
            return
        from import_puzzles import parse_puzzles
        rows = parse_puzzles(pgn_path)
        if rows:
            conn.executemany(
                'INSERT INTO personal_blunders (fen, engine_best_move) VALUES (?, ?)',
                rows
            )
            conn.commit()
    finally:
        conn.close()


init_db()
seed_puzzles_if_empty()

# ── Game analysis feature (Requirements: move tracking, save/rename) ──────────
# Stockfish itself runs client-side now (in-browser WASM — see
# static/js/engine-client.js); this blueprint only stores games and the
# analysis rows the browser computes, no server-side engine subprocess.
from game_analysis import games_bp, init_games_db  # noqa: E402

init_games_db()
app.register_blueprint(games_bp)


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


@app.route('/learn')
def learn():
    return render_template('learn.html')


@app.route('/join')
def join():
    return render_template('join.html')


@app.route('/donate')
def donate():
    return render_template('donate.html')


@app.route('/curriculum')
def curriculum():
    return render_template('curriculum.html', sections=ordered_sections())


@app.route('/curriculum/<section_slug>')
def curriculum_section(section_slug):
    section = get_section(section_slug)
    if section is None:
        return render_template('curriculum.html', sections=ordered_sections()), 404

    videos = [
        dict(v, duration_label=format_duration(v.get('duration')))
        for v in section['videos']
    ]
    return render_template(
        'curriculum_section.html',
        section=section,
        videos=videos,
        all_sections=ordered_sections(),
    )


@app.route('/basics')
def basics():
    return render_template('basics.html')


@app.route('/special-rules')
def special_rules():
    return render_template('special_rules.html')


@app.route('/analysis')
def analysis_page():
    return render_template('analysis.html')


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
            'SELECT username, current_streak, diagnostic_done, daily_time_budget '
            'FROM users WHERE id = ?', (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        session.clear()
        return jsonify({'logged_in': False})
    return jsonify({
        'logged_in':         True,
        'username':          row['username'],
        'streak':            row['current_streak'],
        'diagnostic_done':   row['diagnostic_done']  if row['diagnostic_done']  is not None else 0,
        'daily_time_budget': row['daily_time_budget'] if row['daily_time_budget'] is not None else 30,
    })


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
    except db.IntegrityError:
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
            'SELECT id, password_hash, current_streak, diagnostic_done, daily_time_budget '
            'FROM users WHERE username = ?',
            (username,)
        ).fetchone()
    finally:
        conn.close()

    if row is None or not check_password_hash(row['password_hash'], password):
        return jsonify({'error': 'Invalid username or password.'}), 401

    session['user_id']  = row['id']
    session['username'] = username
    return jsonify({
        'message':           f'Welcome back, {username}!',
        'streak':            row['current_streak'],
        'diagnostic_done':   row['diagnostic_done']  if row['diagnostic_done']  is not None else 0,
        'daily_time_budget': row['daily_time_budget'] if row['daily_time_budget'] is not None else 30,
    })


@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'message': 'Logged out.'})


@app.route('/save-diagnostic', methods=['POST'])
def save_diagnostic():
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    data              = request.get_json(force=True) or {}
    experience_level  = (data.get('experience_level') or '').strip()
    daily_time_budget = int(data.get('daily_time_budget') or 30)
    training_goal     = (data.get('training_goal') or '').strip()

    conn = get_db()
    try:
        conn.execute(
            '''UPDATE users
               SET experience_level  = ?,
                   daily_time_budget = ?,
                   training_goal     = ?,
                   diagnostic_done   = 1
               WHERE id = ?''',
            (experience_level, daily_time_budget, training_goal, user_id)
        )
        conn.commit()
        return jsonify({'message': 'Diagnostic saved.', 'daily_time_budget': daily_time_budget})
    finally:
        conn.close()


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
                COALESCE(AVG(CAST(is_correct AS REAL)), 0)     AS accuracy_raw,
                COALESCE(AVG(CAST(time_spent_ms AS REAL)), 0)  AS avg_time_ms
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


# ── Curriculum video progress ────────────────────────────────────────────────

@app.route('/api/video-progress')
def get_video_progress():
    """Return the list of video IDs the current user has marked complete.

    Anonymous visitors get an empty list — the front-end falls back to
    localStorage for them instead of server-side persistence.
    """
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'logged_in': False, 'completed': []})

    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT video_id FROM user_video_progress WHERE user_id = ?',
            (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'logged_in': True, 'completed': [r['video_id'] for r in rows]})


@app.route('/api/video-progress', methods=['POST'])
def set_video_progress():
    """Mark a video complete/incomplete for the current logged-in user."""
    user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    data      = request.get_json(force=True) or {}
    video_id  = (data.get('video_id') or '').strip()
    section   = (data.get('section') or '').strip()
    completed = bool(data.get('completed', True))

    if not video_id or not section:
        return jsonify({'error': 'video_id and section are required.'}), 400

    conn = get_db()
    try:
        if completed:
            conn.execute(
                '''INSERT INTO user_video_progress (user_id, section, video_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, video_id) DO NOTHING''',
                (user_id, section, video_id)
            )
        else:
            conn.execute(
                'DELETE FROM user_video_progress WHERE user_id = ? AND video_id = ?',
                (user_id, video_id)
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'message': 'Progress saved.', 'video_id': video_id, 'completed': completed})


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
                WHERE ups.next_review <= (NOW() AT TIME ZONE 'UTC')
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
                   WHERE ups.next_review <= (NOW() AT TIME ZONE 'UTC')
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
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
