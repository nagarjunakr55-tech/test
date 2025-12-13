# billing_logic.py
from db import get_connection
from datetime import datetime
import json
import random
import string
from typing import Tuple, List, Any, Optional, Dict
from threading import Thread

GSTIN = "123123"
BRAND_NAME = "DRIP"

# Per-item guard padding: unit_after must be >= bought_price + PAD
GUARD_PAD = 100

# --- IST helpers --------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    from pytz import timezone as ZoneInfo  # fallback


def ist_now():
    """Return current time in Asia/Kolkata."""
    try:
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        from pytz import timezone
        tz = timezone("Asia/Kolkata")
    return datetime.now(tz)


def india_date_str(dt: Optional[datetime] = None, include_time: bool = True) -> str:
    if dt is None:
        dt = ist_now()
    return dt.strftime("%d-%m-%Y %I:%M %p") if include_time else dt.strftime("%d-%m-%Y")


# ---------------------------------
# Product lookups
# ---------------------------------
def find_product_by_barcode(code: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT brand, mrp, bought_price, selling_price, size, category
            FROM barcode_catalog
            WHERE barcode_data = %s
            LIMIT 1
            """,
            (code.strip().upper(),)
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None
    return {
        "code": code,
        "brand": row[0],
        "mrp_price": row[1],
        "bought_price": row[2],
        "selling_price": row[3],
        "size": row[4],
        "category": row[5],
        "price": row[3],
        "qty": 1
    }


def search_products(brand: str = "", category: str = "", size: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    sql = """
      SELECT brand, mrp, bought_price, selling_price, size, category
      FROM barcode_catalog
      WHERE (status IS NULL OR status='available')
    """
    params = []
    if brand:
        sql += " AND brand ILIKE %s"
        params.append(f"%{brand}%")
    if category:
        sql += " AND category = %s"
        params.append(category)
    if size:
        sql += " AND size = %s"
        params.append(size)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "brand": r[0],
            "mrp_price": r[1],
            "bought_price": r[2],
            "selling_price": r[3],
            "price": r[3],
            "size": r[4],
            "category": r[5],
        })
    return out


def get_last_customer_name(phone: str) -> Optional[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT customer_name
        FROM sales_history
        WHERE phone = %s
        ORDER BY id DESC
        LIMIT 1
    """, (phone,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------
# Invoice ID
# ---------------------------------
def generate_unique_invoice_id() -> str:
    conn = get_connection()
    cur = conn.cursor()
    try:
        while True:
            candidate = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            cur.execute("SELECT 1 FROM sales_history WHERE invoice_id = %s LIMIT 1", (candidate,))
            if not cur.fetchone():
                return candidate
    finally:
        conn.close()


# ---------------------------------
# Totals & discount rules
# ---------------------------------
_ALLOWED_PCTS = {5, 10, 15, 20, 25, 30, 35}
_GUARD_PAD = 100


def calculate_totals(items, discount_input: str):
    if not items:
        raise ValueError("empty_cart")

    def _parse_pct(s: str) -> float:
        if not s:
            return 0.0
        s = s.strip()
        if s.endswith("%"):
            s = s[:-1]
        try:
            return float(s)
        except Exception:
            return 0.0

    global_pct = _parse_pct(discount_input)

    subtotal = 0.0
    grand_total = 0.0
    discount_amt_total = 0.0
    bought_total = 0.0
    items_effective = []

    for it in items:
        qty = int(it.get("qty", 1))
        unit_price = float(it.get("price", 0))
        unit_bought = float(it.get("bought_price", 0))
        row_pct = float(it.get("disc_pct") or 0.0)

        eff_pct = row_pct if row_pct > 0 else (global_pct if global_pct > 0 else 0.0)
        unit_after = unit_price * (1.0 - eff_pct / 100.0)

        if eff_pct > 0 and unit_after < (unit_bought + _GUARD_PAD):
            brand = it.get("brand") or ""
            cat = it.get("category") or ""
            size = it.get("size") or ""
            raise ValueError(f"max_discount_item:{brand}/{cat}/{size}")

        line_sub = unit_price * qty
        line_total = unit_after * qty
        line_disc = line_sub - line_total

        subtotal += line_sub
        grand_total += line_total
        discount_amt_total += line_disc
        bought_total += unit_bought * qty

        out_it = dict(it)
        out_it["effective_unit_price"] = int(round(unit_after))
        out_it["effective_pct"] = eff_pct
        items_effective.append(out_it)

    if grand_total < (bought_total + _GUARD_PAD):
        subtotal = 0.0
        grand_total = 0.0
        discount_amt_total = 0.0
        items_effective2 = []
        for it in items:
            qty = int(it.get("qty", 1))
            unit_price = float(it.get("price", 0))
            unit_bought = float(it.get("bought_price", 0))
            row_pct = float(it.get("disc_pct") or 0.0)
            eff_pct = row_pct if row_pct > 0 else 0.0
            unit_after = unit_price * (1.0 - eff_pct / 100.0)

            if eff_pct > 0 and unit_after < (unit_bought + _GUARD_PAD):
                brand = it.get("brand") or ""
                cat = it.get("category") or ""
                size = it.get("size") or ""
                raise ValueError(f"max_discount_item:{brand}/{cat}/{size}")

            line_sub = unit_price * qty
            line_total = unit_after * qty
            line_disc = line_sub - line_total

            subtotal += line_sub
            grand_total += line_total
            discount_amt_total += line_disc

            out_it = dict(it)
            out_it["effective_unit_price"] = int(round(unit_after))
            out_it["effective_pct"] = eff_pct
            items_effective2.append(out_it)

        items_effective = items_effective2

    gst = 0.0
    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt_total, 2),
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
        "items_effective": items_effective,
    }


# ---------------------------------
# Loyalty
# ---------------------------------
REDEEM_CAP = 100
EARN_PER_100 = 2


def _earn_from_amount(amount: float) -> int:
    amt = max(0, int(round(float(amount or 0))))
    return int((amt * EARN_PER_100) // 100)


def lp_ensure_table() -> None:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loyalty_points (
                  phone      TEXT PRIMARY KEY,
                  balance    INTEGER NOT NULL DEFAULT 0,
                  updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                ALTER TABLE sales_history
                  ADD COLUMN IF NOT EXISTS points_earned INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS points_redeemed INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS points_synced_at TIMESTAMPTZ;
            """)
            conn.commit()
    except Exception:
        pass


def lp_get_balance(phone: str) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT balance FROM loyalty_points WHERE phone=%s", (phone,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def lp_upsert_delta(phone: str, delta: int) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO loyalty_points(phone, balance, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (phone) DO UPDATE
            SET balance = loyalty_points.balance + EXCLUDED.balance,
                updated_at = NOW()
            RETURNING balance
        """, (phone, delta))
        newbal = int(cur.fetchone()[0])
        conn.commit()
        return newbal


def loyalty_simulate_preview(phone: str, grand_total: float, requested_redeem: int):
    avail = lp_get_balance(phone)
    pay = max(0, int(round(float(grand_total))))
    use = max(0, min(int(requested_redeem or 0), REDEEM_CAP, avail, pay))
    final_after = pay - use
    earn = _earn_from_amount(final_after)
    bal_after = avail - use + earn
    return use, earn, bal_after, float(final_after)


EARNING_ENABLED = True


def loyalty_finalize(phone: str, grand_total: float, requested_redeem: int):
    avail = lp_get_balance(phone)
    pay = max(0, int(round(float(grand_total))))
    use = max(0, min(int(requested_redeem or 0), REDEEM_CAP, avail, pay))
    final_payable = pay - use
    earned = _earn_from_amount(final_payable) if EARNING_ENABLED else 0
    new_bal = lp_upsert_delta(phone, -use + earned)
    return use, earned, new_bal, float(final_payable)


# ---------------------------------
# Invoice text generation
# ---------------------------------
def generate_invoice_text(
        invoice_id,
        customer_name,
        customer_phone,
        items,
        subtotal,
        gst_amount,
        grand_total,
        discount_amt,
        show_time: bool = True,
        points_used: int = 0,
        points_earned: int = 0,
        points_balance: int = None,
        saved_amount: Optional[float] = None,
    ) -> str:
    # (function body identical to earlier — generate fixed-width invoice)
    # For brevity in this snippet assume the same code you provided earlier is used here.
    # Copy your existing generate_invoice_text implementation unchanged.
    COLS = 38
    W_NO, W_BR, W_SZ, W_Q, W_MRP, W_PRC = 2, 8, 4, 3, 6, 10
    def clamp(s, n):
        return (str(s)[:n]).ljust(n)
    def fmt_signed(n: float, force_plus=False) -> str:
        try:
            n = int(round(float(n or 0)))
        except Exception:
            n = 0
        if n == 0:
            return "0"
        if n > 0:
            return f"+{n}" if force_plus else str(n)
        return f"-{abs(n)}"
    lines: List[str] = []
    header = [
        "DRIP",
        "#6-2-52,",
        "DL Road, Hindupur,",
        "Satya Sai (D), AP-515201",
        "Phone: [REDACTED_PHONE_NUMBER_1]",
    ]
    for s in header:
        lines.append(s.center(COLS))
    lines.append("-" * COLS)
    today_str = india_date_str(include_time=show_time)
    left = f"Invoice: {invoice_id or '-'}"
    right = f"Date: {today_str}"
    lines.append(f"{left:<{COLS - len(right)}}{right}")
    lines.append(clamp(f"Customer: {customer_name} / {customer_phone}", COLS))
    lines.append("-" * COLS)
    hdr = (
        f"{'No':<{W_NO}} "
        f"{'Brand':<{W_BR}} "
        f"{'Size':<{W_SZ}} "
        f"{'Qty':>{W_Q}} "
        f"{'MRP':>{W_MRP}} "
        f"{'Price':>{W_PRC}}"
    )
    lines.append(hdr)
    lines.append("-" * COLS)
    for idx, item in enumerate(items, start=1):
        brand = clamp(item.get("brand", ""), W_BR)
        size = clamp(item.get("size", ""), W_SZ)
        try:
            qty = int(item.get("qty", 1))
        except Exception:
            qty = 1
        try:
            mrp = int(round(float(item.get("mrp_price", 0) or 0)))
        except Exception:
            mrp = 0
        try:
            price_orig = int(round(float(item.get("price", 0) or 0)))
        except Exception:
            price_orig = 0
        eff_unit = item.get("effective_unit_price")
        if eff_unit is None:
            try:
                eff_pct = float(item.get("effective_pct") or 0.0)
            except Exception:
                eff_pct = 0.0
            if eff_pct > 0:
                eff_unit = int(round(price_orig * (1 - eff_pct / 100.0)))
            else:
                eff_unit = price_orig
        try:
            eff_unit = int(round(float(eff_unit or 0)))
        except Exception:
            eff_unit = price_orig
        price_str = f"({price_orig}){eff_unit}" if eff_unit != price_orig else f"{price_orig}"
        row = (
            f"{idx:<{W_NO}} "
            f"{brand:<{W_BR}} "
            f"{size:<{W_SZ}} "
            f"{qty:<{W_Q}} "
            f"{mrp:>{W_MRP}} "
            f"{price_str:>{W_PRC}}"
        )
        lines.append(row)
    lines.append("-" * COLS)
    LBL_W = 18
    def _f(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0
    subtotal_val = _f(subtotal)
    gst_val = _f(gst_amount)
    grand_val = _f(grand_total)
    discount_val = _f(discount_amt)
    computed_saved_total = max(0.0, subtotal_val - grand_val)
    if saved_amount is not None:
        try:
            saved_total = float(saved_amount or 0)
        except Exception:
            saved_total = computed_saved_total
    else:
        saved_total = computed_saved_total
    if (points_used or points_earned or points_balance is not None):
        if points_used and int(points_used) > 0:
            lines.append(f"{clamp('Redeemed (₹)', LBL_W)}{str(int(points_used)).rjust(COLS - LBL_W)}")
        if points_earned and int(points_earned) != 0:
            lines.append(f"{clamp('Earned (₹)', LBL_W)}{fmt_signed(int(points_earned), force_plus=True).rjust(COLS - LBL_W)}")
        if points_balance is not None:
            try:
                bal = int(round(points_balance or 0))
            except Exception:
                bal = 0
            lines.append(f"{clamp('Balance (₹)', LBL_W)}{str(bal).rjust(COLS - LBL_W)}")
        lines.append("-" * COLS)
    if discount_val and discount_val > 0:
        lines.append(f"{clamp('Discount', LBL_W)}{format(discount_val, '.2f').rjust(COLS - LBL_W)}")
    if gst_val and gst_val > 0:
        lines.append(f"{clamp('GST', LBL_W)}{format(gst_val, '.2f').rjust(COLS - LBL_W)}")
    if saved_total and saved_total > 0.009:
        lines.append(f"{clamp('Saved', LBL_W)}{format(saved_total, '.2f').rjust(COLS - LBL_W)}")
    lines.append(f"{clamp('Total', LBL_W)}{format(grand_val, '.2f').rjust(COLS - LBL_W)}")
    lines.append("-" * COLS)
    lines.append("Thanks for shopping with DRIP Fashions!".center(COLS))
    lines.append(" " * COLS)
    MARGIN = 2
    return "\n".join((" " * MARGIN) + ln for ln in lines)


# ---------------------------------
# Persistence
# ---------------------------------
def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

    date_str = india_date_str()

    cur.execute("""
        INSERT INTO sales_history
        (invoice_id, customer_name, phone, items, total, discount, gst, grand_total, gstin, date, invoice_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (invoice_id) DO NOTHING
    """, (
        invoice_id, name, phone, json.dumps(items), subtotal,
        discount_amt, gst_amt, grand_total, GSTIN, date_str, invoice_text
    ))

    for it in items:
        code = (it.get("code") or "").strip()
        qty = int(it.get("qty") or 0)
        if code and qty > 0:
            cur.execute(
                "INSERT INTO sales_barcodes (invoice_id, code, qty) VALUES (%s, %s, %s)",
                (invoice_id, code, qty)
            )

    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------
# Queries for history & reprint
# ---------------------------------
def _has_col(cur, table: str, col: str) -> bool:
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        LIMIT 1
    """, (table, col))
    return cur.fetchone() is not None


def get_invoices(q: str = "", limit: int = 20, offset: int = 0) -> Tuple[List[tuple], bool]:
    table = "sales_history"
    where, params = [], []

    if q:
        where.append("(invoice_id ILIKE %s OR phone ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_connection() as conn, conn.cursor() as cur:
        ts_candidates = ["created_at", "sold_at", "ts", "timestamp", "datetime", "date"]
        ts_col = None
        for c in ts_candidates:
            if _has_col(cur, table, c):
                ts_col = c
                break

        if ts_col:
            if ts_col.lower() == "date":
                dt_expr = (
                    "to_char("
                    "  to_timestamp(date, 'DD-MM-YYYY HH12:MI AM'),"
                    "  'YYYY-MM-DD HH24:MI'"
                    ")"
                )
            else:
                dt_expr = f"to_char({ts_col}, 'YYYY-MM-DD HH24:MI')"
        else:
            dt_expr = "''"

        has_points = _has_col(cur, table, "points_earned")
        points_expr = "COALESCE(points_earned, 0)" if has_points else "0"

        sql = f"""
            SELECT
              invoice_id,
              customer_name,
              phone,
              {dt_expr} AS disp_dt,
              grand_total::numeric(12,2),
              {points_expr}::int
            FROM public.{table}
            {where_sql}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit + 1, offset])
        cur.execute(sql, params)
        rows = cur.fetchall()

    has_next = len(rows) > limit
    if has_next:
        rows = rows[:limit]
    return rows, has_next


def get_invoice_text(invoice_id: str) -> Optional[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_text FROM sales_history WHERE invoice_id = %s", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# -------------------------
# DB-backed small cache
# -------------------------
def upsert_receipt_cache(invoice_id: str, invoice_text: str) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO receipt_cache (invoice_id, invoice_text, created_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (invoice_id) DO UPDATE
                      SET invoice_text = EXCLUDED.invoice_text,
                          created_at = NOW()
                """, (invoice_id, invoice_text))
            conn.commit()
    except Exception:
        # intentionally keep logging to controller where current_app is available
        try:
            from flask import current_app
            current_app.logger.exception("receipt_cache upsert failed for %s", invoice_id)
        except Exception:
            pass


def db_receipt_cache_get(invoice_id: str) -> Optional[str]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT invoice_text FROM receipt_cache WHERE invoice_id = %s", (invoice_id,))
                row = cur.fetchone()
                return row[0] if row and row[0] else None
    except Exception:
        try:
            from flask import current_app
            current_app.logger.exception("receipt_cache read failed for %s", invoice_id)
        except Exception:
            pass
        return None


# ---------------------------------
# Programmatic helpers (preview/finalize)
# ---------------------------------
def _coerce_rupees(x):
    try:
        return max(0, int(round(float(str(x).strip()))))
    except Exception:
        return None


def checkout_preview_logic(customer_name: str, phone: str, items: list,
                           discount_input: str = "", override_grand_total=None,
                           redeem_points: int = 0):
    if not (phone.isdigit() and len(phone) == 10):
        return False, {"error": "invalid_phone"}
    if not items:
        return False, {"error": "empty_cart"}

    totals = calculate_totals(items, discount_input)
    items_for_receipt = totals.get("items_effective", items)

    original_final = int(round(float(totals.get("grand_total", 0))))
    effective_final = original_final

    if override_grand_total is not None:
        coerced = _coerce_rupees(override_grand_total)
        if coerced is None:
            return False, {"error": "Invalid override amount"}
        effective_final = coerced

    use_p, earn_p, bal_p, final_after = loyalty_simulate_preview(
        phone=phone,
        grand_total=float(effective_final),
        requested_redeem=int(redeem_points or 0)
    )

    invoice_text = generate_invoice_text(
        invoice_id="",
        customer_name=customer_name or "Guest",
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

    return True, {
        "invoice_text": invoice_text,
        "points_used": use_p,
        "points_earned": earn_p,
        "points_balance": bal_p,
        "items_for_receipt": items_for_receipt,
        "totals": totals,
        "final_after": final_after
    }


def checkout_finalize_logic(
    invoice_id: Optional[str],
    customer_name: str,
    phone: str,
    items: List[dict],
    discount_input: str = "",
    override_grand_total=None,
    redeem_points: int = 0,
    async_save: bool = False,
):
    if not (phone.isdigit() and len(phone) == 10):
        return False, {"error": "invalid_phone"}

    if not items:
        return False, {"error": "empty_cart"}

    try:
        totals = calculate_totals(items, discount_input)
    except ValueError as e:
        return False, {"error": str(e)}

    items_for_receipt = totals.get("items_effective", items)

    original_final = int(round(float(totals.get("grand_total", 0))))
    effective_final = original_final
    if override_grand_total is not None:
        coerced = _coerce_rupees(override_grand_total)
        if coerced is None:
            return False, {"error": "Invalid override amount"}
        effective_final = coerced

    invoice_id = (invoice_id or "").strip() or generate_unique_invoice_id()

    used_pts, earned_pts, new_balance, final_payable = loyalty_finalize(
        phone=phone,
        grand_total=float(effective_final),
        requested_redeem=int(redeem_points or 0)
    )

    invoice_text = generate_invoice_text(
        invoice_id=invoice_id,
        customer_name=customer_name or "Guest",
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

    def _persist():
        try:
            save_to_db(
                invoice_id=invoice_id,
                name=customer_name or "Guest",
                phone=phone,
                items=items_for_receipt,
                subtotal=totals["subtotal"],
                discount_amt=totals["discount_amt"],
                gst_amt=totals["gst"],
                grand_total=final_payable,
                invoice_text=invoice_text
            )
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
            if not async_save:
                raise

    if async_save:
        Thread(target=_persist, daemon=True).start()
    else:
        _persist()

    return True, {
        "invoice_id": invoice_id,
        "invoice_text": invoice_text,
        "points_used": used_pts,
        "points_earned": earned_pts,
        "points_balance": new_balance
    }
