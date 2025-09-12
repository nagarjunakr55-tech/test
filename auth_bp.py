# auth_bp.py
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth_logic import ensure_user_schema, verify_user
from urllib.parse import urlparse
from flask import current_app

auth_bp = Blueprint("auth", __name__, template_folder="templates")
_ALLOWED_AFTER_LOGIN_PREFIXES = (
    "/",
    "/billing",
    "/history",
    "/print",     # includes /print?invoice_id=...
    "/reprint",   # includes /reprint/<invoice_id>
)
def _safe_next(default_endpoint="billing"):
    """Return a safe local URL to redirect after login."""
    raw = request.args.get("next") or ""
    if not raw:
        return url_for(default_endpoint)

    u = urlparse(raw)
    # disallow external redirects
    if u.scheme or u.netloc:
        return url_for(default_endpoint)

    path = u.path or "/"
    # only allow known routes/prefixes
    for pref in _ALLOWED_AFTER_LOGIN_PREFIXES:
        if path == pref or path.startswith(pref + "/") or path.startswith(pref + "?"):
            # reconstruct path + query
            return path + (("?" + u.query) if u.query else "")

    return url_for(default_endpoint)
# Run once when the blueprint is registered
@auth_bp.record_once
def _on_register(state):
    ensure_user_schema()

# 🔒 Global login guard: runs before every request in the app
@auth_bp.before_app_request
def _guard():
    open_paths = {"/login", "/health", "/healthz", "/diag"}
    p = (request.path or "/").rstrip("/") or "/"
    # allow static + open endpoints
    if p in open_paths or p.startswith("/static"):
        return
    # if not logged in, redirect to login
    if not session.get("user"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login", next=next_url))

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        user = verify_user(u, p)
        if not user:
            flash("Invalid username or password.", "danger")
            return render_template("login.html", username=u)

        session["user"] = user  # {'id','username','role'}
        return redirect(_safe_next())   # 👈 use the safe redirect
    return render_template("login.html")
@auth_bp.before_app_request
def _guard():
    open_paths = {"/login", "/health", "/healthz", "/diag"}
    p = (request.path or "/")
    if p in open_paths or p.startswith("/static"):
        return
    if not session.get("user"):
        # keep only path+query from current request
        nxt = request.path
        if request.query_string:
            nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
        return redirect(url_for("auth.login", next=nxt))


# auth_bp.py
@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))

