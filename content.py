"""Blog posts and partnership listings shown on the homepage.

Both are admin-only to *write* — reuses the same admin gate as app.py's
/admin dashboard (a small local copy of the check rather than an import
from app.py, to avoid a circular import; same reasoning db.py's per-module
get_db() wrapper already follows for this codebase).

Registered as a blueprint from app.py, same pattern as game_analysis.py.
"""
import re
from functools import wraps

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

import db

content_bp = Blueprint('content', __name__)

ADMIN_USERNAME = 'LohitGold123'

EXCERPT_LEN = 200


def get_db():
    return db.connect()


def init_content_db():
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS blog_posts (
                id          SERIAL    PRIMARY KEY,
                slug        TEXT      UNIQUE NOT NULL,
                title       TEXT      NOT NULL,
                excerpt     TEXT,
                body        TEXT      NOT NULL,
                author      TEXT,
                published   INTEGER   DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS partnerships (
                id           SERIAL    PRIMARY KEY,
                name         TEXT      NOT NULL,
                description  TEXT,
                url          TEXT,
                logo_url     TEXT,
                sort_order   INTEGER   DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
    finally:
        conn.close()


# ── Admin gate ─────────────────────────────────────────────────────────────

def _is_admin() -> bool:
    return bool(session.get('is_admin')) or session.get('username') == ADMIN_USERNAME


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    return slug or 'post'


def _unique_slug(conn, base_slug: str) -> str:
    slug = base_slug
    n = 2
    while conn.execute('SELECT id FROM blog_posts WHERE slug = ?', (slug,)).fetchone():
        slug = f'{base_slug}-{n}'
        n += 1
    return slug


def _paragraphs(body: str):
    """Split plain-text body on blank lines into paragraphs — no markdown
    dependency, just enough structure for a clean read."""
    return [p.strip() for p in re.split(r'\n\s*\n', body or '') if p.strip()]


# ── Public: blog ─────────────────────────────────────────────────────────────

@content_bp.route('/blog')
def blog_index():
    conn = get_db()
    try:
        posts = conn.execute(
            '''SELECT slug, title, excerpt, author, created_at FROM blog_posts
               WHERE published = 1 ORDER BY created_at DESC'''
        ).fetchall()
    finally:
        conn.close()
    return render_template('blog_index.html', posts=posts)


@content_bp.route('/blog/<slug>')
def blog_post(slug):
    conn = get_db()
    try:
        post = conn.execute(
            'SELECT * FROM blog_posts WHERE slug = ? AND published = 1', (slug,)
        ).fetchone()
    finally:
        conn.close()
    if post is None:
        abort(404)
    return render_template('blog_post.html', post=post, paragraphs=_paragraphs(post['body']))


# ── Admin: blog ───────────────────────────────────────────────────────────────

@content_bp.route('/admin/blog', methods=['GET', 'POST'])
@admin_required
def admin_blog():
    conn = get_db()
    try:
        if request.method == 'POST':
            title   = (request.form.get('title') or '').strip()
            excerpt = (request.form.get('excerpt') or '').strip()
            body    = (request.form.get('body') or '').strip()
            author  = (request.form.get('author') or '').strip() or session.get('username') or 'Synapchess Team'
            published = 1 if request.form.get('published') else 0

            if title and body:
                if not excerpt:
                    excerpt = (body[:EXCERPT_LEN] + '…') if len(body) > EXCERPT_LEN else body
                slug = _unique_slug(conn, _slugify(title))
                conn.execute(
                    '''INSERT INTO blog_posts (slug, title, excerpt, body, author, published)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (slug, title, excerpt, body, author, published)
                )
                conn.commit()
            return redirect(url_for('content.admin_blog'))

        posts = conn.execute('SELECT * FROM blog_posts ORDER BY created_at DESC').fetchall()
    finally:
        conn.close()
    return render_template('admin_blog.html', posts=posts, default_author=session.get('username'))


@content_bp.route('/admin/blog/<int:post_id>/toggle', methods=['POST'])
@admin_required
def admin_blog_toggle(post_id):
    conn = get_db()
    try:
        conn.execute(
            '''UPDATE blog_posts SET published = 1 - published,
                                      updated_at = (NOW() AT TIME ZONE 'UTC')
               WHERE id = ?''',
            (post_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('content.admin_blog'))


@content_bp.route('/admin/blog/<int:post_id>/delete', methods=['POST'])
@admin_required
def admin_blog_delete(post_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM blog_posts WHERE id = ?', (post_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('content.admin_blog'))


# ── Admin: partnerships ────────────────────────────────────────────────────────

@content_bp.route('/admin/partnerships', methods=['GET', 'POST'])
@admin_required
def admin_partnerships():
    conn = get_db()
    try:
        if request.method == 'POST':
            name        = (request.form.get('name') or '').strip()
            description = (request.form.get('description') or '').strip()
            partner_url = (request.form.get('url') or '').strip()
            logo_url    = (request.form.get('logo_url') or '').strip()
            try:
                sort_order = int(request.form.get('sort_order') or 0)
            except ValueError:
                sort_order = 0

            if name:
                conn.execute(
                    '''INSERT INTO partnerships (name, description, url, logo_url, sort_order)
                       VALUES (?, ?, ?, ?, ?)''',
                    (name, description or None, partner_url or None, logo_url or None, sort_order)
                )
                conn.commit()
            return redirect(url_for('content.admin_partnerships'))

        partners = conn.execute(
            'SELECT * FROM partnerships ORDER BY sort_order ASC, created_at DESC'
        ).fetchall()
    finally:
        conn.close()
    return render_template('admin_partnerships.html', partners=partners)


@content_bp.route('/admin/partnerships/<int:partner_id>/delete', methods=['POST'])
@admin_required
def admin_partnership_delete(partner_id):
    conn = get_db()
    try:
        conn.execute('DELETE FROM partnerships WHERE id = ?', (partner_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('content.admin_partnerships'))


# ── Homepage data ────────────────────────────────────────────────────────────

def get_homepage_content():
    """Latest published posts + all partnerships, for the index() route to
    hand to templates/index.html. Empty lists render as no section at all
    (see the {% if %} guards there) rather than an empty placeholder."""
    conn = get_db()
    try:
        posts = conn.execute(
            '''SELECT slug, title, excerpt, author, created_at FROM blog_posts
               WHERE published = 1 ORDER BY created_at DESC LIMIT 3'''
        ).fetchall()
        partners = conn.execute(
            '''SELECT name, description, url, logo_url FROM partnerships
               ORDER BY sort_order ASC, created_at DESC'''
        ).fetchall()
    finally:
        conn.close()
    return posts, partners
