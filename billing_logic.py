from db import get_connection
from datetime import datetime
import json
import random
import string

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
    cur.execute("""
        SELECT brand, mrp, bought_price, selling_price, size, category
        FROM barcode_catalog
        WHERE barcode_data = %s
          AND (status IS NULL OR status = 'available')
        LIMIT 1
    """, (code,))
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
    Uses per-row item['disc_pct'] when > 0; otherwise applies a global percent from discount_input (e.g., "10" or "10%").
    Returns items_effective with per-row effective_unit_price and effective_pct so the receipt can show (orig)after.
    """
    if not items:
        raise ValueError("empty_cart")

    # Parse global percent (percent codes only)
    def _parse_pct(s: str):
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
        qty         = int(it.get("qty", 1))
        unit_price  = float(it.get("price", 0))
        unit_bought = float(it.get("bought_price", 0))
        row_pct     = float(it.get("disc_pct") or 0.0)

        eff_pct = row_pct if row_pct > 0 else (global_pct if global_pct > 0 else 0.0)
        unit_after = unit_price * (1.0 - eff_pct/100.0)

        # Per-unit guard
        if eff_pct > 0 and unit_after < (unit_bought + _GUARD_PAD):
            brand = it.get("brand") or ""
            cat   = it.get("category") or ""
            size  = it.get("size") or ""
            raise ValueError(f"max_discount_item:{brand}/{cat}/{size}")

        line_sub   = unit_price * qty
        line_total = unit_after * qty
        line_disc  = line_sub - line_total

        subtotal            += line_sub
        grand_total         += line_total
        discount_amt_total  += line_disc
        bought_total        += unit_bought * qty

        out_it = dict(it)
        out_it["effective_unit_price"] = int(round(unit_after))
        out_it["effective_pct"]        = eff_pct
        items_effective.append(out_it)

    # Optional cart-wide guard (kept as a safety): allow dropping global effect only.
    if grand_total < (bought_total + _GUARD_PAD):
        subtotal = 0.0
        grand_total = 0.0
        discount_amt_total = 0.0
        items_effective2 = []
        for it in items:
            qty         = int(it.get("qty", 1))
            unit_price  = float(it.get("price", 0))
            unit_bought = float(it.get("bought_price", 0))
            row_pct     = float(it.get("disc_pct") or 0.0)
            eff_pct     = row_pct if row_pct > 0 else 0.0
            unit_after  = unit_price * (1.0 - eff_pct/100.0)

            if eff_pct > 0 and unit_after < (unit_bought + _GUARD_PAD):
                brand = it.get("brand") or ""
                cat   = it.get("category") or ""
                size  = it.get("size") or ""
                raise ValueError(f"max_discount_item:{brand}/{cat}/{size}")

            line_sub   = unit_price * qty
            line_total = unit_after * qty
            line_disc  = line_sub - line_total

            subtotal           += line_sub
            grand_total        += line_total
            discount_amt_total += line_disc

            out_it = dict(it)
            out_it["effective_unit_price"] = int(round(unit_after))
            out_it["effective_pct"]        = eff_pct
            items_effective2.append(out_it)

        items_effective = items_effective2

    gst = 0.0
    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt_total, 2),  # this is the SAVED amount
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
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
    return use, earn, bal_after, float(final_after)

# billing_logic.py
EARNING_ENABLED = True  # set False to pause earning

def loyalty_finalize(phone: str, grand_total: float, requested_redeem: int):
    avail = lp_get_balance(phone)
    pay   = max(0, int(round(float(grand_total))))
    use   = max(0, min(int(requested_redeem or 0), REDEEM_CAP, avail, pay))
    final_payable = pay - use
    earned = _earn_from_amount(final_payable) if EARNING_ENABLED else 0
    new_bal = lp_upsert_delta(phone, -use + earned)
    return use, earned, new_bal, float(final_payable)



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
        # Loyalty (optional)
        points_used: int = 0,
        points_earned: int = 0,
        points_balance: int = None,
    ):
    COLS = 38
    W_NO, W_BR, W_SZ, W_Q, W_MRP, W_PRC = 2, 8, 4, 3, 6, 10

    def clamp(s, n): 
        return (str(s)[:n]).ljust(n)

    def fmt_signed(n: float, force_plus=False) -> str:
        """Format integers with + / -; zero returns '0' (no sign)."""
        n = int(round(float(n or 0)))
        if n == 0:
            return "0"
        if n > 0:
            return f"+{n}" if force_plus else str(n)
        return f"-{abs(n)}"

    lines = []
    # Header (centered)
    header = [
        "DRIP",
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
    left  = f"Invoice: {invoice_id or '-'}"
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
        size  = clamp(item.get("size", ""), W_SZ)
        qty   = int(item.get("qty", 1))
        mrp   = int(float(item.get("mrp_price", 0)))
        price_orig = int(float(item.get("price", 0)))

        eff_unit = item.get("effective_unit_price")
        if eff_unit is None:
            eff_pct = float(item.get("effective_pct") or 0.0)
            eff_unit = int(round(price_orig * (1 - eff_pct/100.0))) if eff_pct > 0 else price_orig
        eff_unit = int(eff_unit)

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

    # ==== Loyalty block (ABOVE totals), like your sample ====
    LBL_W = 18
    if (points_used or points_earned or points_balance is not None):
        # Only show Redeemed if > 0; always sign it negative
        if points_used:
            lines.append(f"{clamp('Redeemed (₹)', LBL_W)}{fmt_signed(-abs(points_used)).rjust(COLS - LBL_W)}")
        # Earned as +N (force plus)
        if points_earned:
            lines.append(f"{clamp('Earned (₹)', LBL_W)}{fmt_signed(points_earned, force_plus=True).rjust(COLS - LBL_W)}")
        # Balance without sign
        if points_balance is not None:
            bal = int(round(points_balance or 0))
            lines.append(f"{clamp('Balance (₹)', LBL_W)}{str(bal).rjust(COLS - LBL_W)}")
        lines.append("-" * COLS)

    # ==== Totals block ====    

    saved_amt = float(discount_amt or 0)

    # --- NEW: compute extra savings if operator manually overrode total ---
    try:
        calc_total = float(subtotal or 0) - float(saved_amt or 0)
        override_diff = calc_total - float(grand_total or 0)
        if override_diff > 0.009:  # treat as additional savings
            saved_amt += override_diff
    except Exception:
        pass

    gst_val = float(gst_amount or 0)
    if gst_val > 0:
        lines.append(f"{clamp('GST', LBL_W)}{format(gst_val, '.2f').rjust(COLS - LBL_W)}")

    lines.append(f"{clamp('Total', LBL_W)}{format(float(grand_total or 0), '.2f').rjust(COLS - LBL_W)}")

    lines.append("-" * COLS)

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
def get_invoices(search_term: str = "", limit: int = 20, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()

    sql_base = """
        SELECT invoice_id, customer_name, phone, date, grand_total
        FROM sales_history
    """
    sql_where = " WHERE phone ILIKE %s OR invoice_id ILIKE %s" if search_term else ""
    sql_order = " ORDER BY id DESC "
    sql_tail  = " LIMIT %s OFFSET %s"

    sql = sql_base + sql_where + sql_order + sql_tail
    params = (
        (f"%{search_term}%", f"%{search_term}%", limit + 1, offset)
        if search_term else
        (limit + 1, offset)
    )

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    # return first 20 + has_next flag
    return rows[:limit], (len(rows) > limit)



def get_invoice_text(invoice_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_text FROM sales_history WHERE invoice_id = %s", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None