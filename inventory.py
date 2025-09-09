# inventory.py

from flask import Blueprint, render_template, current_app
from db import get_connection

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

@inventory_bp.get("/")
def inventory_page():
    """
    Show Brand | Size | Category | Printed | Sold | Available
    - Printed is total labels ever printed (does NOT change when sold).
    - Sold is total qty sold.
    - Available = Printed - Sold (can be negative only if you sold more than you printed).
    - Uses barcode_print_events if available (accurate copies). Otherwise, assumes 1 per code.
    """
    rows = []
    using_events = False

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Try the accurate path first (needs public.barcode_print_events)
            try:
                # printed_by_code: total copies per code from events
                # sold_by_code:    total qty per code from sales_barcodes
                # Then roll both up *separately* to (brand,size,category),
                # and only then join — this avoids row multiplication.
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
                    ),

                    printed_by_group AS (
                        SELECT bc.brand, bc.size, bc.category,
                               COALESCE(SUM(p.printed_qty), 0)::int AS printed_qty
                        FROM public.barcode_catalog bc
                        LEFT JOIN printed_by_code p
                               ON p.code = bc.barcode_data
                        GROUP BY bc.brand, bc.size, bc.category
                    ),

                    sold_by_group AS (
                        SELECT bc.brand, bc.size, bc.category,
                               COALESCE(SUM(s.sold_qty), 0)::int AS sold_qty
                        FROM public.barcode_catalog bc
                        LEFT JOIN sold_by_code s
                               ON s.code = bc.barcode_data
                        GROUP BY bc.brand, bc.size, bc.category
                    )

                    SELECT g.brand,
                           g.size,
                           g.category,
                           COALESCE(pb.printed_qty, 0) AS printed_qty,
                           COALESCE(sb.sold_qty,    0) AS sold_qty,
                           COALESCE(pb.printed_qty, 0) - COALESCE(sb.sold_qty, 0) AS available_qty
                    FROM (
                        SELECT DISTINCT brand, size, category
                        FROM public.barcode_catalog
                    ) g
                    LEFT JOIN printed_by_group pb
                           ON pb.brand = g.brand AND pb.size = g.size AND pb.category = g.category
                    LEFT JOIN sold_by_group sb
                           ON sb.brand = g.brand AND sb.size = g.size AND sb.category = g.category
                    ORDER BY g.brand, g.category, g.size
                """)
                rows = cur.fetchall()
                using_events = True

            except Exception:
                # Fallback: if events table doesn't exist → printed = count of codes (1 per code)
                current_app.logger.warning("inventory: events table missing; using fallback (1 label per code)")
                cur.execute("""
                    WITH sold_by_code AS (
                        SELECT code, SUM(qty)::int AS sold_qty
                        FROM public.sales_barcodes
                        GROUP BY code
                    ),
                    printed_by_group AS (
                        -- 1 label per code
                        SELECT bc.brand, bc.size, bc.category,
                               COUNT(bc.barcode_data)::int AS printed_qty
                        FROM public.barcode_catalog bc
                        GROUP BY bc.brand, bc.size, bc.category
                    ),
                    sold_by_group AS (
                        SELECT bc.brand, bc.size, bc.category,
                               COALESCE(SUM(s.sold_qty), 0)::int AS sold_qty
                        FROM public.barcode_catalog bc
                        LEFT JOIN sold_by_code s
                               ON s.code = bc.barcode_data
                        GROUP BY bc.brand, bc.size, bc.category
                    )
                    SELECT g.brand,
                           g.size,
                           g.category,
                           COALESCE(pb.printed_qty, 0) AS printed_qty,
                           COALESCE(sb.sold_qty,    0) AS sold_qty,
                           COALESCE(pb.printed_qty, 0) - COALESCE(sb.sold_qty, 0) AS available_qty
                    FROM (
                        SELECT DISTINCT brand, size, category
                        FROM public.barcode_catalog
                    ) g
                    LEFT JOIN printed_by_group pb
                           ON pb.brand = g.brand AND pb.size = g.size AND pb.category = g.category
                    LEFT JOIN sold_by_group sb
                           ON sb.brand = g.brand AND sb.size = g.size AND sb.category = g.category
                    ORDER BY g.brand, g.category, g.size
                """)
                rows = cur.fetchall()
                using_events = False

    return render_template("inventory.html", rows=rows, using_events=using_events)
