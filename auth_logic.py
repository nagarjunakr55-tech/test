# auth_logic.py
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_connection

def ensure_user_schema():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                  id SERIAL PRIMARY KEY,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'user',
                  created_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        conn.commit()

def create_user(username: str, password: str, role: str = "user"):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("username and password required")
    ph = generate_password_hash(password, method="pbkdf2:sha256")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users(username, password_hash, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role
            """, (username, ph, role))
        conn.commit()

def verify_user(username: str, password: str):
    username = (username or "").strip()
    if not username or not password:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, password_hash, role FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    if not row:
        return None
    uid, uname, pwhash, role = row
    if check_password_hash(pwhash, password):
        return {"id": uid, "username": uname, "role": role}
    return None
