import sys
from datetime import datetime, timezone
from flowboard.db import get_session
from flowboard.db.models import UserSession

print("Checking UserSession DB model...")
try:
    with get_session() as session:
        test_uid = "test_diag_session_uid"
        sess = session.get(UserSession, test_uid)
        if sess:
            session.delete(sess)
            session.commit()
            
        sess = UserSession(
            firebase_uid=test_uid,
            active_session_id="test_sess_123",
            last_active_at=datetime.now(timezone.utc)
        )
        session.add(sess)
        session.commit()
        print("Successfully committed UserSession.")
        
        # Fetch it back
        sess = session.get(UserSession, test_uid)
        print("Fetched UserSession. active_session_id:", sess.active_session_id)
        
        # Clean up
        session.delete(sess)
        session.commit()
        print("Successfully cleaned up UserSession.")
except Exception as e:
    print("❌ ERROR in UserSession DB operation:")
    import traceback
    traceback.print_exc()
