import sys
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import UserAccount

print("Checking UserAccount DB model...")
try:
    with get_session() as session:
        # Try inserting a temporary user
        test_uid = "test_diag_uid"
        user = session.get(UserAccount, test_uid)
        if not user:
            user = UserAccount(
                firebase_uid=test_uid,
                email="diag@domain.com",
                is_approved=False,
                is_admin=False
            )
            session.add(user)
            session.commit()
            print("Successfully inserted diagnostic user.")
            session.refresh(user)
        else:
            print("Diagnostic user already exists:", user.email)
            
        # Try fetching
        users = session.exec(select(UserAccount)).all()
        print(f"Fetch successful. Found {len(users)} users in database:")
        for u in users:
            print(f" - {u.email} (Approved: {u.is_approved}, Admin: {u.is_admin})")
            
        # Clean up
        if user:
            session.delete(user)
            session.commit()
            print("Cleaned up diagnostic user successfully.")
except Exception as e:
    print("❌ ERROR in database operation:")
    import traceback
    traceback.print_exc()
