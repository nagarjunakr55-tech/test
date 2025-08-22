# labels.py
from flask import Blueprint, Response, request, render_template
import logging
log = logging.getLogger("drip")

def _to_crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

labels_bp = Blueprint("labels", __name__, url_prefix="/labels")
labels_bp.strict_slashes = False  # avoid trailing-slash 404s

# --- /labels (landing page) ---
@labels_bp.get("/", endpoint="page")
def labels_page():
    base = request.host_url.rstrip("/")
    return render_template("labels_index.html", base=base)

# --- /labels/tspl/ping.txt ---
@labels_bp.get("/tspl/ping.txt", endpoint="tspl_ping")
def tspl_ping():
    tspl = """SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 10,10,"3",0,1,1,"PING"
PRINT 1,1
"""
    return Response(_to_crlf(tspl), mimetype="text/plain; charset=us-ascii")

# --- /labels/tspl/<code>.txt ---
@labels_bp.get("/tspl/<path:code>.txt", endpoint="tspl_for_code")
def tspl_for_code(code: str):
    log.info("TSPL request for code=%r", code)
    tspl = f"""SIZE 50.8 mm, 25 mm
GAP 2 mm, 0 mm
CLS
TEXT 120,10,"3",0,1,1,"DRIP"
BARCODE 40,40,"128",50,1,0,2,2,"{code}"
TEXT 40,100,"2",0,1,1,"Code: {code}"
PRINT 1,1
"""
    return Response(_to_crlf(tspl), mimetype="text/plain; charset=us-ascii")
