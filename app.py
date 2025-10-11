import os
import json
import logging
from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, abort, session, Response, flash
)

from billing_logic import (
    # core
    generate_unique_invoice_id,
    generate_invoice_text,
    save_to_db,
    get_invoices,
    get_invoice_text,
    find_product_by_barcode,
    search_products,
    get_last_customer_name,
    calculate_totals,
    # loyalty
    lp_ensure_table, lp_get_balance, loyalty_simulate_preview, loyalty_finalize, REDEEM_CAP,EARN_PER_100
)

from db import get_connection
from labels import labels_bp
from inventory import inventory_bp
from flask import send_from_directory
from reports_bp import reports_bp
from auth_logic import ensure_user_schema, verify_user
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from threading import Thread
import time
from decimal import Decimal, InvalidOperation

PRINT_CACHE = {}           # { code: (invoice_text, expires_epoch) }
PRINT_CACHE_TTL = 120      # seconds

def _cache_put(code: str, text: str, ttl: int = PRINT_CACHE_TTL):
    PRINT_CACHE[code] = (text, time.time() + ttl)

def _cache_get(code: str):
    v = PRINT_CACHE.get(code)
    if not v:
        return None
    text, exp = v
    if time.time() > exp:
        PRINT_CACHE.pop(code, None)
        return None
    return text

def _cache_pop(code: str):
    PRINT_CACHE.pop(code, None)


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
        "/receipt/lines/",
        "/receipts/tspl/",
        "/health", "/healthz", "/diag",
        "/__routes", "/__endpoint",
        "/static/", "/favicon.ico"
    )

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

    # Admin login/logout
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
        return redirect(url_for("billing"))

    # --- Blueprints ---
    app.register_blueprint(labels_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(reports_bp)    

    # Billing (GET = UI, POST = legacy form fallback)
    @app.route("/billing", methods=["GET", "POST"])
    def billing():
        if request.method == "POST":
            # Legacy HTML form fallback
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

            # No loyalty in legacy flow to keep it simple; use UI/JS APIs.
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

        # GET → rich JS UI (uses /api/*)
        return render_template("billing.html")

    PER_PAGE = 20

    @app.route("/history", methods=["GET", "POST"])
    def history():
        if request.method == "POST":
            q = (request.form.get("search") or request.form.get("q") or "").strip()
            return redirect(url_for("history", q=q, page=1))

        q = (request.args.get("q") or request.args.get("search") or "").strip()
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except ValueError:
            page = 1

        offset = (page - 1) * PER_PAGE
        rows, has_next = get_invoices(q, limit=PER_PAGE, offset=offset)

        is_admin_flag = bool(session.get("role") == "admin" or session.get("is_admin"))

        return render_template(
            "history.html",
            rows=rows,
            search_term=q,
            page=page,
            has_next=has_next,
            is_admin=is_admin_flag,
        )

    @app.route("/reprint/<invoice_id>")
    def reprint(invoice_id):
        invoice = get_invoice_text(invoice_id)
        if invoice:
            return render_template("invoice.html", invoice=invoice, invoice_id=invoice_id)
        return f"Invoice {invoice_id} not found", 404

    # Print views (3-inch, no margins)
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

    @app.get("/health")
    def health():
        return {"ok": True}, 200

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

    # ---- Loyalty APIs ----
    @app.get("/api/loyalty_points")
    def api_loyalty_points():
        phone = (request.args.get("phone") or "").strip()
        if not (phone.isdigit() and len(phone) == 10):
            return jsonify({"ok": False, "points": 0, "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100})
        try:
            pts = lp_get_balance(phone)
            return jsonify({"ok": True, "points": int(pts), "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100})
        except Exception:
            return jsonify({"ok": False, "points": 0, "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100}), 500


    # -----------------------------
    # TSPL / Receipt endpoints
    # -----------------------------
    @app.get("/receipt/lines/<invoice_id>.txt")
    def receipt_lines(invoice_id):
        invoice = _cache_get(invoice_id) or get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")
        body = invoice if invoice.endswith("\n") else invoice + "\n"
        return Response(body, mimetype="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @app.get("/receipts/tspl/<invoice_id>.txt")
    def receipts_tspl(invoice_id):
        import re
        copies = int((request.args.get("copies") or "1").strip() or 1)
        invoice = _cache_get(invoice_id) or get_invoice_text(invoice_id)
        if not invoice:
            abort(404, f"Invoice {invoice_id} not found")

        date_only_pat = re.compile(r"(Date:\s*\d{2}-\d{2}-\d{4})(?:\s+\d{2}:\d{2}(?:\s*[AP]M)?)?")
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

    def _safe_next(default_endpoint="billing"):
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
    # Admin: update total / delete
    # -----------------------------
    @app.post("/invoices/<invoice_id>/update_total")
    def invoices_update_total(invoice_id):
        require_admin()
        wants_json = "application/json" in (request.headers.get("Accept") or "")

        # Read value from JSON or from form
        if request.is_json:
            raw = (request.get_json(silent=True) or {}).get("grand_total", "")
        else:
            raw = (request.form.get("grand_total") or "")

        raw = raw.strip()
        try:
            new_total = Decimal(raw)
            if new_total < 0:
                raise InvalidOperation
        except InvalidOperation:
            if wants_json:
                return jsonify({"ok": False, "error": "badtotal"}), 400
            return redirect(url_for("history", err="badtotal"))

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.sales_history SET grand_total=%s, points_synced_at=NULL WHERE invoice_id=%s",
                        (new_total, invoice_id),
                    )
            if wants_json:
                return jsonify({"ok": True})
            return redirect(url_for("history"))
        finally:
            conn.close()

    @app.post("/invoices/<invoice_id>/delete")
    def invoices_delete(invoice_id):
        """Delete sale and undo its loyalty effect immediately (simple & safe)."""
        require_admin()
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Fetch phone + last known loyalty fields
                    cur.execute("""
                        SELECT phone, points_earned, points_redeemed
                        FROM public.sales_history WHERE invoice_id=%s
                    """, (invoice_id,))
                    row = cur.fetchone()
                    if row:
                        phone, earned, redeemed = row[0], int(row[1] or 0), int(row[2] or 0)
                        # Undo net effect: -earned + redeemed
                        cur.execute("""
                            INSERT INTO loyalty_points(phone, balance, updated_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (phone) DO UPDATE
                              SET balance = loyalty_points.balance + EXCLUDED.balance,
                                  updated_at = NOW()
                        """, (phone, -earned + redeemed))
                    # Delete the invoice
                    cur.execute("DELETE FROM public.sales_history WHERE invoice_id=%s", (invoice_id,))
            return redirect(url_for("history"))
        finally:
            conn.close()

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
                pass
            time.sleep(interval_s)

    # start repair thread (daemon)
    Thread(target=_loyalty_repair_daemon, daemon=True).start()

    # -----------------------------
    # Billing APIs: checkout (preview/save)
    # -----------------------------
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

        # Allowed global discount codes are enforced in calculate_totals
        try:
            totals = calculate_totals(items, data.get("discount", ""))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        items_for_receipt = totals.get("items_effective", items)
        is_preview = bool(data.get("preview"))
        requested_redeem = int(data.get("redeem_points") or 0)

        if is_preview:
            use_p, earn_p, bal_p, final_after = loyalty_simulate_preview(
                phone=phone,
                grand_total=float(totals["grand_total"]),
                requested_redeem=requested_redeem
            )
            invoice_text = generate_invoice_text(
                invoice_id="",
                customer_name=cust_name,
                customer_phone=phone,
                items=items_for_receipt,
                subtotal=totals["subtotal"],
                gst_amount=totals["gst"],
                grand_total=final_after,
                discount_amt=totals["discount_amt"],
                points_used=use_p,
                points_earned=earn_p,
                points_balance=bal_p
            )
            return jsonify({"ok": True, "invoice_text": invoice_text})

        # ---------- PRINT/SAVE ----------
        async_save = bool(data.get("async"))
        invoice_id = generate_unique_invoice_id()

        used_pts, earned_pts, new_balance, final_payable = loyalty_finalize(
            phone=phone,
            grand_total=float(totals["grand_total"]),
            requested_redeem=requested_redeem
        )

        invoice_text = generate_invoice_text(
            invoice_id=invoice_id,
            customer_name=cust_name,
            customer_phone=phone,
            items=items_for_receipt,
            subtotal=totals["subtotal"],
            gst_amount=totals["gst"],
            grand_total=final_payable,
            discount_amt=totals["discount_amt"],
            points_used=used_pts,
            points_earned=earned_pts,
            points_balance=new_balance
        )

        if async_save:
            # Cache for printer
            try:
                _cache_put(invoice_id, invoice_text)
            except Exception:
                pass

            def _persist():
                try:
                    # Save invoice body
                    save_to_db(
                        invoice_id=invoice_id,
                        name=cust_name,
                        phone=phone,
                        items=items_for_receipt,
                        subtotal=totals["subtotal"],
                        discount_amt=totals["discount_amt"],
                        gst_amt=totals["gst"],
                        grand_total=final_payable,
                        invoice_text=invoice_text
                    )
                    # Store loyalty fields into sales_history for async repair visibility
                    with get_connection() as conn2, conn2.cursor() as cur2:
                        cur2.execute("""
                            UPDATE public.sales_history
                               SET points_earned=%s,
                                   points_redeemed=%s,
                                   points_synced_at=NOW()
                             WHERE invoice_id=%s
                        """, (earned_pts, used_pts, invoice_id))
                        conn2.commit()
                except Exception:
                    pass

            Thread(target=_persist, daemon=True).start()
            return jsonify({
                "ok": True,
                "invoice_id": invoice_id,
                "invoice_text": invoice_text,
                "points_used": used_pts,
                "points_earned": earned_pts,
                "points_balance": new_balance
            })

        # Sync save
        save_to_db(
            invoice_id=invoice_id,
            name=cust_name,
            phone=phone,
            items=items_for_receipt,
            subtotal=totals["subtotal"],
            discount_amt=totals["discount_amt"],
            gst_amt=totals["gst"],
            grand_total=final_payable,
            invoice_text=invoice_text
        )
        # Update loyalty fields
        with get_connection() as conn2, conn2.cursor() as cur2:
            cur2.execute("""
                UPDATE public.sales_history
                   SET points_earned=%s,
                       points_redeemed=%s,
                       points_synced_at=NOW()
                 WHERE invoice_id=%s
            """, (earned_pts, used_pts, invoice_id))
            conn2.commit()

        return jsonify({
            "ok": True,
            "invoice_id": invoice_id,
            "invoice_text": invoice_text,
            "points_used": used_pts,
            "points_earned": earned_pts,
            "points_balance": new_balance
        })

    # --- Debug helpers (keep ONE version) ---
    @app.get("/__routes")
    def __routes():
        return {"routes": [
            {"rule": r.rule, "endpoint": r.endpoint}
            for r in app.url_map.iter_rules()
        ]}

    return app


app = create_app()

# --- Dev server entrypoint ---
if __name__ == "__main__":
    try:
        import auth_init  # noqa: F401
    except Exception:
        pass

    print("Starting dev server on http://127.0.0.1:5001 …")
    app.run(host="0.0.0.0", port=5000, debug=True)