"""Game recording, saving/renaming, and Stockfish-backed analysis.

Registered as a blueprint from app.py. Kept in its own module (own DB
bootstrap, own routes) rather than folded into app.py so the puzzle-trainer
code and the full-game-analysis code don't tangle.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, session

import db

DEFAULT_ANALYSIS_DEPTH = 20  # matches EngineClient's ANALYSIS_DEPTH in analysis.html

games_bp = Blueprint("games", __name__, url_prefix="/api")

STARTING_FEN  = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
MAX_TITLE_LEN = 200


def get_db():
    # Postgres enforces foreign keys by default — no PRAGMA equivalent needed.
    return db.connect()


def init_games_db():
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS games (
                id            SERIAL    PRIMARY KEY,
                user_id       INTEGER   NOT NULL,
                title         TEXT      NOT NULL,
                starting_fen  TEXT      NOT NULL DEFAULT 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                result        TEXT      DEFAULT '*',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS game_moves (
                id           SERIAL    PRIMARY KEY,
                game_id      INTEGER   NOT NULL,
                ply          INTEGER   NOT NULL,
                san          TEXT      NOT NULL,
                uci          TEXT      NOT NULL,
                fen_after    TEXT      NOT NULL,
                move_time_ms INTEGER,
                played_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (game_id, ply),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS game_analysis (
                id             SERIAL  PRIMARY KEY,
                game_id        INTEGER NOT NULL,
                ply            INTEGER NOT NULL,
                mover          TEXT,
                cp_before      INTEGER,
                cp_after       INTEGER,
                mate_before    INTEGER,
                mate_after     INTEGER,
                cpl            INTEGER,
                classification TEXT,
                best_move_uci  TEXT,
                best_move_san  TEXT,
                pv             TEXT,
                depth          INTEGER,
                UNIQUE (game_id, ply),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_games_user        ON games(user_id);
            CREATE INDEX IF NOT EXISTS idx_game_moves_game    ON game_moves(game_id);
            CREATE INDEX IF NOT EXISTS idx_game_analysis_game ON game_analysis(game_id);
        ''')
        conn.commit()
    finally:
        conn.close()


def _default_title():
    return "Game - " + datetime.now().strftime("%Y-%m-%d %H:%M")


def _require_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return user_id


def _own_game_or_none(conn, game_id, user_id):
    return conn.execute(
        "SELECT * FROM games WHERE id = ? AND user_id = ?", (game_id, user_id)
    ).fetchone()


# ── Save / list / rename / delete ──────────────────────────────────────────

@games_bp.route("/games", methods=["POST"])
def save_game():
    """Record a full session's moves as a new saved game (Requirement 1 + 2)."""
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    data         = request.get_json(force=True) or {}
    title        = (data.get("title") or "").strip()[:MAX_TITLE_LEN] or _default_title()
    starting_fen = (data.get("starting_fen") or "").strip() or STARTING_FEN
    result       = data.get("result") or "*"
    moves        = data.get("moves") or []

    if not isinstance(moves, list) or not moves:
        return jsonify({"error": "At least one move is required to save a game."}), 400
    if not all(isinstance(mv, dict) for mv in moves):
        return jsonify({"error": "Each move must be an object."}), 400

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO games (user_id, title, starting_fen, result) VALUES (?, ?, ?, ?) RETURNING id",
            (user_id, title, starting_fen, result),
        )
        game_id = cur.lastrowid

        for i, mv in enumerate(moves, start=1):
            conn.execute(
                '''INSERT INTO game_moves
                       (game_id, ply, san, uci, fen_after, move_time_ms, played_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    game_id,
                    mv.get("ply", i),
                    mv.get("san", ""),
                    mv.get("uci", ""),
                    mv.get("fen", ""),
                    mv.get("move_time_ms"),
                    mv.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.commit()
        return jsonify({"game_id": game_id, "title": title, "move_count": len(moves)})
    finally:
        conn.close()


@games_bp.route("/games", methods=["GET"])
def list_games():
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT g.id, g.title, g.result, g.created_at, g.updated_at,
                      COUNT(gm.id) AS move_count,
                      EXISTS(SELECT 1 FROM game_analysis ga WHERE ga.game_id = g.id) AS analyzed
               FROM games g
               LEFT JOIN game_moves gm ON gm.game_id = g.id
               WHERE g.user_id = ?
               GROUP BY g.id
               ORDER BY g.created_at DESC''',
            (user_id,),
        ).fetchall()
        return jsonify({"games": [dict(r) for r in rows]})
    finally:
        conn.close()


@games_bp.route("/games/<int:game_id>", methods=["GET"])
def get_game(game_id):
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    conn = get_db()
    try:
        game = _own_game_or_none(conn, game_id, user_id)
        if game is None:
            return jsonify({"error": "Game not found."}), 404

        moves = conn.execute(
            "SELECT ply, san, uci, fen_after, move_time_ms, played_at "
            "FROM game_moves WHERE game_id = ? ORDER BY ply ASC",
            (game_id,),
        ).fetchall()

        analysis = conn.execute(
            "SELECT ply, mover, cp_before, cp_after, mate_before, mate_after, cpl, "
            "classification, best_move_uci, best_move_san, pv, depth "
            "FROM game_analysis WHERE game_id = ? ORDER BY ply ASC",
            (game_id,),
        ).fetchall()

        return jsonify({
            "game":     dict(game),
            "moves":    [dict(m) for m in moves],
            "analysis": [dict(a) for a in analysis],
        })
    finally:
        conn.close()


@games_bp.route("/games/<int:game_id>", methods=["PATCH"])
def rename_game(game_id):
    """Requirement 2: update/rename a saved game's title."""
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    data      = request.get_json(force=True) or {}
    new_title = (data.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not new_title:
        return jsonify({"error": "Title cannot be empty."}), 400

    conn = get_db()
    try:
        game = _own_game_or_none(conn, game_id, user_id)
        if game is None:
            return jsonify({"error": "Game not found."}), 404

        conn.execute(
            "UPDATE games SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_title, game_id),
        )
        conn.commit()
        return jsonify({"game_id": game_id, "title": new_title})
    finally:
        conn.close()


@games_bp.route("/games/<int:game_id>", methods=["DELETE"])
def delete_game(game_id):
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    conn = get_db()
    try:
        game = _own_game_or_none(conn, game_id, user_id)
        if game is None:
            return jsonify({"error": "Game not found."}), 404

        conn.execute("DELETE FROM game_analysis WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM game_moves WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
        conn.commit()
        return jsonify({"message": "Game deleted."})
    finally:
        conn.close()


# ── Analysis storage ──────────────────────────────────────────────────────
# Stockfish itself now runs client-side (in-browser, WASM — see
# static/js/engine-client.js), not as a server subprocess. This route only
# persists the rows the browser already computed, so a signed-in user's
# analysis survives across sessions/devices.

@games_bp.route("/games/<int:game_id>/analyze", methods=["POST"])
def save_analysis(game_id):
    """Requirement 3 + 4: store per-move analysis rows computed client-side."""
    user_id = _require_user()
    if user_id is None:
        return jsonify({"error": "Not authenticated."}), 401

    data     = request.get_json(force=True) or {}
    analysis = data.get("analysis")
    depth    = data.get("depth", DEFAULT_ANALYSIS_DEPTH)

    if not isinstance(analysis, list) or not analysis:
        return jsonify({"error": "analysis (a non-empty list of per-move rows) is required."}), 400

    conn = get_db()
    try:
        game = _own_game_or_none(conn, game_id, user_id)
        if game is None:
            return jsonify({"error": "Game not found."}), 404

        conn.execute("DELETE FROM game_analysis WHERE game_id = ?", (game_id,))
        for row in analysis:
            pv = row.get("pv_uci")
            conn.execute(
                '''INSERT INTO game_analysis
                       (game_id, ply, mover, cp_before, cp_after, mate_before, mate_after,
                        cpl, classification, best_move_uci, best_move_san, pv, depth)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    game_id, row.get("ply"), row.get("mover"), row.get("cp_before"), row.get("cp_after"),
                    row.get("mate_before"), row.get("mate_after"), row.get("cpl"), row.get("classification"),
                    row.get("best_move_uci"), row.get("best_move_san"),
                    " ".join(pv) if pv else None, depth,
                ),
            )
        conn.commit()

        summary = _summarize(analysis)
        return jsonify({"game_id": game_id, "depth": depth, "summary": summary})
    finally:
        conn.close()


def _summarize(analysis):
    """Per-side move-quality counts + average centipawn loss (Requirement 4)."""
    sides = {"w": {"cpl_sum": 0, "cpl_n": 0, "counts": {}}, "b": {"cpl_sum": 0, "cpl_n": 0, "counts": {}}}
    for row in analysis:
        side = sides[row["mover"]]
        label = row["classification"]
        side["counts"][label] = side["counts"].get(label, 0) + 1
        if row["cpl"] is not None:
            side["cpl_sum"] += row["cpl"]
            side["cpl_n"]   += 1

    out = {}
    for side_key, label in (("w", "white"), ("b", "black")):
        s = sides[side_key]
        avg_cpl = round(s["cpl_sum"] / s["cpl_n"], 1) if s["cpl_n"] else 0
        # Simple bounded accuracy heuristic (not Lichess's win%-based formula,
        # but monotonic in avg CPL and good enough to rank games/sessions).
        accuracy = round(max(0.0, 100 - avg_cpl / 3), 1)
        out[label] = {"avg_cpl": avg_cpl, "accuracy": accuracy, "counts": s["counts"]}
    return out
