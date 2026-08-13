"""Shared sqlite path. Kept out of app.py so game_analysis.py doesn't need to
import app.py (which caused a circular import when app.py is run directly as
__main__ — that execution path re-imports itself under the name "app",
re-triggering the `from game_analysis import games_bp` line while
game_analysis is still mid-import).
"""
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chess_data.db')
