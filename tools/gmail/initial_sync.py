#  tools/gmail/initial_sync.py
"""
Initial sync pipeline (integrated with AI extraction and Firestore saving).

PHASE 4A: Now fetches from both INBOX and SENT folders

Behavior:
- Fetch emails from INBOX and SENT for the user in the strict window:
    [gmail_connected_at - 2 days, gmail_connected_at]
- Filter newsletters from INBOX (not SENT)
- For each valid email, call AI extractor from services/gmail/extract_initial_commitments.py
- Commitments are already post-processed (deadline_iso, status, etc.) by extract_commitments_from_email
- Print formatted output for testing
- Save to Firestore using services/gmail/save_commitment.py

This file expects:
- COMPOSIO_API_KEY in environment
- Firebase Admin already initialized (credentials loaded) in main.py (if saving)
- services/gmail/extract_initial_commitments.py available
- services/gmail/save_commitment.py available (if saving)

Run as module for testing (will call run_initial_sync with a dev user id).
"""

from __future__ import annotations
import os
import re
import json
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from composio import Composio

# Import AI extractor from services/gmail
from services.gmail.extract_initial_commitments import extract_commitments_from_email
# Import Firestore save function
from services.gmail.save_commitment import save_commitment_to_firestore

# Initialize Firebase Admin

load_dotenv()

# Initialize Firebase (only once)
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")
TOOLKIT_VERSION = os.getenv("COMPOSIO_GMAIL_TOOLKIT", "20251119_00")

# PHASE 4A: Split limits between INBOX and SENT
MAX_COUNT_INBOX = int(os.getenv("INITIAL_SYNC_MAX_INBOX", "100"))
MAX_COUNT_SENT = int(os.getenv("INITIAL_SYNC_MAX_SENT", "100"))
BATCH_SIZE = int(os.getenv("INITIAL_SYNC_BATCH", "50"))

SKIP_SENDER_PATTERNS = [
    r"no-?reply@", r"noreply@", r"newsletter@", r"news@", r"do-not-reply@", r"bounce@"
]
SKIP_SUBJECT_PATTERNS = [
    r"receipt", r"order confirmation", r"unsubscribe", r"invoice", r"your receipt"
]
SKIP_HEADERS = ["List-Unsubscribe", "Precedence", "Auto-Submitted"]


# --------------------------
# Helpers
# --------------------------

def safe_b64decode(data: str) -> str:
    if not data:
        return ""
    try:
        padding = '=' * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.extract()
    return soup.get_text(separator="\n", strip=True)


def extract_email_text(payload: Dict[str, Any]) -> str:
    if not payload:
        return ""

    mime_type = payload.get("mimeType", "").lower()
    body = payload.get("body", {}) or {}
    data = body.get("data")

    if mime_type == "text/plain" and data:
        return safe_b64decode(data)

    if mime_type == "text/html" and data:
        return html_to_text(safe_b64decode(data))

    for part in payload.get("parts", []) or []:
        text = extract_email_text(part)
        if text:
            return text

    return ""


def parse_message_timestamp(msg: Dict[str, Any], headers: Dict[str, str]) -> datetime:
    internal_date_str = msg.get("internalDate")
    if internal_date_str:
        try:
            ts = int(internal_date_str) / 1000
            return datetime.fromtimestamp(ts, timezone.utc)
        except Exception:
            pass

    date_header = headers.get("Date", "")
    if date_header:
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(date_header).astimezone(timezone.utc)
        except Exception:
            pass

    return datetime.now(timezone.utc)


def is_likely_newsletter(headers: Dict[str, str], sender_email: str, subject: str) -> bool:
    for pat in SKIP_SENDER_PATTERNS:
        if re.search(pat, sender_email, re.I):
            return True

    for pat in SKIP_SUBJECT_PATTERNS:
        if re.search(pat, subject, re.I):
            return True

    for hdr in SKIP_HEADERS:
        if hdr in headers:
            return True

    return False


def build_query(from_time: datetime, to_time: datetime) -> str:
    return (
        f"after:{from_time:%Y/%m/%d} "
        f"before:{(to_time + timedelta(days=1)):%Y/%m/%d}"
    )


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




# --------------------------
# PHASE 4A: Email Processing Helper
# --------------------------

def process_email_batch(
    messages: List[Dict[str, Any]],
    user_id: str,
    from_time: datetime,
    to_time: datetime,
    folder: str,
    founder_email: Optional[str],
    openai_api_key: Optional[str],
    apply_newsletter_filter: bool = True
) -> tuple[int, int]:
    """
    Process a batch of emails and return (valid_count, commitment_count).
    
    Args:
        messages: List of Gmail message objects
        user_id: Firebase user ID
        from_time: Start of time window
        to_time: End of time window
        folder: "INBOX" or "SENT"
        founder_email: Founder's email (for SENT emails)
        openai_api_key: OpenAI API key
        apply_newsletter_filter: Whether to filter newsletters (False for SENT)
    """
    valid_count = 0
    commitment_count = 0

    for msg_idx, msg in enumerate(messages, 1):
        payload = msg.get("payload", {}) or {}
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        # Basic sender/subject extraction
        sender_header = headers.get("From", "")
        sender_match = re.search(r"<([^>]+)>", sender_header)
        sender_email = sender_match.group(1) if sender_match else sender_header
        subject = headers.get("Subject", "(No Subject)")

        # Newsletter filtering (only for INBOX)
        if apply_newsletter_filter and is_likely_newsletter(headers, sender_email, subject):
            continue

        msg_dt = parse_message_timestamp(msg, headers)

        # Strict window check
        if not (from_time <= msg_dt <= to_time):
            continue

        # Build email JSON to send to LLM
        body_text = extract_email_text(payload)
        message_id = msg.get("id") or headers.get("Message-ID") or headers.get("Message-Id")

        # PHASE 4A: Set sender based on folder
        if folder == "SENT":
            # For SENT emails, founder is the sender
            email_sender = founder_email or sender_email
            email_sender_name = "You"  # Or could extract from headers
        else:
            # For INBOX, use actual sender
            email_sender = sender_email
            email_sender_name = re.sub(r"<[^>]+>", "", sender_header).strip()

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

        email_json = {
            "sender": email_sender,
            "sender_name": email_sender_name,
            "subject": subject,
            "body": body_text,
            "date": msg_dt.isoformat(),
            "message_id": message_id,
            "folder": folder,  # PHASE 4A: "INBOX" or "SENT"
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
        }

        # Call AI extractor (this includes post-processing: deadline_iso, status, etc.)
        try:
            ai_result = extract_commitments_from_email(email_json, user_id, openai_api_key=openai_api_key)
        except Exception as e:
            print(f"❌ AI extractor failed for message {message_id}: {e}\n")
            continue

        if not ai_result.get("has_commitment"):
            # Skip non-commitment emails
            continue

        valid_count += 1

        # Print email header
        print("\n" + "="*80)
        print(f"📧 EMAIL #{valid_count} [{folder}]")
        print("="*80)
        print(f"FROM: {email_json.get('sender_name')} <{email_json.get('sender')}>")
        if folder == "SENT" and recipient_email:
            print(f"TO: {recipient_name} <{recipient_email}>")
        print(f"DATE: {email_json.get('date')}")
        print(f"SUBJECT: {email_json.get('subject')}")
        print(f"BODY PREVIEW: {body_text[:150]}...")
        print("="*80)

        # Get commitments (already processed with deadline_iso, status, etc.)
        commitments = ai_result.get("commitments", [])
        classification = ai_result.get("classification", {})

        print(f"\n📊 CLASSIFICATION:")
        print(f"  Sender Role: {classification.get('sender_role', 'unknown')}")
        print(f"  Confidence: {classification.get('confidence', 0.0):.2f}")
        
        if 'reasoning' in classification:
            reasoning = classification['reasoning']
            print(f"  Reasoning:")
            print(f"    • Domain Match: {reasoning.get('domain_match', False)}")
            print(f"    • Domain: {reasoning.get('domain', 'N/A')}")
            print(f"    • Signature Match: {reasoning.get('signature_match', False)}")
            print(f"    • Subject Hint: {reasoning.get('subject_hint', False)}")
            print(f"    • Body Hint: {reasoning.get('body_hint', False)}")
            print(f"    • Fallback Used: {reasoning.get('fallback_used', False)}")

        print(f"\n✅ FOUND {len(commitments)} COMMITMENT(S):")

        for idx, c in enumerate(commitments, 1):
            print(f"\n{'─'*80}")
            print(f"🎯 COMMITMENT #{idx}")
            print(f"{'─'*80}")
            print(f"  What: {c.get('what', '')}")
            print(f"  To Whom: {c.get('to_whom', '')}")
            print(f"  Given By: {c.get('given_by', '')}")
            print(f"  Direction: {ai_result.get('direction', 'incoming')}")  # PHASE 4A
            print(f"  Assigned to Me: {c.get('assigned_to_me', False)}")  # PHASE 4A
            print(f"  ")
            print(f"  Deadline (raw): {c.get('deadline_raw', 'N/A')}")
            print(f"  Deadline (ISO): {c.get('deadline_iso', 'None')}")
            print(f"  ")
            print(f"  Priority: {c.get('priority', 'medium')}")
            print(f"  Commitment Type: {c.get('commitment_type', 'deliverable')}")
            print(f"  Estimated Hours: {c.get('estimated_hours', 0)}")
            print(f"  Confidence: {c.get('confidence', 0.0):.2f}")
            print(f"  ")
            print(f"  Status: {c.get('status', 'no_deadline')}")
            print(f"  Days Overdue: {c.get('days_overdue', 0)}")
            print(f"  Overdue Flag: {c.get('overdue_flag', False)}")
            print(f"  Completed: {c.get('completed', False)}")

            # Build complete Firestore document (FLATTENED - Option B)
            # Flatten classification_details into individual fields
            classification_details = classification.get("reasoning", {})
            
            commitment_doc = {
                # Top-level extraction fields
                "has_commitment": True,
                "direction": ai_result.get("direction", "incoming"),
                "assigned_to_me": c.get("assigned_to_me", False),
                "summary": ai_result.get("summary", ""),
                
                # Commitment ID and user
                "commitment_id": None,  # will be set by save function
                "user_id": user_id,
                
                # Core commitment fields
                "what": c.get("what", ""),
                "to_whom": c.get("to_whom", ""),
                "given_by": c.get("given_by", ""),
                "deadline_raw": c.get("deadline_raw", ""),
                "deadline_iso": c.get("deadline_iso"),
                "status": c.get("status", "no_deadline"),
                "completed": c.get("completed", False),
                "completed_at": c.get("completed_at"),
                "days_overdue": c.get("days_overdue", 0),
                "overdue_flag": c.get("overdue_flag", False),
                "priority": c.get("priority", "medium"),
                "commitment_type": c.get("commitment_type", "deliverable"),
                "confidence": c.get("confidence", 0.0),
                "estimated_hours": c.get("estimated_hours", 2),
                
                # Email metadata (flattened)
                "message_id": email_json.get("message_id"),
                "email_subject": email_json.get("subject"),
                "email_sender": email_json.get("sender"),
                "email_sender_name": email_json.get("sender_name"),
                "email_date": email_json.get("date"),
                "source_email_folder": email_json.get("folder", "INBOX"),
                
                # Classification (flattened)
                "sender_role": classification.get("sender_role", "unknown"),
                "classification_confidence": classification.get("confidence", 0.0),
                "classification_domain_match": classification_details.get("domain_match", False),
                "classification_domain": classification_details.get("domain", ""),
                "classification_signature_match": classification_details.get("signature_match", False),
                "classification_subject_hint": classification_details.get("subject_hint", False),
                "classification_body_hint": classification_details.get("body_hint", False),
                "classification_fallback_used": classification_details.get("fallback_used", False),
                
                # Timestamps
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }

            print(f"\n📦 FIRESTORE DOCUMENT (ready to save):")
            print(json.dumps(commitment_doc, indent=2, default=str))

            # Save to Firestore
            try:
                saved = save_commitment_to_firestore(user_id, commitment_doc)
                if saved:
                    print(f"\n  ✅ Saved to Firestore with ID: {saved}")
                    commitment_count += 1
                else:
                    print(f"\n  ❌ Failed to save to Firestore")
            except Exception as e:
                print(f"\n  ❌ Failed to save to Firestore: {e}")
            
            print("\n" + "-"*80 + "\n")

    return valid_count, commitment_count


# --------------------------
# Main integration function
# --------------------------

def run_initial_sync(user_id: str, gmail_connected_at: datetime, openai_api_key: Optional[str] = None):
    """
    Runs initial sync for given user_id using the exact window around gmail_connected_at.
    PHASE 4A: Fetches from both INBOX and SENT folders.
    Prints formatted output for testing.
    """
    from credit_engine import initialize_credits_if_missing
    initialize_credits_if_missing(user_id)

    print(f"\n{'='*80}")
    print(f"🚀 STARTING INITIAL SYNC (PHASE 4A: INBOX + SENT)")
    print(f"{'='*80}")
    print(f"User ID: {user_id}")
    print(f"Gmail Connected At: {gmail_connected_at}")
    print(f"{'='*80}\n")

    from_time = gmail_connected_at - timedelta(days=2)
    to_time = gmail_connected_at

    print(f"📅 Sync Window: {from_time.strftime('%Y-%m-%d %H:%M:%S')} → {to_time.strftime('%Y-%m-%d %H:%M:%S')}")

    query = build_query(from_time, to_time)
    print(f"🔍 Gmail Query: {query}\n")

    composio = Composio(api_key=COMPOSIO_API_KEY, toolkit_versions={"gmail": TOOLKIT_VERSION})

    # Get founder's email (for SENT emails)
    founder_email = None
    try:
        # Try to get from Firestore user profile
        from firebase_admin import firestore
        db = firestore.client()
        user_doc = db.collection("users").document(user_id).get()
        if user_doc.exists:
            founder_email = user_doc.to_dict().get("email")
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════
    # STEP 1: FETCH AND PROCESS INBOX EMAILS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("📥 STEP 1: FETCHING INBOX EMAILS")
    print("="*80)

    inbox_messages: List[Dict[str, Any]] = []
    page_token = None
    fetched_count = 0

    print("📬 Fetching emails from INBOX...")

    while True:
        if fetched_count >= MAX_COUNT_INBOX:
            break

        try:
            resp = composio.tools.execute(
                slug="GMAIL_FETCH_EMAILS",
                user_id=user_id,
                arguments={
                    "label_ids": ["INBOX", "CATEGORY_PRIMARY"],
                    "q": query,
                    "include_payload": True,
                    "max_results": BATCH_SIZE,
                    "page_token": page_token,
                    "include_spam_trash": False,
                    "ids_only": False,
                },
                version=TOOLKIT_VERSION
            )
        except Exception as exc:
            print(f"❌ Gmail API error: {exc}")
            time.sleep(2)
            break

        data = resp.get("data", {}) or {}
        msgs = data.get("messages", []) or []

        for m in msgs:
            if fetched_count < MAX_COUNT_INBOX:
                inbox_messages.append(m)
                fetched_count += 1

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    print(f"✅ Fetched {len(inbox_messages)} INBOX emails\n")

    # Process INBOX emails
    inbox_valid, inbox_commitments = process_email_batch(
        messages=inbox_messages,
        user_id=user_id,
        from_time=from_time,
        to_time=to_time,
        folder="INBOX",
        founder_email=founder_email,
        openai_api_key=openai_api_key,
        apply_newsletter_filter=True  # Filter newsletters for INBOX
    )

    # ═══════════════════════════════════════════════════════════════
    # STEP 2: FETCH AND PROCESS SENT EMAILS (PHASE 4A - NEW)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "="*80)
    print("📤 STEP 2: FETCHING SENT EMAILS (PHASE 4A)")
    print("="*80)

    sent_messages: List[Dict[str, Any]] = []
    page_token = None
    fetched_count = 0

    print("📬 Fetching emails from SENT...")

    while True:
        if fetched_count >= MAX_COUNT_SENT:
            break

        try:
            resp = composio.tools.execute(
                slug="GMAIL_FETCH_EMAILS",
                user_id=user_id,
                arguments={
                    "label_ids": ["SENT"],  # PHASE 4A: SENT instead of INBOX
                    "q": query,
                    "include_payload": True,
                    "max_results": BATCH_SIZE,
                    "page_token": page_token,
                    "include_spam_trash": False,
                    "ids_only": False,
                },
                version=TOOLKIT_VERSION
            )
        except Exception as exc:
            print(f"❌ Gmail API error (SENT): {exc}")
            time.sleep(2)
            break

        data = resp.get("data", {}) or {}
        msgs = data.get("messages", []) or []

        for m in msgs:
            if fetched_count < MAX_COUNT_SENT:
                sent_messages.append(m)
                fetched_count += 1

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    print(f"✅ Fetched {len(sent_messages)} SENT emails\n")

    # Process SENT emails
    sent_valid, sent_commitments = process_email_batch(
        messages=sent_messages,
        user_id=user_id,
        from_time=from_time,
        to_time=to_time,
        folder="SENT",
        founder_email=founder_email,
        openai_api_key=openai_api_key,
        apply_newsletter_filter=False  # Don't filter newsletters for SENT
    )

    # ═══════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print(f"🎉 INITIAL SYNC COMPLETE (PHASE 4A)")
    print(f"{'='*80}")
    print(f"📊 FINAL STATISTICS:")
    print(f"  ")
    print(f"  INBOX:")
    print(f"    • Emails fetched: {len(inbox_messages)}")
    print(f"    • Emails with commitments: {inbox_valid}")
    print(f"    • Commitments found: {inbox_commitments}")
    print(f"  ")
    print(f"  SENT:")
    print(f"    • Emails fetched: {len(sent_messages)}")
    print(f"    • Emails with commitments: {sent_valid}")
    print(f"    • Commitments found: {sent_commitments}")
    print(f"  ")
    print(f"  TOTAL:")
    print(f"    • Total emails: {len(inbox_messages) + len(sent_messages)}")
    print(f"    • Total commitments: {inbox_commitments + sent_commitments}")
    print(f"  ")
    print(f"  • Time window: {from_time.strftime('%Y-%m-%d %H:%M')} → {to_time.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*80}\n")

    return {
        "initial_sync_completed": True,
        "user_id": user_id,
        "inbox_count": inbox_commitments,
        "sent_count": sent_commitments
    }

# Development test harness
if __name__ == "__main__":
      # Example dev user id
    DEV_USER_ID = os.getenv("DEV_USER_ID", "H42RstkmuUOfUIDCgIwt8upzT6g1")
    run_initial_sync(DEV_USER_ID, datetime.now(timezone))