import time
import re
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, session, abort, Response
from db import get_connection

from billing_logic import (
    generate_unique_invoice_id,
    generate_invoice_text,
    save_to_db,
    get_invoices,
    get_invoice_text,
    find_product_by_barcode,
    search_products,
    get_last_customer_name,
    get_last_phone_by_name,
    calculate_totals,
    lp_get_balance,
    loyalty_simulate_preview,
    loyalty_finalize,
    get_receipt_text_for_instant_print,
    REDEEM_CAP,
    EARN_PER_100,
)
from threading import Thread


billing_bp = Blueprint("billing", __name__)

# ---- tiny transport-only cache for printer race ----
_PRINT_CACHE = {}
_PRINT_CACHE_TTL = 300

def _cache_put(code: str, text: str, ttl: int = _PRINT_CACHE_TTL):
    _PRINT_CACHE[code] = (text, time.time() + ttl)

def _cache_get(code: str):
    v = _PRINT_CACHE.get(code)
    if not v:
        return None
    text, exp = v
    if time.time() > exp:
        _PRINT_CACHE.pop(code, None)
        return None
    return text

# ---- auth helpers used by templates ----
def is_admin() -> bool:
    return session.get("is_admin") is True

@billing_bp.app_context_processor
def inject_admin():
    return {"is_admin": is_admin()}

# ----------------- Routes -----------------

@billing_bp.get("/")
def root():
    return redirect(url_for("billing.billing"))

@billing_bp.route("/billing", methods=["GET", "POST"], endpoint="billing")
def billing():
    # Legacy POST fallback kept here; UI uses JS + /api/*
    if request.method == "POST":
        name = (request.form.get("name") or "Guest").strip() or "Guest"
        phone = (request.form.get("phone") or "").strip()
        cart_json = request.form.get("cart_json")
        discount_input = (request.form.get("discount") or "").strip()

        try:
            items = (cart_json and __import__("json").loads(cart_json)) or []
        except Exception:
            items = []

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
            invoice_id=invoice_id, name=name, phone=phone,
            items=items_for_receipt, subtotal=totals["subtotal"],
            discount_amt=totals["discount_amt"], gst_amt=totals["gst"],
            grand_total=totals["grand_total"], invoice_text=invoice_text
        )
        return render_template("invoice.html", invoice=invoice_text, invoice_id=invoice_id)

    # GET -> SPA page
    return render_template("billing.html")

# ------------ APIs the billing.html JS uses ------------

@billing_bp.post("/api/checkout_prepare", endpoint="api_checkout_prepare")
def api_checkout_prepare():
    invoice_id = generate_unique_invoice_id()
    try:
        _cache_put(invoice_id, "Preparing receipt…")
    except Exception:
        pass
    return jsonify({"ok": True, "invoice_id": invoice_id})

@billing_bp.post("/api/checkout", endpoint="api_checkout")
def api_checkout():
    from threading import Thread

    data = request.get_json(silent=True) or {}

    cust_name = (data.get("customer_name") or "Guest").strip() or "Guest"
    phone     = (data.get("phone") or "").strip()
    items     = data.get("items") or []
    discount  = data.get("discount", "")
    preview   = bool(data.get("preview"))
    override_total = data.get("override_grand_total", None)
    redeem    = int((data.get("redeem_points") or 0) or 0)
    async_save = bool(data.get("async"))

    if not (phone.isdigit() and len(phone) == 10):
        return jsonify({"ok": False, "error": "invalid_phone"}), 400
    if not items:
        return jsonify({"ok": False, "error": "empty_cart"}), 400

    try:
        totals = calculate_totals(items, discount)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    items_eff = totals.get("items_effective", items)

    def _rupees(x):
        try:
            return max(0, int(round(float(str(x).strip()))))
        except Exception:
            return None

    # canonical computed grand total (server-side)
    computed_grand_total = float(totals.get("grand_total", 0))
    effective_final = int(round(float(computed_grand_total)))

    # If client provided an override, coerce and use it as the effective_final (for display/charge)
    if override_total is not None:
        coerced = _rupees(override_total)
        if coerced is None:
            return jsonify({"ok": False, "error": "Invalid override amount"}), 400
        effective_final = coerced

    # -----------------------
    # PREVIEW (no writes)
    # -----------------------
    if preview:
        # Use the effective_final as the grand_total for preview simulation
        use_p, earn_p, bal_p, final_after = loyalty_simulate_preview(
            phone=phone, grand_total=float(effective_final), requested_redeem=redeem
        )

        invoice_text = generate_invoice_text(
            invoice_id="", customer_name=cust_name, customer_phone=phone,
            items=items_eff, subtotal=totals["subtotal"], gst_amount=totals["gst"],
            grand_total=final_after, discount_amt=totals["discount_amt"],
            points_used=use_p, points_earned=earn_p, points_balance=bal_p
        )
        return jsonify({
            "ok": True,
            "invoice_text": invoice_text,
            "points_used": use_p,
            "points_earned": earn_p,
            "points_balance": bal_p
        })

    # -----------------------
    # FINALIZE (writes)
    # -----------------------
    invoice_id = (data.get("invoice_id") or "").strip() or generate_unique_invoice_id()

    # Strategy:
    #  - If client provided override_total we want to honor that final charge.
    #  - We still MUST deduct the redeemed points (capped by user's allowance).
    #  - To avoid double-deducting, we'll:
    #      1) determine how many points can actually be used (cap) via loyalty_simulate_preview
    #      2) call loyalty_finalize in a way that causes it to deduct those points, but return a final_payable
    #         that equals effective_final wherever possible. We do that by passing grand_total = effective_final + applied_redeem.
    #
    # This ensures: final_payable = (effective_final + applied_redeem) - used_pts == effective_final (when used_pts == applied_redeem).
    #
    # If the loyalty system caps used_pts < requested_redeem, final_payable will be higher than the requested effective_final
    # and we'll convey that back to the client so they know the requested redeem couldn't be honoured fully.

    # 1) Determine allowed redeem (best-effort) using server-side simulation on a sensible base.
    #    Use the canonical computed grand total for simulation so caps based on subtotal are accurate.
    sim_use_p, sim_earn_p, sim_bal_p, sim_final_after = loyalty_simulate_preview(
        phone=phone, grand_total=float(computed_grand_total), requested_redeem=redeem
    )
    applied_redeem = int(sim_use_p or 0)

    # If client provided override_total, we want the finalize call to
    # deduct 'applied_redeem' but result in 'effective_final' if possible.
    # Achieve this by calling finalize with grand_total = effective_final + applied_redeem
    finalize_grand_total = float(effective_final)
    if applied_redeem > 0:
        finalize_grand_total = float(effective_final + applied_redeem)

    # Call loyalty_finalize -> performs the actual deduction and returns used/earned/new_balance/final_payable
    used_pts, earned_pts, new_balance, final_payable = loyalty_finalize(
        phone=phone, grand_total=float(finalize_grand_total), requested_redeem=applied_redeem
    )

    # If loyalty system used fewer points than our applied_redeem (cap), we need to re-derive final_payable:
    # final_payable returned by loyalty_finalize is authoritative. We'll accept it.
    # However, compute saved_amount (difference between canonical subtotal and final_payable)
    saved_amount = int(round(float(totals.get("subtotal", 0)))) - int(round(float(final_payable or 0)))

    invoice_text = generate_invoice_text(
        invoice_id=invoice_id, customer_name=cust_name, customer_phone=phone,
        items=items_eff, subtotal=totals["subtotal"], gst_amount=totals["gst"],
        grand_total=final_payable, discount_amt=totals["discount_amt"],
        points_used=used_pts, points_earned=earned_pts, points_balance=new_balance,
        saved_amount=saved_amount
    )

    # Put final invoice into cache so Android can fetch immediately
    try:
        _cache_put(invoice_id, invoice_text)
    except Exception:
        pass

    def _persist():
        try:
            # Persist invoice and reporting fields
            save_to_db(
                invoice_id=invoice_id,
                name=cust_name,
                phone=phone,
                items=items_eff,
                subtotal=totals["subtotal"],
                discount_amt=totals["discount_amt"],
                gst_amt=totals["gst"],
                grand_total=final_payable,
                invoice_text=invoice_text
            )
            # Update points info on sales_history row (background)
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
            # swallow in background
            pass

    if async_save:
        Thread(target=_persist, daemon=True).start()
    else:
        try:
            _persist()
        except Exception as e:
            return jsonify({"ok": False, "error": f"save_failed: {e}"}), 500

    # Return authoritative data to client
    return jsonify({
        "ok": True,
        "invoice_id": invoice_id,
        "invoice_text": invoice_text,
        "points_used": used_pts,
        "points_earned": earned_pts,
        "points_balance": new_balance,
        "saved_amount": saved_amount,
        "override_used": bool(override_total is not None)
    })




@billing_bp.get("/api/loyalty_points", endpoint="api_loyalty_points")
def api_loyalty_points():
    phone = (request.args.get("phone") or "").strip()
    if not (phone.isdigit() and len(phone) == 10):
        return jsonify({"ok": False, "points": 0, "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100})
    try:
        pts = lp_get_balance(phone)
        return jsonify({"ok": True, "points": int(pts), "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100})
    except Exception:
        return jsonify({"ok": False, "points": 0, "cap": REDEEM_CAP, "rate_per_100": EARN_PER_100}), 500

@billing_bp.get("/api/customer_last_name", endpoint="api_customer_last_name")
def api_customer_last_name():
    phone = (request.args.get("phone") or "").strip()
    if not (phone and phone.isdigit() and len(phone) == 10):
        return jsonify({"ok": False, "name": ""})
    name = get_last_customer_name(phone)
    return jsonify({"ok": True, "name": name or ""})

@billing_bp.get("/api/customer_last_phone", endpoint="api_customer_last_phone")
def api_customer_last_phone():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "phone": ""})
    phone = get_last_phone_by_name(name)
    return jsonify({"ok": True, "phone": phone or ""})

@billing_bp.post("/api/search_products", endpoint="api_search_products")
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

@billing_bp.post("/api/barcode_lookup", endpoint="api_barcode_lookup")
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

# -----------------------------
# TSPL / Receipt endpoints (public fetch by printer)
# -----------------------------
@billing_bp.get("/receipt/lines/<invoice_id>.txt")
def receipt_lines(invoice_id):
    invoice = _cache_get(invoice_id) or get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    body = invoice if invoice.endswith("\n") else invoice + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store"})

@billing_bp.get("/receipts/tspl/<invoice_id>.txt")
def receipts_tspl(invoice_id):
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
# Instant Printing API
# -----------------------------
@billing_bp.post("/api/invoice/instant-print")
def instant_print_invoice():
    """API endpoint for instant printing via Android native method"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON'})
    
    invoice_id = data.get('invoice_id')
    if not invoice_id:
        return jsonify({'success': False, 'error': 'Invoice ID required'})
    
    try:
        receipt_text, sms_to, sms_text = get_receipt_text_for_instant_print(invoice_id)
        
        if not receipt_text:
            return jsonify({'success': False, 'error': 'Invoice not found'})
        
        return jsonify({
            'success': True,
            'receipt_text': receipt_text,
            'sms_to': sms_to or '',
            'sms_text': sms_text or ''
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})