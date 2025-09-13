# reports_bp.py
from flask import Blueprint, render_template, request
from reports_logic import get_monthly_profit, get_daily_profit

reports_bp = Blueprint("reports", __name__, template_folder="templates")

@reports_bp.route("/reports/profit")
def profit():
    rows = get_monthly_profit() or []  # guard against None
    totals = {
        "invoices": sum(r["invoices"] for r in rows),
        "spent": round(sum(r["spent"] for r in rows), 2),
        "profit": round(sum(r["profit"] for r in rows), 2),
    }
    return render_template("reports_profit.html", rows=rows, totals=totals)

@reports_bp.route("/reports/profit/daily")
def profit_daily():
    selected_date_iso = (request.args.get("date") or "").strip() or None
    rows = get_daily_profit(selected_date_iso) or []  # guard against None
    totals = {
        "invoices": sum(r["invoices"] for r in rows),
        "spent": round(sum(r["spent"] for r in rows), 2),
        "profit": round(sum(r["profit"] for r in rows), 2),
    }
    return render_template(
        "reports_profit_daily.html",
        rows=rows,
        totals=totals,
        selected_date_iso=selected_date_iso
    )
