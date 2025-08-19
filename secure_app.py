# secure_app.py
import os
from flask import session, request, redirect, url_for
from app import app as base_app            # imports your existing app instance
from auth_bp import auth_bp

app = base_app

# Secret key for sessions (kept outside your existing files)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "please-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
# app.config["SESSION_COOKIE_SECURE"] = True  # enable under HTTPS in prod

# Register the login/logout blueprint
app.register_blueprint(auth_bp)

# Global login gate (allowlist a few endpoints)
OPEN_PATHS = {"/login", "/health", "/healthz", "/diag"}

@app.before_request
def require_login():
    p = request.path
    if p in OPEN_PATHS or p.startswith("/static/"):
        return
    if not session.get("user"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login", next=next_url))

if __name__ == "__main__":
    print("Starting secure server on http://127.0.0.1:5000 …")
    app.run(host="0.0.0.0", port=5001, debug=True)
