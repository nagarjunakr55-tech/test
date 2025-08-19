# logic/billing_logic.py
from db import get_connection
from datetime import datetime
import json
import random
import string

GSTIN = "123123"
BRAND_NAME = "DRIP"

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
        date TIMESTAMP,
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
        status TEXT
    )
    """)

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
    return {
        "brand": row[0],
        "mrp": row[1],
        "bought_price": row[2],
        "selling_price": row[3],
        "size": row[4],
        "category": row[5],
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

    out = []
    for r in rows:
        out.append({
            "brand": r[0],
            "mrp": r[1],
            "bought_price": r[2],
            "selling_price": r[3],
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
_ALLOWED_PCTS = {5, 10, 15, 20, 25}

# logic/billing_logic.py
def calculate_totals(items, discount_input: str):
    """
    items: [{price, qty, bought_price, ...}]
    discount_input:
      - "<number>%"  -> percentage, NO max rule
      - "<number>"   -> flat amount, APPLY max rule: (subtotal - flat) >= (bought_total + 250)
    """
    if not items:
        raise ValueError("empty_cart")

    # sums
    subtotal = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    bought_total = sum(float(i.get("bought_price", 0)) * int(i.get("qty", 1)) for i in items)

    discount_amt = 0.0
    di = (discount_input or "").strip()

    if di.endswith("%"):
        # percentage, no max rule
        try:
            pct = float(di[:-1])
        except Exception:
            raise ValueError("invalid_discount")
        if pct < 0 or pct > 100:
            raise ValueError("invalid_discount")
        discount_amt = subtotal * (pct / 100.0)
    elif di:
        # flat amount, apply max rule
        try:
            flat = float(di)
        except Exception:
            raise ValueError("invalid_discount")
        if flat < 0:
            raise ValueError("invalid_discount")

        # check max rule BEFORE clamping
        grand_after = subtotal - flat
        if grand_after < (bought_total + 250):
            raise ValueError("max_discount_reached")

        discount_amt = min(flat, subtotal)  # clamp at subtotal
    else:
        discount_amt = 0.0

    grand_total = max(0.0, subtotal - discount_amt)
    gst = grand_total - (grand_total / 1.05)

    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt, 2),
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
    }


# ---------------------------------
# Invoice text (monospace)
# ---------------------------------
def generate_invoice_text(invoice_id, customer_name, customer_phone,
                          items, subtotal, gst_amount, grand_total, discount_amt):
    """
    Render a strictly monospaced 3" receipt with fixed column widths:
      No(3) Brand(20) Size(5) Qty(3) MRP(7) Price(7)  -> total row width = 45 chars
    """
    from datetime import datetime
    COL_NO, COL_BRAND, COL_SIZE, COL_QTY, COL_MRP, COL_PRICE = 3, 20, 5, 3, 7, 7
    ROW_W = COL_NO + COL_BRAND + COL_SIZE + COL_QTY + COL_MRP + COL_PRICE  # 45
    BAR = "-" * ROW_W
    now = datetime.now().strftime("%Y-%m-%d")

    # Header
    lines = []
    lines.append(BRAND_NAME.center(ROW_W))
    lines.append(BAR)
    # left/right within the same line
    left = f"Invoice: {invoice_id}"
    right = f"Date: {now}"
    lines.append(f"{left:<{ROW_W - len(right)}}{right}")
    lines.append(f"Customer: {customer_name} / {customer_phone}")
    lines.append(BAR)

    # Table header (match exact column widths)
    lines.append(
        f"{'No':<{COL_NO}}"
        f"{'Brand':<{COL_BRAND}}"
        f"{'Size':<{COL_SIZE}}"
        f"{'Qty':>{COL_QTY}}"
        f"{'MRP':>{COL_MRP}}"
        f"{'Price':>{COL_PRICE}}"
    )
    lines.append(BAR)

    # Rows
    for idx, item in enumerate(items, start=1):
        # Trim/normalize values
        brand = str(item.get("brand", ""))[:COL_BRAND]
        size = str(item.get("size", ""))[:COL_SIZE]
        # Force numeric qty (bad values get clamped to 0)
        try:
            qty = int(item.get("qty", 0))
        except Exception:
            qty = 0
        # Row prices: integers look better on 3" receipts
        try:
            mrp = int(round(float(item.get("mrp_price", 0))))
        except Exception:
            mrp = 0
        try:
            price = int(round(float(item.get("price", 0))))
        except Exception:
            price = 0

        lines.append(
            f"{idx:<{COL_NO}}"
            f"{brand:<{COL_BRAND}}"
            f"{size:<{COL_SIZE}}"
            f"{qty:>{COL_QTY}d}"
            f"{mrp:>{COL_MRP}d}"
            f"{price:>{COL_PRICE}d}"
        )

    lines.append(BAR)

    # Totals: keep right-aligned numeric block
    NUM_W = 12                      # width for the number at the end
    LABEL_W = ROW_W - NUM_W         # everything before it
    lines.append(f"{'Subtotal':<{LABEL_W}}{subtotal:>{NUM_W}.2f}")
    if discount_amt and float(discount_amt) > 0:
        lines.append(f"{'Discount':<{LABEL_W}}{-float(discount_amt):>{NUM_W}.2f}")
    # If you want to show GST as a line, uncomment:
    # lines.append(f"{'GST (approx)':<{LABEL_W}}{gst_amount:>{NUM_W}.2f}")
    lines.append(f"{'Grand Total':<{LABEL_W}}{grand_total:>{NUM_W}.2f}")
    lines.append(BAR)
    lines.append("Thank you for shopping with DRIP!".center(ROW_W))
    return "\n".join(lines)


# ---------------------------------
# Persistence
# ---------------------------------
def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

    # Ensure table exists (safe if already created)
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
        date TIMESTAMP,
        invoice_text TEXT
    )
    """)

    cur.execute("""
        INSERT INTO sales_history
        (invoice_id, customer_name, phone, items, total, discount, gst, grand_total, gstin, date, invoice_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (invoice_id) DO NOTHING
    """, (
        invoice_id, name, phone, json.dumps(items), subtotal,
        discount_amt, gst_amt, grand_total, GSTIN, invoice_text
    ))

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
