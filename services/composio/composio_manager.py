# services/composio/connection_state_manager.py
"""
Connection State Manager - Track first-time connection vs reconnection
Prevents duplicate initial_sync on reconnection
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
            "is_first_time": bool,           # True if never connected before
            "first_connected_at": datetime,   # When user first connected (None if never)
            "composio_enabled": bool,         # Current connection status
            "trigger_id": str,                # Current trigger ID (None if disconnected)
            "entity_id": str,                 # Composio entity ID
            "last_sync_time": datetime        # Last email sync timestamp
        }
    """
    try:
        db = _get_db()
        doc = db.collection("users").document(user_id).get()
        
        if not doc.exists:
            # User document doesn't exist - definitely first time
            return {
                "is_first_time": True,
                "first_connected_at": None,
                "composio_enabled": False,
                "trigger_id": None,
                "entity_id": None,
                "last_sync_time": None
            }
        
        data = doc.to_dict()
        composio_conn = data.get("composio_connection", {})
        
        # If composio_connection doesn't exist or first_connected_at is None -> first time
        first_connected_at = composio_conn.get("first_connected_at")
        is_first_time = first_connected_at is None
        
        return {
            "is_first_time": is_first_time,
            "first_connected_at": first_connected_at,
            "composio_enabled": composio_conn.get("composio_enabled", False),
            "trigger_id": composio_conn.get("trigger_id"),
            "entity_id": composio_conn.get("entity_id"),
            "last_sync_time": composio_conn.get("last_sync_time")
        }
        
    except Exception as e:
        print(f"❌ Error getting connection state: {e}")
        # On error, treat as first time to be safe
        return {
            "is_first_time": True,
            "first_connected_at": None,
            "composio_enabled": False,
            "trigger_id": None,
            "entity_id": None,
            "last_sync_time": None
        }


def mark_first_connection(user_id: str, entity_id: str, trigger_id: str):
    """
    Mark user's first-time connection to Composio.
    This should ONLY be called after initial_sync completes successfully.
    
    Args:
        user_id: Firebase user ID
        entity_id: Composio entity ID (connected account)
        trigger_id: Composio trigger ID
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        now = firestore.SERVER_TIMESTAMP
        
        # Update with nested composio_connection object
        user_ref.set({
            "composio_connection": {
                "first_connected_at": now,
                "is_first_time": False,  # Set to False after first connection
                "composio_enabled": True,
                "trigger_id": trigger_id,
                "entity_id": entity_id,
                "last_sync_time": now
            }
        }, merge=True)
        
        print(f"✅ Marked first connection for user: {user_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error marking first connection: {e}")
        return False


def mark_reconnection(user_id: str, entity_id: str, trigger_id: str):
    """
    Mark user's reconnection to Composio.
    This should be called when user reconnects (NOT first time).
    
    IMPORTANT: This does NOT update first_connected_at or last_sync_time
    (those remain from the original connection)
    
    Args:
        user_id: Firebase user ID
        entity_id: Composio entity ID (connected account)
        trigger_id: New trigger ID
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        # Only update these specific fields - preserve history
        user_ref.update({
            "composio_connection.composio_enabled": True,
            "composio_connection.trigger_id": trigger_id,
            "composio_connection.entity_id": entity_id
            # ✅ DO NOT update: first_connected_at, is_first_time, last_sync_time
        })
        
        print(f"✅ Marked reconnection for user: {user_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error marking reconnection: {e}")
        return False


def mark_disconnection(user_id: str):
    """
    Mark user's disconnection from Composio.
    Preserves connection history for future reconnections.
    
    Args:
        user_id: Firebase user ID
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        
        # Only update status fields - preserve history
        user_ref.update({
            "composio_connection.composio_enabled": False,
            "composio_connection.trigger_id": None
            # ✅ DO NOT delete: first_connected_at, last_sync_time
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
    Migrate existing user to new connection state schema.
    Call this for users who connected before this update.
    
    This sets their current state as "first_connected_at" to prevent
    them from running initial_sync again on next reconnection.
    """
    try:
        db = _get_db()
        user_ref = db.collection("users").document(user_id)
        doc = user_ref.get()
        
        if not doc.exists:
            print(f"⚠️ User {user_id} not found")
            return False
        
        data = doc.to_dict()
        
        # Check if already migrated
        if "composio_connection" in data and data["composio_connection"].get("first_connected_at"):
            print(f"✅ User {user_id} already migrated")
            return True
        
        # Get existing data
        trigger_id = data.get("trigger_id")
        gmail_connection_id = data.get("gmail_connection_id")
        initial_sync_completed_at = data.get("initial_sync_completed_at")
        
        # Create new connection state
        now = firestore.SERVER_TIMESTAMP
        migration_time = initial_sync_completed_at or now
        
        user_ref.set({
            "composio_connection": {
                "first_connected_at": migration_time,
                "is_first_time": False,
                "composio_enabled": True if trigger_id else False,
                "trigger_id": trigger_id,
                "entity_id": gmail_connection_id,
                "last_sync_time": migration_time
            }
        }, merge=True)
        
        print(f"✅ Migrated user {user_id} to new schema")
        return True
        
    except Exception as e:
        print(f"❌ Error migrating user: {e}")
        return False