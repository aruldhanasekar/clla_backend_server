# services/composio/connection_state_manager.py
"""
Connection State Manager - Track first-time connection vs reconnection
Prevents duplicate initial_sync on reconnection

PHASE 4B: Now supports two triggers (INBOX + SENT)
"""

from datetime import datetime, timezone
from firebase_admin import firestore
from typing import Dict, Optional


def _get_db():
    """Get Firestore client (lazy initialization)"""
    return firestore.client()


def get_connection_state(user_id: str) -> Dict:
    """
    Get user's Composio connection state from Firestore.
    
    Returns:
        {
            "is_first_time": bool,
            "first_connected_at": datetime,
            "composio_enabled": bool,
            "inbox_trigger_id": str,      # PHASE 4B: INBOX trigger
            "sent_trigger_id": str,       # PHASE 4B: SENT trigger (NEW)
            "entity_id": str,
            "last_sync_time": datetime
        }
    """
    try:
        db = _get_db()
        doc = db.collection("users").document(user_id).get()
        
        if not doc.exists:
            return {
                "is_first_time": True,
                "first_connected_at": None,
                "composio_enabled": False,
                "inbox_trigger_id": None,
                "sent_trigger_id": None,
                "entity_id": None,
                "last_sync_time": None
            }
        
        data = doc.to_dict()
        composio_conn = data.get("composio_connection", {})
        
        first_connected_at = composio_conn.get("first_connected_at")
        is_first_time = first_connected_at is None
        
        return {
            "is_first_time": is_first_time,
            "first_connected_at": first_connected_at,
            "composio_enabled": composio_conn.get("composio_enabled", False),
            "inbox_trigger_id": composio_conn.get("inbox_trigger_id"),      # PHASE 4B
            "sent_trigger_id": composio_conn.get("sent_trigger_id"),        # PHASE 4B: NEW
            "entity_id": composio_conn.get("entity_id"),
            "last_sync_time": composio_conn.get("last_sync_time")
        }
        
    except Exception as e:
        print(f"❌ Error getting connection state: {e}")
        return {
            "is_first_time": True,
            "first_connected_at": None,
            "composio_enabled": False,
            "inbox_trigger_id": None,
            "sent_trigger_id": None,
            "entity_id": None,
            "last_sync_time": None
        }


def mark_first_connection(
    user_id: str, 
    entity_id: str, 
    inbox_trigger_id: str,
    sent_trigger_id: str  # PHASE 4B: NEW parameter
):
    """
    Mark user's first-time connection to Composio.
    This should ONLY be called after initial_sync completes successfully.
    
    PHASE 4B: Now stores both INBOX and SENT trigger IDs
    
    Args:
        user_id: Firebase user ID
        entity_id: Composio entity ID (connected account)
        inbox_trigger_id: INBOX trigger ID (GMAIL_NEW_GMAIL_MESSAGE)
        sent_trigger_id: SENT trigger ID (GMAIL_EMAIL_SENT_TRIGGER)
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        now = firestore.SERVER_TIMESTAMP
        
        user_ref.set({
            "composio_connection": {
                "first_connected_at": now,
                "is_first_time": False,
                "composio_enabled": True,
                "inbox_trigger_id": inbox_trigger_id,      # PHASE 4B
                "sent_trigger_id": sent_trigger_id,        # PHASE 4B: NEW
                "entity_id": entity_id,
                "last_sync_time": now
            }
        }, merge=True)
        
        print(f"✅ Marked first connection for user: {user_id}")
        print(f"   INBOX trigger: {inbox_trigger_id}")
        print(f"   SENT trigger: {sent_trigger_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error marking first connection: {e}")
        return False


def mark_reconnection(
    user_id: str, 
    entity_id: str, 
    inbox_trigger_id: str,
    sent_trigger_id: str  # PHASE 4B: NEW parameter
):
    """
    Mark user's reconnection to Composio.
    This should be called when user reconnects (NOT first time).
    
    PHASE 4B: Now updates both trigger IDs
    
    IMPORTANT: This does NOT update first_connected_at or last_sync_time
    
    Args:
        user_id: Firebase user ID
        entity_id: Composio entity ID (connected account)
        inbox_trigger_id: New INBOX trigger ID
        sent_trigger_id: New SENT trigger ID
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        user_ref.update({
            "composio_connection.composio_enabled": True,
            "composio_connection.inbox_trigger_id": inbox_trigger_id,      # PHASE 4B
            "composio_connection.sent_trigger_id": sent_trigger_id,        # PHASE 4B: NEW
            "composio_connection.entity_id": entity_id
        })
        
        print(f"✅ Marked reconnection for user: {user_id}")
        print(f"   INBOX trigger: {inbox_trigger_id}")
        print(f"   SENT trigger: {sent_trigger_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error marking reconnection: {e}")
        return False


def mark_disconnection(user_id: str):
    """
    Mark user's disconnection from Composio.
    Preserves connection history for future reconnections.
    
    PHASE 4B: Clears both trigger IDs
    
    Args:
        user_id: Firebase user ID
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        user_ref.update({
            "composio_connection.composio_enabled": False,
            "composio_connection.inbox_trigger_id": None,      # PHASE 4B
            "composio_connection.sent_trigger_id": None        # PHASE 4B: NEW
        })
        
        print(f"✅ Marked disconnection for user: {user_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error marking disconnection: {e}")
        return False


def should_run_initial_sync(user_id: str) -> bool:
    """
    Determine if initial_sync should be run for this user.
    
    Returns:
        True if this is first-time connection (run initial_sync)
        False if this is reconnection (skip initial_sync)
    """
    state = get_connection_state(user_id)
    
    if state["is_first_time"]:
        print(f"🆕 First-time connection detected - will run initial_sync")
        return True
    else:
        print(f"🔄 Reconnection detected - will SKIP initial_sync")
        print(f"   First connected at: {state['first_connected_at']}")
        return False


# ======================================================
# MIGRATION HELPER (Optional - for existing users)
# ======================================================

def migrate_existing_user(user_id: str):
    """
    Migrate existing user to new connection state schema with dual triggers.
    Call this for users who connected before Phase 4B update.
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        doc = user_ref.get()
        
        if not doc.exists:
            print(f"⚠️ User {user_id} not found")
            return False
        
        data = doc.to_dict()
        
        # Check if already migrated to Phase 4B schema
        composio_conn = data.get("composio_connection", {})
        if "sent_trigger_id" in composio_conn:
            print(f"✅ User {user_id} already migrated to Phase 4B")
            return True
        
        # Get old trigger_id (now becomes inbox_trigger_id)
        old_trigger_id = composio_conn.get("trigger_id") or data.get("trigger_id")
        
        # Migrate to new schema
        now = firestore.SERVER_TIMESTAMP
        migration_data = {
            "composio_connection": {
                "first_connected_at": composio_conn.get("first_connected_at") or now,
                "is_first_time": False,
                "composio_enabled": composio_conn.get("composio_enabled", False),
                "inbox_trigger_id": old_trigger_id,  # Old trigger_id → inbox_trigger_id
                "sent_trigger_id": None,  # Will be set when they reconnect
                "entity_id": composio_conn.get("entity_id"),
                "last_sync_time": composio_conn.get("last_sync_time") or now
            }
        }
        
        user_ref.set(migration_data, merge=True)
        
        print(f"✅ Migrated user {user_id} to Phase 4B schema")
        print(f"   Old trigger_id → inbox_trigger_id: {old_trigger_id}")
        print(f"   sent_trigger_id will be created on next reconnection")
        return True
        
    except Exception as e:
        print(f"❌ Error migrating user: {e}")
        return False