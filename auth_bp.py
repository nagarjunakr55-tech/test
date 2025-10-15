# auth_bp.py
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth_logic import ensure_user_schema, verify_user
from urllib.parse import urlparse

auth_bp = Blueprint("auth", __name__, template_folder="templates")

# Where it's safe to redirect after login (prevents open redirect)
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

# Run once when the blueprint is registered (ensures user table exists)
@auth_bp.record_once
def _on_register(state):
    ensure_user_schema()

# 🔒 Global login guard: runs before every request in the app
@auth_bp.before_app_request
def _guard():
    # exact open paths (no login required)
    OPEN_PATHS = {
        "/login",
        "/health",
        "/healthz",
        "/diag",
        "/favicon.ico",
    }
    # open prefixes (no login required)
    OPEN_PREFIXES = (
        "/static/",          # static assets
        "/labels/tspl/",     # ANDROID LABEL DOWNLOADS (tspl://printer fetch)
        "/receipts/tspl/",   # TSPL receipt downloads (if used)
        "/receipt/lines/",   # plain-text receipt body (if used)
        "/__routes",         # optional debug helpers
        "/__endpoint",       # optional debug helpers
    )

    p = (request.path or "/")
    # allow open paths/prefixes
    if p in OPEN_PATHS or any(p.startswith(pref) for pref in OPEN_PREFIXES):
        return

    # if not logged in, redirect to login with ?next=...
    if not session.get("user"):
        nxt = request.path
        if request.query_string:
            nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
        return redirect(url_for("login", next=nxt))

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
        return redirect(_safe_next())   # 👈 safe redirect

    return render_template("login.html")

@auth_bp.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out.", "info")
    return redirect(url_for("login"))
