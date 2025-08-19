# logic/billing_logic.py
from db import get_connection
from datetime import datetime
import json
import random
import string

GSTIN = "123123"
BRAND_NAME = "DRIP"

# ---------- Table bootstrap ----------
def ensure_tables():
    conn = get_connection()
    cur = conn.cursor()
    # sales_history (as you had)
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
    # barcode_catalog for product lookup/search
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

# ---------- Product lookups ----------
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
        "brand": row[0], "mrp": row[1], "bought_price": row[2],
        "selling_price": row[3], "size": row[4], "category": row[5]
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
            "brand": r[0], "mrp": r[1], "bought_price": r[2],
            "selling_price": r[3], "size": r[4], "category": r[5]
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

# ---------- Invoice ID ----------
def generate_unique_invoice_id() -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invoice_id FROM sales_history")
    existing = {r[0] for r in cur.fetchall() if r[0]}
    cur.close(); conn.close()
    while True:
        candidate = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if candidate not in existing:
            return candidate

# ---------- Totals & rules ----------
_ALLOWED_PCTS = {5, 10, 15, 20, 25}

def calculate_totals(items, discount_input: str):
    """
    items: [{price, qty, bought_price, ...}]
    discount_input: "10%" or "100" (flat)
    Rules:
      - If %: must be in _ALLOWED_PCTS
      - If %: final must be >= sum(bought_price*qty) + 250
      - If flat: no max rule, just clamp >= 0
    """
    if not items:
        raise ValueError("empty_cart")

    subtotal = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    bought_total = sum(float(i.get("bought_price", 0)) * int(i.get("qty", 1)) for i in items)

    discount_amt = 0.0
    di = (discount_input or "").strip()
    if di.endswith("%"):
        try:
            pct = float(di[:-1])
        except:
            raise ValueError("invalid_discount")
        if pct not in _ALLOWED_PCTS:
            raise ValueError(f"allowed_percents:{sorted(_ALLOWED_PCTS)}")
        discount_amt = subtotal * (pct / 100.0)
        final = subtotal - discount_amt
        if final < (bought_total + 250):
            raise ValueError("max_discount_reached")
    elif di:
        # flat amount
        try:
            flat = float(di)
        except:
            raise ValueError("invalid_discount")
        discount_amt = min(flat, subtotal)
    # totals
    grand_total = max(0.0, subtotal - discount_amt)
    gst = grand_total - (grand_total / 1.05)

    return {
        "subtotal": round(subtotal, 2),
        "discount_amt": round(discount_amt, 2),
        "grand_total": round(grand_total, 2),
        "gst": round(gst, 2),
    }

# ---------- Invoice text (same format you used) ----------
def generate_invoice_text(invoice_id, customer_name, customer_phone, items, subtotal, gst_amount, grand_total, discount_amt):
    lines = []
    now = datetime.now().strftime("%Y-%m-%d")

    lines.append(" " * 20 + BRAND_NAME)
    lines.append("-" * 48)
    lines.append(f"{'Invoice: ' + str(invoice_id):<30}{'Date: ' + now:>18}")
    lines.append(f"Customer: {customer_name} / {customer_phone}")
    lines.append("-" * 48)
    lines.append(f"{'No Brand':<13} {'Size Qty MRP Price':>25}")
    lines.append("-" * 48)

    for idx, item in enumerate(items, start=1):
        no = str(idx).ljust(3)
        brand = str(item.get("brand", ""))[:20].ljust(20)
        size = str(item.get("size", "")).ljust(5)
        qty = str(int(item.get("qty", 1)))
        mrp_val = f"{float(item.get('mrp_price', 0)):.0f}"
        price_val = f"{float(item.get('price', 0)):.0f}"
        lines.append(f"{no}{brand}{size}{qty}{mrp_val:<6}{price_val:>6}")

    lines.append("-" * 48)
    lines.append(f"{'Subtotal':<24}{subtotal:>24.2f}")
    if discount_amt and float(discount_amt) > 0:
        lines.append(f"{'Discount':<24}{('-' + format(discount_amt, '.2f')):>24}")
    lines.append(f"{'Grand Total':<24}{grand_total:>24.2f}")
    lines.append("-" * 48)
    lines.append("Thank you for shopping with DRIP!".center(48))

    return "\n".join(lines)

# ---------- Persistence (your original, kept) ----------
def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

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

# ---------- Existing (kept) ----------
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
        cur.execute("SELECT invoice_id, customer_name, phone, date, grand_total FROM sales_history ORDER BY id DESC")
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

def generate_invoice_text(invoice_id, customer_name, customer_phone, items, subtotal, gst_amount, grand_total, discount_amt):
    lines = []
    now = datetime.now().strftime("%Y-%m-%d")

    lines.append(" " * 20 + BRAND_NAME)
    lines.append("-" * 48)
    lines.append(f"{'Invoice: ' + str(invoice_id):<30}{'Date: ' + now:>18}")
    lines.append(f"Customer: {customer_name} / {customer_phone}")
    lines.append("-" * 48)
    lines.append(f"{'No Brand':<13} {'Size Qty MRP Price':>25}")
    lines.append("-" * 48)

    for idx, item in enumerate(items, start=1):
        no = str(idx).ljust(3)
        brand = item.get("brand", "")[:20].ljust(20)
        size = str(item.get("size", "")).ljust(5)
        qty = str(item.get("qty", 1))
        mrp_val = f"{float(item.get('mrp_price', 0)):.0f}"
        price_val = f"{float(item.get('price', 0)):.0f}"
        lines.append(f"{no}{brand}{size}{qty}{mrp_val:<6}{price_val:>6}")

    lines.append("-" * 48)
    lines.append(f"{'Subtotal':<24}{subtotal:>24.2f}")
    if discount_amt > 0:
        lines.append(f"{'Discount':<24}{'-'+format(discount_amt, '.2f'):>24}")
    lines.append(f"{'Grand Total':<24}{grand_total:>24.2f}")
    lines.append("-" * 48)
    lines.append("Thank you for shopping with DRIP!".center(48))

    return "\n".join(lines)


def save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice_text=""):
    conn = get_connection()
    cur = conn.cursor()

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
        cur.execute("SELECT invoice_id, customer_name, phone, date, grand_total FROM sales_history ORDER BY id DESC")
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
