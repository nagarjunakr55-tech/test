# billing_logic.py
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
# Bootstrap tables (Postgres)
# ---------------------------------
def ensure_tables():
    '''
    conn = get_connection()
    cur = conn.cursor()

    # Sales history (receipts)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_history (
            id SERIAL PRIMARY KEY,
            invoice_id TEXT UNIQUE,
            customer_name TEXT,
            phone TEXT,
            items TEXT,
            total NUMERIC,
            discount NUMERIC,
            gst NUMERIC,
            grand_total NUMERIC,
            gstin TEXT,
            date TEXT,
            invoice_text TEXT
        )
    """)

    # Product catalog for barcode/search
    cur.execute("""
        CREATE TABLE IF NOT EXISTS barcode_catalog (
            id SERIAL PRIMARY KEY,
            brand TEXT,
            mrp NUMERIC,
            bought_price NUMERIC,
            selling_price NUMERIC,
            size TEXT,
            category TEXT,
            barcode_data TEXT UNIQUE,
            status TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Printed counter (ok if it already exists)
    try:
        cur.execute("ALTER TABLE barcode_catalog ADD COLUMN IF NOT EXISTS printed_qty INTEGER DEFAULT 0")
    except Exception:
        pass

    # Per-barcode sold quantities
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_barcodes (
            id SERIAL PRIMARY KEY,
            invoice_id TEXT,
            code TEXT,
            qty INTEGER DEFAULT 1
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_barcodes_code ON sales_barcodes(code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_barcodes_invoice ON sales_barcodes(invoice_id)")

    conn.commit()
    cur.close()
    conn.close()
    '''

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
# ---------------------------------
# Totals & discount rules
# ---------------------------------
_ALLOWED_PCTS = {5, 10, 15, 20, 25, 30, 35}
GUARD_PAD = 100  # minimum margin over bought price per unit

# ---------------------------------
# Totals & discount rules
# ---------------------------------
_ALLOWED_PCTS = {5, 10, 15, 20, 25, 30, 35}
GUARD_PAD = 100  # unit after-discount must be >= bought_price + GUARD_PAD

def _parse_percent(di: str):
    """Return float percent if parseable, else None."""
    if not di:
        return None
    di = di.strip()
    if di.endswith("%"):
        di = di[:-1]
    try:
        return float(di)
    except Exception:
        return None

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
    # Since we already guarded per-item above, grand_total should usually be fine.
    if grand_total < (bought_total + _GUARD_PAD):
        # Roll back ONLY the global part: recompute with per-row only
        subtotal = 0.0
        grand_total = 0.0
        discount_amt_total = 0.0
        items_effective2 = []
        for it in items:
            qty         = int(it.get("qty", 1))
            unit_price  = float(it.get("price", 0))
            unit_bought = float(it.get("bought_price", 0))
            row_pct     = float(it.get("disc_pct") or 0.0)  # ignore global now
            eff_pct     = row_pct if row_pct > 0 else 0.0
            unit_after  = unit_price * (1.0 - eff_pct/100.0)

            # keep per-item guard
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
# Invoice text (monospace)
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
    ):
    COLS = 38
    W_NO, W_BR, W_SZ, W_Q, W_MRP, W_PRC = 2, 8, 4, 3, 6, 10

    def clamp(s, n): return (str(s)[:n]).ljust(n)

    lines = []
    header = ["DRIP", "#6-2-52,", "DL Road, Hindupur,", "Satya Sai (D), AP-515201", "Phone: 8106204688"]
    for s in header:
        lines.append(s.center(COLS))
    lines.append("-" * COLS)

    today_str = india_date_str(include_time=show_time)
    left  = f"Invoice: {invoice_id}"
    right = f"Date: {today_str}"
    lines.append(f"{left:<{COLS - len(right)}}{right}")

    cust_line = f"Customer: {customer_name} / {customer_phone}"
    lines.append(clamp(cust_line, COLS))
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

    # Render each row: if an effective price exists and differs, show (orig)after
    for idx, item in enumerate(items, start=1):
        brand = clamp(item.get("brand", ""), W_BR)
        size  = clamp(item.get("size", ""), W_SZ)
        qty   = int(item.get("qty", 1))
        mrp   = int(float(item.get("mrp_price", 0)))
        price_orig = int(float(item.get("price", 0)))

        eff_unit = item.get("effective_unit_price")
        if eff_unit is None:
            # fallback: try to compute from effective_pct if present
            eff_pct = float(item.get("effective_pct") or 0.0)
            eff_unit = int(round(price_orig * (1 - eff_pct/100.0))) if eff_pct > 0 else price_orig
        eff_unit = int(eff_unit)

        if eff_unit != price_orig:
            price_str = f"({price_orig}){eff_unit}"
        else:
            price_str = f"{price_orig}"

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

    # Show “Saved” (₹) instead of “Discount (%)”
    saved_amt = float(discount_amt or 0)
    if saved_amt > 0:
        label = "Saved"
        lines.append(f"{clamp(label, LBL_W)}{('-' + format(saved_amt, '.2f')).rjust(COLS - LBL_W)}")

    total_label = "Total"
    lines.append(f"{clamp(total_label, LBL_W)}{format(grand_total, '.2f').rjust(COLS - LBL_W)}")
    lines.append("-" * COLS)
    lines.append("Thank you for shopping with DRIP!".center(COLS))
    lines.append(" " * COLS)

    MARGIN = 2
    lines = [(" " * MARGIN) + line for line in lines]
    return "\n".join(lines)




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
        SELECT id, invoice_id, customer_name, phone, date, grand_total
        FROM sales_history
    """
    sql_where = " WHERE phone ILIKE %s OR invoice_id ILIKE %s" if search_term else ""
    sql_order = " ORDER BY id DESC "
    sql_limit = " LIMIT %s OFFSET %s"
    sql = sql_base + sql_where + sql_order + sql_limit

    params = (
        (f"%{search_term}%", f"%{search_term}%", limit + 1, offset)
        if search_term else
        (limit + 1, offset)
    )

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows[:limit], (len(rows) > limit)









def get_invoice_text(invoice_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_text FROM sales_history WHERE invoice_id = %s", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None