# db.py
import os
import psycopg2

# Use env var if set; otherwise fall back to your local DSN
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://drip:Invalid.55@127.0.0.1:5432/dripdb?sslmode=disable"
)

def get_connection():
    # Do NOT force sslmode here; let it come from the URL
    return psycopg2.connect(DATABASE_URL)
