from db import get_connection
from datetime import datetime
import json
import random
import string
from typing import Tuple, List, Any
from db import get_connection
from threading import Thread
from typing import Optional, List, Dict, Any, Tuple



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


def india_date_str(dt=None, include_time: bool = True):
    """
    Format as:
      - DD-MM-YYYY hh:mm AM/PM when include_time=True
      - DD-MM-YYYY                when include_time=False
    """
    if dt is None:
        dt = ist_now()
    return dt.strftime("%d-%m-%Y %I:%M %p") if include_time else dt.strftime("%d-%m-%Y")


# ---------------------------------
# Product lookups
# ---------------------------------
def find_product_by_barcode(code: str):
    conn = get_connection()
    cur = conn.cursor()
    ccur.execute("""
    SELECT brand, mrp, bought_price, selling_price, size, category
    FROM barcode_catalog
    WHERE barcode_data = %s
    LIMIT 1
""", (code.strip().upper(),))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None
    # Keys aligned to your frontend cart usage
    return {
        "code": code,
        "brand": row[0],
        "mrp_price": row[1],
        "bought_price": row[2],
        "selling_price": row[3],
        "size": row[4],
        "category": row[5],
        "price": row[3],   # default selling price
        "qty": 1
    }


def search_products(brand: str = "", category: str = "", size: str = "", limit: int = 20):
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

    # Return keys expected by your search modal: mrp_price & price
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


def get_last_customer_name(phone: str):
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
    """Generate a 6-char unique invoice id and verify against DB."""
    conn = get_connection()
    cur = conn.cursor()
    while True:
        candidate = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        cur.execute("SELECT 1 FROM sales_history WHERE invoice_id = %s LIMIT 1", (candidate,))
        if not cur.fetchone():
            conn.close()
            return candidate


# ---------------------------------
# Totals & discount rules
# ---------------------------------
_ALLOWED_PCTS = {5, 10, 15, 20, 25, 30, 35}
_GUARD_PAD = 100  # backend safety: unit_after must be >= bought + 100


def calculate_totals(items, discount_input: str):
    """
    Calculate totals at base price (no discounts).
    Returns items with effective_unit_price = base price.
    Saved amount = MRP total - Selling price total.
    """
    if not items:
        raise ValueError("empty_cart")

    subtotal = 0
    mrp_total = 0
    items_effective = []

    for it in items:
        qty = int(it.get("qty", 1))
        unit_price = int(round(float(it.get("price", 0))))
        unit_mrp = int(round(float(it.get("mrp_price", 0))))
        
        # Use effective_unit_price if provided (edited price), otherwise use price
        effective_price = it.get("effective_unit_price")
        if effective_price is not None:
            effective_price = int(round(float(effective_price)))
        else:
            effective_price = unit_price
        
        line_total = effective_price * qty
        line_mrp = unit_mrp * qty
        
        subtotal += line_total
        mrp_total += line_mrp

        out_it = dict(it)
        out_it["effective_unit_price"] = effective_price
        out_it["effective_pct"] = 0
        items_effective.append(out_it)

    saved_amount = max(0, mrp_total - subtotal)
    gst = 0
    
    return {
        "subtotal": subtotal,
        "discount_amt": saved_amount,
        "grand_total": subtotal,
        "gst": gst,
        "items_effective": items_effective,
    }


# ---------------------------------
# Loyalty (minimal, single-table)
# ---------------------------------
REDEEM_CAP = 100  # max 100 points per purchase
EARN_PER_100 = 2
def _earn_from_amount(amount: float) -> int:
    """Whole points based on final payable amount and EARN_PER_100."""
    amt = max(0, int(round(float(amount or 0))))
    return int((amt * EARN_PER_100) // 100)
def lp_ensure_table():
    """Create tiny balance table per phone (safe to call on boot)."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loyalty_points (
                  phone      TEXT PRIMARY KEY,
                  balance    INTEGER NOT NULL DEFAULT 0,
                  updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Add loyalty columns to sales_history (used for async repair visibility)
            cur.execute("""
                ALTER TABLE sales_history
                  ADD COLUMN IF NOT EXISTS points_earned INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS points_redeemed INTEGER NOT NULL DEFAULT 0,
                  ADD COLUMN IF NOT EXISTS points_synced_at TIMESTAMPTZ;
            """)
            conn.commit()
    except Exception:
        # non-fatal on boot
        pass

def lp_get_balance(phone: str) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT balance FROM loyalty_points WHERE phone=%s", (phone,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

def lp_upsert_delta(phone: str, delta: int) -> int:
    """Add delta to balance (can be negative). Returns new balance."""
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
    """No DB writes. Returns (use_preview, earn_preview, balance_after_preview, final_after)."""
    avail = lp_get_balance(phone)
    pay   = max(0, int(round(float(grand_total))))
    use   = max(0, min(int(requested_redeem or 0), REDEEM_CAP, avail, pay))
    final_after = pay - use
    earn = _earn_from_amount(final_after)
    bal_after = avail - use + earn
    return use, earn, bal_after, final_after

# billing_logic.py
EARNING_ENABLED = True  # set False to pause earning

def loyalty_finalize(phone: str, grand_total: float, requested_redeem: int):
    avail = lp_get_balance(phone)
    pay   = max(0, int(round(float(grand_total))))
    use   = max(0, min(int(requested_redeem or 0), REDEEM_CAP, avail, pay))
    final_payable = pay - use
    earned = _earn_from_amount(final_payable) if EARNING_ENABLED else 0
    new_bal = lp_upsert_delta(phone, -use + earned)
    return use, earned, new_bal, final_payable



def generate_invoice_text(
        invoice_id: str,
        customer_name: str,
        customer_phone: str,
        items: List[dict],
        subtotal: float,
        gst_amount: float,
        grand_total: float,
        discount_amt: float,
        show_time: bool = True,
        # Loyalty (optional)
        points_used: int = 0,
        points_earned: int = 0,
        points_balance: int = None,
        saved_amount: Optional[float] = None,
    ):
    """
    Produce a fixed-width invoice text. Backwards compatible: saved_amount is optional.
    saved_amount, if not provided, is computed as subtotal - grand_total (total discount).
    """
    COLS = 38
    W_NO, W_BR, W_SZ, W_Q, W_MRP, W_PRC = 2, 8, 4, 3, 6, 10

    def clamp(s, n):
        return (str(s)[:n]).ljust(n)

    def fmt_signed(n: float, force_plus=False) -> str:
        """Format integers with + / -; zero returns '0' (no sign)."""
        try:
            n = int(round(float(n or 0)))
        except Exception:
            n = 0
        if n == 0:
            return "0"
        if n > 0:
            return f"+{n}" if force_plus else str(n)
        return f"-{abs(n)}"

    lines = []
    # Header (centered)
    header = [
        "DRIP Fashions",
        "#6-2-52,",
        "DL Road, Hindupur,",
        "Satya Sai (D), AP-515201",
        "Phone: 8106204688",
    ]
    for s in header:
        lines.append(s.center(COLS))
    lines.append("-" * COLS)

    # Invoice/date line
    today_str = india_date_str(include_time=show_time)
    left = f"Invoice: {invoice_id or '-'}"
    right = f"Date: {today_str}"
    lines.append(f"{left:<{COLS - len(right)}}{right}")

    # Customer line
    lines.append(clamp(f"Customer: {customer_name} / {customer_phone}", COLS))
    lines.append("-" * COLS)

    # Table header
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

    # Items
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

    # ==== Loyalty & totals block ====
    LBL_W = 18

    # Normalize numeric inputs defensively
    def _f(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    subtotal_val = _f(subtotal)
    gst_val = _f(gst_amount)
    grand_val = _f(grand_total)
    discount_val = _f(discount_amt)

    # Compute saved_total: canonical definition is subtotal - grand_total
    computed_saved_total = max(0, int(round(subtotal_val - grand_val)))

    # If caller provided saved_amount, prefer it; else use computed value
    if saved_amount is not None:
        try:
            saved_total = float(saved_amount or 0)
        except Exception:
            saved_total = computed_saved_total
    else:
        saved_total = computed_saved_total

    # Loyalty block: show Redeemed, Earned, Balance (if applicable)
    if (points_used or points_earned or points_balance is not None):
        if points_used and int(points_used) > 0:
            # show redeemed in rupees (positive number), left label indicates it's redeem
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

    if gst_val and gst_val > 0:
        lines.append(f"{clamp('GST', LBL_W)}{str(int(round(gst_val))).rjust(COLS - LBL_W)}")

    # Saved: show the total rupee savings (positive number). If saved_total computed as 0, only show if > 0.01
    if saved_total and saved_total > 0.009:
        lines.append(f"{clamp('Saved', LBL_W)}{str(int(round(saved_total))).rjust(COLS - LBL_W)}")

    # Final total
    lines.append(f"{clamp('Total', LBL_W)}{str(int(round(grand_val))).rjust(COLS - LBL_W)}")

    lines.append("-" * COLS)

    # Footer
    lines.append("Thanks for shopping with DRIP Fashions!".center(COLS))
    lines.append(" " * COLS)

    # Left margin inset for printer
    MARGIN = 2
    return "\n".join((" " * MARGIN) + ln for ln in lines)




# ---------------------------------
# Persistence
# ---------------------------------
def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

    date_str = india_date_str()

    # invoice header/body
    cur.execute("""
        INSERT INTO sales_history
        (invoice_id, customer_name, phone, items, total, discount, gst, grand_total, gstin, date, invoice_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (invoice_id) DO NOTHING
    """, (
        invoice_id, name, phone, json.dumps(items), subtotal,
        discount_amt, gst_amt, grand_total, GSTIN, date_str, invoice_text
    ))

    # record barcode sales (skip manual items without a code)
    for it in items:
        code = (it.get("code") or "").strip()
        qty  = int(it.get("qty") or 0)
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
_COL_CACHE: dict = {}  # Cache for column existence checks

def _has_col(cur, table: str, col: str) -> bool:
    key = (table, col)
    if key in _COL_CACHE:
        return _COL_CACHE[key]
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        LIMIT 1
    """, (table, col))
    result = cur.fetchone() is not None
    _COL_CACHE[key] = result
    return result

def get_invoices(q: str = "", limit: int = 20, offset: int = 0) -> Tuple[List[tuple], bool]:
    """
    Returns rows shaped as:
      0=invoice_id
      1=customer_name
      2=phone
      3=display_date (YYYY-MM-DD HH:MM)
      4=grand_total (numeric)
      5=points_earned (int, 0 if column absent)
    """
    table = "sales_history"
    where, params = [], []

    if q:
        where.append("(invoice_id ILIKE %s OR phone ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_connection() as conn, conn.cursor() as cur:
        # Detect timestamp column
        ts_candidates = ["created_at", "sold_at", "ts", "timestamp", "datetime", "date"]
        ts_col = None
        for c in ts_candidates:
            if _has_col(cur, table, c):
                ts_col = c
                break

        # Build date expr safely - return raw value, avoid expensive to_timestamp parsing
        if ts_col:
            dt_expr = ts_col
        else:
            dt_expr = "''"


        # Detect points_earned column
        has_points = _has_col(cur, table, "points_earned")
        points_expr = "COALESCE(points_earned, 0)" if has_points else "0"

        sql = f"""
            SELECT
              invoice_id,                 -- 0
              customer_name,              -- 1
              phone,                      -- 2
              {dt_expr} AS disp_dt,       -- 3
              grand_total::numeric(12,2), -- 4
              {points_expr}::int          -- 5
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




def get_invoice_text(invoice_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_text FROM sales_history WHERE invoice_id = %s", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# New code here

# services/billing_logic.py  (additions)



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

    totals = calculate_totals(items, discount_input)  # may raise ValueError
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
        grand_total=effective_final,
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


from typing import Optional, List

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
        grand_total=effective_final,
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
            from db import get_connection
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
        from threading import Thread
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