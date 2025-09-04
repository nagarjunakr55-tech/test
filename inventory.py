# inventory.py
from flask import Blueprint, render_template, current_app
from db import get_connection

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")


def _ensure_sales_barcodes_table():
    """Make sure sales_barcodes exists so joins never blow up."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.sales_barcodes (
                    id SERIAL PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    qty INTEGER NOT NULL DEFAULT 1
                )
            """)
        conn.commit()


def _has_print_events_table(cur) -> bool:
    """
    Returns True if barcode_print_events exists (schema: code TEXT, copies INT, created_at TIMESTAMP).
    We keep it simple: try a harmless query.
    """
    try:
        cur.execute("SELECT 1 FROM public.barcode_print_events LIMIT 1")
        _ = cur.fetchone()  # ignore result
        return True
    except Exception:
        return False


@inventory_bp.get("/")
def inventory_page():
    """
    Shows:
      Brand | Size | Category | Printed | Sold | Available
    - If barcode_print_events exists → uses SUM(copies) for accurate printed count (includes re-prints).
    - Else → falls back to 1-per-barcode in catalog (basic).
    """
    rows = []
    using_events = False

    # Ensure dependent tables exist
    _ensure_sales_barcodes_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                using_events = _has_print_events_table(cur)

                if using_events:
                    # Accurate counts from print events (includes copies & reprints)
                    cur.execute("""
                        WITH printed AS (
                            SELECT code, SUM(copies)::int AS printed_qty
                            FROM public.barcode_print_events
                            GROUP BY code
                        ),
                        sold AS (
                            SELECT code, SUM(qty)::int AS sold_qty
                            FROM public.sales_barcodes
                            GROUP BY code
                        )
                        SELECT 
                            bc.brand,
                            bc.size,
                            bc.category,
                            COALESCE(SUM(p.printed_qty), 0)                         AS printed_qty,
                            COALESCE(SUM(s.sold_qty), 0)                            AS sold_qty,
                            COALESCE(SUM(p.printed_qty), 0) - COALESCE(SUM(s.sold_qty), 0) AS available_qty
                        FROM public.barcode_catalog bc
                        LEFT JOIN printed p ON p.code = bc.barcode_data
                        LEFT JOIN sold    s ON s.code = bc.barcode_data
                        GROUP BY bc.brand, bc.size, bc.category
                        ORDER BY bc.brand, bc.category, bc.size
                    """)
                else:
                    # Fallback: assume each catalog row == 1 printed
                    current_app.logger.warning(
                        "inventory: barcode_print_events not found; using fallback qty=1 per barcode."
                    )
                    cur.execute("""
                        WITH sold AS (
                            SELECT code, SUM(qty)::int AS sold_qty
                            FROM public.sales_barcodes
                            GROUP BY code
                        )
                        SELECT 
                            bc.brand,
                            bc.size,
                            bc.category,
                            COUNT(bc.barcode_data)::int                               AS printed_qty,
                            COALESCE(SUM(s.sold_qty), 0)                              AS sold_qty,
                            COUNT(bc.barcode_data)::int - COALESCE(SUM(s.sold_qty), 0) AS available_qty
                        FROM public.barcode_catalog bc
                        LEFT JOIN sold s ON s.code = bc.barcode_data
                        GROUP BY bc.brand, bc.size, bc.category
                        ORDER BY bc.brand, bc.category, bc.size
                    """)
                rows = cur.fetchall()
            except Exception as e:
                current_app.logger.exception("inventory_page failed")
                rows = []

    return render_template("inventory.html", rows=rows, using_events=using_events)
