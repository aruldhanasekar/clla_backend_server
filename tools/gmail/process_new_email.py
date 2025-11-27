import base64
import os
import re
from datetime import datetime, timezone
from typing import Dict, Any

from composio import Composio
from firebase_admin import firestore

from services.gmail.extract_initial_commitments import extract_commitments_from_email
from services.gmail.save_commitment import save_commitment_to_firestore
from services.gmail.deadline_parser import parse_deadline_raw



COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")
TOOLKIT_VERSION = os.getenv("TOOLKIT_VERSION", "20251119_00")


def safe_b64decode(text: str) -> str:
    """Decode Gmail base64 safely."""
    try:
        missing_padding = len(text) % 4
        if missing_padding:
            text += "=" * (4 - missing_padding)
        return base64.urlsafe_b64decode(text).decode("utf-8", errors="ignore")
    except:
        return ""


def get_founder_email(user_id: str) -> str:
    """
    Get founder's email from Firestore user profile.
    Used for SENT emails where founder is the sender.
    
    PHASE 4B: NEW function
    """
    try:
        db = firestore.client()
        user_doc = db.collection("users").document(user_id).get()
        if user_doc.exists:
            email = user_doc.to_dict().get("email")
            if email:
                return email
        print(f"⚠️ Could not find email for user {user_id}")
        return "unknown@unknown.com"
    except Exception as e:
        print(f"❌ Error fetching founder email: {e}")
        return "unknown@unknown.com"




def extract_email_address(header_value: str) -> str:
    """Extract email address from header like 'John Doe <john@example.com>'"""
    if not header_value:
        return ""
    match = re.search(r"<([^>]+)>", header_value)
    if match:
        return match.group(1).strip()
    return header_value.strip()


def extract_name_from_header(header_value: str) -> str:
    """Extract name from email header like 'John Doe <john@example.com>'"""
    if not header_value:
        return ""
    if "<" in header_value:
        name = header_value.split("<")[0].strip()
        if name:
            return name.strip('"').strip("'")
    email = extract_email_address(header_value)
    if email and "@" in email:
        return email.split("@")[0]
    return ""

def build_email_json(
    full_msg: Dict[str, Any], 
    original_message_id: str,
    folder: str,  # PHASE 4B: NEW parameter
    user_id: str  # PHASE 4B: NEW parameter for SENT emails
) -> Dict[str, Any]:
    """
    Convert Gmail API message to email_json format.
    
    PHASE 4B: Now handles both INBOX and SENT folders
    """
    payload = full_msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

    # Extract body text
    body_text = ""

    def extract_part(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if mime == "text/plain" and data:
            return safe_b64decode(data)

        if mime == "text/html" and data:
            html = safe_b64decode(data)
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, "html.parser").get_text("\n")
            except:
                return ""

        for sub in part.get("parts", []):
            txt = extract_part(sub)
            if txt:
                return txt

        return ""

    # Try top-level body
    if payload.get("body", {}).get("data"):
        body_text = safe_b64decode(payload["body"]["data"])
    else:
        for p in payload.get("parts", []):
            body_text = extract_part(p)
            if body_text:
                break

    # Fallback
    if not body_text:
        body_text = full_msg.get("snippet", "")

    # PHASE 4B: Sender logic based on folder
    if folder == "SENT":
        # For SENT emails, founder is the sender
        sender_email = get_founder_email(user_id)
        sender_name = "You"
    else:
        # For INBOX, extract from headers
        sender_header = headers.get("From", "")
        sender_name = sender_header.split("<")[0].strip()
        sender_email = sender_header

    # Message ID
    message_id = full_msg.get("id") or headers.get("Message-ID") or headers.get("Message-Id") or original_message_id

    # Date
    internal_date = full_msg.get("internalDate")
    if internal_date:
        dt = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    # Extract recipient for SENT emails
    recipient_email = ""
    recipient_name = ""
    if folder == "SENT":
        to_header = headers.get("To", "")
        if to_header:
            # Handle multiple recipients - take first one
            if "," in to_header:
                to_header = to_header.split(",")[0].strip()
            recipient_email = extract_email_address(to_header)
            recipient_name = extract_name_from_header(to_header)
            if not recipient_name and "@" in recipient_email:
                recipient_name = recipient_email.split("@")[0]

    # Build final json
    return {
        "sender": sender_email,
        "sender_name": sender_name,
        "subject": headers.get("Subject", "(No Subject)"),
        "body": body_text,
        "date": dt.isoformat(),
        "message_id": message_id,
        "folder": folder,  # PHASE 4B: Dynamic folder (INBOX or SENT)
        "recipient_email": recipient_email,
        "recipient_name": recipient_name,
    }


def check_commitment_exists(user_id: str, message_id: str) -> bool:
    """Check Firestore for existing commitment with same message_id."""
    db = firestore.client()
    ref = db.collection("users").document(user_id).collection("commitments")
    docs = ref.where("message_id", "==", message_id).limit(1).stream()
    return any(True for _ in docs)


def process_new_email(user_id: str, connected_account_id: str, message_id: str):
    print("\n" + "=" * 80)
    print("📬 PROCESSING NEW EMAIL (Trigger) - PHASE 4B")
    print(f"User ID: {user_id}")
    print(f"Connected Account ID: {connected_account_id}")
    print(f"Message ID: {message_id}")
    print("=" * 80)

    composio = Composio(api_key=COMPOSIO_API_KEY,
                        toolkit_versions={"gmail": TOOLKIT_VERSION})

    # 1. Fetch the full Gmail message
    try:
        resp = composio.tools.execute(
            slug="GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            arguments={
                "message_id": message_id,
                "format": "full"
            },
            user_id=user_id,
            connected_account_id=connected_account_id,
            version=TOOLKIT_VERSION
        )
    except Exception as e:
        print("❌ Failed to fetch message:", e)
        import traceback
        traceback.print_exc()
        return

    msg = resp.get("data", {})
    if not msg:
        print("❌ Gmail returned empty message payload")
        return

    # PHASE 4B: Detect folder from labelIds
    label_ids = msg.get("labelIds", [])
    print(f"📋 Label IDs: {label_ids}")
    
    if "SENT" in label_ids:
        folder = "SENT"
        print("📤 Detected SENT email")
    elif "INBOX" in label_ids:
        folder = "INBOX"
        print("📥 Detected INBOX email")
    else:
        # Default to INBOX if unclear
        folder = "INBOX"
        print("⚠️ No clear folder detected, defaulting to INBOX")

    # 2. Build email_json with detected folder
    email_json = build_email_json(msg, message_id, folder, user_id)

    print(f"📧 Email Details:")
    print(f"   Folder: {email_json['folder']}")
    print(f"   From: {email_json['sender_name']} <{email_json['sender']}>")
    if folder == "SENT" and email_json.get('recipient_email'):
        print(f"   To: {email_json['recipient_name']} <{email_json['recipient_email']}>")
    print(f"   Subject: {email_json['subject']}")

    # Deduplicate
    if check_commitment_exists(user_id, email_json["message_id"]):
        print("⚠️ Commitment for this message_id already exists – skipping.")
        return

    # 3. Run extractor
    try:
        ai_result = extract_commitments_from_email(email_json, user_id)
    except Exception as e:
        print("❌ Extractor failed:", e)
        import traceback
        traceback.print_exc()
        return

    if not ai_result.get("has_commitment"):
        print("ℹ️ No commitments found in this email.")
        return

    commitments = ai_result.get("commitments", [])
    classification = ai_result.get("classification", {})
    direction = ai_result.get("direction", "")
    summary = ai_result.get("summary", "")

    print(f"📊 Found {len(commitments)} commitment(s) in this new email.")
    print(f"   Direction: {direction}")

    # 4. Save each commitment
    now_iso = datetime.now(timezone.utc).isoformat()

    for c in commitments:
        deadline_raw = c.get("deadline_raw")
        deadline_iso = None
        if deadline_raw:
            deadline_iso = parse_deadline_raw(deadline_raw, email_json["date"])

        commitment_doc = {
            "has_commitment": True,
            "direction": direction if direction else "incoming",
            "assigned_to_me": c.get("assigned_to_me", False),
            "summary": summary,

            "commitment_id": None,
            "user_id": user_id,

            "what": c.get("what"),
            "to_whom": c.get("to_whom"),
            "given_by": c.get("given_by"),

            "deadline_raw": deadline_raw,
            "deadline_iso": deadline_iso,

            "status": c.get("status", "active"),
            "completed": c.get("completed", False),
            "completed_at": None,
            "days_overdue": c.get("days_overdue", 0),
            "overdue_flag": c.get("overdue_flag", False),

            "priority": c.get("priority", "medium"),
            "commitment_type": c.get("commitment_type", "general"),
            "confidence": c.get("confidence", 1.0),
            "estimated_hours": c.get("estimated_hours", 0),

            "message_id": email_json["message_id"],
            "email_subject": email_json["subject"],
            "email_sender": email_json["sender"],
            "email_sender_name": email_json["sender_name"],
            "email_date": email_json["date"],
            "source_email_folder": email_json["folder"],  # PHASE 4B: Dynamic folder

            "sender_role": classification.get("sender_role", classification.get("role", "unknown")),
            "classification_confidence": classification.get("confidence"),
            "classification_domain_match": classification.get("domain_match"),
            "classification_domain": classification.get("domain"),
            "classification_signature_match": classification.get("signature_match"),
            "classification_subject_hint": classification.get("subject_hint"),
            "classification_body_hint": classification.get("body_hint"),
            "classification_fallback_used": classification.get("fallback_used"),

            "created_at": now_iso,
            "updated_at": now_iso,
            "extracted_at": now_iso
        }

        saved = save_commitment_to_firestore(user_id, commitment_doc)
        print(f"✅ Saved commitment: {saved}")
        print(f"   What: {c.get('what')}")
        print(f"   Direction: {direction}")
        print(f"   Assigned to me: {c.get('assigned_to_me')}")

    print("🎉 New email processing complete.\n")