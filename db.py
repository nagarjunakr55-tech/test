import psycopg2
import os

DATABASE_URL = "postgresql://postgres:Invalid.55@db.mgfopgggikhpsnotgdyu.supabase.co:6543/postgres"


def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")
