# logic/billing_logic.py
from datetime import datetime
import json
import random
import string

from db import get_connection

GSTIN = "123123"
BRAND_NAME = "DRIP"

# ---------------------------
# Bootstrap / Tables
# ---------------------------
def ensure_tables():
    conn = get_connection()
    cur = conn.cursor()
    # sales_history
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
    # barcode_catalog
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

# ---------------------------
# Product lookups
# ---------------------------
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
        "mrp": float(row[1] or 0),
        "bought_price": float(row[2] or 0),
        "selling_price": float(row[3] or 0),
        "size": row[4] or "",
        "category": row[5] or "",
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
            "brand": r[0] or "",
            "mrp": float(r[1] or 0),
            "bought_price": float(r[2] or 0),
            "selling_price": float(r[3] or 0),
            "size": r[4] or "",
            "category": r[5] or "",
        })
    return out

def get_last_customer_name(phone: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT customer_name FROM sales_history
        WHERE phone = %s
        ORDER BY id DESC LIMIT 1
    """, (phone,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

# ---------------------------
# Invoice ID
# ---------------------------
def generate_unique_invoice_id() -> str:
    """
    Generate a 6-char unique ID not present in sales_history.invoice_id
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_id FROM sales_history")
    existing = {r[0] for r in cur.fetchall() if r[0]}
    cur.close()
    conn.close()

    while True:
        candidate = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if candidate not in existing:
            return candidate

# ---------------------------
# Totals & rules
# ---------------------------
_ALLOWED_PCTS = {10, 15, 20, 25}

def calculate_totals(items, discount_input: str):
    """
    items: [{price, qty, bought_price, ...}]
    discount_input: "20%" or "20" (plain number => percent)
    Only 10/15/20/25 allowed. Max rule enforced:
      (subtotal - discount) >= (bought_total + 250)
    """
    if not items:
        raise ValueError("empty_cart")

    subtotal = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    bought_total = sum(float(i.get("bought_price", 0)) * int(i.get("qty", 1)) for i in items)

    di = (discount_input or "").strip()
    discount_amt = 0.0
    grand_total = subtotal

    if di:
        # normalize to a percent value
        raw = di[:-1] if di.endswith("%") else di
        try:
            pct = float(raw)
        except Exception:
            raise ValueError("invalid_discount")

        if pct not in _ALLOWED_PCTS:
            raise ValueError("invalid_discount")

        discount_amt = subtotal * (pct / 100.0)
        grand_total = max(0.0, subtotal - discount_amt)

        # enforce MAX rule
        if grand_total < (bought_total + 250):
            raise ValueError("max_discount_reached")

    gst = grand_total - (grand_total / 1.05)

    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt, 2),
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
    }

# ---------------------------
# Invoice text (42 columns, no "Subtotal")
# ---------------------------
def generate_invoice_text(invoice_id, customer_name, customer_phone, items, subtotal, gst_amount, grand_total, discount_amt):
    """
    Fixed-width 42 columns (good for 80mm / 3" printers with Font A).
    Columns sum to 42:
      2 +1 +18 +1 +3 +1 +2 +1 +6 +1 +6
    """
    COLS = 42

    def clamp(s, n):
        return (str(s)[:n]).ljust(n)

    lines = []
    today = datetime.now().strftime("%Y-%m-%d")

    lines.append(BRAND_NAME.center(COLS))
    lines.append("-" * COLS)
    # 24 + 18 = 42
    lines.append(f"{('Invoice: ' + str(invoice_id)):<24}{('Date: ' + today):>18}")
    lines.append(clamp(f"Customer: {customer_name} / {customer_phone}", COLS))
    lines.append("-" * COLS)

    # Header
    header = f"{'No':>2} {'Brand':<18} {'Sz':<3} {'Q':>2} {'MRP':>6} {'Price':>6}"
    lines.append(header)
    lines.append("-" * COLS)

    for idx, item in enumerate(items, start=1):
        brand = clamp(item.get("brand", ""), 18)
        size  = clamp(item.get("size", ""), 3)
        qty   = int(item.get("qty", 1))
        mrp   = int(float(item.get("mrp_price", 0)))
        price = int(float(item.get("price", 0)))

        row = (
            f"{idx:>2} "      # 2 + 1
            f"{brand}"        # 18
            f" {size}"        # 1 + 3
            f" {qty:>2}"      # 1 + 2
            f" {mrp:>6}"      # 1 + 6
            f" {price:>6}"    # 1 + 6  => 42 total
        )
        lines.append(row)

    lines.append("-" * COLS)
    # (No "Subtotal" line)
    if discount_amt and float(discount_amt) > 0:
        lines.append(f"{'Discount':<20}{('-' + format(discount_amt, '.2f')):>{COLS-20}}")
    lines.append(f"{'Grand Total':<20}{format(grand_total, '.2f'):>{COLS-20}}")
    lines.append("-" * COLS)
    lines.append("Thank you for shopping with DRIP!".center(COLS))

    return "\n".join(lines)

# ---------------------------
# Persistence / Queries
# ---------------------------
def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sales_history
        (invoice_id, customer_name, phone, items, total, discount, gst, grand_total, gstin, date, invoice_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (invoice_id) DO NOTHING
    """, (
        invoice_id, name, phone, json.dumps(items),
        subtotal, discount_amt, gst_amt, grand_total, GSTIN, invoice_text
    ))
    conn.commit()
    cur.close()
    conn.close()

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
