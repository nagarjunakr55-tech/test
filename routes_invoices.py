# routes_invoices.py
from flask import (
    current_app, request, render_template, redirect, url_for,
    session, flash
)
from db import get_connection
from logic.billing_logic import get_invoices, get_invoice_text
from decimal import Decimal, InvalidOperation

# -----------------------------
# Helpers / bootstrap tables
# -----------------------------

def _ensure_sales_barcodes_table():
    """Create sales_barcodes if it doesn't exist (idempotent)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.sales_barcodes (
                    id SERIAL PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    qty INTEGER NOT NULL DEFAULT 1
                )
            """)
        conn.commit()

def _coerce_money(val):
    if val is None:
        return None
    try:
        # Accept strings like "123", "123.45"
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None

def _next_url_fallback():
    """
    Prefer ?next=... in query string or the Referer; else go to history list.
    This keeps you on the same page after actions (update/delete/login).
    """
    nxt = request.args.get("next") or request.referrer
    if not nxt:
        try:
            nxt = url_for("history")
        except Exception:
            nxt = "/"
    return nxt

# -----------------------------
# Route registration
# -----------------------------

def register_invoice_routes(app):
    """
    Bind routes directly to 'app' so your templates can continue using:
      url_for('history'), url_for('admin_login'), url_for('admin_logout'),
      url_for('invoices_update_total', invoice_id=...), url_for('invoices_delete', ...),
      url_for('reprint', invoice_id=...)
    """
    # make sure sessions work
    if not app.secret_key:
        app.secret_key = app.config.get("SECRET_KEY", "dev-secret-key")

    # --------- HISTORY (GET/POST search) ----------
    def history():
        search_term = ""
        if request.method == "POST":
            search_term = (request.form.get("search") or "").strip()
        else:
            search_term = (request.args.get("search") or "").strip()

        try:
            rows = get_invoices(search_term)
        except Exception as e:
            current_app.logger.exception("history: get_invoices failed")
            rows = []
            flash(f"Failed to load invoices: {e}", "error")

        return render_template(
            "history.html",
            rows=rows,
            search_term=search_term,
            is_admin=bool(session.get("is_admin"))
        )

    app.add_url_rule("/history", view_func=history, methods=["GET", "POST"], endpoint="history")

    # --------- ADMIN LOGIN ----------
    def admin_login():
        pin = (request.form.get("pin") or "").strip()
        expected = str(current_app.config.get("ADMIN_PIN", "1234"))
        if pin == expected:
            session["is_admin"] = True
            flash("Admin logged in.", "success")
            return redirect(_next_url_fallback())
        else:
            # Preserve your existing behavior: ?err=badpin so template shows message
            return redirect(url_for("history", err="badpin"))

    app.add_url_rule("/admin/login", view_func=admin_login, methods=["POST"], endpoint="admin_login")

    # --------- ADMIN LOGOUT ----------
    def admin_logout():
        session["is_admin"] = False
        flash("Logged out.", "success")
        return redirect(_next_url_fallback())

    app.add_url_rule("/admin/logout", view_func=admin_logout, methods=["POST"], endpoint="admin_logout")

    # --------- UPDATE TOTAL (ADMIN) ----------
    def invoices_update_total(invoice_id):
        if not session.get("is_admin"):
            flash("Admin only.", "error")
            return redirect(_next_url_fallback())

        new_total = _coerce_money(request.form.get("grand_total"))
        if new_total is None:
            flash("Invalid amount.", "error")
            return redirect(_next_url_fallback())

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sales_history SET grand_total = %s WHERE invoice_id = %s",
                        (str(new_total), invoice_id)
                    )
                conn.commit()
            flash(f"Invoice {invoice_id} total updated to ₹{new_total}.", "success")
        except Exception as e:
            current_app.logger.exception("invoices_update_total failed")
            flash(f"Update failed: {e}", "error")

        return redirect(_next_url_fallback())

    app.add_url_rule(
        "/invoices/<invoice_id>/update_total",
        view_func=invoices_update_total,
        methods=["POST"],
        endpoint="invoices_update_total"
    )

    # --------- DELETE INVOICE (ADMIN) ----------
    def invoices_delete(invoice_id):
        if not session.get("is_admin"):
            flash("Admin only.", "error")
            return redirect(_next_url_fallback())

        # Ensure sales_barcodes table exists (so delete below never fails)
        _ensure_sales_barcodes_table()

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # 1) Remove barcode rows so Inventory reflects correctly
                    cur.execute("DELETE FROM public.sales_barcodes WHERE invoice_id = %s", (invoice_id,))
                    # 2) Remove invoice header
                    cur.execute("DELETE FROM sales_history WHERE invoice_id = %s", (invoice_id,))
                conn.commit()
            flash(f"Invoice {invoice_id} deleted.", "success")
        except Exception as e:
            current_app.logger.exception("invoices_delete failed")
            flash(f"Delete failed: {e}", "error")

        return redirect(_next_url_fallback())

    app.add_url_rule(
        "/invoices/<invoice_id>/delete",
        view_func=invoices_delete,
        methods=["POST"],
        endpoint="invoices_delete"
    )

    # --------- REPRINT PREVIEW ----------
    def reprint(invoice_id):
        try:
            invoice_text = get_invoice_text(invoice_id)
        except Exception as e:
            current_app.logger.exception("reprint: get_invoice_text failed")
            invoice_text = None

        if not invoice_text:
            flash("Invoice not found.", "error")
            return redirect(url_for("history"))

        # Your template `invoice_preview.html` expects: invoice_id, invoice
        return render_template("invoice_preview.html", invoice_id=invoice_id, invoice=invoice_text)

    app.add_url_rule("/reprint/<invoice_id>", view_func=reprint, methods=["GET"], endpoint="reprint")
