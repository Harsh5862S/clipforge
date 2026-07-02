"""
One-off helper script to create the admin account or reset its password
directly in the database, bypassing the email/OTP signup flow.

Make sure your PostgreSQL connection settings in db.py (or environment
variables) are correct, then run:
    python set_admin_password.py
"""

import db

ADMIN_EMAIL = input("Admin email: ").strip()
ADMIN_PASSWORD = "Harsh_admin_5911"

db.init_db()

existing = db.get_user_by_email(ADMIN_EMAIL)

if existing is None:
    user_id = db.create_user(ADMIN_EMAIL, ADMIN_PASSWORD)
    db.mark_verified(ADMIN_EMAIL)
    print(f"\n✅ Created new account for {ADMIN_EMAIL} with password: {ADMIN_PASSWORD}")
else:
    db.set_password(ADMIN_EMAIL, ADMIN_PASSWORD)
    db.mark_verified(ADMIN_EMAIL)
    print(f"\n✅ Password reset for {ADMIN_EMAIL} to: {ADMIN_PASSWORD}")

user = db.get_user_by_email(ADMIN_EMAIL)
if user["is_admin"]:
    print("   This account has admin privileges.")
else:
    print("   ⚠️  This account is NOT an admin (it wasn't the first account created).")
    print("   To make it admin, run this SQL manually:")
    print(f"   UPDATE users SET is_admin = TRUE WHERE email = '{ADMIN_EMAIL}';")

print("\nYou can now log in with this email + password (still requires OTP verification).")
