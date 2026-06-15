import chess
import chess.pgn
import chess.engine
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "chess_data.db"
ENGINE_PATH = ROOT / "Stockfish.exe.exe"
PGN_PATH = ROOT / "games.pgn"
BLUNDER_THRESHOLD = 150
ANALYSIS_DEPTH = 18


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS personal_blunders (
            id               INTEGER PRIMARY KEY,
            fen              TEXT,
            engine_best_move TEXT,
            timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def score_to_cp(score: chess.engine.Score) -> int | None:
    """Return centipawn value from White's perspective, or None for mate scores."""
    if score.is_mate():
        return None
    return score.white().score()


def analyze_pgn(pgn_path: str, engine: chess.engine.SimpleEngine, conn: sqlite3.Connection) -> None:
    with open(pgn_path, "r") as pgn_file:
        while True:
            game = chess.pgn.read_game(pgn_file)
            if game is None:
                break

            board = game.board()

            for move in game.mainline_moves():
                info_before = engine.analyse(board, chess.engine.Limit(depth=ANALYSIS_DEPTH))
                cp_before = score_to_cp(info_before["score"])

                board.push(move)

                info_after = engine.analyse(board, chess.engine.Limit(depth=ANALYSIS_DEPTH))
                cp_after = score_to_cp(info_after["score"])

                if cp_before is not None and cp_after is not None:
                    # Evaluate drop from the perspective of the side that just moved.
                    # After pushing the move the board's turn flips, so we negate cp_after.
                    drop = cp_before - (-cp_after)
                    if drop > BLUNDER_THRESHOLD:
                        fen = board.fen()
                        best_move = info_after["pv"][0].uci() if info_after.get("pv") else "N/A"
                        conn.execute(
                            "INSERT INTO personal_blunders (fen, engine_best_move) VALUES (?, ?)",
                            (fen, best_move),
                        )
                        conn.commit()
                        print(f"Blunder detected | drop: {drop} cp | best: {best_move} | FEN: {fen}")



def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH)
    try:
        analyze_pgn(PGN_PATH, engine, conn)
    finally:
        engine.quit()
        conn.close()
        print("Engine closed. Analysis complete.")


if __name__ == "__main__":
    main()
