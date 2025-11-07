# inventory.py

from flask import Blueprint, render_template, current_app
from db import get_connection

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

@inventory_bp.get("/")
def inventory_page():
    """
    Show Brand | Size | Category | Code | Printed | Sold | Available
    - Printed is total labels ever printed for that code (uses barcode_print_events if present,
      otherwise defaults to 1 per code).
    - Sold is total qty sold for that code (from sales_barcodes).
    - Available = Printed - Sold.
    """
    rows = []
    using_events = False

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Try the accurate path first (needs public.barcode_print_events)
            try:
                cur.execute("""
                    WITH printed_by_code AS (
                      SELECT code, SUM(copies)::int AS printed_qty
                      FROM public.barcode_print_events
                      GROUP BY code
                    ),
                    sold_by_code AS (
                      SELECT code, SUM(qty)::int AS sold_qty
                      FROM public.sales_barcodes
                      GROUP BY code
                    )
                    SELECT
                      bc.brand,
                      bc.size,
                      bc.category,
                      bc.barcode_data AS code,
                      -- if there is an event use its sum, otherwise default to 1
                      COALESCE(p.printed_qty, 1)::int AS printed_qty,
                      COALESCE(s.sold_qty, 0)::int AS sold_qty,
                      COALESCE(p.printed_qty, 1)::int - COALESCE(s.sold_qty, 0)::int AS available_qty
                    FROM public.barcode_catalog bc
                    LEFT JOIN printed_by_code p ON p.code = bc.barcode_data
                    LEFT JOIN sold_by_code    s ON s.code = bc.barcode_data
                    ORDER BY bc.brand DESC, bc.category DESC, bc.size DESC, bc.barcode_data DESC;
                """)
                rows = cur.fetchall()
                using_events = True

            except Exception:
                # Fallback: events table missing → printed = 1 per code
                current_app.logger.warning("inventory: events table missing; using fallback (1 label per code)")
                cur.execute("""
                    WITH sold_by_code AS (
                      SELECT code, SUM(qty)::int AS sold_qty
                      FROM public.sales_barcodes
                      GROUP BY code
                    )
                    SELECT
                      bc.brand,
                      bc.size,
                      bc.category,
                      bc.barcode_data AS code,
                      COUNT(bc.barcode_data)::int AS printed_qty,  -- this will be 1 per row, but kept for clarity
                      COALESCE(s.sold_qty, 0)::int AS sold_qty,
                      COUNT(bc.barcode_data)::int - COALESCE(s.sold_qty, 0)::int AS available_qty
                    FROM public.barcode_catalog bc
                    LEFT JOIN sold_by_code s ON s.code = bc.barcode_data
                    GROUP BY bc.brand, bc.size, bc.category, bc.barcode_data, s.sold_qty
                    ORDER BY bc.brand DESC, bc.category DESC, bc.size DESC, bc.barcode_data DESC;
                """)
                rows = cur.fetchall()
                using_events = False

    return render_template("inventory.html", rows=rows, using_events=using_events)

