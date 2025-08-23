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
    discount_input: "20%" or "20" (plain number = percent)
    MAX rule: (subtotal - discount) must be >= (bought_total + 250) else reject.
    """
    if not items:
        raise ValueError("empty_cart")

    subtotal = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    bought_total = sum(float(i.get("bought_price", 0)) * int(i.get("qty", 1)) for i in items)

    di = (discount_input or "").strip()
    discount_amt = 0.0

    if di:
        is_percent = di.endswith("%") or di.replace(".", "", 1).isdigit()
        try:
            if di.endswith("%"):
                pct = float(di[:-1])
                discount_amt = subtotal * (pct / 100.0)
            elif di.replace(".", "", 1).isdigit():
                # plain number -> treat as percent
                pct = float(di)
                discount_amt = subtotal * (pct / 100.0)
            else:
                # flat amount (rare here, but supported)
                flat = float(di)
                discount_amt = min(flat, subtotal)
        except Exception:
            raise ValueError("invalid_discount")

        grand_total = max(0.0, subtotal - discount_amt)
        if grand_total < (bought_total + 100):
            raise ValueError("max_discount_reached")
    else:
        grand_total = subtotal

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

from datetime import datetime

BRAND_NAME = "DRIP"

def generate_invoice_text(invoice_id, customer_name, customer_phone, items, subtotal, gst_amount, grand_total, discount_amt):
    # Narrower total width so 3" printers don't clip the right edge
    COLS = 35  # safe on most 3" (was 42)
    # Field widths: 2 +1 +12 +1 +2 +1 +2 +1 +6 +1 +6 = 35
    W_NO   = 2
    W_BR   = 12
    W_SZ   = 2
    W_Q    = 2
    W_MRP  = 6
    W_PRC  = 6

    def clamp(s, n): return (str(s)[:n]).ljust(n)

    lines = []
    today = datetime.now().strftime("%Y-%m-%d")

    # --- Header (shop name + address, centered) ---
    header = [
        "DRIP",
        "",
        "#6-2-52,",
        "D L Road, Hindupur,",
        "Satya Sai (D), AP-515201",
        "Phone: 8106204688"
      
    ]
    
    for s in header:
        lines.append(s.center(COLS))

    

    lines.append("-" * COLS)

    # --- Left/Right info line (unchanged) ---
    today = datetime.now().strftime("%Y-%m-%d")
    left  = f"Invoice: {invoice_id}"
    right = f"Date: {today}"
    lines.append(f"{left:<{COLS - len(right)}}{right}")

    # --- Customer line (unchanged) ---
    cust_line = f"Customer: {customer_name} / {customer_phone}"
    lines.append(clamp(cust_line, COLS))
    lines.append("-" * COLS)


    # Header (kept explicit so "Price" doesn't get cut)
    header = (
        f"{'No':>{W_NO}} "
        f"{'Brand':<{W_BR}} "
        f"{'Sz':<{W_SZ}} "
        f"{'Q':>{W_Q}} "
        f"{'MRP':>{W_MRP}} "
        f"{'Price':>{W_PRC}}"
    )
    lines.append(header)
    lines.append("-" * COLS)

    for idx, item in enumerate(items, start=1):
        brand = clamp(item.get("brand", ""), W_BR)
        size  = clamp(item.get("size", ""), W_SZ)
        qty   = int(item.get("qty", 1))
        mrp   = int(float(item.get("mrp_price", 0)))
        price = int(float(item.get("price", 0)))

        row = (
            f"{idx:>{W_NO}} "
            f"{brand} "
            f"{size} "
            f"{qty:>{W_Q}} "
            f"{mrp:>{W_MRP}} "
            f"{price:>{W_PRC}}"
        )
        lines.append(row)

    lines.append("-" * COLS)
    # No "Subtotal" (as requested)
    if discount_amt and float(discount_amt) > 0:
        lines.append(f"{'Discount':<18}{('-' + format(discount_amt, '.2f')).rjust(COLS-18)}")
    lines.append(f"{'Grand Total':<18}{format(grand_total, '.2f').rjust(COLS-18)}")
    lines.append("-" * COLS)

    lines.append("Thank you for shopping with DRIP!".center(COLS))

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