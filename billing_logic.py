# logic/billing_logic.py
from db import get_connection
from datetime import datetime
import json
import random
import string

GSTIN = "123123"
BRAND_NAME = "DRIP"

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
_ALLOWED_PCTS = {5, 10, 15, 20, 25, 30,35}


def calculate_totals(items, discount_input: str):
    if not items:
        raise ValueError("empty_cart")

    subtotal = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    bought_total = sum(float(i.get("bought_price", 0)) * int(i.get("qty", 1)) for i in items)

    di = (discount_input or "").strip()
    discount_amt = 0.0

    # parse percent; your UI only uses percent codes
    def _parse_percent(di_: str):
        if not di_:
            return None
        if di_.endswith("%"):
            return float(di_[:-1])
        # treat plain number as percent too, like before
        if di_.replace(".", "", 1).isdigit():
            return float(di_)
        return None

    pct = _parse_percent(di)

    if pct is not None:
        # ✅ Per-item unit guard
        for it in items:
            unit_sell   = float(it.get("price", 0))
            unit_bought = float(it.get("bought_price", 0))
            unit_after  = unit_sell * (1 - pct/100.0)
            if unit_after < (unit_bought + 100):
                # match your front-end message style; API can map this to a friendly error
                raise ValueError("max_discount_reached_item")

        discount_amt = subtotal * (pct / 100.0)
    elif di:
        # If you ever support flat, keep old behavior but note it won't meet per-item rule
        flat = float(di)
        discount_amt = min(flat, subtotal)

    grand_total = max(0.0, subtotal - discount_amt)

    # (Optional) keep the old cart-wide guard as a second safety net
    if di and grand_total < (bought_total + 100):
        raise ValueError("max_discount_reached")

    gst = 0

    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt, 2),
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
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
        show_time: bool = True,   # << control time on/off in Date
    ):
        # Narrow total width so 3" printers don't clip the right edge
        COLS = 38  # safe on most 3"

        # Tweaked widths for better alignment on thermal printers
        # Sum with spaces must stay within COLS
        W_NO   = 2   # "No"
        W_BR   = 8   # reduced to leave space for price "(orig)after"
        W_SZ   = 4   # "Size"
        W_Q    = 3   # "Qty"
        W_MRP  = 6   # "MRP"
        W_PRC  = 10  # "Price" wide enough for "(2599)999"

        def clamp(s, n): return (str(s)[:n]).ljust(n)

        lines = []

        # Header (centered)
        header = [
            "DRIP",
            "#6-2-52,",
            "D L Road, Hindupur,",
            "Satya Sai (D), AP-515201",
            "Phone: 8106204688"
        ]
        for s in header:
            lines.append(s.center(COLS))
        lines.append("-" * COLS)

        # Left/Right info line
        today_str = india_date_str(include_time=show_time)
        left  = f"Invoice: {invoice_id}"
        right = f"Date: {today_str}"
        lines.append(f"{left:<{COLS - len(right)}}{right}")

        # Customer line
        cust_line = f"Customer: {customer_name} / {customer_phone}"
        lines.append(clamp(cust_line, COLS))
        lines.append("-" * COLS)

        # Column header
        header = (
            f"{'No':<{W_NO}} "
            f"{'Brand':<{W_BR}} "
            f"{'Size':<{W_SZ}} "
            f"{'Qty':>{W_Q}} "
            f"{'MRP':>{W_MRP}} "
            f"{'Price':>{W_PRC}}"
        )
        lines.append(header)
        lines.append("-" * COLS)

        # --- FIX: define eff_pct_num safely before using it
        eff_pct_num = 0
        try:
            if float(subtotal or 0) > 0 and float(discount_amt or 0) > 0:
                eff = (float(discount_amt) / float(subtotal)) * 100.0
                eff_pct_num = int(round(eff))
        except Exception:
            eff_pct_num = 0

        # Item rows
        for idx, item in enumerate(items, start=1):
            brand = clamp(item.get("brand", ""), W_BR)
            size  = clamp(item.get("size", ""), W_SZ)
            qty   = int(item.get("qty", 1))
            mrp   = int(float(item.get("mrp_price", 0)))
            price_orig = int(float(item.get("price", 0)))

            # Show (orig)after if discount percent is applied; else show orig only
            if eff_pct_num > 0:
                price_after = int(round(price_orig * (1 - eff_pct_num/100.0)))
                price_str = f"({price_orig}){price_after}"
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

        # Effective discount % text for summary line (optional)
        disc_pct_text = ""
        try:
            if float(subtotal or 0) > 0 and float(discount_amt or 0) > 0:
                eff = (float(discount_amt) / float(subtotal)) * 100.0
                disc_pct_text = f" ({int(round(eff))}%)" if abs(eff - round(eff)) < 0.01 else f" ({eff:.1f}%)"
        except Exception:
            pass

        if float(discount_amt or 0) > 0:
            label = f"Discount{disc_pct_text}"
            lines.append(f"{clamp(label, LBL_W)}{('-' + format(discount_amt, '.2f')).rjust(COLS - LBL_W)}")

        total_label = "Total"
        lines.append(f"{clamp(total_label, LBL_W)}{format(grand_total, '.2f').rjust(COLS - LBL_W)}")
        lines.append("-" * COLS)
        lines.append("Thank you for shopping with DRIP!".center(COLS))
        lines.append(" " * COLS)

        # Left margin for printers
        MARGIN = 2
        lines = [(" " * MARGIN) + line for line in lines]
        return "\n".join(lines)



# ---------------------------------
# Persistence
# ---------------------------------

def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

    # ensure tables (safe to call)
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_barcodes (
            id SERIAL PRIMARY KEY,
            invoice_id TEXT,
            code TEXT,
            qty INTEGER DEFAULT 1
        )
    """)

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
def get_invoices(search_term=""):
    conn = get_connection()
    cur = conn.cursor()
    if search_term:
        cur.execute("""
            SELECT invoice_id, customer_name, phone, date, grand_total
            FROM sales_history
            WHERE phone ILIKE %s OR invoice_id ILIKE %s
            ORDER BY id DESC
        """, (f"%{search_term}%", f"%{search_term}%"))
    else:
        cur.execute("""
            SELECT invoice_id, customer_name, phone, date, grand_total
            FROM sales_history
            ORDER BY id DESC
        """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_invoice_text(invoice_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_text FROM sales_history WHERE invoice_id = %s", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None
