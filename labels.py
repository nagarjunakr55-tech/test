# labels.py
import os, random, string, io
from datetime import datetime
from flask import Blueprint, Response, request, render_template, url_for, current_app, abort
from PIL import Image
from barcode import Code128
from barcode.writer import ImageWriter

# Use your existing DB connection helper (Postgres)
from db import get_connection

def _to_crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

labels_bp = Blueprint("labels", __name__, url_prefix="/labels")
labels_bp.strict_slashes = False

# ---------- DB helpers ----------
def ensure_barcode_table():
    ddl = """
    CREATE TABLE IF NOT EXISTS barcode_catalog (
        id SERIAL PRIMARY KEY,
        brand TEXT,
        mrp NUMERIC,
        bought_price NUMERIC,
        selling_price NUMERIC,
        size TEXT,
        category TEXT,
        barcode_data TEXT UNIQUE,
        status TEXT DEFAULT 'available',
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()

def find_existing_code(brand, size, category, mrp, bought, sp):
    sql = """
    SELECT barcode_data
      FROM barcode_catalog
     WHERE brand=%s AND size=%s AND category=%s
       AND ((mrp IS NULL AND %s IS NULL) OR mrp = %s)
       AND ((bought_price IS NULL AND %s IS NULL) OR bought_price = %s)
       AND ((selling_price IS NULL AND %s IS NULL) OR selling_price = %s)
     ORDER BY id DESC
     LIMIT 1
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (brand, size, category, mrp, mrp, bought, bought, sp, sp))
            row = cur.fetchone()
            return row[0] if row else None

def insert_code(brand, size, category, mrp, bought, sp, code):
    sql = """
    INSERT INTO barcode_catalog
        (brand, mrp, bought_price, selling_price, size, category, barcode_data, status, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, 'available', %s)
    ON CONFLICT (barcode_data) DO NOTHING
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (brand, mrp, bought, sp, size, category, code, datetime.utcnow()))
        conn.commit()

def generate_unique_code():
    # 6 alnum, retry a few times
    for _ in range(10):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM barcode_catalog WHERE barcode_data=%s", (code,))
                if not cur.fetchone():
                    return code
    return code

# ---------- PNG generation ----------
def make_barcode_png(data: str) -> str:
    """
    Create a 180x60 PNG under static/barcodes/<CODE>_180x60.png, return relative path for url_for('static', filename=...).
    """
    os.makedirs(os.path.join(current_app.root_path, "static", "barcodes"), exist_ok=True)
    clean = data.upper().replace("|","").replace(" ","").replace(".","").replace(",","")
    # Save to a temp BytesIO first
    code128 = Code128(clean, writer=ImageWriter())
    out = io.BytesIO()
    code128.write(out, options={
        "module_width": 0.28,
        "module_height": 18.0,
        "quiet_zone": 2.0,
        "font_size": 10,
        "text_distance": 1.0,
        "write_text": True
    })
    out.seek(0)
    img = Image.open(out).convert("RGB")
    try:
        resized = img.resize((180, 60), Image.Resampling.LANCZOS)
    except Exception:
        resized = img.resize((180, 60), Image.LANCZOS)

    rel = f"barcodes/{clean}_180x60.png"
    abs_path = os.path.join(current_app.root_path, "static", rel)
    resized.save(abs_path)
    return rel

# ---------- TSPL builder ----------
def tspl_for_item(code: str, brand: str, size: str, mrp, sp) -> str:
    label_width_mm = 50.8
    dpi = 203
    label_width_dots = int(label_width_mm * dpi / 25.4)  # ~406

    def center_x(chars, font_w):
        return (label_width_dots - (chars * font_w)) // 2

    drip_x = center_x(len("DRIP"), 24)  # font "3" approx 24-dot width

    lines = []
    if mrp is not None:
        try: lines.append(f'TEXT 40,155,"2",0,1,1,"MRP: {int(float(mrp))}"')
        except: pass
    if sp is not None:
        try: lines.append(f'TEXT 210,155,"2",0,1,1,"SP: {int(float(sp))}"')
        except: pass

    tspl = f"""
SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
DIRECTION 1
CLS

TEXT {drip_x},5,"3",0,1,1,"DRIP"
BARCODE 40,40,"128",50,1,0,2,2,"{code}"

TEXT 40,125,"2",0,1,1,"{brand}"
TEXT 210,125,"2",0,1,1,"Size: {size}"
{os.linesep.join(lines)}

PRINT 1,1
"""
    return _to_crlf(tspl)

# ---------- Web pages ----------
@labels_bp.get("/", endpoint="page")
def labels_page():
    ensure_barcode_table()
    return render_template("labels_form.html")

@labels_bp.post("/create", endpoint="create")
def labels_create():
    ensure_barcode_table()

    brand = (request.form.get("brand") or "").strip()
    size = (request.form.get("size") or "").strip()
    category = (request.form.get("category") or "").strip()
    mrp = request.form.get("mrp") or None
    bought = request.form.get("bought") or None
    sp = request.form.get("sp") or None
    copies = int(request.form.get("copies") or 1)

    def safe_float(x):
        if x is None or str(x).strip() == "": return None
        try: return float(x)
        except: return None

    mrp = safe_float(mrp)
    bought = safe_float(bought)
    sp = safe_float(sp)

    if not brand or not size or not category:
        abort(400, "brand, size, category required")

    # Reuse existing code if the exact combo exists
    code = find_existing_code(brand, size, category, mrp, bought, sp)
    if not code:
        code = generate_unique_code()
        insert_code(brand, size, category, mrp, bought, sp, code)

    # PNG preview
    rel_img = make_barcode_png(code)

    # TSPL
    tspl = tspl_for_item(code, brand, size, mrp, sp)

    # Deep link (single line!)
    base = request.host_url.rstrip("/")
    deeplink = f"tspl://print?base={base}&code={code}&copies={copies}"

    return render_template(
        "labels_created.html",
        code=code, brand=brand, size=size, category=category,
        mrp=mrp, sp=sp, copies=copies,
        rel_img=rel_img, tspl=tspl, deeplink=deeplink
    )

# ---------- TSPL raw endpoints (already used by Android) ----------
@labels_bp.get("/tspl/ping.txt", endpoint="tspl_ping")
def tspl_ping():
    tspl = """SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 10,10,"3",0,1,1,"PING"
PRINT 1,1
"""
    return Response(_to_crlf(tspl), mimetype="text/plain; charset=us-ascii")

@labels_bp.get("/tspl/<path:code>.txt", endpoint="tspl_for_code")
def tspl_for_code(code: str):
    # For safety, pull minimal item info if present; otherwise print the code only
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT brand,size,mrp,selling_price FROM barcode_catalog WHERE barcode_data=%s",
                (code,)
            )
            row = cur.fetchone()

    if row:
        brand, size, mrp, sp = row
        tspl = tspl_for_item(code, brand or "", size or "", mrp, sp)
    else:
        tspl = f"""SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 120,10,"3",0,1,1,"DRIP"
BARCODE 40,40,"128",50,1,0,2,2,"{code}"
TEXT 40,100,"2",0,1,1,"Code: {code}"
PRINT {copies},1
"""
        tspl = _to_crlf(tspl)

    return Response(tspl, mimetype="text/plain; charset=us-ascii")
