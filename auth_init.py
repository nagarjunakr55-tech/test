# auth_init.py
import os, sys
# --- debug versions ---
import sys, flask, werkzeug
print("RUNNING WITH:", sys.executable)
print("Flask:", flask.__version__, "Werkzeug:", werkzeug.__version__)
# ----------------------


# Get the real Flask app created in app.py when run as __main__
app = None
main_mod = sys.modules.get("__main__")
if main_mod and hasattr(main_mod, "app"):
    app = main_mod.app
if app is None:
    # Fallback when imported from a WSGI server or tests where module name is 'app'
    from app import app  # noqa: F401

from auth_bp import auth_bp  # after app is resolved

# Minimal session config (kept out of your app.py)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "please-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
# app.config["SESSION_COOKIE_SECURE"] = True  # enable in prod over HTTPS

# Register the auth blueprint (adds /login, /logout and the global guard)
app.register_blueprint(auth_bp)
