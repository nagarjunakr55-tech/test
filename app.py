# app.py
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
import json

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

# -----------------------------
# Billing (GET = UI, POST = legacy form fallback)
# -----------------------------
@app.route("/billing", methods=["GET", "POST"])
def billing():
    if request.method == "POST":
        # Legacy HTML form fallback (optional)
        name = (request.form.get("name") or "Guest").strip() or "Guest"
        phone = (request.form.get("phone") or "").strip()

        # Optional: support posting a cart JSON + discount from a simple form
        cart_json = request.form.get("cart_json")
        discount_input = (request.form.get("discount") or "").strip()

        if cart_json:
            try:
                items = json.loads(cart_json)
            except Exception:
                items = []
        else:
            # Example items (matches your original demo)
            items = [
                {"brand": "Nike", "mrp_price": 1200, "bought_price": 800, "price": 1000, "qty": 1, "size": "M", "category": "Shirt"},
                {"brand": "Adidas", "mrp_price": 1500, "bought_price": 1000, "price": 1200, "qty": 2, "size": "L", "category": "Pant"},
            ]

        if not (phone.isdigit() and len(phone) == 10):
            return render_template("billing.html", error="Enter valid 10-digit phone"), 400

        totals = calculate_totals(items, discount_input)
        invoice_id = generate_unique_invoice_id()
        invoice_text = generate_invoice_text(
            invoice_id=invoice_id,
            customer_name=name,
            customer_phone=phone,
            items=items,
            subtotal=totals["subtotal"],
            gst_amount=totals["gst"],
            grand_total=totals["grand_total"],
            discount_amt=totals["discount_amt"]
        )

        save_to_db(
            invoice_id=invoice_id,
            name=name,
            phone=phone,
            items=items,
            subtotal=totals["subtotal"],
            discount_amt=totals["discount_amt"],
            gst_amt=totals["gst"],
            grand_total=totals["grand_total"],
            invoice_text=invoice_text
        )

        return render_template("invoice.html", invoice=invoice_text, invoice_id=invoice_id)

    # GET → rich JS UI (uses /api/* below)
    return render_template("billing.html")

# -----------------------------
# History & Reprint
# -----------------------------
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

# -----------------------------
# Print views (3-inch, no margins)
# -----------------------------
@app.route("/print/<invoice_id>")
def print_invoice(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    # If your print.html supports label_mm, you can pass it as a param:
    # return render_template("print.html", invoice=invoice, label_mm=80)
    return render_template("print.html", invoice=invoice)

@app.route("/print")
def print_invoice_query():
    invoice_id = (request.args.get("invoice_id") or "").strip()
    if not invoice_id:
        abort(404, "Missing ?invoice_id=...")
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    return render_template("print.html", invoice=invoice)

# -----------------------------
# APIs used by billing.html JS
# -----------------------------
@app.post("/api/barcode_lookup")
def api_barcode_lookup():
    data = request.get_json(silent=True) or {}
    code = (data.get("barcode") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "barcode required"}), 400
    prod = find_product_by_barcode(code)
    if not prod:
        return jsonify({"ok": False, "error": "not_found"}), 404
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
        return jsonify
