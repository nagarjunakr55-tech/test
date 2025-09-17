# app.py (merged: app.py + auth_bp.py)
import os
import json
import logging
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, abort,
    session, Response, flash, send_from_directory
)

# ---- your existing imports ----
from billing_logic import (
    ensure_tables,
    generate_unique_invoice_id,
    generate_invoice_text,
    save_to_db,
    get_invoices,
    get_invoice_text,
    search_products,
    get_last_customer_name,
    calculate_totals,
)
from db import get_connection
from labels import labels_bp
from inventory import inventory_bp
from reports_bp import reports_bp

# ---- auth logic stays as a module (not a blueprint) ----
from auth_logic import ensure_user_schema, verify_user


# ---------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------
def create_app() -> Flask:
    # Logging (stdout)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("drip")

    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    # Ensure auth schema exists (moved from auth_bp.record_once)
    try:
        ensure_user_schema()
        log.info("Boot: ensure_user_schema OK")
    except Exception as e:
        log.error("Boot: ensure_user_schema FAILED: %s", e)

    # -----------------------------
    # Auth: safe redirect handling
    # -----------------------------
    _ALLOWED_AFTER_LOGIN_PREFIXES = (
        "/", "/billing", "/history", "/print", "/reprint"
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
        for pref in _ALLOWED_AFTER_LOGIN_PREFIXES:
            if path == pref or path.startswith(pref + "/") or path.startswith(pref + "?"):
                return path + (("?" + u.query) if u.query else "")
        return url_for(default_endpoint)

    # ---------- PUBLIC allowlist so Android can fetch raw lines without login ----------
    PUBLIC_PREFIXES = (
        "/receipt/lines/",       # plain text invoice body
        "/receipts/tspl/",       # TSPL for Android printers
        "/health", "/healthz", "/diag",
        "/__routes", "/__endpoint",
        "/static/", "/favicon.ico"
    )

    # -----------------------------
    # Global auth guard
    # -----------------------------
    @app.before_request
    def _auth_guard():
        p = (request.path or "/")
        # public endpoints
        if any(p.startswith(pref) for pref in PUBLIC_PREFIXES):
            return
        # allow login itself
        if p == "/login" or p.startswith("/login?"):
            return
        # require session["user"]
        if not session.get("user"):
            nxt = request.path
            if request.query_string:
                nxt += "?" + request.query_string.decode("utf-8", errors="ignore")
            return redirect(url_for("auth.login", next=nxt))

    # -----------------------------
    # Auth routes (keeps endpoint names 'auth.login' & 'auth.logout')
    # -----------------------------
    @app.route("/login", methods=["GET", "POST"], endpoint="auth.login")
    def auth_login():
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

    @app.route("/logout", endpoint="auth.logout")
    def auth_logout():
        session.pop("user", None)
        flash("Logged out.", "info")
        return redirect(url_for("auth.login"))

    # -----------------------------
    # Favicon
    # -----------------------------
    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon'
        )

    # -----------------------------
    # Receipts (Android printing)
    # -----------------------------
    @app.get("/receipts/tspl/<invoice_id>.txt")
    def receipts_tspl(invoice_id):
        import re
        copies = int((request.args.get("copies") or "1").strip() or 1)
        invoice = get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")

        date_only_pat = re.compile(
            r"(Date:\s*\d{2}-\d{2}-\d{4})(?:\s+\d{2}:\d{2}(?:\s*[AP]M)?)?"
        )
        lines = [date_only_pat.sub(r"\1", ln) for ln in invoice.splitlines()]

        y = 20
        dy = 28
        tspl = [
            "SIZE 72 mm, 200 mm",
            "GAP 2 mm, 0",
            "DIRECTION 1",
            "REFERENCE 0,0",
            "CLS",
        ]
        for line in lines[:250]:
            safe = line.replace('"', "'")
            tspl.append(f'TEXT 20,{y},"0",0,1,1,"{safe}"')
            y += dy
        tspl.append(f"PRINT {copies},1")

        return Response("\n".join(tspl) + "\n", mimetype="text/plain; charset=utf-8")

    @app.get("/receipt/lines/<invoice_id>.txt")
    def receipt_lines(invoice_id):
        invoice = get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")
        body = invoice if invoice.endswith("\n") else invoice + "\n"
        return Response(body, mimetype="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    # -----------------------------
    # Admin helpers (PIN for History/Reports links)
    # -----------------------------
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
        if pin == expected:
            session["is_admin"] = True
            return redirect(url_for("history"))
        return redirect(url_for("history", err="badpin"))

    @app.post("/admin/logout")
    def admin_logout():
        session["is_admin"] = false
        return redirect(url_for("history"))

    @app.post("/invoices/<invoice_id>/update_total")
    def invoices_update_total(invoice_id):
        require_admin()
        raw = (request.form.get("grand_total") or "").strip()
        try:
            new_total = Decimal(raw)
            if new_total < 0:
                raise InvalidOperation
        except InvalidOperation:
            return redirect(url_for("history", err="badtotal"))

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.sales_history SET grand_total=%s WHERE invoice_id=%s",
                        (new_total, invoice_id),
                    )
            return redirect(url_for("history"))
        finally:
            conn.close()

    # -----------------------------
    # Blueprints (labels/inventory/reports)
    # -----------------------------
    app.register_blueprint(labels_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(reports_bp)

    # -----------------------------
    # Debug helpers
    # -----------------------------
    @app.get("/__routes")
    def __routes():
        return {"routes": [
            {"rule": r.rule, "endpoint": r.endpoint}
            for r in app.url_map.iter_rules()
        ]}

    @app.get("/__endpoint/<path:endpoint>")
    def __endpoint_locator(endpoint):
        view = app.view_functions.get(endpoint)
        if not view:
            abort(404, f"No endpoint named {endpoint!r}")
        import inspect
        file = inspect.getsourcefile(view)
        line = inspect.getsourcelines(view)[1]
        return {
            "endpoint": endpoint,
            "file": file,
            "line": line,
            "qualname": getattr(view, "__qualname__", view.__name__),
        }

    log.info("Boot: Flask app created")

    # --- Quick DB sanity on boot (non-fatal) ---
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        log.info("Boot: DB connection OK")
    except Exception as e:
        log.error("Boot: DB connection FAILED: %s", e)

    # --- Create app tables (non-fatal) ---
    try:
        ensure_tables()
        log.info("Boot: ensure_tables OK")
    except Exception as e:
        log.error("Boot: ensure_tables FAILED: %s", e)

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

    @app.get("/health")
    def health():
        return {"ok": True}, 200

    # -----------------------------
    # Home & pages
    # -----------------------------
    @app.route("/")
    def home():
        if session.get("user"):
            return redirect(url_for("billing"))
        return redirect(url_for("auth.login"))

    # Billing (GET = UI, POST = legacy form fallback)
    @app.route("/billing", methods=["GET", "POST"])
    def billing():
        if request.method == "POST":
            name = (request.form.get("name") or "Guest").strip() or "Guest"
            phone = (request.form.get("phone") or "").strip()
            cart_json = request.form.get("cart_json")
            discount_input = (request.form.get("discount") or "").strip()

            if cart_json:
                try:
                    items = json.loads(cart_json)
                except Exception:
                    items = []
            else:
                items = [
                    {"brand": "Nike", "mrp_price": 1200, "bought_price": 800, "price": 1000, "qty": 1, "size": "M", "category": "Shirt"},
                    {"brand": "Adidas", "mrp_price": 1500, "bought_price": 1000, "price": 1200, "qty": 2, "size": "L", "category": "Pant"},
                ]

            if not (phone.isdigit() and len(phone) == 10):
                return render_template("billing.html", error="Enter valid 10-digit phone"), 400

            totals = calculate_totals(items, discount_input)
            items_for_receipt = totals.get("items_effective", items)
            invoice_id = generate_unique_invoice_id()
            invoice_text = generate_invoice_text(
                invoice_id=invoice_id,
                customer_name=name,
                customer_phone=phone,
                items=items_for_receipt,
                subtotal=totals["subtotal"],
                gst_amount=totals["gst"],
                grand_total=totals["grand_total"],
                discount_amt=totals["discount_amt"]
            )
            save_to_db(
                invoice_id=invoice_id,
                name=name,
                phone=phone,
                items=items_for_receipt,
                subtotal=totals["subtotal"],
                discount_amt=totals["discount_amt"],
                gst_amt=totals["gst"],
                grand_total=totals["grand_total"],
                invoice_text=invoice_text
            )
            return render_template("invoice.html", invoice=invoice_text, invoice_id=invoice_id)

        return render_template("billing.html")

    @app.route("/history", methods=["GET", "POST"])
    def history():
        search_term = request.form.get("search", "").strip() if request.method == "POST" else ""
        rows = get_invoices(search_term)
        return render_template("history.html", rows=rows, search_term=search_term)

    @app.route("/reprint/<invoice_id>")
    def reprint(invoice_id):
        invoice = get_invoice_text(invoice_id)
        if invoice:
            return render_template("invoice.html", invoice=invoice, invoice_id=invoice_id)
        return f"Invoice {invoice_id} not found", 404

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
    # APIs used by billing.html JS
    # -----------------------------
    @app.post("/api/barcode_lookup")
    def api_barcode_lookup():
        data = request.get_json(silent=True) or {}
        code = (data.get("barcode") or "").strip().upper()
        if not code:
            return jsonify({"ok": False, "error": "empty_barcode"}), 400

        def to_int_or_none(x):
            if x is None: return None
            try:
                return int(float(str(x).strip()))
            except Exception:
                return None

        row = None
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand, category, size, mrp, bought_price, selling_price
                    FROM public.barcode_catalog
                    WHERE barcode_data = %s
                    LIMIT 1
                    """,
                    (code,)
                )
                row = cur.fetchone()

        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404

        brand, category, size, mrp, bought, sp = row
        item = {
            "code":        code,
            "brand":       brand or "",
            "category":    category or "",
            "size":        size or "",
            "qty":         1,
            "mrp_price":   to_int_or_none(mrp),
            "bought_price":to_int_or_none(bought),
            "price":       to_int_or_none(sp) or 0
        }
        return jsonify({"ok": True, "item": item})

    @app.post("/api/search_products")
    def api_search_products():
        data = request.get_json(silent=True) or {}
        brand = (data.get("brand") or "").strip()
        category = (data.get("category") or "").strip()
        size = (data.get("size") or "").strip()
        limit = int(data.get("limit") or 20)
        rows = search_products(brand=brand, category=category, size=size, limit=limit)
        out = []
        for r in rows:
            out.append({
                "brand": r["brand"],
                "mrp_price": float(r.get("mrp", 0)),
                "bought_price": float(r.get("bought_price", 0)),
                "price": float(r.get("selling_price", 0)),
                "qty": 1,
                "size": r.get("size", ""),
                "category": r.get("category", "")
            })
        return jsonify({"ok": True, "results": out})

    @app.get("/api/customer_last_name")
    def api_customer_last_name():
        phone = (request.args.get("phone") or "").strip()
        if not phone or not phone.isdigit() or len(phone) != 10:
            return jsonify({"ok": False, "name": ""})
        name = get_last_customer_name(phone)
        return jsonify({"ok": True, "name": name or ""})

    @app.post("/invoices/<invoice_id>/delete", endpoint="invoices_delete")
    def invoices_delete(invoice_id):
        require_admin()
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # If you have dependent tables, delete those first.
                    # cur.execute("DELETE FROM public.sales_items WHERE invoice_id=%s", (invoice_id,))
                    cur.execute(
                        "DELETE FROM public.sales_history WHERE invoice_id=%s",
                        (invoice_id,)
                    )
            return redirect(url_for("history"))
        finally:
            conn.close()
    

    @app.post("/api/checkout")
    def api_checkout():
        data = request.get_json(silent=True) or {}
        cust_name = (data.get("customer_name") or "Guest").strip() or "Guest"
        phone = (data.get("phone") or "").strip()

        if not (phone.isdigit() and len(phone) == 10):
            return jsonify({"ok": False, "error": "invalid_phone"}), 400

        items = data.get("items") or []
        if not items:
            return jsonify({"ok": False, "error": "empty_cart"}), 400

        try:
            totals = calculate_totals(items, data.get("discount", ""))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        items_for_receipt = totals.get("items_effective", items)
        is_preview = bool(data.get("preview"))

        if is_preview:
            invoice_text = generate_invoice_text(
                invoice_id="",
                customer_name=cust_name,
                customer_phone=phone,
                items=items_for_receipt,
                subtotal=totals["subtotal"],
                gst_amount=totals["gst"],
                grand_total=totals["grand_total"],
                discount_amt=totals["discount_amt"],
            )
            return jsonify({"ok": True, "invoice_text": invoice_text})

        invoice_id = generate_unique_invoice_id()
        invoice_text = generate_invoice_text(
            invoice_id=invoice_id,
            customer_name=cust_name,
            customer_phone=phone,
            items=items_for_receipt,
            subtotal=totals["subtotal"],
            gst_amount=totals["gst"],
            grand_total=totals["grand_total"],
            discount_amt=totals["discount_amt"],
        )
        save_to_db(
            invoice_id=invoice_id,
            name=cust_name,
            phone=phone,
            items=items_for_receipt,
            subtotal=totals["subtotal"],
            discount_amt=totals["discount_amt"],
            gst_amt=totals["gst"],
            grand_total=totals["grand_total"],
            invoice_text=invoice_text
        )
        return jsonify({"ok": True, "invoice_id": invoice_id, "invoice_text": invoice_text})

    return app


# Create the app
app = create_app()

# Dev server entrypoint
if __name__ == "__main__":
    print("Starting dev server on http://127.0.0.1:5001 …")
    app.run(host="0.0.0.0", port=5000, debug=True)
