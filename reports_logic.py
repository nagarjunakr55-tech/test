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
    Also handles Python datetime/date objects from DB adapters.
    Falls back to utcnow() if parsing fails.
    """
    if isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, date):
        return datetime.combine(date_val, time())

    s = str(date_val or "").strip()
    if not s:
        return datetime.utcnow()

    # Normalize: allow slash, T, and trailing Z
    s_norm = s.replace("/", "-").replace("T", " ").rstrip("Z")

    # Try ISO (Python 3.9 supports without 'Z')
    try:
        return datetime.fromisoformat(s_norm)
    except Exception:
        pass

    # Try a set of common formats
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
    for it in items or []:
        qty = int((it.get("qty") or 1))
        bp = float((it.get("bought_price") or 0))
        spent += bp * qty
    return float(spent)


def get_monthly_profit():
    """
    Returns list of rows:
      {
        'label': 'July-2025',
        'year': int,
        'month': int,
        'invoices': int,
        'spent': float,    # Σ(bought_price * qty)
        'profit': float,   # grand_total - spent (GST kept in revenue)
        'total': float     # spent + profit  (equals grand_total)
      }
    """
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
        sales = float(grand_total or 0.0)  # includes GST; you’re not paying it out
        profit = sales - spent

        a = agg[key]
        a["invoices"] += 1
        a["spent"] += spent
        a["profit"] += profit

    out = []
    for (year, month), a in sorted(agg.items()):
        label = datetime(year, month, 1).strftime("%B-%Y")
        spent = round(a["spent"], 2)
        profit = round(a["profit"], 2)
        total = round(spent + profit, 2)  # equals sales sum
        out.append({
            "year": year,
            "month": month,
            "label": label,
            "invoices": a["invoices"],
            "spent": spent,
            "profit": profit,
            "total": total,
        })
    return out


def get_daily_profit(selected_date_iso: Optional[str] = None):
    """
    Returns list of rows:
      {
        'label': '13-09-2025',
        'year': int,
        'month': int,
        'day': int,
        'invoices': int,
        'spent': float,    # Σ(bought_price * qty)
        'profit': float,   # grand_total - spent (GST kept in revenue)
        'total': float     # spent + profit  (equals grand_total)
      }
    If selected_date_iso ('YYYY-MM-DD') is provided, includes only that date.
    """
    filter_date = None
    if selected_date_iso:
        try:
            filter_date = datetime.strptime(selected_date_iso, "%Y-%m-%d").date()
        except Exception:
            filter_date = None  # ignore bad input, behave as "no filter"

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
        label = d.strftime("%d-%m-%Y")
        spent = round(a["spent"], 2)
        profit = round(a["profit"], 2)
        total = round(spent + profit, 2)
        out.append({
            "year": d.year,
            "month": d.month,
            "day": d.day,
            "label": label,
            "invoices": a["invoices"],
            "spent": spent,
            "profit": profit,
            "total": total,
        })
    return out
