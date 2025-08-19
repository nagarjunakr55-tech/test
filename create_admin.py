# create_admin.py
from auth_logic import ensure_user_schema, create_user

if __name__ == "__main__":
    ensure_user_schema()
    create_user("admin", "drip98", role="admin")
    create_user("user", "drip54", role="user")
    print("Admin created: admin / admin123")
