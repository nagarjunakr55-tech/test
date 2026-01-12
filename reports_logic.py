# reports_logic.py
from collections import defaultdict
from datetime import datetime, date, time
from typing import Optional
import json

from db import get_connection


def _parse_invoice_dt(date_val):
    """
    Accepts:
      - 'DD-MM-YYYY hh:mm AM/PM'
      - 'DD-MM-YYYY'
      - 'YYYY-MM-DD HH:MM[:SS]'
      - 'YYYY-MM-DD'
      - 'YYYY-MM-DDTHH:MM[:SS][.ffffff][Z]'
    """
    if isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, date):
        return datetime.combine(date_val, time())

    s = str(date_val or "").strip()
    if not s:
        return datetime.utcnow()

    s_norm = s.replace("/", "-").replace("T", " ").rstrip("Z")

    try:
        return datetime.fromisoformat(s_norm)
    except Exception:
        pass

    fmts = (
        "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(s_norm, fmt)
        except Exception:
            continue

    return datetime.utcnow()


def _spent_from_items(items_json) -> float:
    """Σ(bought_price * qty) from items JSON string/list."""
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
    except Exception:
        items = []

    spent = 0.0
    for it in items:
        qty = int(it.get("qty") or 1)
        bp = float(it.get("bought_price") or 0)
        spent += bp * qty

    return float(spent)


# -----------------------------
# MONTHLY PROFIT (MONTHS ONLY)
# -----------------------------
def get_monthly_profit():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT date, items, grand_total FROM sales_history")
    rows = cur.fetchall()
    conn.close()

    agg = defaultdict(lambda: {"invoices": 0, "spent": 0.0, "profit": 0.0})

    for date_str, items_json, grand_total in rows:
        dt = _parse_invoice_dt(date_str)
        key = (dt.year, dt.month)

        spent = _spent_from_items(items_json)
        sales = float(grand_total or 0.0)
        profit = sales - spent

        a = agg[key]
        a["invoices"] += 1
        a["spent"] += spent
        a["profit"] += profit

    out = []
    for (year, month), a in sorted(agg.items()):
        spent = round(a["spent"], 2)
        profit = round(a["profit"], 2)

        out.append({
            "year": year,
            "month": month,
            "label": datetime(year, month, 1).strftime("%B-%Y"),
            "invoices": a["invoices"],
            "spent": spent,
            "profit": profit,
            "total": round(spent + profit, 2),
        })

    return out


# -----------------------------
# TODAY REPORT (SEPARATE)
# -----------------------------
def _get_today_totals():
    today = date.today()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT date, items, grand_total FROM sales_history")
    rows = cur.fetchall()
    conn.close()

    invoices = 0
    spent = 0.0
    profit = 0.0

    for date_str, items_json, grand_total in rows:
        dt = _parse_invoice_dt(date_str)
        if dt.date() != today:
            continue

        s = _spent_from_items(items_json)
        sales = float(grand_total or 0.0)

        invoices += 1
        spent += s
        profit += (sales - s)

    spent = round(spent, 2)
    profit = round(profit, 2)

    return {
        "label": f"Today ({today.strftime('%d-%m-%Y')})",
        "invoices": invoices,
        "spent": spent,
        "profit": profit,
        "total": round(spent + profit, 2),
    }


# -----------------------------
# DAILY PROFIT (DATES ONLY)
# -----------------------------
def get_daily_profit(selected_date_iso: Optional[str] = None):
    filter_date = None
    if selected_date_iso:
        try:
            filter_date = datetime.strptime(selected_date_iso, "%Y-%m-%d").date()
        except Exception:
            filter_date = None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT date, items, grand_total FROM sales_history")
    rows = cur.fetchall()
    conn.close()

    agg = defaultdict(lambda: {"invoices": 0, "spent": 0.0, "profit": 0.0})

    for date_str, items_json, grand_total in rows:
        dt = _parse_invoice_dt(date_str)
        dkey = dt.date()

        if filter_date and dkey != filter_date:
            continue

        spent = _spent_from_items(items_json)
        sales = float(grand_total or 0.0)
        profit = sales - spent

        a = agg[dkey]
        a["invoices"] += 1
        a["spent"] += spent
        a["profit"] += profit

    out = []
    for d, a in sorted(agg.items()):
        spent = round(a["spent"], 2)
        profit = round(a["profit"], 2)

        out.append({
            "year": d.year,
            "month": d.month,
            "day": d.day,
            "label": d.strftime("%d-%m-%Y"),
            "invoices": a["invoices"],
            "spent": spent,
            "profit": profit,
            "total": round(spent + profit, 2),
        })

    return out
