# labels.py
import os
import platform
import random
import string
import re
from typing import Optional
from urllib.parse import quote as urlquote

from flask import (
    Blueprint, Response, render_template, request, jsonify, current_app,
    session, redirect, url_for, flash
)

from db import get_connection
from datetime import datetime
from zoneinfo import ZoneInfo   # Python 3.9+

IST = ZoneInfo("Asia/Kolkata")

labels_bp = Blueprint("labels", __name__, url_prefix="/labels")
labels_bp.strict_slashes = False  # avoid trailing-slash 404s

# --- Optional Windows printing support ---------------------------------------
try:
    import win32print  # only available on Windows
    WIN32_AVAILABLE = True
except Exception:
    WIN32_AVAILABLE = False


# --- Time helpers -------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


# --- Text helpers -------------------------------------------------------------
def _to_crlf(s: str) -> str:
    """Normalize newlines to CRLF for TSPL printers."""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def safe_int(x) -> Optional[int]:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"none", "null", "nan"}:
        return None
    try:
        return int(float(s))  # handle "123.0" too
    except Exception:
        return None


# --- Schema bootstraps --------------------------------------------------------
def ensure_barcode_table():
    """Create barcode_catalog table (idempotent)."""
    '''with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.barcode_catalog (
                    id SERIAL PRIMARY KEY,
                    brand TEXT,
                    mrp INTEGER,
                    bought_price INTEGER,
                    selling_price INTEGER,
                    size TEXT,
                    category TEXT,
                    barcode_data TEXT UNIQUE,
                    status TEXT DEFAULT 'available',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
                """
            )
        conn.commit()
    '''    


def ensure_print_events_table():
    """Create barcode_print_events table (idempotent)."""
    '''with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.barcode_print_events (
                    id SERIAL PRIMARY KEY,
                    code TEXT NOT NULL,
                    copies INTEGER NOT NULL DEFAULT 1,
                    how TEXT,                            -- 'mobile' or 'pc'
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
                """
            )
        conn.commit()
    '''
import psycopg2  # add near top of file if not present

def insert_print_event(code: str, copies: int = 1, how: Optional[str] = None) -> int:
    """
    Upsert the exact provided `copies` for `code` (replace existing value).
    Returns stored copies.
    Tries atomic ON CONFLICT upsert first; falls back to serialized select/update/insert
    if Postgres rejects the ON CONFLICT (e.g., no unique constraint available).
    """
    if not code:
        raise ValueError("missing code")

    code_u = (code or "").strip().upper()
    try:
        copies_i = int(copies or 1)
    except Exception:
        copies_i = 1
    if copies_i < 1:
        copies_i = 1

    # Attempt atomic upsert
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.barcode_print_events (code, copies, how, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (code) DO UPDATE
                      SET copies = EXCLUDED.copies,
                          how = EXCLUDED.how,
                          created_at = EXCLUDED.created_at
                    RETURNING copies
                """, (code_u, copies_i, how, now_ist()))
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else copies_i

    except psycopg2.errors.InvalidColumnReference as icr:
        # This is the "there is no unique ... matching the ON CONFLICT" error (42P10).
        # Fall back to serialized update/insert approach.
        current_app.logger.warning("Upsert ON CONFLICT(code) not available; falling back: %s", icr)

    except Exception as e:
        # For any other error, log and re-raise (or optionally fall back).
        current_app.logger.exception("insert_print_event upsert failed (non-ONCONFLICT)")
        # Optionally choose to fall back here as well; we'll fall back below.
    
    # Fallback path: advisory lock + seq sync + select -> update/insert
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                ADVISORY_KEY = 987654321
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))

                # try to keep sequence sane (non-fatal)
                try:
                    cur.execute("SELECT COALESCE(MAX(id), 0) FROM public.barcode_print_events")
                    max_id = cur.fetchone()[0] or 0
                    cur.execute(
                        "SELECT setval(pg_get_serial_sequence('public.barcode_print_events','id'), %s)",
                        (max_id,)
                    )
                except Exception:
                    current_app.logger.debug("sequence sync failed in fallback", exc_info=True)

                # check existing row for code
                cur.execute("SELECT id FROM public.barcode_print_events WHERE code = %s LIMIT 1", (code_u,))
                row = cur.fetchone()
                if row:
                    row_id = row[0]
                    cur.execute("""
                        UPDATE public.barcode_print_events
                           SET copies = %s, how = %s, created_at = %s
                         WHERE id = %s
                    """, (copies_i, how, now_ist(), row_id))
                    stored = copies_i
                else:
                    cur.execute("""
                        INSERT INTO public.barcode_print_events (code, copies, how, created_at)
                        VALUES (%s, %s, %s, %s)
                        RETURNING copies
                    """, (code_u, copies_i, how, now_ist()))
                    rr = cur.fetchone()
                    stored = int(rr[0]) if rr and rr[0] is not None else copies_i
            conn.commit()
        return int(stored)
    except Exception:
        current_app.logger.exception("Fallback insert/update in insert_print_event failed")
        raise





# --- Catalog ops --------------------------------------------------------------
def find_existing_code(brand, size, category, mrp, bought, sp) -> Optional[str]:
    sql = """
    SELECT barcode_data FROM public.barcode_catalog
     WHERE brand=%s AND size=%s AND category=%s
       AND ((mrp = %s) OR (mrp IS NULL AND %s IS NULL))
       AND ((bought_price = %s) OR (bought_price IS NULL AND %s IS NULL))
       AND ((selling_price = %s) OR (selling_price IS NULL AND %s IS NULL))
     LIMIT 1
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (brand, size, category,
                              mrp, mrp,
                              bought, bought,
                              sp, sp))
            row = cur.fetchone()
            return row[0] if row else None


def insert_code(brand, size, category, mrp, bought, sp, code: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.barcode_catalog
                  (brand, mrp, bought_price, selling_price, size, category, barcode_data, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'available', %s)
                ON CONFLICT (barcode_data) DO NOTHING
                """,
                (brand, mrp, bought, sp, size, category, code, now_ist())
            )
        conn.commit()


# --- TSPL generation ----------------------------------------------------------
def tspl_for_item(code: str, brand: str, size: str,
                  mrp: Optional[float], sp: Optional[float],
                  copies: int = 1) -> str:
    # ---- Layout ----
    X_MRP_LABEL  = 40
    X_SP_TEXT    = 190           # "ONLY Rs:" label start
    Y_PRICE      = 155
    BAR_Y        = 165
    BAR_THICK    = 2
    DIGIT_W      = 12            # per-digit width for strike line (font "2" @1x)

    # --- Width helper for TSPL font "2" @1x (203 dpi). Tweak a value by ±1 if needed.
    FONT2_WIDTHS = {
        "0":12,"1":8,"2":12,"3":12,"4":12,"5":12,"6":12,"7":11,"8":12,"9":12,
        "A":12,"B":12,"C":12,"D":12,"E":12,"F":12,"G":12,"H":12,"I":8,"J":12,"K":12,"L":12,"M":12,"N":12,
        "O":12,"P":12,"Q":12,"R":12,"S":12,"T":12,"U":12,"V":12,"W":12,"X":12,"Y":12,"Z":12,
        "a":12,"b":12,"c":12,"d":12,"e":12,"f":8,"g":12,"h":12,"i":6,"j":8,"k":11,"l":6,"m":18,"n":12,
        "o":12,"p":12,"q":12,"r":9,"s":11,"t":8,"u":12,"v":11,"w":16,"x":11,"y":11,"z":11,
        " ":6, ":":6
    }

    def w2(text: str) -> int:
        return sum(FONT2_WIDTHS.get(ch, 12) for ch in text)

    lines = [
        "SIZE 50.8 mm, 25 mm",
        "GAP 2 mm, 0 mm",
        "DIRECTION 1",
        "CLS",
        "SETMAG 1,1",
        'TEXT 120,5,"3",0,1,1,"DRIP"',
        f'BARCODE 40,40,"128",50,1,0,2,2,"{code}"',
        f'TEXT 40,125,"2",0,1,1,"{brand}"',
        f'TEXT 190,125,"2",0,1,1,"Size: {size}"',
    ]

    # ---- MRP (font "2", 1x1) + strike only digits ----
    if mrp is not None:
        mrp_digits = str(int(mrp))
        # Print label and digits with tight spacing
        lines.append(f'TEXT {X_MRP_LABEL},{Y_PRICE},"2",0,1,1,"MRP:"')
        X_MRP_DIGITS = X_MRP_LABEL + w2("MRP:") + 1  # +1 pixel gap after colon
        lines.append(f'TEXT {X_MRP_DIGITS},{Y_PRICE},"2",0,1,1,"{mrp_digits}"')
        # Strike only the digits
        bar_w = max(1, len(mrp_digits) * DIGIT_W)
        lines.append(f'BAR {X_MRP_DIGITS},{BAR_Y},{bar_w},{BAR_THICK}')

    # ---- ONLY Rs: + big digits (tight) ----
    if sp is not None:
        sp_digits = str(int(sp))
        label = "ONLY:  "
        # print label (small)
        lines.append(f'TEXT {X_SP_TEXT},{Y_PRICE},"2",0,1,1,"{label}"')
        # compute tight start X for big digits
        X_SP_DIGITS = X_SP_TEXT + w2(label) + 2   # +2px breathing room
        SP_Y        = Y_PRICE - 6                 # slight vertical tweak for 2×2 digits
        lines.append(f'TEXT {X_SP_DIGITS},{SP_Y},"2",0,2,2,"{sp_digits}"')

    lines.append(f"PRINT {max(1, int(copies))},1")
    return "\n".join(lines)




def print_tspl_windows(tspl_text: str, printer_name: str) -> None:
    """Send raw TSPL once (it already contains PRINT {copies},1)."""
    if platform.system() != "Windows" or not WIN32_AVAILABLE:
        raise RuntimeError("PC printing only works on Windows with pywin32 installed.")
    hPrinter = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(hPrinter, 1, ("Barcode Label", None, "RAW"))
        win32print.StartPagePrinter(hPrinter)
        win32print.WritePrinter(hPrinter, tspl_text.encode("ascii"))
        win32print.EndPagePrinter(hPrinter)
        win32print.EndDocPrinter(hPrinter)
    finally:
        win32print.ClosePrinter(hPrinter)

@labels_bp.post("/print_event", endpoint="print_event")
def print_event():
    """
    Accepts JSON {code, copies, how} and inserts/updates barcode_print_events.
    Returns JSON {ok: True, printed_total: <int>} on success.
    """
    try:
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip().upper()
        if not code:
            return jsonify(ok=False, error="missing_code"), 400

        try:
            copies = int(data.get("copies") or 1)
            if copies < 1:
                copies = 1
        except Exception:
            copies = 1

        how = (data.get("how") or "").strip()

        # Basic validation for code format (same pattern used elsewhere)
        if not CODE_RE.match(code):
            return jsonify(ok=False, error="invalid_code"), 400

        # Use insert_print_event which should upsert and RETURN the stored copies
        try:
            stored = insert_print_event(code, copies, how)  # expected to return int
        except Exception:
            current_app.logger.exception("insert_print_event failed")
            return jsonify(ok=False, error="db_error"), 500

        # Ensure we always return an integer printed_total (fallback to copies)
        try:
            printed_total = int(stored) if stored is not None else int(copies)
        except Exception:
            printed_total = int(copies)

        return jsonify(ok=True, printed_total=printed_total), 200

    except Exception as e:
        current_app.logger.exception("print_event crashed")
        return jsonify(ok=False, error=str(e)), 500


# --- Routes: page -------------------------------------------------------------
@labels_bp.get("/", endpoint="page")
def labels_page():
    """Form + recent list + reuse (all on one page)."""
    # Ensure schemas exist so the page never crashes
    ensure_barcode_table()
    try:
        ensure_print_events_table()
    except Exception:
        # non-fatal
        current_app.logger.warning("print-events table ensure failed")

    # Prefill from query (optional)
    brand = (request.args.get("brand") or "").strip()
    mrp = request.args.get("mrp")
    bought = request.args.get("bought")
    sp = request.args.get("sp")
    size = (request.args.get("size") or "").strip()
    category = (request.args.get("category") or "").strip()
    copies = int(request.args.get("copies") or 1)

    # Recent rows
    rows = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT brand, mrp, bought_price, selling_price, size, category, barcode_data, created_at
                FROM public.barcode_catalog
                ORDER BY created_at DESC NULLS LAST, id DESC
                LIMIT 500
            """)
            rows = cur.fetchall()

    is_admin = bool(session.get("is_admin"))

    return render_template(
        "labels_form.html",
        brand_val=brand,
        mrp_val=mrp if mrp is not None else "",
        bought_val=bought if bought is not None else "",
        sp_val=sp if sp is not None else "",
        size_val=size,
        category_val=category,
        copies_val=copies,
        recent_rows=rows,
        is_admin=is_admin
    )


# --- Routes: TSPL file for Android app ---------------------------------------
@labels_bp.get("/tspl/<path:code>.txt", endpoint="tspl_for_code")
def tspl_for_code(code: str):
    """
    Serve TSPL for a code. Accepts ?copies= so Android can fetch with repetitions
    embedded (the app then sends the job once over BT).
    """
    try:
        copies = int((request.args.get("copies") or "1").strip() or "1")
    except Exception:
        copies = 1

    brand = size = None
    mrp = sp = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT brand,size,mrp,selling_price FROM public.barcode_catalog WHERE barcode_data=%s LIMIT 1",
                (code,)
            )
            row = cur.fetchone()
            if row:
                brand, size, mrp, sp = row

    if brand or size or mrp or sp:
        tspl = tspl_for_item(code, brand or "", size or "", mrp, sp, copies=copies)
    else:
        # fallback: minimal label
        tspl = f"""SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 120,10,"3",0,1,1,"DRIP"
BARCODE 40,40,"128",50,1,0,2,2,"{code}"
TEXT 40,100,"2",0,1,1,"Code: {code}"
PRINT {max(1, copies)},1
"""

    tspl = _to_crlf(tspl)
    return Response(tspl, mimetype="text/plain; charset=us-ascii")


# --- Routes: delete barcode (admin) ------------------------------------------
@labels_bp.post("/barcodes/<code>/delete", endpoint="barcodes_delete")
def barcodes_delete(code: str):
    """Hard delete a barcode row by its code (admin-only)."""
    if not session.get("is_admin"):
        flash("Admin only.", "error")
        return redirect(url_for("labels.page"))

    code = (code or "").strip().upper()
    if not code:
        flash("Invalid code.", "error")
        return redirect(url_for("labels.page"))

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.barcode_catalog WHERE barcode_data=%s", (code,))
            conn.commit()
        flash(f"Deleted barcode {code}.", "success")
    except Exception as e:
        current_app.logger.exception("barcodes_delete failed")
        flash(f"Delete failed: {e}", "error")

    return redirect(url_for("labels.page"))


# --- Routes: quick print API (mobile/pc) -------------------------------------
CODE_RE = re.compile(r"^[A-Z0-9_-]{1,40}$")

@labels_bp.post("/barcodes/<code>/exists", endpoint="barcodes_exists")  # kept endpoint compatibility
@labels_bp.get("/barcodes/<code>/exists", endpoint="barcodes_exists")   # allow GET too
def barcodes_exists(code: str):
    """Return {ok, exists} for a proposed barcode string (barcode_data)."""
    ensure_barcode_table()
    code = (code or "").upper()
    # Treat invalid code as "exists" so Save never enables for bad inputs
    if not CODE_RE.match(code):
        return jsonify(ok=True, exists=True)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.barcode_catalog WHERE barcode_data=%s LIMIT 1",
                (code,)
            )
            exists = cur.fetchone() is not None
    return jsonify(ok=True, exists=exists)


@labels_bp.post("/barcodes/create", endpoint="barcodes_create")
def barcodes_create():
    """Insert into barcode_catalog without printing; only if code is unique."""
    ensure_barcode_table()
    data = request.get_json(silent=True) or {}

    brand    = (data.get("brand") or "").strip()
    size     = (data.get("size") or "").strip()
    category = (data.get("category") or "").strip()
    code     = (data.get("code") or "").strip().upper()

    mrp    = safe_int(data.get("mrp"))
    bought = safe_int(data.get("bought"))
    sp     = safe_int(data.get("sp"))

    if not brand or not size or not category:
        return jsonify(ok=False, error="missing_fields"), 400
    if not CODE_RE.match(code):
        return jsonify(ok=False, error="invalid_code"), 400

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.barcode_catalog WHERE barcode_data=%s", (code,))
            if cur.fetchone():
                return jsonify(ok=False, error="code_exists"), 409

            cur.execute("""
                INSERT INTO public.barcode_catalog
                  (brand, mrp, bought_price, selling_price, size, category, barcode_data, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'available', %s)
            """, (brand, mrp, bought, sp, size, category, code, now_ist()))
        conn.commit()
    return jsonify(ok=True, code=code)


@labels_bp.post("/quick_print", endpoint="quick_print")
def quick_print():
    """
    Creates/looks up a barcode, returns deeplink for Android OR prints via Windows,
    and logs a print event so Inventory can count printed copies.
    """
    try:
        ensure_barcode_table()

        data = request.get_json(silent=True) or request.form or {}

        brand = (data.get("brand") or "").strip()
        size = (data.get("size") or "").strip()
        category = (data.get("category") or "").strip()

        mrp = safe_int(data.get("mrp"))
        bought = safe_int(data.get("bought"))
        sp = safe_int(data.get("sp"))

        try:
            copies = int((data.get("copies") or 1) or 1)
        except Exception:
            copies = 1

        action = (data.get("action") or "mobile").strip().lower()

        if not brand or not size or not category:
            return jsonify({"ok": False, "error": "Missing brand/size/category"}), 400

        # --- FIND or INSERT ---
        try:
            code = find_existing_code(brand, size, category, mrp, bought, sp)
        except Exception:
            current_app.logger.exception("find_existing_code failed")
            code = None

        if not code:
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            try:
                insert_code(brand, size, category, mrp, bought, sp, code)
            except Exception:
                current_app.logger.exception("insert_code failed (continuing)")

        # --- Build TSPL (with copies embedded) ---
        try:
            tspl = tspl_for_item(code, brand, size, mrp, sp, copies=copies)
        except Exception:
            current_app.logger.exception("tspl_for_item failed, using fallback")
            tspl = f"""SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 120,10,"3",0,1,1,"DRIP"
BARCODE 40,40,"128",50,1,0,2,2,"{code}"
TEXT 40,100,"2",0,1,1,"Code: {code}"
PRINT {max(1, copies)},1
"""
        tspl = _to_crlf(tspl)

        # --- Action: mobile vs pc ---
        if action == "mobile":
            # Log the intended/initiated print (server-side). This is the signal
            # used by Inventory to count "Printed" including copies/reuse.
            
            base = request.url_root.rstrip("/")
            deeplink = f"tspl://print?base={urlquote(base)}&code={urlquote(code)}&copies={copies}"
            return jsonify({"ok": True, "deeplink": deeplink, "code": code}), 200

        elif action == "pc":
            try:
                printer_name = current_app.config.get("DEFAULT_PRINTER_NAME", "4BARCODE 4B-2034PA")
                print_tspl_windows(tspl, printer_name)
                return jsonify({"ok": True, "code": code}), 200
            except Exception as e:
                current_app.logger.exception("print_tspl_windows failed")
                return jsonify({"ok": False, "error": str(e), "code": code}), 500

        else:
            return jsonify({"ok": False, "error": "Unknown action"}), 400

    except Exception as e:
        current_app.logger.exception("quick_print crashed")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500