"""
import_puzzles_csv.py
Streams the Lichess puzzle database export (a .csv.zst file, e.g.
lichess_db_puzzle.csv.zst) straight into the personal_blunders table,
replacing whatever is there. Also truncates user_puzzles_state, since
truncating personal_blunders reassigns puzzle ids from 1 and any existing
spaced-repetition state would otherwise silently point at unrelated new
puzzles instead of the ones it was scheduled against.

Lichess CSV columns: PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,
NbPlays,Themes,GameUrl,OpeningTags,DailyDate

FEN is the position BEFORE the first move in Moves, and that first move is the
blunder/setup move, not part of the solution. So each row is transformed:
    board = Board(FEN); board.push(Moves[0])
    fen              = board.fen()          # position the solver sees
    engine_best_move = Moves[1:]             # the actual solution
to match what the rest of the app expects (personal_blunders.fen is always
a position where it's the solver's turn to make the first solution move).

Loads via COPY (psycopg2 copy_expert) instead of executemany/INSERT: at a
few million rows, row-at-a-time INSERTs would take hours; COPY takes minutes.

Usage:
    DATABASE_URL=postgres://... python import_puzzles_csv.py lichess_db_puzzle.csv.zst

Requires: python-chess, zstandard, psycopg2  (pip install chess zstandard psycopg2-binary)
"""

import csv
import io
import os
import sys
import time

import chess
import psycopg2
import zstandard as zstd

LOG_EVERY = 200_000


def transform_rows(csv_path: str):
    """Yield (fen, engine_best_move, solution_len) tuples streamed from the
    compressed Lichess CSV, without holding the decompressed file in memory."""
    skipped = 0
    parsed  = 0
    t0      = time.time()

    with open(csv_path, 'rb') as fh:
        dctx   = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(fh)
        text   = io.TextIOWrapper(reader, encoding='utf-8', newline='')
        for row in csv.reader(text):
            if not row or row[0] == 'PuzzleId':
                continue
            try:
                fen, moves = row[1], row[2]
                uci_moves = moves.split(' ')
                solution = uci_moves[1:]
                if not solution:
                    skipped += 1
                    continue
                # Lichess data is pre-vetted, so push() (no legality check)
                # instead of push_uci() (which does) is meaningfully faster
                # at millions of rows.
                board = chess.Board(fen)
                board.push(chess.Move.from_uci(uci_moves[0]))
                yield (board.fen(), ' '.join(solution), len(solution))
            except (ValueError, IndexError):
                skipped += 1
                continue

            parsed += 1
            if parsed % LOG_EVERY == 0:
                elapsed = time.time() - t0
                print(f'  Parsed {parsed:,} puzzles... ({elapsed:.0f}s, {parsed / elapsed:,.0f}/s)')

    print(f'\n  Finished parsing: {parsed:,} valid | {skipped:,} skipped')


_COPY_SQL = (
    "COPY personal_blunders (fen, engine_best_move, solution_len) "
    "FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t')"
)
BATCH = 50_000


def load(csv_path: str) -> None:
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL environment variable is not set.')

    pg_conn = psycopg2.connect(database_url)
    try:
        cur = pg_conn.cursor()

        # Self-sufficient against a brand-new, empty database (e.g. a fresh
        # Neon project); doesn't assume app.py's init_db() has run yet.
        print('Ensuring schema...')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS personal_blunders (
                id               SERIAL    PRIMARY KEY,
                fen              TEXT,
                engine_best_move TEXT,
                solution_len     INTEGER,
                timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_puzzles_state (
                user_id     INTEGER   NOT NULL,
                puzzle_id   INTEGER   NOT NULL,
                ease_factor REAL      DEFAULT 2.5,
                interval    INTEGER   DEFAULT 0,
                repetitions INTEGER   DEFAULT 0,
                next_review TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, puzzle_id)
            )
        ''')
        cur.execute('ALTER TABLE personal_blunders ADD COLUMN IF NOT EXISTS solution_len INTEGER')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_personal_blunders_solution_len
            ON personal_blunders (solution_len)
        ''')

        print('Clearing personal_blunders and user_puzzles_state...')
        cur.execute('TRUNCATE personal_blunders RESTART IDENTITY')
        cur.execute('TRUNCATE user_puzzles_state')

        buf    = io.StringIO()
        writer = csv.writer(buf, delimiter='\t')
        count_in_batch = 0

        for fen, engine_best_move, solution_len in transform_rows(csv_path):
            writer.writerow([fen, engine_best_move, solution_len])
            count_in_batch += 1
            if count_in_batch >= BATCH:
                buf.seek(0)
                cur.copy_expert(_COPY_SQL, buf)
                buf.seek(0)
                buf.truncate(0)
                count_in_batch = 0

        if count_in_batch:
            buf.seek(0)
            cur.copy_expert(_COPY_SQL, buf)

        pg_conn.commit()
        cur.execute('SELECT COUNT(*) FROM personal_blunders')
        final_count = cur.fetchone()[0]
        cur.close()
        print(f'Done. {final_count:,} puzzles loaded into personal_blunders.')
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pg_conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print('Usage: DATABASE_URL=postgres://... python import_puzzles_csv.py <path-to-csv.zst>')
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f'Puzzle file not found: {csv_path}')

    print(f'Loading puzzles from: {csv_path}')
    load(csv_path)


if __name__ == '__main__':
    main()
