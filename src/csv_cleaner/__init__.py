"""Flask application factory."""

import secrets

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from .state import SessionStore


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024 + 256 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    app.json.ensure_ascii = False
    app.extensions["jobs"] = SessionStore()
    CSRFProtect(app)

    from .web import bp

    app.register_blueprint(bp)
    return app
