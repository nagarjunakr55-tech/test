# controllers/barcodes_controller.py
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, abort
from db import get_connection
from datetime import datetime
from typing import Optional
import json
import re

barcodes_bp = Blueprint("barcodes", __name__)

def _is_admin() -> bool:
    return session.get("is_admin") is True

@barcodes_bp.app_context_processor
def inject_admin():
    return {"is_admin": _is_admin()}

# ---------- Page ----------
@barcodes_bp.get("/barcodes")
def page():
    # recent rows: brand, mrp, bought_price, selling_price, size, category, code, created_at
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT brand, mrp, bought_price, selling_price, size, category, barcode_data, created_at
            FROM public.barcode_catalog
            ORDER BY id DESC
            LIMIT 100
        """)
        recent_rows = cur.fetchall()

    # optional prefill params (compatible with your template fields)
    ctx = {
        "brand_val": request.args.get("brand", ""),
        "mrp_val": request.args.get("mrp", ""),
        "bought_val": request.args.get("bought", ""),
        "sp_val": request.args.get("sp", ""),
        "size_val": request.args.get("size", ""),
        "category_val": request.args.get("category", ""),
        "copies_val": request.args.get("copies", 1),
        "recent_rows": recent_rows,
    }
    return render_template("barcodes.html", **ctx)

# ---------- AJAX: exists ----------
@barcodes_bp.get("/barcodes/exists/<code>")
def exists(code: str):
    code = (code or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "empty"}), 400
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM public.barcode_catalog WHERE barcode_data=%s LIMIT 1", (code,))
        row = cur.fetchone()
        return jsonify({"ok": True, "exists": bool(row)})

# ---------- AJAX: create (save only) ----------
@barcodes_bp.post("/barcodes/create")
def create():
    data = request.get_json(silent=True) or {}
    brand    = (data.get("brand") or "").strip()
    mrp      = (data.get("mrp") or "").strip()
    bought   = (data.get("bought") or "").strip()
    sp       = (data.get("sp") or "").strip()
    size     = (data.get("size") or "").strip()
    category = (data.get("category") or "").strip()
    code     = (data.get("code") or "").strip().upper()

    if not brand or not size or not category or not code:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    if not re.fullmatch(r"[A-Z0-9_-]{6,40}", code):
        return jsonify({"ok": False, "error": "bad_code"}), 400

    try:
        mrp_f    = float(mrp) if mrp else None
        bought_f = float(bought) if bought else None
        sp_f     = float(sp) if sp else None
    except Exception:
        return jsonify({"ok": False, "error": "bad_numbers"}), 400

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.barcode_catalog
              (brand, mrp, bought_price, selling_price, size, category, barcode_data, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'available', NOW())
            ON CONFLICT (barcode_data) DO NOTHING
        """, (brand, mrp_f, bought_f, sp_f, size, category, code))
        conn.commit()

    return jsonify({"ok": True, "code": code})

# ---------- Delete (admin only) ----------
@barcodes_bp.post("/barcodes/delete/<code>")
def delete(code: str):
    if not _is_admin():
        abort(403)
    code = (code or "").strip().upper()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM public.barcode_catalog WHERE barcode_data=%s", (code,))
        conn.commit()
    # Return to page; template posts a form
    return redirect(url_for("barcodes.page"))

# ---------- Quick print ----------
@barcodes_bp.post("/barcodes/quick-print")
def quick_print():
    """
    Keeps behavior minimal: saves/updates the row and returns ok.
    If you already have a TSPL deeplink flow, you can generate and return it here.
    """
    data = request.get_json(silent=True) or {}
    brand    = (data.get("brand") or "").strip()
    mrp      = (data.get("mrp") or "").strip()
    bought   = (data.get("bought") or "").strip()
    sp       = (data.get("sp") or "").strip()
    size     = (data.get("size") or "").strip()
    category = (data.get("category") or "").strip()
    copies   = int(data.get("copies") or 1)

    if not brand or not size or not category:
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    try:
        mrp_f    = float(mrp) if mrp else None
        bought_f = float(bought) if bought else None
        sp_f     = float(sp) if sp else None
    except Exception:
        return jsonify({"ok": False, "error": "bad_numbers"}), 400

    # Store a quick row (without unique code generation here)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.barcode_catalog
              (brand, mrp, bought_price, selling_price, size, category, status, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,'available', NOW())
        """, (brand, mrp_f, bought_f, sp_f, size, category))
        conn.commit()

    # If you later add TSPL generation, return a mobile deeplink here:
    # return jsonify({"ok": True, "deeplink": f"/barcodes/tspl/{code}.txt"})
    return jsonify({"ok": True})
