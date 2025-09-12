# reports_bp.py
from flask import Blueprint, render_template
from reports_logic import get_monthly_profit

reports_bp = Blueprint("reports", __name__, template_folder="templates")

@reports_bp.route("/reports/profit")
def profit():
    rows = get_monthly_profit()
    totals = {
        "invoices": sum(r["invoices"] for r in rows),
        "spent": round(sum(r["spent"] for r in rows), 2),
        "profit": round(sum(r["profit"] for r in rows), 2),
    }
    return render_template("reports_profit.html", rows=rows, totals=totals)
