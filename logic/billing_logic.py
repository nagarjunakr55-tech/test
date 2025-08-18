from db import get_connection
import json
from datetime import datetime

GSTIN = "123123"
BRAND_NAME = "DRIP"

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
