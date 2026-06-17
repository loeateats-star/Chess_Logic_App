import sqlite3
import os
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chess_data.db')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/curriculum')
def curriculum():
    return render_template('curriculum.html')


@app.route('/get-puzzle', methods=['GET'])
def get_puzzle():
    streak = request.args.get('streak', 0, type=int)

    # Count moves by counting spaces: N moves = N-1 spaces in the UCI string.
    # streak  0–4  → 1–2-move puzzles (0–1 spaces)
    # streak  5–9  → 3–4-move puzzles (2–3 spaces)
    # streak 10+   → 5+-move puzzles  (4+ spaces)
    SPACE_COUNT = "LENGTH(engine_best_move) - LENGTH(REPLACE(engine_best_move, ' ', ''))"

    if streak < 5:
        tier_filter = f'{SPACE_COUNT} BETWEEN 0 AND 1'
    elif streak < 10:
        tier_filter = f'{SPACE_COUNT} BETWEEN 2 AND 3'
    else:
        tier_filter = f'{SPACE_COUNT} >= 4'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()

        # Primary: tier-matched puzzle
        cursor.execute(
            f'SELECT fen, engine_best_move FROM personal_blunders '
            f'WHERE {tier_filter} ORDER BY RANDOM() LIMIT 1'
        )
        row = cursor.fetchone()

        # Fallback: any puzzle so the frontend never crashes
        if row is None:
            cursor.execute(
                'SELECT fen, engine_best_move FROM personal_blunders ORDER BY RANDOM() LIMIT 1'
            )
            row = cursor.fetchone()

        if row is None:
            return jsonify({'error': 'No puzzles found'}), 404

        return jsonify({'fen': row['fen'], 'engine_best_move': row['engine_best_move']})
    finally:
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
