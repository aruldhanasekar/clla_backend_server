from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import firebase_admin
from firebase_admin import auth, initialize_app, credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import os
import requests
import base64
import json

from composio import Composio
from tools.gmail.initial_sync import run_initial_sync
from tools.gmail.process_new_email import process_new_email
from routes.chat_routes import router as chat_router
from routes.commitment_routes import router as commitment_router
from routes.credit_routes import router as credit_router

# ✅ Connection state manager imports
from services.composio.connection_state_manager import (
    get_connection_state,
    should_run_initial_sync,
    mark_first_connection,
    mark_reconnection,
    mark_disconnection
)

load_dotenv()

if not firebase_admin._apps:
    service_account_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64")
    
    if service_account_b64:
        service_account_json = base64.b64decode(service_account_b64).decode()
        service_account_dict = json.loads(service_account_json)
        cred = credentials.Certificate(service_account_dict)
    else:
        SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT", "serviceAccountKey.json")
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    
    firebase_admin.initialize_app(cred)

db = firestore.client()

GMAIL_AUTH_CONFIG = os.getenv("AUTH_CONFIG_ID")
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")

# ======================================================
# FRONTEND URL CONFIGURATION
# ======================================================
FRONTEND_URL = os.getenv("FRONTEND_URL")
ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "http://localhost:5173",
    "http://localhost:3000",
    "https://www.useclla.com/"
]

app = FastAPI()
app.include_router(chat_router, prefix="/api/chat", tags=["Chat"])
app.include_router(commitment_router, prefix="/api/commitments", tags=["Commitments"])
app.include_router(credit_router, prefix="/api/credits", tags=["Credits"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================================================
# HELPER FUNCTION: GET CALLBACK URL
# ======================================================
def get_callback_url(request: Request) -> str:
    """Get callback URL with security validation."""
    if FRONTEND_URL:
        callback = f"{FRONTEND_URL}/chat?gmail_connected=true"
        print(f"🔗 Using FRONTEND_URL from .env: {callback}")
        return callback
    
    origin = request.headers.get("origin")
    
    if not origin:
        referer = request.headers.get("referer", "")
        if referer:
            try:
                parsed = urlparse(referer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
            except Exception as e:
                print(f"⚠️ Failed to parse referer: {e}")
    
    if origin and origin in ALLOWED_ORIGINS:
        callback = f"{origin}/chat?gmail_connected=true"
        return callback
    
    if origin and origin.startswith("https://") and origin.endswith(".lovable.app"):
        callback = f"{origin}/chat?gmail_connected=true"
        return callback
    
    if origin and origin.startswith("https://"):
        if ".ngrok.io" in origin or ".ngrok-free.app" in origin or ".ngrok.app" in origin:
            callback = f"{origin}/chat?gmail_connected=true"
            return callback
    
    return "https://www.useclla.com/chat?gmail_connected=true"


# ======================================================
# TOKEN VERIFICATION
# ======================================================
def verify_token(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")

    if auth_header.startswith("Bearer INTERNAL_CALL_"):
        internal_uid = auth_header.replace("Bearer INTERNAL_CALL_", "")
        return {"uid": internal_uid}

    token = auth_header.replace("Bearer ", "")
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except Exception as e:
        print(f"Token verification error: {e}")
        raise HTTPException(status_code=401, detail="Invalid Firebase Id token")


# ======================================================
# ✅ FIXED: HELPER TO CHECK IF TRIGGER EXISTS
# ======================================================
def check_triggers_exist(composio: Composio, user_id: str, connection_id: str) -> tuple[bool, str, bool, str]:
    """
    Check if BOTH Gmail triggers exist.
    Returns: (inbox_exists, inbox_trigger_id, sent_exists, sent_trigger_id)
    
    ✅ FIX #1: Changed user_ids → connected_account_ids
    """
    try:
        # ✅ FIXED: Check INBOX trigger with correct API
        inbox_triggers = composio.triggers.list_active(
            trigger_names=["GMAIL_NEW_GMAIL_MESSAGE"],
            connected_account_ids=[connection_id]  # ✅ FIXED: was user_ids=[user_id]
        )
        
        inbox_exists = False
        inbox_trigger_id = None
        for trigger in inbox_triggers.items:
            if getattr(trigger, "connected_account_id", "") == connection_id:
                inbox_trigger_id = getattr(trigger, "id", None) or getattr(trigger, "trigger_id", None)
                inbox_exists = True
                break
        
        # ✅ FIXED: Check SENT trigger with correct API
        sent_triggers = composio.triggers.list_active(
            trigger_names=["GMAIL_EMAIL_SENT_TRIGGER"],
            connected_account_ids=[connection_id]  # ✅ FIXED: was user_ids=[user_id]
        )
        
        sent_exists = False
        sent_trigger_id = None
        for trigger in sent_triggers.items:
            if getattr(trigger, "connected_account_id", "") == connection_id:
                sent_trigger_id = getattr(trigger, "id", None) or getattr(trigger, "trigger_id", None)
                sent_exists = True
                break
        
        return (inbox_exists, inbox_trigger_id, sent_exists, sent_trigger_id)
        
    except Exception as e:
        print(f"⚠️ Error checking triggers: {e}")
        return (False, None, False, None)


# ======================================================
# HELPER: GET EXISTING GMAIL CONNECTION
# ======================================================
def get_existing_gmail_connection(composio: Composio, user_id: str) -> dict:
    """Check if user already has a Gmail connection."""
    try:
        resp = composio.connected_accounts.list(user_ids=[user_id])
        connections = getattr(resp, "items", resp)
        
        for conn in connections:
            conn_id = getattr(conn, "id", None)
            status = getattr(conn, "status", "UNKNOWN")
            
            # Check if it's Gmail
            integration_id = str(getattr(conn, "integration_id", "")).lower()
            toolkit = getattr(conn, "toolkit", None)
            toolkit_slug = str(getattr(toolkit, "slug", "")).lower() if toolkit else ""
            app_name = str(getattr(conn, "app_name", getattr(conn, "appName", ""))).lower()
            
            is_gmail = "gmail" in integration_id or "gmail" in toolkit_slug or "gmail" in app_name
            
            if is_gmail:
                print(f"📧 Found Gmail connection: {conn_id} (Status: {status})")
                return {
                    "exists": True,
                    "connection": conn,
                    "status": status,
                    "connection_id": conn_id
                }
        
        return {"exists": False, "connection": None, "status": None, "connection_id": None}
        
    except Exception as e:
        print(f"❌ Error checking connection: {e}")
        return {"exists": False, "connection": None, "status": None, "connection_id": None}


# ======================================================
# HELPER: CHECK/SET SYNC STATUS IN FIRESTORE
# ======================================================
def get_user_sync_status(user_id: str) -> dict:
    """Get user's Gmail sync status from Firestore."""
    try:
        user_doc = db.collection("users").document(user_id).get()
        if not user_doc.exists:
            return {
                "initial_sync_completed": False,
                "sync_in_progress": False,
                "trigger_registered": False,
                "trigger_creation_in_progress": False,
                "trigger_creation_started_at": None,
            }
        
        data = user_doc.to_dict()
        return {
            "initial_sync_completed": data.get("initial_sync_completed", False),
            "initial_sync_started_at": data.get("initial_sync_started_at"),
            "initial_sync_completed_at": data.get("initial_sync_completed_at"),
            "trigger_registered": data.get("trigger_registered", False),
            "connection_id": data.get("gmail_connection_id"),
            "sync_in_progress": data.get("sync_in_progress", False),
            "trigger_creation_in_progress": data.get("trigger_creation_in_progress", False),
            "trigger_creation_started_at": data.get("trigger_creation_started_at"),
        }
    except Exception as e:
        print(f"❌ Error getting sync status: {e}")
        return {
            "initial_sync_completed": False,
            "sync_in_progress": False,
            "trigger_registered": False,
            "trigger_creation_in_progress": False,
            "trigger_creation_started_at": None,
        }


def set_sync_started(user_id: str, connection_id: str):
    """Mark sync as started in Firestore."""
    try:
        db.collection("users").document(user_id).set({
            "sync_in_progress": True,
            "initial_sync_started_at": firestore.SERVER_TIMESTAMP,
            "gmail_connection_id": connection_id
        }, merge=True)
        print(f"✅ Marked sync as started for user: {user_id}")
    except Exception as e:
        print(f"❌ Error setting sync started: {e}")


def set_trigger_creation_started(user_id: str, connection_id: str):
    """Mark trigger creation as in progress."""
    try:
        db.collection("users").document(user_id).set({
            "trigger_creation_in_progress": True,
            "trigger_creation_started_at": firestore.SERVER_TIMESTAMP,
            "gmail_connection_id": connection_id
        }, merge=True)
        print(f"✅ Marked trigger creation as started for user: {user_id}")
    except Exception as e:
        print(f"❌ Error setting trigger creation started: {e}")


# ======================================================
# ✅ NEW: HELPER TO CLEAR TRIGGER CREATION LOCK
# ======================================================
def clear_trigger_creation_lock(user_id: str):
    """
    Clear trigger creation lock.
    
    ✅ FIX #2: Safety mechanism to clear lock after reconnection
    """
    try:
        db.collection("users").document(user_id).update({
            "trigger_creation_in_progress": False,
            "trigger_creation_started_at": None
        })
        print(f"✅ Cleared trigger creation lock for user: {user_id}")
    except Exception as e:
        print(f"❌ Error clearing trigger creation lock: {e}")


def reset_sync_status(user_id: str):
    """
    ⚠️ DEPRECATED: Use mark_disconnection() instead.
    """
    try:
        db.collection("users").document(user_id).set({
            "initial_sync_completed": False,
            "sync_in_progress": False,
            "initial_sync_started_at": None,
            "initial_sync_completed_at": None,
            "trigger_registered": False,
            "trigger_id": None,
            "gmail_connection_id": None,
            "trigger_creation_in_progress": False,
        }, merge=True)
        print(f"✅ Reset sync status for user: {user_id}")
    except Exception as e:
        print(f"❌ Error resetting sync status: {e}")


# ======================================================
# ✅ UPDATED: BACKGROUND TASK FOR FIRST-TIME CONNECTION
# ======================================================
def run_initial_sync_and_trigger_first_time(user_id: str, connection_id: str, gmail_connected_at: datetime):
    """
    Run initial sync + create BOTH triggers for first-time connection.
    
    PHASE 4B: Now creates INBOX + SENT triggers
    """
    print(f"\n{'='*80}")
    print(f"🆕 FIRST-TIME CONNECTION FLOW")
    print(f"   User: {user_id}")
    print(f"   Connection: {connection_id}")
    print(f"{'='*80}\n")

    try:
        # Mark sync started
        db.collection("users").document(user_id).set({
            "sync_in_progress": True,
            "initial_sync_started_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        # Run initial sync (fetches INBOX + SENT from Phase 4A)
        print("📥 Running initial sync...")
        run_initial_sync(user_id, gmail_connected_at)
        print("✅ Initial sync completed\n")

        # ═══════════════════════════════════════════════════════════════
        # ✅ NEW: Count commitments after sync
        # ═══════════════════════════════════════════════════════════════
        # ✅ Count commitments after sync
        commitment_count = 0
        try:
            # Correct Firestore path: users/{user_id}/commitments
            commitments_ref = (
                db.collection("users")
                .document(user_id)
                .collection("commitments")
            )
            
            # Query for non-completed commitments
            commitments = commitments_ref.where(
                filter=FieldFilter("completed", "==", False)
            ).stream()
            
            commitment_count = len(list(commitments))
            print(f"📊 Found {commitment_count} commitments after initial sync")
        except Exception as e:
            print(f"⚠️ Error counting commitments: {e}")
            import traceback
            traceback.print_exc()

        # Create INBOX trigger
        print("📬 Creating INBOX trigger...")
        composio = Composio(api_key=COMPOSIO_API_KEY)
        
        inbox_trigger = composio.triggers.create(
            slug="GMAIL_NEW_GMAIL_MESSAGE",
            user_id=user_id,
            connected_account_id=connection_id,
            trigger_config={}
        )
        inbox_trigger_id = getattr(inbox_trigger, "id", None) or getattr(inbox_trigger, "trigger_id", None)
        print(f"✅ INBOX trigger created: {inbox_trigger_id}\n")

        # PHASE 4B: Create SENT trigger
        print("📤 Creating SENT trigger...")
        sent_trigger = composio.triggers.create(
            slug="GMAIL_EMAIL_SENT_TRIGGER",
            user_id=user_id,
            connected_account_id=connection_id,
            trigger_config={
                "interval": 1,  # Check every 1 minute
                "userId": "me"
            }
        )
        sent_trigger_id = getattr(sent_trigger, "id", None) or getattr(sent_trigger, "trigger_id", None)
        print(f"✅ SENT trigger created: {sent_trigger_id}\n")

        # Mark first connection with BOTH triggers
        mark_first_connection(
            user_id=user_id,
            entity_id=connection_id,
            inbox_trigger_id=inbox_trigger_id,
            sent_trigger_id=sent_trigger_id  # PHASE 4B: NEW
        )

        # ═══════════════════════════════════════════════════════════════
        # ✅ MODIFIED: Store commitment count in Firestore
        # ═══════════════════════════════════════════════════════════════
        db.collection("users").document(user_id).set({
            "initial_sync_completed": True,
            "initial_sync_completed_at": firestore.SERVER_TIMESTAMP,
            "sync_in_progress": False,
            "gmail_connection_id": connection_id,
            "trigger_registered": True,
            "total_commitments_found": commitment_count, 
        }, merge=True)

        print(f"{'='*80}")
        print(f"🎉 FIRST-TIME SETUP COMPLETE")
        print(f"   INBOX Trigger: {inbox_trigger_id}")
        print(f"   SENT Trigger: {sent_trigger_id}")
        print(f"   Commitments Found: {commitment_count}")  # ✅ NEW
        print(f"{'='*80}\n")

    except Exception as e:
        print(f"❌ Error in first-time setup: {e}")
        import traceback
        traceback.print_exc()
        
        db.collection("users").document(user_id).set({
            "sync_in_progress": False,
            "sync_error": str(e)
        }, merge=True)



# ======================================================
# ✅ FIXED: BACKGROUND TASK FOR RECONNECTION
# ======================================================
def create_trigger_on_reconnection(user_id: str, connection_id: str):
    """
    Create BOTH triggers on reconnection (skip initial_sync).
    
    PHASE 4B: Now creates INBOX + SENT triggers
    ✅ FIX #2: Added try-finally to always clear lock
    """
    print(f"\n{'='*80}")
    print(f"🔄 RECONNECTION FLOW")
    print(f"   User: {user_id}")
    print(f"   Connection: {connection_id}")
    print(f"{'='*80}\n")

    try:
        composio = Composio(api_key=COMPOSIO_API_KEY)

        # Create INBOX trigger
        print("📬 Creating INBOX trigger...")
        inbox_trigger = composio.triggers.create(
            slug="GMAIL_NEW_GMAIL_MESSAGE",
            user_id=user_id,
            connected_account_id=connection_id,
            trigger_config={}
        )
        inbox_trigger_id = getattr(inbox_trigger, "id", None) or getattr(inbox_trigger, "trigger_id", None)
        print(f"✅ INBOX trigger created: {inbox_trigger_id}\n")

        # PHASE 4B: Create SENT trigger
        print("📤 Creating SENT trigger...")
        sent_trigger = composio.triggers.create(
            slug="GMAIL_EMAIL_SENT_TRIGGER",
            user_id=user_id,
            connected_account_id=connection_id,
            trigger_config={
                "interval": 1,
                "userId": "me"
            }
        )
        sent_trigger_id = getattr(sent_trigger, "id", None) or getattr(sent_trigger, "trigger_id", None)
        print(f"✅ SENT trigger created: {sent_trigger_id}\n")

        # Mark reconnection with BOTH triggers
        mark_reconnection(
            user_id=user_id,
            entity_id=connection_id,
            inbox_trigger_id=inbox_trigger_id,
            sent_trigger_id=sent_trigger_id  # PHASE 4B: NEW
        )

        # Update Firestore
        db.collection("users").document(user_id).set({
            "gmail_connection_id": connection_id,
            "trigger_registered": True,
        }, merge=True)

        print(f"{'='*80}")
        print(f"🎉 RECONNECTION COMPLETE")
        print(f"   INBOX Trigger: {inbox_trigger_id}")
        print(f"   SENT Trigger: {sent_trigger_id}")
        print(f"{'='*80}\n")

    except Exception as e:
        print(f"❌ Error in reconnection: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # ✅ FIX #2: Always clear lock, even if error occurs
        clear_trigger_creation_lock(user_id)
        print(f"🔓 Trigger creation lock cleared for user: {user_id}")


# ======================================================
# DEPRECATED: OLD FUNCTION
# ======================================================
def run_initial_sync_and_trigger(user_id: str, connection_id: str, gmail_connected_at: datetime):
    """⚠️ DEPRECATED"""
    print(f"\n⚠️ WARNING: Using deprecated run_initial_sync_and_trigger")
    run_initial_sync_and_trigger_first_time(user_id, connection_id, gmail_connected_at)


# ======================================================
# USER PROFILE
# ======================================================
@app.get("/user")
def get_current_user(request: Request):
    decoded = verify_token(request)
    uid = decoded.get("uid")
    email = decoded.get("email")
    name = decoded.get("name", "")

    name_parts = name.split(" ", 1) if name else ["", ""]
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    print(f"User authenticated → {uid} | {email}")
    
    # ✅ CRITICAL: Initialize user document if it doesn't exist
    try:
        user_ref = db.collection("users").document(uid)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            print(f"🆕 Creating user document for: {uid}")
            user_ref.set({
                "uid": uid,
                "email": email,
                "name": name,
                "created_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
    except Exception as e:
        print(f"⚠️ Error initializing user document: {e}")

    return {
        "uid": uid,
        "email": email,
        "name": name,
        "firstName": first_name,
        "lastName": last_name,
    }


@app.get("/")
def home():
    return {"message": "Backend is working!", "status": "ok"}


# ======================================================
# ✅ FIXED: CHECK GMAIL CONNECTION (WITH ALL FIXES!)
# ======================================================
@app.get("/check-gmail-connection")
def check_gmail_connection(request: Request, background_tasks: BackgroundTasks):
    """
    Check Gmail connection status and AUTO-TRIGGER sync if needed.
    
    ✅ FIXED: 
    - Uses correct Composio SDK API (list_active with connected_account_ids)
    - Prevents duplicate trigger creation with lock + timeout
    - Better error handling
    """
    decoded = verify_token(request)
    uid = decoded.get("uid")

    print(f"\n{'='*60}")
    print(f"🔍 CHECK GMAIL CONNECTION (with all fixes)")
    print(f"   User: {uid}")
    print(f"{'='*60}")

    try:
        composio = Composio(api_key=COMPOSIO_API_KEY)
        existing = get_existing_gmail_connection(composio, uid)
        
        gmail_connected = existing["exists"] and existing["status"] == "ACTIVE"
        connection_id = existing.get("connection_id")

        if not gmail_connected:
            print(f"📭 Gmail not connected")
            return {
                "connected": False,
                "uid": uid,
                "sync_status": "not_connected"
            }

        # Get connection state
        connection_state = get_connection_state(uid)
        is_first_time = connection_state["is_first_time"]
        
        print(f"📊 Connection state:")
        print(f"   • First time: {is_first_time}")
        print(f"   • First connected at: {connection_state.get('first_connected_at')}")
        
        # Check sync status
        sync_status = get_user_sync_status(uid)
        print(f"📊 Sync status: {sync_status}")

        # ✅ FIXED: Check if trigger actually exists using correct API
        if sync_status["initial_sync_completed"]:
            inbox_exists, inbox_trigger_id, sent_exists, sent_trigger_id = check_triggers_exist(composio, uid, connection_id)

            if inbox_exists and sent_exists:
                # Both triggers exist - all good
                print(f"✅ Both triggers exist")
                return {
                    "connected": True,
                    "uid": uid,
                    "connection_id": connection_id,
                    "sync_status": "completed",
                    "trigger_registered": True,
                    "inbox_trigger_id": inbox_trigger_id,
                    "sent_trigger_id": sent_trigger_id
                }
            else:
                # One or both triggers missing
                print(f"⚠️ Triggers missing - INBOX: {inbox_exists}, SENT: {sent_exists}")
                
                # ✅ FIX #2: Check for stale lock (timeout-based safety)
                if sync_status.get("trigger_creation_in_progress"):
                    lock_started = sync_status.get("trigger_creation_started_at")
                    
                    if lock_started:
                        # Calculate lock age
                        if isinstance(lock_started, datetime):
                            age_seconds = (datetime.now(timezone.utc) - lock_started).total_seconds()
                        else:
                            # Firestore timestamp
                            age_seconds = (datetime.now(timezone.utc) - lock_started.replace(tzinfo=timezone.utc)).total_seconds()
                        
                        age_minutes = age_seconds / 60
                        
                        if age_minutes < 5:
                            # Lock is fresh, respect it
                            print(f"⏳ Trigger creation already in progress ({age_minutes:.1f} min)")
                            return {
                                "connected": True,
                                "uid": uid,
                                "connection_id": connection_id,
                                "sync_status": "reconnecting",
                                "message": "Trigger creation in progress",
                            }
                        else:
                            # Lock is stale, clear it
                            print(f"⚠️ Clearing stale lock ({age_minutes:.1f} minutes old)")
                            clear_trigger_creation_lock(uid)
                
                # ✅ FIX #2: Set lock BEFORE starting reconnection
                print(f"🔄 RECONNECTION: Creating missing triggers")
                set_trigger_creation_started(uid, connection_id)
                
                background_tasks.add_task(
                    create_trigger_on_reconnection,
                    uid,
                    connection_id
                )
                
                return {
                    "connected": True,
                    "uid": uid,
                    "connection_id": connection_id,
                    "sync_status": "reconnecting",
                    "message": "Reconnection in progress (creating triggers)",
                }

        # Sync in progress
        if sync_status["sync_in_progress"]:
            print(f"⏳ Sync already in progress")
            return {
                "connected": True,
                "uid": uid,
                "connection_id": connection_id,
                "sync_status": "in_progress",
            }

        # New connection - decide flow
        if is_first_time:
            # FIRST-TIME CONNECTION
            print(f"🆕 FIRST-TIME CONNECTION - Running initial sync")
            
            set_sync_started(uid, connection_id)
            gmail_connected_at = datetime.now(timezone.utc)
            
            background_tasks.add_task(
                run_initial_sync_and_trigger_first_time,
                uid,
                connection_id,
                gmail_connected_at
            )
            
            return {
                "connected": True,
                "uid": uid,
                "connection_id": connection_id,
                "sync_status": "started",
                "message": "Initial sync started (first-time connection)",
            }
        
        else:
            # RECONNECTION
            print(f"🔄 RECONNECTION - Skipping initial sync")
            
            # ✅ FIX #2: Set lock before starting
            set_trigger_creation_started(uid, connection_id)
            
            background_tasks.add_task(
                create_trigger_on_reconnection,
                uid,
                connection_id
            )
            
            return {
                "connected": True,
                "uid": uid,
                "connection_id": connection_id,
                "sync_status": "reconnected",
                "message": "Reconnection complete (no sync needed)",
            }

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {"connected": False, "uid": uid, "error": str(e)}


# ======================================================
# ✅ UPDATED: DISCONNECT GMAIL
# ======================================================
@app.post("/disconnect-gmail")
def disconnect_gmail_endpoint(request: Request):
    """Disconnect Gmail connection and delete BOTH triggers."""
    decoded = verify_token(request)
    uid = decoded.get("uid")

    print(f"🔌 Disconnecting Gmail for user: {uid}")

    try:
        composio = Composio(api_key=COMPOSIO_API_KEY)
        
        # Get connection state to find trigger IDs
        connection_state = get_connection_state(uid)
        inbox_trigger_id = connection_state.get("inbox_trigger_id")
        sent_trigger_id = connection_state.get("sent_trigger_id")
        
        # Delete INBOX trigger
        if inbox_trigger_id:
            try:
                composio.triggers.delete(trigger_id=inbox_trigger_id)
                print(f"✅ Deleted INBOX trigger: {inbox_trigger_id}")
            except Exception as e:
                print(f"⚠️ Failed to delete INBOX trigger: {e}")
        
        # Delete SENT trigger
        if sent_trigger_id:
            try:
                composio.triggers.delete(trigger_id=sent_trigger_id)
                print(f"✅ Deleted SENT trigger: {sent_trigger_id}")
            except Exception as e:
                print(f"⚠️ Failed to delete SENT trigger: {e}")
        
        # Delete connection
        existing = get_existing_gmail_connection(composio, uid)
        if existing["exists"]:
            connection_id = existing["connection_id"]
            composio.connected_accounts.delete(connection_id)
            print(f"✅ Deleted connection: {connection_id}")
        
        # Mark disconnection in Firestore
        mark_disconnection(uid)
        
        # Clear locks
        clear_trigger_creation_lock(uid)
        
        return {"status": "disconnected", "uid": uid}
        
    except Exception as e:
        print(f"❌ Error disconnecting: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# GMAIL CONNECTION ENDPOINT
# ======================================================
@app.get("/connect/composio-gmail")
async def connect_composio_gmail(request: Request):
    """
    Initiate Gmail connection via Composio OAuth.
    Now accepts optional chat_page_id to preserve chat context across OAuth redirect.
    """
    try:
        # Verify user
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            raise HTTPException(status_code=401, detail="Missing Authorization Header")
        
        token = auth_header.replace("Bearer ", "")
        decoded = auth.verify_id_token(token)
        user_id = decoded.get("uid")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found")
        
        # ═══════════════════════════════════════════════════════════════
        # ✅ NEW: Get chat_page_id from query params (if provided)
        # ═══════════════════════════════════════════════════════════════
        chat_page_id = request.query_params.get("chat_page_id")
        
        # Build callback URL
        callback = get_callback_url(request)
        
        # ═══════════════════════════════════════════════════════════════
        # ✅ NEW: Include chat_page_id in callback if present
        # ═══════════════════════════════════════════════════════════════
        if chat_page_id:
            # Parse existing URL and add chat_id parameter
            if "?" in callback:
                callback = f"{callback}&chat_id={chat_page_id}"
            else:
                callback = f"{callback}?chat_id={chat_page_id}"
            print(f"🔗 Including chat_page_id in callback: {chat_page_id}")
        
        print("=" * 60)
        print("🔵 GMAIL CONNECTION REQUEST")
        print(f"   User: {user_id}")
        print(f"   Callback: {callback}")
        if chat_page_id:
            print(f"   Preserving Chat: {chat_page_id}")
        print("=" * 60)
        
        # Rest of your existing code...
        composio = Composio(api_key=COMPOSIO_API_KEY)
        
        # Check if connection already exists
        try:
            connections = composio.connected_accounts.list(
                user_ids=[user_id],
                toolkit_slugs=["GMAIL"]
            )
            
            for conn in connections.items:
                if conn.status == "ACTIVE":
                    print(f"✅ Gmail already connected: {conn.id}")
                    return {
                        "already_connected": True,
                        "connection_id": conn.id,
                        "redirect_url": callback
                    }
        except Exception as e:
            print(f"⚠️ Error checking existing connection: {e}")
        
        # Create new connection
        print("🔄 Creating new Gmail connection...")
        connection_request = composio.connected_accounts.initiate(
            user_id=user_id,
            auth_config_id=GMAIL_AUTH_CONFIG,
            callback_url=callback,
            allow_multiple=True  # ✅ Allow multiple connections
        )
        
        # ✅ CORRECT: Use .id and .redirect_url
        print(f"✅ Connection created: {connection_request.id}")
        
        return {
            "redirect_url": connection_request.redirect_url,  # ✅ CORRECT
            "connection_id": connection_request.id  # ✅ CORRECT
        }
        
    except Exception as e:
        print(f"❌ Error connecting Gmail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# CALLBACK ENDPOINT
# ======================================================
@app.get("/composio/callback")
def composio_callback_endpoint(request: Request, background_tasks: BackgroundTasks):
    decoded = verify_token(request)
    uid = decoded.get("uid")

    print(f"\n{'='*60}")
    print(f"✅ GMAIL CONNECTION CALLBACK")
    print(f"   User: {uid}")
    print(f"{'='*60}\n")

    try:
        composio = Composio(api_key=COMPOSIO_API_KEY)
        existing = get_existing_gmail_connection(composio, uid)

        if not existing["exists"] or existing["status"] != "ACTIVE":
            raise HTTPException(status_code=400, detail="No active connection found")

        connection_id = existing["connection_id"]
        gmail_connected_at = datetime.now(timezone.utc)

        # Check if first time or reconnection
        is_first_time = should_run_initial_sync(uid)

        if is_first_time:
            background_tasks.add_task(
                run_initial_sync_and_trigger_first_time,
                uid,
                connection_id,
                gmail_connected_at
            )
        else:
            # ✅ FIX #2: Set lock before starting reconnection
            set_trigger_creation_started(uid, connection_id)
            
            background_tasks.add_task(
                create_trigger_on_reconnection,
                uid,
                connection_id
            )

        return {
            "status": "started",
            "uid": uid,
            "connection_id": connection_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# ✅ FIXED: REGISTER GMAIL TRIGGER
# ======================================================
@app.post("/register-gmail-trigger")
def register_gmail_trigger(request: Request):
    decoded = verify_token(request)
    user_id = decoded.get("uid")

    print(f"\n{'='*60}")
    print(f"🔵 REGISTER GMAIL TRIGGER")
    print(f"   User: {user_id}")
    print(f"{'='*60}\n")

    try:
        composio = Composio(api_key=COMPOSIO_API_KEY)
        existing = get_existing_gmail_connection(composio, user_id)
        
        if not existing["exists"] or existing["status"] != "ACTIVE":
            raise HTTPException(status_code=400, detail="No active Gmail connection")

        connected_account_id = existing["connection_id"]

        # ✅ FIXED: Check existing triggers using correct API
        try:
            inbox_exists, inbox_trigger_id, sent_exists, sent_trigger_id = check_triggers_exist(composio, user_id, connected_account_id)

            if inbox_exists and sent_exists:
                print(f"⚠️ Both triggers already exist")
                print(f"   INBOX: {inbox_trigger_id}")
                print(f"   SENT: {sent_trigger_id}")
                return {
                    "status": "already_exists",
                    "inbox_trigger_id": inbox_trigger_id,
                    "sent_trigger_id": sent_trigger_id,
                    "user_id": user_id,
                }
        except Exception as e:
            print(f"⚠️ Error checking existing triggers: {e}")

        # Create trigger
        trigger = composio.triggers.create(
            slug="GMAIL_NEW_GMAIL_MESSAGE",
            user_id=user_id,
            connected_account_id=connected_account_id,
            trigger_config={},
        )

        trigger_id = getattr(trigger, "id", None) or getattr(trigger, "trigger_id", None)
        print(f"✅ Trigger created: {trigger_id}")

        return {
            "status": "success",
            "trigger_id": trigger_id,
            "user_id": user_id,
            "connected_account_id": connected_account_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# COMPOSIO WEBHOOK
# ======================================================
@app.post("/composio/webhook")
async def composio_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()

        print(f"\n========== COMPOSIO WEBHOOK ==========")
        print(f"Type: {body.get('type')}")
        print(f"=======================================\n")

        data = body.get("data", {})
        
        user_id = data.get("user_id")
        connected_account_id = data.get("connection_nano_id") or data.get("connection_id")
        message_id = data.get("message_id") or data.get("id")

        if not user_id or not connected_account_id or not message_id:
            print("❌ Missing fields")
            return {"status": "error", "reason": "missing_fields"}
        
        from credit_engine import has_enough_credits
        if not has_enough_credits(user_id):
            print(f"⚠️ User {user_id} has no credits - skipping email processing")
            return {"status": "skipped", "reason": "no_credits"}

        background_tasks.add_task(
            process_new_email,
            user_id,
            connected_account_id,
            message_id
        )

        print("✅ Email processing queued")
        return {"status": "ok"}

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        raise HTTPException(status_code=500, detail="Webhook error")


# ======================================================
# GET SYNC STATUS
# ======================================================
@app.get("/sync-status")
def get_sync_status(request: Request):
    """Get user's Gmail sync status."""
    decoded = verify_token(request)
    uid = decoded.get("uid")
    
    composio = Composio(api_key=COMPOSIO_API_KEY)
    existing = get_existing_gmail_connection(composio, uid)
    sync_status = get_user_sync_status(uid)
    
    # Include connection state
    connection_state = get_connection_state(uid)
    
    # ═══════════════════════════════════════════════════════════════
    # ✅ NEW: Get commitment count from Firestore
    # ═══════════════════════════════════════════════════════════════
    commitment_count = 0
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            commitment_count = user_doc.to_dict().get("total_commitments_found", 0)
    except Exception as e:
        print(f"⚠️ Error getting commitment count: {e}")
    
    return {
        "user_id": uid,
        "gmail_connected": existing["exists"] and existing["status"] == "ACTIVE",
        "connection_id": existing.get("connection_id"),
        "connection_status": existing.get("status"),
        "sync": sync_status,
        "commitments_found": commitment_count,  # ✅ NEW
        "connection_state": {
            "is_first_time": connection_state["is_first_time"],
            "first_connected_at": str(connection_state.get("first_connected_at")) if connection_state.get("first_connected_at") else None,
            "composio_enabled": connection_state["composio_enabled"]
        }
    }

# ======================================================
# DEBUG ENDPOINT
# ======================================================
@app.get("/debug/connection-info")
def debug_connection_info(request: Request):
    """Debug endpoint for connection info."""
    decoded = verify_token(request)
    uid = decoded.get("uid")
    
    composio = Composio(api_key=COMPOSIO_API_KEY)
    
    try:
        resp = composio.connected_accounts.list(user_ids=[uid])
        connections = getattr(resp, "items", resp)
        
        connection_list = []
        for conn in connections:
            toolkit = getattr(conn, "toolkit", None)
            connection_list.append({
                "id": getattr(conn, "id", "N/A"),
                "status": getattr(conn, "status", "N/A"),
                "toolkit_slug": getattr(toolkit, "slug", "N/A") if toolkit else "N/A",
            })
        
        sync_status = get_user_sync_status(uid)
        connection_state = get_connection_state(uid)
        
        return {
            "user_id": uid,
            "connections": connection_list,
            "sync_status": sync_status,
            "connection_state": {
                "is_first_time": connection_state["is_first_time"],
                "first_connected_at": str(connection_state.get("first_connected_at")) if connection_state.get("first_connected_at") else None,
                "composio_enabled": connection_state["composio_enabled"],
                "inbox_trigger_id": connection_state.get("inbox_trigger_id"),
                "sent_trigger_id": connection_state.get("sent_trigger_id"),
                "entity_id": connection_state.get("entity_id")
            }
        }
        
    except Exception as e:
        return {"error": str(e)}


# ======================================================
# START SERVER
# ======================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)