"""Static YouTube curriculum data: one lecture playlist per skill tier.

Video lists were pulled from each playlist's public feed (id, title, channel,
duration) so the site can render its own scrollable video list + player
instead of just linking out to YouTube.
"""

CURRICULUM_SECTIONS = {
    'new-to-chess': {
        'slug': 'new-to-chess',
        'label': 'New to Chess',
        'tagline': 'Never played before? Start here.',
        'description': (
            'The absolute fundamentals — how each piece moves, how a game is won, '
            'and the very first checkmate patterns every new player needs.'
        ),
        'playlist_id': 'PLSf8xoS5sUoY',
        'videos': [
            {'id': 'YdnvlntAQH8', 'title': 'How to Play Chess: Chess Rules for Beginners | ChessKid', 'channel': 'ChessKid', 'duration': 1159},
            {'id': 'WUEgprkpj10', 'title': 'King & Queen Checkmate | ChessKid', 'channel': 'ChessKid', 'duration': 493},
            {'id': 'yeGA3XFs4Zg', 'title': 'One Rook and King Checkmate | Basic Checkmates the Right Way', 'channel': 'NM Robert Ramirez', 'duration': 593},
        ],
    },
    'beginner': {
        'slug': 'beginner',
        'label': 'Beginner',
        'tagline': 'Know the rules — now build good habits.',
        'description': (
            'Opening principles, piece values, notation, and the core two-piece '
            'checkmate patterns that turn a rule-follower into a real player.'
        ),
        'playlist_id': 'PLbiBAyuqFPH4',
        'videos': [
            {'id': 'UoFqV1lxA7Q', 'title': '3 Basic Opening Strategy Principles | Chess', 'channel': 'Howcast', 'duration': 182},
            {'id': 'kMn0Vf1zEdg', 'title': 'Board Vision | Exercises to Stop Making Beginner Mistakes', 'channel': 'NM Robert Ramirez', 'duration': 624},
            {'id': 'KnQm1wgAN4Q', 'title': 'The Point Value of the Chess Pieces', 'channel': 'NM Robert Ramirez', 'duration': 864},
            {'id': 'Txkg8Zmn6-A', 'title': "Two Rook Checkmate | Don't Let Them Escape Once You're Ahead", 'channel': 'NM Robert Ramirez', 'duration': 538},
            {'id': 'UKVpMT6dfRY', 'title': 'Basic Checkmates | Practice & Review', 'channel': 'NM Robert Ramirez', 'duration': 373},
            {'id': 'O3H9M39bHsU', 'title': 'How to Use Chess Notation | Chess', 'channel': 'Howcast', 'duration': 277},
            {'id': 'yeGA3XFs4Zg', 'title': 'One Rook and King Checkmate | Basic Checkmates the Right Way', 'channel': 'NM Robert Ramirez', 'duration': 593},
        ],
    },
    'intermediate': {
        'slug': 'intermediate',
        'label': 'Intermediate',
        'tagline': 'Openings are second nature — sharpen your tactics.',
        'description': (
            'Forks, pins, skewers, one-move mating patterns, and the mental '
            'process for reasoning through a position like a stronger player.'
        ),
        'playlist_id': 'PLRenyoYGhTMc',
        'videos': [
            {'id': 'IcGt2A1b3NE', 'title': 'Top 3 Best Chess Openings for Beginners | ChessKid', 'channel': 'ChessKid', 'duration': 505},
            {'id': 'nNpfWy4yWdg', 'title': 'Checkmates in 1 Move — Typical Patterns You Must Know', 'channel': 'NM Robert Ramirez', 'duration': 1043},
            {'id': 'rS3fEU_pog4', 'title': "Chess Reasoning | Categorize Every Move or Don't Play Chess", 'channel': 'NM Robert Ramirez', 'duration': 340},
            {'id': 'AALdLsQqtfg', 'title': "Think Backwards — It's Time to Start Thinking Like a Chess Master", 'channel': 'NM Robert Ramirez', 'duration': 436},
            {'id': 'nF4k-CnK8O8', 'title': 'Chess Tactics — Forks & Double Attacks: How Many Can You Solve?', 'channel': 'NM Robert Ramirez', 'duration': 772},
            {'id': 'vAcFa_OypEU', 'title': 'Chess Tactics | Pins vs Skewers', 'channel': 'NM Robert Ramirez', 'duration': 624},
            {'id': 'B_pwkYeRrlU', 'title': 'King and Pawn vs King Endgame | Practice Drill', 'channel': 'NM Robert Ramirez', 'duration': 1071},
        ],
    },
    'advanced': {
        'slug': 'advanced',
        'label': 'Advanced',
        'tagline': 'Tactics are solid — refine positional understanding.',
        'description': (
            'Pawn structure (doubled, isolated, backward pawns), zwischenzug, and '
            'the fundamental minor-piece and king-and-pawn endgames every strong '
            'player must know cold.'
        ),
        'playlist_id': 'PLaMek9hHpYeg',
        'videos': [
            {'id': 'eT_rMnvIfto', 'title': 'Doubled Pawns | Improve Your Technique and Positional Play', 'channel': 'NM Robert Ramirez', 'duration': 775},
            {'id': 'IjqlvObkMGI', 'title': 'Isolated Pawns | Positional Chess the Right Way', 'channel': 'NM Robert Ramirez', 'duration': 587},
            {'id': 'gi7WPKTVQ4c', 'title': 'Backward Pawns | Left-Behind Pawns | Game Analysis', 'channel': 'NM Robert Ramirez', 'duration': 826},
            {'id': 'R1sPLpkroyI', 'title': 'Zwischenzug | Learn & Master In-Between Moves (Intermezzo)', 'channel': 'NM Robert Ramirez', 'duration': 885},
            {'id': 'AnbPH3ndPuU', 'title': 'Checkmate With Two Bishops', 'channel': 'NM Robert Ramirez', 'duration': 1020},
            {'id': 'RGoIO3wUn60', 'title': 'Checkmate With Bishop and Knight (Easy Method)', 'channel': 'NM Robert Ramirez', 'duration': 1379},
            {'id': 'hKiKMdAXJ9A', 'title': 'Fundamental Chess Endgames | Philidor Position', 'channel': 'NM Robert Ramirez', 'duration': 767},
        ],
    },
}

# Stable display order for section-to-section navigation.
SECTION_ORDER = ['new-to-chess', 'beginner', 'intermediate', 'advanced']


def get_section(slug):
    return CURRICULUM_SECTIONS.get(slug)


def ordered_sections():
    return [CURRICULUM_SECTIONS[s] for s in SECTION_ORDER]


def format_duration(seconds):
    if not seconds:
        return ''
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'
