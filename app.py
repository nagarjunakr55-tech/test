# app.py (add imports at top)
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from logic.billing_logic import (
    ensure_tables,
    generate_unique_invoice_id,
    generate_invoice_text,
    save_to_db,
    get_invoices,
    get_invoice_text,
    find_product_by_barcode,
    search_products,
    get_last_customer_name,
    calculate_totals,
)

app = Flask(__name__)

# Ensure tables exist on boot
ensure_tables()

@app.route("/")
def home():
    return redirect(url_for("billing"))

# ---------- New: rich billing page (UI handled in billing.html with JS) ----------
@app.route("/billing", methods=["GET"])
def billing():
    return render_template("billing.html")

# ---------- Existing ----------
@app.route("/history", methods=["GET", "POST"])
def history():
    search_term = request.form.get("search", "").strip() if request.method == "POST" else ""
    rows = get_invoices(search_term)
    return render_template("history.html", rows=rows, search_term=search_term)

@app.route("/reprint/<invoice_id>")
def reprint(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if invoice:
        return render_template("invoice.html", invoice=invoice, invoice_id=invoice_id)
    return f"Invoice {invoice_id} not found", 404

# ---------- Print views (make sure you already added these) ----------
@app.route("/print/<invoice_id>")
def print_invoice(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    return render_template("print.html", invoice=invoice)

@app.route("/print")
def print_invoice_query():
    invoice_id = request.args.get("invoice_id", "").strip()
    if not invoice_id:
        abort(404, "Missing ?invoice_id=...")
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    return render_template("print.html", invoice=invoice)

# =========================
#           APIs
# =========================

@app.post("/api/barcode_lookup")
def api_barcode_lookup():
    data = request.get_json(silent=True) or {}
    code = (data.get("barcode") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "barcode required"}), 400
    prod = find_product_by_barcode(code)
    if not prod:
        return jsonify({"ok": False, "error": "not_found"}), 404
    # Normalize to cart item shape used by UI
    item = {
        "brand": prod["brand"],
        "mrp_price": float(prod.get("mrp", 0)),
        "bought_price": float(prod.get("bought_price", 0)),
        "price": float(prod.get("selling_price", 0)),
        "qty": 1,
        "size": prod.get("size", ""),
        "category": prod.get("category", "")
    }
    return jsonify({"ok": True, "item": item})

@app.post("/api/search_products")
def api_search_products():
    data = request.get_json(silent=True) or {}
    brand = (data.get("brand") or "").strip()
    category = (data.get("category") or "").strip()
    size = (data.get("size") or "").strip()
    limit = int(data.get("limit") or 20)
    rows = search_products(brand=brand, category=category, size=size, limit=limit)
    # Return list of normalized items ready to add
    out = []
    for r in rows:
        out.append({
            "brand": r["brand"],
            "mrp_price": float(r.get("mrp", 0)),
            "bought_price": float(r.get("bought_price", 0)),
            "price": float(r.get("selling_price", 0)),
            "qty": 1,
            "size": r.get("size", ""),
            "category": r.get("category", "")
        })
    return jsonify({"ok": True, "results": out})

@app.get("/api/customer_last_name")
def api_customer_last_name():
    phone = (request.args.get("phone") or "").strip()
    if not phone or not phone.isdigit() or len(phone) != 10:
        return jsonify({"ok": False, "name": ""})
    name = get_last_customer_name(phone)
    return jsonify({"ok": True, "name": name or ""})

@app.post("/api/checkout")
def api_checkout():
    """
    Body: {
      customer_name, phone, items:[{brand, mrp_price, bought_price, price, qty, size, category}],
      discount: "10%" or "100"
    }
    """
    data = request.get_json(silent=True) or {}
    cust_name = (data.get("customer_name") or "Guest").strip() or "Guest"
    phone = (data.get("phone") or "").strip()

    if not (phone.isdigit() and len(phone) == 10):
        return jsonify({"ok": False, "error": "invalid_phone"}), 400

    items = data.get("items") or []
    if not items:
        return jsonify({"ok": False, "error": "empty_cart"}), 400

    # Server-side totals + rules
    try:
        totals = calculate_totals(items, data.get("discount", ""))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    invoice_id = generate_unique_invoice_id()
    invoice_text = generate_invoice_text(
        invoice_id=invoice_id,
        customer_name=cust_name,
        customer_phone=phone,
        items=items,
        subtotal=totals["subtotal"],
        gst_amount=totals["gst"],
        grand_total=totals["grand_total"],
        discount_amt=totals["discount_amt"]
    )

    # Save
    save_to_db(
        invoice_id=invoice_id,
        name=cust_name,
        phone=phone,
        items=items,
        subtotal=totals["subtotal"],
        discount_amt=totals["discount_amt"],
        gst_amt=totals["gst"],
        grand_total=totals["grand_total"],
        invoice_text=invoice_text
    )

    return jsonify({"ok": True, "invoice_id": invoice_id, "invoice_text": invoice_text})


@app.route("/billing", methods=["GET", "POST"])
def billing():
    if request.method == "POST":
        name = request.form.get("name", "Guest")
        phone = request.form.get("phone", "9999999999")

        items = [
            {"brand": "Nike", "mrp_price": 1200, "price": 1000, "qty": 1, "size": "M", "category": "Shirt"},
            {"brand": "Adidas", "mrp_price": 1500, "price": 1200, "qty": 2, "size": "L", "category": "Pant"},
        ]

        subtotal = sum(i["price"] * i["qty"] for i in items)
        discount_amt = 100
        grand_total = subtotal - discount_amt
        gst_amt = grand_total - (grand_total / 1.05)

        invoice_id = "INV001"  # later: auto-generate unique IDs
        invoice = generate_invoice_text(invoice_id, name, phone, items, subtotal, gst_amt, grand_total, discount_amt)

        save_to_db(invoice_id, name, phone, items, subtotal, discount_amt, gst_amt, grand_total, invoice)

        return render_template("invoice.html", invoice=invoice, invoice_id=invoice_id)


    return render_template("billing.html")

@app.route("/history", methods=["GET", "POST"])
def history():
    search_term = request.form.get("search", "").strip() if request.method == "POST" else ""
    rows = get_invoices(search_term)
    return render_template("history.html", rows=rows, search_term=search_term)

@app.route("/reprint/<invoice_id>")
def reprint(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if invoice:
        return render_template("invoice.html", invoice=invoice)
    return f"Invoice {invoice_id} not found", 404
# app.py (add this route)
from flask import Flask, render_template, request, redirect, url_for
# ...existing imports...

from flask import send_file  # already imported render_template etc.

from flask import render_template, request, abort

@app.route("/print/<invoice_id>")
def print_invoice(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    # Try 80 first (common for 3" drivers). If there's extra space, change to 76.2
    return render_template("print.html", invoice=invoice, label_mm=80)


# /print?invoice_id=INV123  → friendly fallback if someone visits /print/ without the ID
@app.route("/print")
def print_invoice_query():
    invoice_id = request.args.get("invoice_id", "").strip()
    if not invoice_id:
        abort(404, "Missing ?invoice_id=... in URL")
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    return render_template("print.html", invoice=invoice)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
