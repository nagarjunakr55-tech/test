# reports_logic.py
from collections import defaultdict
from datetime import datetime
from typing import Optional
import json

from db import get_connection


def _parse_invoice_dt(date_str: str):
    """Parse 'DD-MM-YYYY hh:mm AM/PM' or 'DD-MM-YYYY' to datetime."""
    if not date_str:
        return datetime.utcnow()
    for fmt in ("%d-%m-%Y %I:%M %p", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except Exception:
            continue
    return datetime.utcnow()

def get_monthly_profit():
    """
    Returns list of rows like:
      {'label':'July-2025','year':int,'month':int,'invoices':int,'spent':float,'profit':float}
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

        try:
            items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
        except Exception:
            items = []

        spent = 0.0
        for it in items or []:
            qty = int((it.get("qty") or 1))
            bp  = float((it.get("bought_price") or 0))
            spent += bp * qty

        sales  = float(grand_total or 0)
        profit = sales - spent

        a = agg[key]
        a["invoices"] += 1
        a["spent"]    += spent
        a["profit"]   += profit

    out = []
    for (year, month), a in sorted(agg.items()):
        label = datetime(year, month, 1).strftime("%B-%Y")
        out.append({
            "year": year,
            "month": month,
            "label": label,
            "invoices": a["invoices"],
            "spent": round(a["spent"], 2),
            "profit": round(a["profit"], 2),
        })
    return out  # <- DO NOT REMOVE

def get_daily_profit(selected_date_iso: Optional[str] = None):
    """
    Returns list of rows like:
      {'label':'13-09-2025','year':int,'month':int,'day':int,'invoices':int,'spent':float,'profit':float}
    If selected_date_iso ('YYYY-MM-DD') is provided, returns only that day.
    """
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

        try:
            items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
        except Exception:
            items = []

        spent = 0.0
        for it in items or []:
            qty = int((it.get("qty") or 1))
            bp  = float((it.get("bought_price") or 0))
            spent += bp * qty

        sales  = float(grand_total or 0)
        profit = sales - spent

        a = agg[dkey]
        a["invoices"] += 1
        a["spent"]    += spent
        a["profit"]   += profit

    out = []
    for d, a in sorted(agg.items()):
        label = d.strftime("%d-%m-%Y")
        out.append({
            "year": d.year,
            "month": d.month,
            "day": d.day,
            "label": label,
            "invoices": a["invoices"],
            "spent": round(a["spent"], 2),
            "profit": round(a["profit"], 2),
        })
    return out  # <- DO NOT REMOVE
