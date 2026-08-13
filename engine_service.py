"""Thin wrapper around a persistent Stockfish (UCI) process via python-chess.

Kept separate from analyzer.py (the offline PGN blunder-miner) because this
module serves live, per-request evaluations from Flask and therefore needs a
long-lived engine handle guarded by a lock instead of a spawn-per-run process.
"""
import threading
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).parent
ENGINE_PATH = ROOT / "Stockfish.exe.exe"

DEFAULT_LIVE_DEPTH = 16
DEFAULT_ANALYSIS_DEPTH = 14
MAX_DEPTH = 20

# (max_cpl_for_this_tier, label) — first tier the loss falls at or under wins.
# A move that exactly matches the engine's top choice is always "best",
# regardless of these thresholds (see classify_move below).
_CPL_TIERS = [
    (10, "best"),
    (25, "excellent"),
    (50, "good"),
    (100, "inaccuracy"),
    (200, "mistake"),
    (float("inf"), "blunder"),
]

_lock = threading.Lock()
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = chess.engine.SimpleEngine.popen_uci(str(ENGINE_PATH))
    return _engine


def shutdown_engine():
    global _engine
    with _lock:
        if _engine is not None:
            try:
                _engine.quit()
            except chess.engine.EngineError:
                pass  # already terminated (e.g. process killed before a graceful quit)
            _engine = None


def classify_cpl(cpl):
    """Map a non-negative centipawn-loss value to a human label."""
    if cpl is None:
        return "unknown"
    cpl = max(0, cpl)
    for ceiling, label in _CPL_TIERS:
        if cpl <= ceiling:
            return label
    return "blunder"


def _score_parts(score: "chess.engine.PovScore"):
    """Return (cp, mate) from a PovScore already oriented to the side of interest."""
    if score.is_mate():
        return None, score.mate()
    return score.score(), None


def evaluate_fen(fen: str, depth: int = DEFAULT_LIVE_DEPTH, multipv: int = 1):
    """Evaluate a single position. Returns eval + PV(s) from the side-to-move's perspective."""
    depth = max(1, min(MAX_DEPTH, int(depth)))
    multipv = max(1, min(4, int(multipv)))
    board = chess.Board(fen)

    with _lock:
        engine = _get_engine()
        infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)

    if isinstance(infos, dict):
        infos = [infos]

    lines = []
    for info in infos:
        cp, mate = _score_parts(info["score"].pov(board.turn))
        pv_moves = info.get("pv", [])
        pv_uci, pv_san = [], []
        walker = board.copy()
        for mv in pv_moves:
            pv_san.append(walker.san(mv))
            pv_uci.append(mv.uci())
            walker.push(mv)
        lines.append({
            "cp": cp,
            "mate": mate,
            "pv_uci": pv_uci,
            "pv_san": pv_san,
            "best_move_uci": pv_uci[0] if pv_uci else None,
            "best_move_san": pv_san[0] if pv_san else None,
        })

    return {
        "fen": fen,
        "turn": "w" if board.turn == chess.WHITE else "b",
        "depth": depth,
        "lines": lines,
    }


def analyze_game_moves(starting_fen, uci_moves, depth: int = DEFAULT_ANALYSIS_DEPTH):
    """Walk a full move list once, evaluating before/after each ply.

    Only N+1 engine calls are needed for N moves (the "after" position of ply
    k is reused as the "before" position of ply k+1), versus analyzer.py's
    2N calls for its offline blunder scan.
    """
    depth = max(1, min(MAX_DEPTH, int(depth)))
    board = chess.Board(starting_fen) if starting_fen else chess.Board()

    results = []
    with _lock:
        engine = _get_engine()
        info_before = engine.analyse(board, chess.engine.Limit(depth=depth))

        for ply, uci in enumerate(uci_moves, start=1):
            mover_is_white = board.turn == chess.WHITE
            cp_before, mate_before = _score_parts(info_before["score"].pov(board.turn))
            best_pv = info_before.get("pv") or []
            best_move_uci = best_pv[0].uci() if best_pv else None
            best_move_san = board.san(best_pv[0]) if best_pv else None

            move = chess.Move.from_uci(uci)
            played_san = board.san(move)
            board.push(move)

            info_after = engine.analyse(board, chess.engine.Limit(depth=depth))
            # info_after's score is from the *new* side-to-move's perspective;
            # flip it back to the mover's perspective for an apples-to-apples comparison.
            cp_after, mate_after = _score_parts(info_after["score"].pov(mover_is_white))

            cpl = None
            if cp_before is not None and cp_after is not None:
                cpl = max(0, cp_before - cp_after)
            elif mate_before is not None and mate_before > 0 and mate_after is None:
                cpl = 1000  # had a forced mate and let it slip

            classification = "best" if best_move_uci == uci else classify_cpl(cpl)

            results.append({
                "ply": ply,
                "uci": uci,
                "san": played_san,
                "fen_after": board.fen(),
                "mover": "w" if mover_is_white else "b",
                "cp_before": cp_before,
                "cp_after": cp_after,
                "mate_before": mate_before,
                "mate_after": mate_after,
                "cpl": cpl,
                "classification": classification,
                "best_move_uci": best_move_uci,
                "best_move_san": best_move_san,
                "pv_uci": [m.uci() for m in best_pv],
            })

            info_before = info_after

    return results
