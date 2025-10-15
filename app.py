import os
import json
import logging
import time
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, abort, session, Response, flash, send_from_directory
)

from db import get_connection
from labels import labels_bp
from inventory import inventory_bp
from reports_bp import reports_bp
from auth_logic import verify_user
from billing_logic import (
    lp_ensure_table,
    REDEEM_CAP, EARN_PER_100,
)
from threading import Thread

from controllers.billing_controller import billing_bp
from controllers.history_controller import history_bp



def create_app() -> Flask:
    # Logging (stdout)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("drip")

    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    # -----------------------------
    # Health / Diagnostics
    # -----------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/diag")
    def diag():
        status = {
            "ok": True,
            "db_connect_ok": False,
            "db_url": os.getenv("DATABASE_URL", "<not set>"),
        }
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_database(), version()")
                    dbname, version = cur.fetchone()
            status["db_connect_ok"] = True
            status["db_name"] = dbname
            status["db_version"] = version.split(" on ")[0]
        except Exception as e:
            status["ok"] = False
            status["db_error"] = str(e)
        return jsonify(status)

    # --- Quick DB sanity on boot (non-fatal) ---
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        log.info("Boot: DB connection OK")
    except Exception as e:
        log.error("Boot: DB connection FAILED: %s", e)

    # --- Create tables (non-fatal) ---
    try:
        # tiny loyalty table + sales_history columns (safe to run)
        lp_ensure_table()
        log.info("Boot: loyalty ensure OK")
    except Exception as e:
        log.error("Boot: loyalty ensure FAILED: %s", e)

    # -----------------------------
    # Public endpoints allow-list (no auth)
    # -----------------------------
    PUBLIC_PREFIXES = (
    "/receipt/lines/",   # plain invoice text for Android
    "/receipts/tspl/",   # TSPL for full receipt
    "/labels/tspl/",     # TSPL for barcode labels (Android app fetches this)
    "/health", "/healthz", "/diag",
    "/__routes", "/__endpoint",
    "/static/", "/favicon.ico"
    )

    @app.before_request
    def _auth_guard():
        p = (request.path or "/")

        # Public endpoints
        if any(p.startswith(pref) for pref in PUBLIC_PREFIXES):
            return

        # Allow login/logout
        if p in ("/login", "/logout"):
            return

        # Everything else needs a session
        if not session.get("user"):
            nxt = request.path
            if request.query_string:
                nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
            return redirect(url_for("login", next=nxt))


    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon'
        )

    _ALLOWED_AFTER_LOGIN_PREFIXES = ("/", "/billing", "/history", "/print", "/reprint")

    # --- Admin helpers ---
    def is_admin() -> bool:
        return session.get("is_admin") is True

    def require_admin():
        if not is_admin():
            abort(403, "Admin only")

    @app.context_processor
    def inject_admin():
        return {"is_admin": is_admin()}

    @app.post("/admin/login")
    def admin_login():
        pin = (request.form.get("pin") or "").strip()
        expected = os.getenv("ADMIN_PIN", "5175")
        nxt = request.form.get("next") or request.referrer or url_for("history")
        if pin == expected:
            session["is_admin"] = True
            return redirect(nxt)
        u = urlparse(nxt)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        q["err"] = "badpin"
        nxt_err = urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))
        return redirect(nxt_err)

    @app.post("/admin/logout")
    def admin_logout():
        session["is_admin"] = False
        nxt = request.form.get("next") or request.referrer or url_for("history")
        return redirect(nxt)

    # -----------------------------
    # Routes
    # -----------------------------
    @app.route("/")
    def home():
        # /billing is inside the billing blueprint
        return redirect(url_for("billing.billing"))

    # --- Blueprints ---
    app.register_blueprint(billing_bp)   # all billing UI + APIs
    app.register_blueprint(labels_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(history_bp) 

    PER_PAGE = 20


    @app.route("/print/<invoice_id>")
    def print_invoice(invoice_id):
        invoice = get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")
        return render_template("print.html", invoice=invoice, label_mm=80)

    @app.route("/print")
    def print_invoice_query():
        invoice_id = (request.args.get("invoice_id") or "").strip()
        if not invoice_id:
            abort(404, "Missing ?invoice_id=...")
        invoice = get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")
        return render_template("print.html", invoice=invoice)

    # -----------------------------
    # Auth routes
    # -----------------------------
    @app.route("/logout", endpoint="logout")
    def logout():
        session.pop("user", None)
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"], endpoint="login")
    def login():
        if request.method == "POST":
            u = (request.form.get("username") or "").strip()
            p = request.form.get("password") or ""
            user = verify_user(u, p)
            if not user:
                flash("Invalid username or password.", "danger")
                return render_template("login.html", username=u), 401
            session["user"] = user  # {'id','username','role'}
            return redirect(_safe_next())
        return render_template("login.html")

    def _safe_next(default_endpoint="billing.billing"):
        raw = request.args.get("next") or ""
        if not raw:
            return url_for(default_endpoint)
        u = urlparse(raw)
        if u.scheme or u.netloc:
            return url_for(default_endpoint)
        path = u.path or "/"
        for pref in _ALLOWED_AFTER_LOGIN_PREFIXES:
            if path == pref or path.startswith(pref + "/") or path.startswith(pref + "?"):
                return path + (("?" + u.query) if u.query else "")
        return url_for(default_endpoint)

    # -----------------------------
    # Background fixer (async repair for admin-edited totals)
    # -----------------------------
    def _loyalty_repair_daemon(interval_s=60):
        """Periodically recompute points for rows flagged unsynced."""
        while True:
            try:
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute("""
                        SELECT invoice_id, phone, grand_total, points_earned, points_redeemed
                        FROM public.sales_history
                        WHERE points_synced_at IS NULL
                        ORDER BY id DESC
                        LIMIT 100
                    """)
                    rows = cur.fetchall()
                for inv, phone, total, earned_old, redeemed in rows:
                    total = float(total or 0)
                    redeemed = int(redeemed or 0)
                    base_amt = max(0, int(round(total)) - int(redeemed or 0))
                    should_earn = int((base_amt * EARN_PER_100) // 100)
                    delta = should_earn - int(earned_old or 0)
                    with get_connection() as conn2, conn2.cursor() as cur2:
                        if delta != 0:
                            cur2.execute("""
                                INSERT INTO loyalty_points(phone, balance, updated_at)
                                VALUES (%s, %s, NOW())
                                ON CONFLICT (phone) DO UPDATE
                                  SET balance = loyalty_points.balance + EXCLUDED.balance,
                                      updated_at = NOW()
                            """, (phone, delta))
                        cur2.execute("""
                            UPDATE public.sales_history
                               SET points_earned=%s, points_synced_at=NOW()
                             WHERE invoice_id=%s
                        """, (should_earn, inv))
                        conn2.commit()
            except Exception:
                # keep daemon alive
                pass
            time.sleep(interval_s)

    # start repair thread (daemon)
    Thread(target=_loyalty_repair_daemon, daemon=True).start()

    # --- Debug helpers (keep ONE version) ---
    @app.get("/__routes")
    def __routes():
        return {"routes": [
            {"rule": r.rule, "endpoint": r.endpoint}
            for r in app.url_map.iter_rules()
        ]}

    return app


# -----------------------------
# Entry point (keep this)
# -----------------------------
app = create_app()

if __name__ == "__main__":
    try:
        import auth_init  # noqa: F401
    except Exception:
        pass

    print("Starting dev server on http://127.0.0.1:5000 …")
    app.run(host="0.0.0.0", port=5000, debug=True)
