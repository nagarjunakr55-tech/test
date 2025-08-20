# labels.py
import os, sqlite3, random, string
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, send_file, abort, flash, jsonify, make_response
from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "shop.db")
BARCODE_DIR = os.path.join(BASE_DIR, "static", "barcodes")
os.makedirs(BARCODE_DIR, exist_ok=True)

labels_bp = Blueprint("labels", __name__, template_folder="templates")

def ensure_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS barcode_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand TEXT,
            mrp REAL,
            bought_price REAL,
            selling_price REAL,
            size TEXT,
            category TEXT,
            barcode_data TEXT UNIQUE,
            status TEXT DEFAULT 'available',
            created_at TEXT
        )
    """)
    conn.commit(); conn.close()

def safe_float(value):
    if value is None: return None
    s = str(value).strip()
    if s.lower() in ("", "none", "null", "nan"): return None
    try:
        return float(s)
    except ValueError:
        return None

def generate_unique_code():
    for _ in range(20):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
        cur.execute("SELECT 1 FROM barcode_catalog WHERE barcode_data = ?", (code,))
        ok = cur.fetchone() is None
        conn.close()
        if ok: return code
    return code

def generate_barcode_png(data, filename_prefix):
    """
    Create Code128 PNG (180x60) and return relative static path to serve.
    """
    clean = str(data).upper().replace("|","").replace(" ","").replace(".","").replace(",","")
    base_path = os.path.join(BARCODE_DIR, filename_prefix)
    # create raw (large) barcode
    raw_path = Code128(clean, writer=ImageWriter()).save(base_path, {
        "module_width": 0.28,
        "module_height": 18.0,
        "quiet_zone": 2.0,
        "font_size": 10,
        "text_distance": 1.0,
        "write_text": True
    })
    # resize to 180x60 for compact labels
    final_rel = f"barcodes/{filename_prefix}_180x60.png"
    final_abs = os.path.join(BASE_DIR, "static", final_rel)
    img = Image.open(raw_path).convert("RGB")
    try:
        resized = img.resize((180, 60), Image.Resampling.LANCZOS)
    except Exception:
        resized = img.resize((180, 60), Image.LANCZOS)
    resized.save(final_abs)
    try:
        os.remove(raw_path)
    except Exception:
        pass
    return final_rel

def build_tspl(unique_code, brand, size, mrp, sp):
    label_width_mm = 50.8
    dpi = 203
    label_width_dots = int(label_width_mm * dpi / 25.4)  # ~406 dots

    def center_x(text_len, font_w):
        return (label_width_dots - (text_len * font_w)) // 2

    drip_x = center_x(len("DRIP"), 24)
    lines = []
    if mrp is not None:
        try: lines.append(f'TEXT 40,155,"2",0,1,1,"MRP: {int(mrp)}"')
        except: pass
    if sp is not None:
        try: lines.append(f'TEXT 210,155,"2",0,1,1,"SP: {int(sp)}"')
        except: pass

    tspl = f"""
SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
DIRECTION 1
CLS

TEXT {drip_x},5,"3",0,1,1,"DRIP"

BARCODE 40,40,"128",50,1,0,2,2,"{unique_code}"

TEXT 40,125,"2",0,1,1,"{brand}"
TEXT 210,125,"2",0,1,1,"Size: {size}"
{os.linesep.join(lines)}

PRINT 1,1
"""
    # strip leading spaces on each line for cleaner view
    return "\n".join(line.strip() for line in tspl.strip().splitlines()) + "\n"

@labels_bp.route("/labels", methods=["GET"])
def page():
    ensure_table()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, mrp, bought_price, selling_price, size, category, barcode_data, created_at
        FROM barcode_catalog ORDER BY id DESC LIMIT 50
    """)
    rows = cur.fetchall()
    conn.close()
    return render_template("labels.html", rows=rows)

@labels_bp.route("/labels/create", methods=["POST"])
def create():
    ensure_table()
    brand = (request.form.get("brand") or "").strip()
    size = (request.form.get("size") or "").strip()
    category = (request.form.get("category") or "").strip() or "Shirt"
    mrp = safe_float(request.form.get("mrp"))
    bought = safe_float(request.form.get("bought_price"))
    sp = safe_float(request.form.get("selling_price"))
    copies = int(request.form.get("copies") or 1)

    if not brand or not size:
        flash("Please fill Brand and Size", "warning")
        return redirect(url_for("labels.page"))

    # reuse if exact combo exists
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("""
        SELECT barcode_data FROM barcode_catalog
        WHERE brand=? AND size=? AND category=?
          AND (mrp IS ? OR mrp = ?)
          AND (bought_price IS ? OR bought_price = ?)
          AND (selling_price IS ? OR selling_price = ?)
        LIMIT 1
    """, (brand, size, category, mrp, mrp, bought, bought, sp, sp))
    row = cur.fetchone()
    if row:
        code = row[0]
    else:
        code = generate_unique_code()
        cur.execute("""
            INSERT INTO barcode_catalog
            (brand, mrp, bought_price, selling_price, size, category, barcode_data, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (brand, mrp, bought, sp, size, category, code, "available", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    conn.close()

    # generate image (for preview/regular printer)
    rel_img = generate_barcode_png(code, f"{code}_{brand}")

    # Prepare a TSPL file content for thermal printer users to download
    tspl = build_tspl(code, brand, size, mrp, sp)

    # Store last action in session? For now, render with data
    return render_template("labels_done.html",
                           code=code, brand=brand, size=size, mrp=mrp, sp=sp,
                           copies=copies, rel_img=rel_img, tspl=tspl)

@labels_bp.route("/labels/tspl/<code>.txt")
def tspl_download(code):
    # Look up details for code
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("""
        SELECT brand, size, mrp, selling_price
        FROM barcode_catalog WHERE barcode_data = ? LIMIT 1
    """, (code,))
    row = cur.fetchone(); conn.close()
    if not row: abort(404)
    brand, size, mrp, sp = row
    tspl = build_tspl(code, brand, size, mrp, sp)
    resp = make_response(tspl)
    resp.headers["Content-Type"] = "text/plain"
    resp.headers["Content-Disposition"] = f'attachment; filename="{code}.tspl.txt"'
    return resp

@labels_bp.route("/labels/print/<code>")
def print_png_page(code):
    # Find the PNG we generated for this code
    # (filename pattern: <code>_<brand>_180x60.png)
    # We'll fetch brand to construct path.
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("""
        SELECT brand FROM barcode_catalog WHERE barcode_data = ? LIMIT 1
    """, (code,))
    row = cur.fetchone(); conn.close()
    if not row:
        abort(404)
    brand = row[0]
    rel_img = f"barcodes/{code}_{brand}_180x60.png"
    return render_template("labels_print.html", rel_img=rel_img, code=code)

