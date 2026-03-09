# reports_bp.py
import os
from flask import Blueprint, render_template, request
from reports_logic import get_monthly_profit, get_daily_profit

# Make template path explicit & robust (works locally and on Render)
HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")

reports_bp = Blueprint("reports", __name__, template_folder=TPL_DIR)

@reports_bp.route("/reports/profit")
def profit():
    rows = get_monthly_profit() or []

    totals = {
        "invoices": sum(r["invoices"] for r in rows),
        "spent": round(sum(r["spent"] for r in rows), 2),
        "profit": round(sum(r["profit"] for r in rows), 2),
    }
    totals["total"] = round(totals["spent"] + totals["profit"], 2)

    return render_template("reports_profit.html", rows=rows, totals=totals)


@reports_bp.route("/reports/profit/daily")
def profit_daily():
    selected_date_iso = (request.args.get("date") or "").strip() or None
    rows = get_daily_profit(selected_date_iso) or []
    totals = {
        "invoices": sum(r["invoices"] for r in rows),
        "spent": round(sum(r["spent"] for r in rows), 2),
        "profit": round(sum(r["profit"] for r in rows), 2),
    }
    totals["total"] = round(totals["spent"] + totals["profit"], 2)
    return render_template(
        "reports_profit_daily.html",
        rows=rows,
        totals=totals,
        selected_date_iso=selected_date_iso
    )
