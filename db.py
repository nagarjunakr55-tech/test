import psycopg2
import os

#DATABASE_URL ="postgresql://postgres.pvhadjtnenzidnkarvxg:Invalid.55@aws-1-us-east-2.pooler.supabase.com:6543/postgres"
DATABASE_URL ="postgresql://postgres:Invalid.55@db.pvhadjtnenzidnkarvxg.supabase.co:5432/postgres"
def get_connection():

    return psycopg2.connect(DATABASE_URL, sslmode="require")
