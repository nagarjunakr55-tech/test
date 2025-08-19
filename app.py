from flask import Flask, render_template, request, redirect, url_for
from logic.billing_logic import generate_invoice_text, save_to_db, get_invoices, get_invoice_text

app = Flask(__name__)

@app.route("/")
def home():
    return redirect(url_for("billing"))

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

        return render_template("invoice.html", invoice=invoice)

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

@app.route("/print/<invoice_id>")
def print_invoice(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        return f"Invoice {invoice_id} not found", 404
    return render_template("print.html", invoice=invoice)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
