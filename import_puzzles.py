"""
import_puzzles.py
Reads every puzzle from a PGN file and bulk-inserts them into the
personal_blunders table of the PostgreSQL database at DATABASE_URL.

Usage:
    DATABASE_URL=postgres://... python import_puzzles.py

Requires: python-chess  (pip install chess)
"""

import os
import chess.pgn

import db

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
PGN_PATH  = os.path.join(BASE_DIR, 'puzzles.pgn')
LOG_EVERY = 1000


def parse_puzzles(pgn_path: str) -> list[tuple[str, str]]:
    """Return a list of (fen, uci_sequence) tuples parsed from the PGN file."""
    rows: list[tuple[str, str]] = []
    skipped = 0

    with open(pgn_path, encoding='utf-8', errors='ignore') as pgn_file:
        while True:
            try:
                game = chess.pgn.read_game(pgn_file)
            except Exception as exc:
                print(f'  [warn] Could not parse game: {exc}')
                skipped += 1
                continue

            if game is None:
                break  # end of file

            # Get the board at the puzzle start position.
            # chess.pgn respects the [FEN "..."] header automatically.
            board = game.board()
            fen   = board.fen()

            # Extract the full main-line solution as space-separated UCI moves.
            mainline = list(game.mainline_moves())
            if not mainline:
                skipped += 1
                continue

            uci_sequence = ' '.join(m.uci() for m in mainline)

            rows.append((fen, uci_sequence))
            parsed = len(rows)

            if parsed % LOG_EVERY == 0:
                print(f'  Parsed {parsed:,} puzzles...')

    print(f'\n  Finished parsing: {len(rows):,} valid  |  {skipped} skipped')
    return rows


def insert_puzzles(rows: list[tuple[str, str]]) -> None:
    """Clear old data and bulk-insert all rows in a single transaction."""
    conn = db.connect()
    try:
        conn.execute('DELETE FROM personal_blunders')
        conn.executemany(
            'INSERT INTO personal_blunders (fen, engine_best_move, solution_len) VALUES (?, ?, ?)',
            [(fen, moves, moves.count(' ') + 1) for fen, moves in rows]
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    if not os.path.exists(PGN_PATH):
        raise FileNotFoundError(
            f'Puzzle file not found: {PGN_PATH}\n'
            'Update PGN_PATH at the top of this script to match your filename.'
        )

    print(f'Reading puzzles from: {PGN_PATH}')
    rows = parse_puzzles(PGN_PATH)

    if not rows:
        print('No valid puzzles found — nothing was written to the database.')
        return

    print(f'Writing {len(rows):,} puzzles to the database at DATABASE_URL')
    insert_puzzles(rows)
    print(f'Done. {len(rows):,} puzzles inserted into personal_blunders.')


if __name__ == '__main__':
    main()
