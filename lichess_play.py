"""'Play vs the Lichess pool' — connects a Synapchess account to Lichess via
OAuth2 (Authorization Code + PKCE — Lichess doesn't require pre-registering
an app for this flow, see https://lichess.org/api#tag/OAuth), then uses the
Board API to seek a real opponent from Lichess's own matchmaking pool and
relay the resulting game onto our own board UI.

Registered as a blueprint from app.py, same pattern as game_analysis.py.

How a game happens, end to end:
  1. /lichess/connect sends the user through Lichess's OAuth consent screen;
     /lichess/callback exchanges the returned code for an access token
     (board:play scope) and stores it in `lichess_accounts`.
  2. POST /api/lichess/seek spawns a background thread that opens two
     streaming HTTP connections to Lichess at once, per their docs: a POST
     to /api/board/seek (which just emits blank keep-alive lines until
     paired) and a GET on /api/stream/event (which announces the new game's
     id via a `gameStart` event once paired).
  3. That thread then streams /api/board/game/stream/{id} and mirrors the
     game's state (fen, clocks, status) into `lichess_live_games` after
     every update, replaying the move list with python-chess to get the
     current FEN.
  4. The browser never talks to Lichess directly — it polls
     GET /api/lichess/status (cheap DB read, works from any process) and
     posts moves through POST /api/lichess/move, which forwards them to
     Lichess's move endpoint using the stored token.

Everything needed to serve a status poll is persisted to Postgres rather
than kept only in the background thread's memory, so it doesn't matter
which gunicorn worker (or which later request) handles a given poll.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlencode

import chess
import requests
from flask import Blueprint, jsonify, redirect, request, session, url_for

import db

lichess_bp = Blueprint('lichess', __name__)

LICHESS_BASE = 'https://lichess.org'
CLIENT_ID    = 'synapchess'   # public app identifier — Lichess's PKCE flow needs no pre-registration
OAUTH_SCOPE  = 'board:play'

# time (minutes), increment (seconds) — mirrors real-time seek limits Lichess accepts
TIME_CONTROLS = {
    'blitz': (5, 0),
    'rapid': (10, 0),
}
DEFAULT_TIME_CONTROL = 'blitz'

# Rated: these are real Lichess games and count toward the connected
# account's actual public rating — that's a deliberate choice (rated seeks
# also draw from a much deeper pool of waiting opponents than casual ones,
# which is most of why casual seeks were pairing so rarely).
RATED = True

STREAM_TIMEOUT = 120    # per-read timeout on each streaming connection — generous vs. Lichess's ~9s heartbeat
MAX_SEEK_WAIT  = 600    # give up and report "no opponent" only after ~10 minutes of retrying


def get_db():
    return db.connect()


def init_lichess_db():
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS lichess_accounts (
                user_id      INTEGER   PRIMARY KEY,
                lichess_id   TEXT      NOT NULL,
                access_token TEXT      NOT NULL,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS lichess_live_games (
                user_id    INTEGER   PRIMARY KEY,
                state      TEXT      NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        ''')
        conn.commit()
    finally:
        conn.close()


# ── small helpers ───────────────────────────────────────────────────────────

def _auth_header(token):
    return {'Authorization': 'Bearer ' + token}


def _require_login():
    return session.get('user_id')


def _get_account(user_id):
    conn = get_db()
    try:
        return conn.execute(
            'SELECT lichess_id, access_token FROM lichess_accounts WHERE user_id = ?',
            (user_id,)
        ).fetchone()
    finally:
        conn.close()


def _get_state(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT state FROM lichess_live_games WHERE user_id = ?', (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row['state']) if row else {'status': 'idle'}


def _set_state(user_id, state):
    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO lichess_live_games (user_id, state, updated_at)
               VALUES (?, ?, (NOW() AT TIME ZONE 'UTC'))
               ON CONFLICT (user_id) DO UPDATE SET
                   state = excluded.state, updated_at = excluded.updated_at''',
            (user_id, json.dumps(state))
        )
        conn.commit()
    finally:
        conn.close()


def _is_current(user_id, seek_id):
    """True while `seek_id` is still the seek/game this user cares about.

    Every state write from a background thread carries the seek_id it was
    started with. Cancelling, or starting a fresh seek, overwrites state
    with a different (or absent) seek_id — the old thread notices next time
    it checks and quietly stops touching the database instead of clobbering
    whatever superseded it.
    """
    return _get_state(user_id).get('seek_id') == seek_id


# ── OAuth ────────────────────────────────────────────────────────────────────

@lichess_bp.route('/lichess/connect')
def lichess_connect():
    user_id = _require_login()
    if user_id is None:
        return redirect('/')

    verifier  = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state     = secrets.token_urlsafe(24)

    session['lichess_pkce_verifier'] = verifier
    session['lichess_oauth_state']   = state

    params = {
        'response_type':        'code',
        'client_id':             CLIENT_ID,
        'redirect_uri':          url_for('lichess.lichess_callback', _external=True),
        'code_challenge_method': 'S256',
        'code_challenge':        challenge,
        'scope':                 OAUTH_SCOPE,
        'state':                 state,
    }
    return redirect(LICHESS_BASE + '/oauth?' + urlencode(params))


@lichess_bp.route('/lichess/callback')
def lichess_callback():
    user_id = _require_login()
    if user_id is None:
        return redirect('/')

    if request.args.get('error'):
        return redirect('/play?lichess_error=' + request.args['error'])

    code     = request.args.get('code')
    state    = request.args.get('state')
    verifier = session.pop('lichess_pkce_verifier', None)
    expected = session.pop('lichess_oauth_state', None)

    if not code or not state or not verifier or state != expected:
        return redirect('/play?lichess_error=bad_state')

    try:
        token_resp = requests.post(
            LICHESS_BASE + '/api/token',
            data={
                'grant_type':    'authorization_code',
                'code':           code,
                'code_verifier':  verifier,
                'redirect_uri':   url_for('lichess.lichess_callback', _external=True),
                'client_id':      CLIENT_ID,
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get('access_token')
        if not access_token:
            raise ValueError('no access_token in response')

        account = requests.get(
            LICHESS_BASE + '/api/account', headers=_auth_header(access_token), timeout=15
        ).json()
    except (requests.RequestException, ValueError):
        return redirect('/play?lichess_error=token_exchange_failed')

    lichess_id = account.get('username') or account.get('id') or 'Lichess player'

    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO lichess_accounts (user_id, lichess_id, access_token, connected_at)
               VALUES (?, ?, ?, (NOW() AT TIME ZONE 'UTC'))
               ON CONFLICT (user_id) DO UPDATE SET
                   lichess_id = excluded.lichess_id,
                   access_token = excluded.access_token,
                   connected_at = excluded.connected_at''',
            (user_id, lichess_id, access_token)
        )
        conn.commit()
    finally:
        conn.close()

    return redirect('/play')


@lichess_bp.route('/lichess/disconnect', methods=['POST'])
def lichess_disconnect():
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    account = _get_account(user_id)
    conn = get_db()
    try:
        conn.execute('DELETE FROM lichess_accounts WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM lichess_live_games WHERE user_id = ?', (user_id,))
        conn.commit()
    finally:
        conn.close()

    if account:
        try:
            requests.delete(
                LICHESS_BASE + '/api/token',
                headers=_auth_header(account['access_token']), timeout=10
            )
        except requests.RequestException:
            pass  # token still gets forgotten locally either way

    return jsonify({'message': 'Disconnected.'})


# ── Matchmaking (background thread) ────────────────────────────────────────

def _run_seek(user_id, token, minutes, increment, seek_id):
    """Keeps re-seeking until paired, cancelled, or MAX_SEEK_WAIT elapses.

    A single /api/board/seek connection isn't guaranteed to stay open
    forever — Lichess itself can close it (with no match) well before a
    human waiting on lichess.org's own "Create a game" screen would give up,
    which is what made pairing look instant-fail rather than patient. So
    this just re-issues the seek in a loop instead of treating one closed
    connection as a final answer. Both streaming connections (the seek
    itself, and the account event stream that announces the match) also
    reconnect on a dropped/timed-out read rather than giving up the whole
    attempt.
    """
    game_info = {'id': None, 'color': None}
    stop_flag = threading.Event()
    deadline  = time.monotonic() + MAX_SEEK_WAIT

    def watch_events():
        while not stop_flag.is_set() and time.monotonic() < deadline:
            if game_info['id'] is not None or not _is_current(user_id, seek_id):
                return
            try:
                resp = requests.get(
                    LICHESS_BASE + '/api/stream/event',
                    headers=_auth_header(token), stream=True, timeout=STREAM_TIMEOUT
                )
            except requests.RequestException:
                time.sleep(2)
                continue
            try:
                for line in resp.iter_lines():
                    if stop_flag.is_set() or game_info['id'] is not None or not _is_current(user_id, seek_id):
                        return
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'gameStart':
                        game_info['id']    = event['game']['gameId']
                        game_info['color'] = event['game'].get('color')
                        return
            except requests.RequestException:
                pass  # connection dropped mid-stream — loop around and reconnect
            finally:
                resp.close()

    events_thread = threading.Thread(target=watch_events, daemon=True)
    events_thread.start()

    seek_error = None
    while (
        game_info['id'] is None
        and _is_current(user_id, seek_id)
        and time.monotonic() < deadline
    ):
        try:
            seek_resp = requests.post(
                LICHESS_BASE + '/api/board/seek',
                headers=_auth_header(token),
                data={'rated': 'true' if RATED else 'false', 'time': minutes, 'increment': increment},
                stream=True, timeout=STREAM_TIMEOUT,
            )
        except requests.RequestException:
            time.sleep(2)
            continue

        if not seek_resp.ok:
            try:
                seek_error = seek_resp.json().get('error')
            except ValueError:
                seek_error = None
            seek_resp.close()
            break  # Lichess rejected the request outright — retrying won't help

        try:
            for _ in seek_resp.iter_lines():
                if game_info['id'] is not None or not _is_current(user_id, seek_id):
                    break
        except requests.RequestException:
            pass  # dropped mid-wait — loop around and re-seek
        finally:
            seek_resp.close()

    stop_flag.set()
    events_thread.join(timeout=5)

    if not _is_current(user_id, seek_id):
        return  # cancelled, or superseded by a newer seek — leave its state alone

    if not game_info['id']:
        _set_state(user_id, {'status': 'idle', 'message': seek_error or 'No opponent found — try again.'})
        return

    _stream_game(user_id, token, game_info['id'], game_info['color'], seek_id)


def _stream_game(user_id, token, game_id, my_color, seek_id):
    try:
        resp = requests.get(
            LICHESS_BASE + '/api/board/game/stream/' + game_id,
            headers=_auth_header(token), stream=True, timeout=STREAM_TIMEOUT,
        )
    except requests.RequestException:
        if _is_current(user_id, seek_id):
            _set_state(user_id, {'status': 'idle', 'message': 'Lost connection to Lichess.'})
        return

    white_info = black_info = {}
    initial_fen = chess.STARTING_FEN

    try:
        for raw_line in resp.iter_lines():
            if not _is_current(user_id, seek_id):
                return

            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except ValueError:
                continue

            event_type = event.get('type')

            if event_type == 'gameFull':
                white_info  = event.get('white') or {}
                black_info  = event.get('black') or {}
                initial_fen = event.get('initialFen') or 'startpos'
                if initial_fen == 'startpos':
                    initial_fen = chess.STARTING_FEN
                inner_state = event.get('state') or {}
                _publish_game_state(
                    user_id, seek_id, game_id, my_color,
                    white_info, black_info, initial_fen, inner_state
                )

            elif event_type == 'gameState':
                finished = _publish_game_state(
                    user_id, seek_id, game_id, my_color,
                    white_info, black_info, initial_fen, event
                )
                if finished:
                    return

            elif event_type == 'opponentGone':
                current = _get_state(user_id)
                if current.get('seek_id') == seek_id:
                    current['opponent_gone'] = bool(event.get('gone'))
                    _set_state(user_id, current)
    finally:
        resp.close()


def _publish_game_state(user_id, seek_id, game_id, my_color, white_info, black_info, initial_fen, game_state):
    """Replays the move list onto initial_fen with python-chess to get the
    current position, writes the merged state, and returns True once the
    game has actually ended (so the caller can stop streaming)."""
    board = chess.Board(initial_fen)
    for uci in (game_state.get('moves') or '').split():
        try:
            board.push_uci(uci)
        except ValueError:
            break

    status   = game_state.get('status', 'started')
    winner   = game_state.get('winner')
    finished = status not in ('created', 'started')

    state = {
        'status':      'finished' if finished else 'playing',
        'seek_id':      seek_id,
        'game_id':      game_id,
        'fen':          board.fen(),
        'turn':         'white' if board.turn else 'black',
        'my_color':     my_color,
        'white':        {
            'name':   white_info.get('name') or white_info.get('id') or 'White',
            'rating': white_info.get('rating'),
            'ai':     white_info.get('aiLevel'),
        },
        'black':        {
            'name':   black_info.get('name') or black_info.get('id') or 'Black',
            'rating': black_info.get('rating'),
            'ai':     black_info.get('aiLevel'),
        },
        'wtime_ms':      game_state.get('wtime'),
        'btime_ms':      game_state.get('btime'),
        'lichess_status': status,
        'result_text':   _describe_result(status, winner, my_color) if finished else None,
    }
    _set_state(user_id, state)
    return finished


def _describe_result(status, winner, my_color):
    if status in ('draw', 'stalemate', 'insufficientMaterialClaim'):
        return "It's a draw."
    if status == 'aborted':
        return 'Game aborted.'
    if status == 'noStart':
        return 'Opponent never showed up — game aborted.'
    if winner:
        verb = {
            'mate':      'checkmate',
            'resign':    'resignation',
            'timeout':   'the clock',
            'outoftime': 'the clock',
            'cheat':     "the opponent's disqualification",
            'variantEnd': 'the variant ending',
        }.get(status, status)
        return ('You won' if winner == my_color else 'You lost') + f' by {verb}.'
    return 'Game over.'


# ── JSON API used by templates/play.html ────────────────────────────────────

@lichess_bp.route('/api/lichess/status')
def lichess_status():
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    account = _get_account(user_id)
    payload = _get_state(user_id)
    payload['connected']        = account is not None
    payload['lichess_username'] = account['lichess_id'] if account else None
    return jsonify(payload)


@lichess_bp.route('/api/lichess/seek', methods=['POST'])
def lichess_seek():
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    account = _get_account(user_id)
    if account is None:
        return jsonify({'error': 'Connect your Lichess account first.'}), 400

    current = _get_state(user_id)
    if current.get('status') in ('seeking', 'playing'):
        return jsonify(current)

    data    = request.get_json(silent=True) or {}
    control = data.get('time_control') if data.get('time_control') in TIME_CONTROLS else DEFAULT_TIME_CONTROL
    minutes, increment = TIME_CONTROLS[control]

    seek_id = secrets.token_hex(8)
    state = {'status': 'seeking', 'seek_id': seek_id, 'time_control': control}
    _set_state(user_id, state)

    threading.Thread(
        target=_run_seek,
        args=(user_id, account['access_token'], minutes, increment, seek_id),
        daemon=True,
    ).start()

    return jsonify(state)


@lichess_bp.route('/api/lichess/cancel', methods=['POST'])
def lichess_cancel():
    """Also doubles as 'dismiss' for a finished game — the frontend calls this
    before returning to the idle screen so a stale 'finished' status from a
    still-in-flight poll can't immediately bounce the UI back."""
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401
    if _get_state(user_id).get('status') in ('seeking', 'finished'):
        _set_state(user_id, {'status': 'idle'})
    return jsonify({'status': 'idle'})


@lichess_bp.route('/api/lichess/move', methods=['POST'])
def lichess_move():
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    state = _get_state(user_id)
    if state.get('status') != 'playing':
        return jsonify({'error': 'No game in progress.'}), 400

    account = _get_account(user_id)
    if account is None:
        return jsonify({'error': 'Lichess account not connected.'}), 400

    data = request.get_json(silent=True) or {}
    uci  = (data.get('move') or '').strip()
    if not uci or not (4 <= len(uci) <= 5):
        return jsonify({'error': 'Invalid move.'}), 400

    try:
        resp = requests.post(
            f"{LICHESS_BASE}/api/board/game/{state['game_id']}/move/{uci}",
            headers=_auth_header(account['access_token']), timeout=10,
        )
    except requests.RequestException:
        return jsonify({'error': 'Could not reach Lichess.'}), 502

    if not resp.ok:
        return jsonify({'error': 'Lichess rejected that move.'}), 400
    return jsonify({'message': 'Move sent.'})


@lichess_bp.route('/api/lichess/resign', methods=['POST'])
def lichess_resign():
    user_id = _require_login()
    if user_id is None:
        return jsonify({'error': 'Not authenticated.'}), 401

    state = _get_state(user_id)
    if state.get('status') != 'playing':
        return jsonify({'error': 'No game in progress.'}), 400

    account = _get_account(user_id)
    if account is None:
        return jsonify({'error': 'Lichess account not connected.'}), 400

    try:
        requests.post(
            f"{LICHESS_BASE}/api/board/game/{state['game_id']}/resign",
            headers=_auth_header(account['access_token']), timeout=10,
        )
    except requests.RequestException:
        pass
    return jsonify({'message': 'Resigned.'})
