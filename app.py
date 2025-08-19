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

# /print/<invoice_id>  → primary route
@app.route("/print/<invoice_id>")
def print_invoice(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        abort(404, f"Invoice {invoice_id} not found")
    return render_template("print.html", invoice=invoice)

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



# app.py (add this)
from io import BytesIO
from flask import send_file
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

@app.route("/printpdf/<invoice_id>")
def printpdf(invoice_id):
    invoice = get_invoice_text(invoice_id)
    if not invoice:
        return f"Invoice {invoice_id} not found", 404

    # Compute height based on lines (12pt * 1.2 line height ~= 14.4pt)
    lines = invoice.splitlines()
    line_height_pt = 12 * 1.2
    top_margin_pt = 0  # zero margins
    bottom_margin_pt = 0
    height_pt = int(len(lines) * line_height_pt) + 10  # small buffer

    width_pt = 76.2 * mm  # exactly 3 inches wide

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width_pt, height_pt))

    # Draw text starting near the top-left (origin is bottom-left)
    x = 0    # left edge
    y = height_pt - line_height_pt  # start from top
    c.setFont("Courier", 10)  # mono font

    for ln in lines:
        c.drawString(x, y, ln)
        y -= line_height_pt
        if y < 0:  # very long receipts: new page
            c.showPage()
            y = height_pt - line_height_pt
            c.setFont("Courier", 10)

    c.showPage()
    c.save()
    buf.seek(0)

    return send_file(
        buf, mimetype="application/pdf",
        as_attachment=False, download_name=f"{invoice_id}.pdf"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
