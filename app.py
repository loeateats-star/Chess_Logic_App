import sqlite3
import os
from flask import Flask, render_template, jsonify

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chess_data.db')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/get-puzzle', methods=['GET'])
def get_puzzle():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
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
