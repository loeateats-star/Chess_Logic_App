"""
backfill_puzzle_rating_theme.py
Backfills `rating`, `themes`, and `primary_theme` onto the existing
personal_blunders table from the same Lichess CSV export that was already
imported. Additive only: this script never truncates or deletes rows,
unlike import_puzzles_csv.py (which is a full-reload tool).

Why a backfill instead of a full reload: the original import ran with
TRUNCATE ... RESTART IDENTITY followed by a straight top-to-bottom COPY
with 0 rows skipped, so today personal_blunders.id maps 1:1, in order, to
CSV data-row number. That means rating/themes can be attached to existing
rows by id instead of re-running the slow FEN/move transform (chess.Board
legality pushes) that the original import needed.

Lichess CSV columns: PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,
NbPlays,Themes,GameUrl,OpeningTags,DailyDate

Usage:
    DATABASE_URL=postgres://... python backfill_puzzle_rating_theme.py lichess_db_puzzle.csv.zst

Requires: zstandard, psycopg2  (pip install zstandard psycopg2-binary)
"""

import csv
import io
import os
import sys
import time

import psycopg2
import zstandard as zstd

LOG_EVERY = 200_000
BATCH = 50_000

# Phase/evaluation/length/audience tags: real Lichess metadata, but not
# instructive as "the" theme of a puzzle, so never picked as primary_theme.
GENERIC_THEMES = {
    'opening', 'middlegame', 'endgame', 'rookEndgame', 'pawnEndgame', 'bishopEndgame',
    'knightEndgame', 'queenEndgame', 'queenRookEndgame', 'advantage', 'crushing',
    'equality', 'short', 'long', 'veryLong', 'oneMove', 'master', 'masterVsMaster', 'superGM',
}

# Ordered most -> least instructive. First match in a puzzle's theme set wins.
PRIORITY_THEMES = [
    # Named mating patterns
    'smotheredMate', 'backRankMate', 'arabianMate', 'anastasiaMate', 'bodenMate',
    'dovetailMate', 'hookMate', 'doubleBishopMate', 'pillsburysMate', 'operaMate',
    'epauletteMate', 'swallowstailMate', 'triangleMate', 'morphysMate',
    'blindSwineMate', 'killBoxMate', 'vukovicMate', 'balestraMate', 'cornerMate',
    # Mate-in-N
    'mateIn1', 'mateIn2', 'mateIn3', 'mateIn4', 'mateIn5',
    # Core tactical motifs
    'fork', 'pin', 'skewer', 'discoveredAttack', 'discoveredCheck', 'doubleCheck',
    'deflection', 'attraction', 'xRayAttack', 'intermezzo', 'clearance', 'collinearMove',
    'interference', 'zugzwang', 'trappedPiece', 'hangingPiece', 'exposedKing',
    'capturingDefender', 'defensiveMove', 'quietMove', 'sacrifice',
    # Pawn / king-safety / special-move motifs
    'advancedPawn', 'promotion', 'underPromotion', 'enPassant', 'castling',
    'attackingF2F7', 'kingsideAttack', 'queensideAttack',
    # Broad catch-all, reached only if nothing more specific matched
    'mate',
]


def pick_primary_theme(themes_raw: str) -> str:
    tokens = themes_raw.split()
    token_set = set(tokens)
    for theme in PRIORITY_THEMES:
        if theme in token_set:
            return theme
    for t in tokens:
        if t not in GENERIC_THEMES:
            return t
    return tokens[0] if tokens else 'tactics'


def stream_meta(csv_path: str):
    """Yield (id, rating, themes_raw, primary_theme), id assigned by
    enumeration order. Must replicate import_puzzles_csv.py's row-skip
    condition exactly so the id sequence stays aligned with what's already
    in the table. A parse failure here is fatal (not skipped): silently
    skipping would desync every id after it."""
    t0 = time.time()
    parsed = 0

    with open(csv_path, 'rb') as fh:
        dctx = zstd.ZstdDecompressor()
        text = io.TextIOWrapper(dctx.stream_reader(fh), encoding='utf-8', newline='')
        for row in csv.reader(text):
            if not row or row[0] == 'PuzzleId':
                continue

            parsed += 1
            rating = int(row[3])
            themes_raw = row[7]
            yield parsed, rating, themes_raw, pick_primary_theme(themes_raw)

            if parsed % LOG_EVERY == 0:
                elapsed = time.time() - t0
                print(f'  Streamed {parsed:,} rows... ({elapsed:.0f}s, {parsed / elapsed:,.0f}/s)')

    print(f'\n  Finished streaming: {parsed:,} rows')


_STAGING_COPY_SQL = (
    "COPY puzzle_meta_staging (id, rating, themes, primary_theme) "
    "FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t')"
)


def run(csv_path: str) -> None:
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL environment variable is not set.')

    pg_conn = psycopg2.connect(database_url)
    try:
        cur = pg_conn.cursor()

        print('Ensuring schema...')
        cur.execute('ALTER TABLE personal_blunders ADD COLUMN IF NOT EXISTS rating INTEGER')
        cur.execute('ALTER TABLE personal_blunders ADD COLUMN IF NOT EXISTS themes TEXT')
        cur.execute('ALTER TABLE personal_blunders ADD COLUMN IF NOT EXISTS primary_theme TEXT')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_personal_blunders_rating ON personal_blunders (rating)')

        print('Pre-check: confirming id sequence has no gaps...')
        cur.execute('SELECT COUNT(*), COALESCE(MAX(id), 0) FROM personal_blunders')
        row_count, max_id = cur.fetchone()
        if row_count != max_id:
            raise RuntimeError(
                f'personal_blunders has {row_count:,} rows but MAX(id)={max_id:,}. '
                'id sequence has gaps, so row-number-as-id backfill is NOT safe. Aborting.'
            )
        print(f'  {row_count:,} rows, ids 1..{max_id:,} with no gaps, safe to proceed.')

        cur.execute('CREATE TEMP TABLE puzzle_meta_staging (id INTEGER PRIMARY KEY, rating INTEGER, themes TEXT, primary_theme TEXT)')

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter='\t')
        count_in_batch = 0
        total_streamed = 0

        for puzzle_id, rating, themes_raw, primary_theme in stream_meta(csv_path):
            writer.writerow([puzzle_id, rating, themes_raw, primary_theme])
            count_in_batch += 1
            total_streamed += 1
            if count_in_batch >= BATCH:
                buf.seek(0)
                cur.copy_expert(_STAGING_COPY_SQL, buf)
                buf.seek(0)
                buf.truncate(0)
                count_in_batch = 0

        if count_in_batch:
            buf.seek(0)
            cur.copy_expert(_STAGING_COPY_SQL, buf)

        if total_streamed != row_count:
            raise RuntimeError(
                f'Streamed {total_streamed:,} CSV rows but personal_blunders has {row_count:,} rows. '
                'Counts do not match. Aborting before touching personal_blunders.'
            )

        print('Applying bulk UPDATE...')
        cur.execute('''
            UPDATE personal_blunders pb
            SET rating = s.rating, themes = s.themes, primary_theme = s.primary_theme
            FROM puzzle_meta_staging s
            WHERE pb.id = s.id
        ''')
        if cur.rowcount != row_count:
            raise RuntimeError(
                f'UPDATE touched {cur.rowcount:,} rows, expected {row_count:,}. Rolling back.'
            )
        print(f'  Updated {cur.rowcount:,} rows.')

        pg_conn.commit()
        print('Committed.')

        cur.execute('SELECT COUNT(*) FROM personal_blunders WHERE rating IS NULL')
        null_rating = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM personal_blunders WHERE primary_theme IS NULL')
        null_theme = cur.fetchone()[0]
        cur.execute('SELECT MIN(rating), MAX(rating), ROUND(AVG(rating))::INT FROM personal_blunders')
        rmin, rmax, ravg = cur.fetchone()
        cur.close()

        print(f'Verification: {null_rating:,} NULL ratings, {null_theme:,} NULL themes, '
              f'rating range {rmin}-{rmax} (avg {ravg}).')
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pg_conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print('Usage: DATABASE_URL=postgres://... python backfill_puzzle_rating_theme.py <path-to-csv.zst>')
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f'Puzzle file not found: {csv_path}')

    print(f'Backfilling rating/theme from: {csv_path}')
    run(csv_path)


if __name__ == '__main__':
    main()
