# controllers/history_controller.py
import time
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, jsonify,
    session, abort
)

from db import get_connection
from billing_logic import (
    get_invoices,
    get_invoice_text,
    EARN_PER_100,
)

history_bp = Blueprint("history", __name__)

# ---- auth helpers used by templates ----
def is_admin() -> bool:
    return session.get("is_admin") is True

def require_admin():
    if not is_admin():
        abort(403, "Admin only")

@history_bp.app_context_processor
def inject_admin():
    return {"is_admin": is_admin()}

PER_PAGE = 10

# -----------------------------
# /history (list + search)
# -----------------------------
@history_bp.route("/history", methods=["GET", "POST"], endpoint="index")
def index():
    if request.method == "POST":
        q = (request.form.get("search") or request.form.get("q") or "").strip()
        return redirect(url_for("history.index", q=q, page=1))

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

# -----------------------------
# Reprint (view invoice again)
# -----------------------------
@history_bp.get("/reprint/<invoice_id>")
def reprint(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        return f"Invoice {invoice_id} not found", 404
    return render_template("invoice.html", invoice=invoice, invoice_id=invoice_id)

# -----------------------------
# Admin: update total / delete
# -----------------------------
@history_bp.post("/invoices/<invoice_id>/update_total")
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
        return redirect(url_for("history.index", err="badtotal"))

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
        return redirect(url_for("history.index"))
    finally:
        conn.close()

@history_bp.post("/invoices/<invoice_id>/delete")
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
        return redirect(url_for("history.index"))
    finally:
        conn.close()