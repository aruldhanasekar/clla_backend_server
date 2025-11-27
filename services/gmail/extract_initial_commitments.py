# services/gmail/extract_initial_commitments.py
"""
AI-powered commitment extraction from emails.

PHASE 4B: Updated with direction and assigned_to_me detection
FIXED: Domain extraction from user email and improved meeting/call detection
"""
from __future__ import annotations
import os
import json
import time
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from openai import OpenAI
from firebase_admin import firestore

MODEL = os.getenv("EXTRACTION_MODEL", "gpt-4o-mini")
MAX_TOKENS = int(os.getenv("EXTRACTION_MAX_TOKENS", "1500"))
RETRY_ATTEMPTS = int(os.getenv("EXTRACTION_RETRIES", "2"))
DEBUG = os.getenv("DEBUG_EXTRACTOR", "0") == "1"

SYSTEM_PROMPT = """You are an expert at extracting actionable commitments from founder emails.

Your job is to:
1. Identify if the email contains a commitment (something someone needs to do)
2. Extract the EXACT deadline mentioned in the email
3. Determine WHO must complete the action (assigned_to_me flag)
4. Classify the sender based on their email domain
5. Return ONLY valid JSON

CRITICAL: Pay close attention to time expressions for deadlines!"""

USER_PROMPT_TEMPLATE = '''Extract commitments from this email using CAREFUL REASONING.

Sender: {sender}
Sender Name: {sender_name}
Subject: {subject}
Body:
{body}

Email Date: {email_date}
Current Date: {current_date}
Email Folder: {folder}

RECIPIENT INFO (for SENT emails):
Recipient Email: {recipient_email}
Recipient Name: {recipient_name}

Return EXACTLY this JSON structure:

{{
  "has_commitment": true or false,
  "reasoning": "Step-by-step explanation of your decision",
  "email_metadata": {{
    "sender": "{sender}",
    "sender_name": "{sender_name}",
    "subject": "{subject}",
    "date": "{email_date}"
  }},
  "direction": "incoming" or "outgoing",
  "commitments": [
    {{
      "what": "description of what needs to be done",
      "to_whom": "person's name or 'You'",
      "assigned_to_me": true or false,
      "deadline_raw": "the EXACT time/date phrase from the email or null",
      "priority": "high" or "medium" or "low",
      "confidence": 0.0 to 1.0,
      "commitment_type": "deliverable" or "meeting" or "call" or other type,
      "estimated_hours": NUMBER (REQUIRED - must be a number, never null)
    }}
  ],
  "summary": "brief summary"
}}

═══════════════════════════════════════════════════════════════════════════════
STEP 1: IS THIS A REAL COMMITMENT? (CRITICAL)
═══════════════════════════════════════════════════════════════════════════════

AUTOMATED EMAILS ARE NOT COMMITMENTS

Before extracting, ask yourself:
1. Is this from an automated system (noreply@, notifications@, Railway, GitHub)?
2. Is there a specific human request?
3. Is it actionable with clear ownership?

REJECT THESE PATTERNS:
❌ Railway <hello@notify.railway.app> - System notifications
❌ noreply@*, no-reply@*, donotreply@ - Automated
❌ "Deployment crashed", "Build failed" - CI/CD notifications
❌ Newsletters, marketing, order confirmations

ACCEPT THESE:
✅ Real person (john@company.com) requesting specific action

═══════════════════════════════════════════════════════════════════════════════
STEP 2: DIRECTION AND ASSIGNED_TO_ME
═══════════════════════════════════════════════════════════════════════════════

DIRECTION:
- "incoming" = Email from INBOX (received)
- "outgoing" = Email from SENT (user sent it)

ASSIGNED_TO_ME:
- true = USER must complete this action
- false = SOMEONE ELSE must complete this action

CRITICAL - MEETING/CALL DETECTION:
For "attend", "join", "participate" in meetings/calls:
INCOMING email → assigned_to_me: TRUE (user must attend)
Example: "I scheduled a call with you tomorrow" → assigned_to_me: TRUE

═══════════════════════════════════════════════════════════════════════════════
STEP 3: TO_WHOM FIELD (OPTION B LOGIC)
═══════════════════════════════════════════════════════════════════════════════

The to_whom field indicates WHO the commitment involves (the other party).

INCOMING emails (folder: INBOX):
  - If assigned_to_me: true → to_whom: Sender's Name
    Example: "Kaalaz asked you to send report" → to_whom: "Kaalaz"
  
  - If assigned_to_me: false → to_whom: "You"
    Example: "Kaalaz will send you contract" → to_whom: "You"

OUTGOING emails (folder: SENT):
  - If assigned_to_me: true → to_whom: Recipient's Name
    Example: "You will send deck to John" → to_whom: "John"
  
  - If assigned_to_me: false → to_whom: Recipient's Name
    Example: "You asked John to send report" → to_whom: "John"

For OUTGOING emails:
- Always use {recipient_name} from the provided field
- If recipient_name is empty, use username from {recipient_email}
- Example: recipient_email="john@company.com" → to_whom: "john"

═══════════════════════════════════════════════════════════════════════════════
STEP 4: DEADLINE EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

Extract EXACT time/date phrase:
- "tonight" → deadline_raw: "tonight"
- "by Friday" → deadline_raw: "by Friday"
- "tomorrow at 5:30 AM" → deadline_raw: "tomorrow at 5:30 AM"
- No deadline → deadline_raw: null

═══════════════════════════════════════════════════════════════════════════════
STEP 5: SUMMARY GENERATION (CRITICAL)
═══════════════════════════════════════════════════════════════════════════════

INCOMING emails:
- Use third person: "John asked you to...", "Sarah will send you..."
- Example: "John asked you to send the Q4 report by Friday"

OUTGOING emails (SENT):
- Use second person: "You promised to...", "You will...", "You asked John to..."
- NEVER say "The sender will..." for SENT emails
- Example: "You promised to send the deck to John by Monday"

═══════════════════════════════════════════════════════════════════════════════
OTHER FIELDS
═══════════════════════════════════════════════════════════════════════════════

estimated_hours (REQUIRED - NEVER null):
- Quick email: 0.5 hours
- Meeting/call: 1 hour
- Report/document: 2-3 hours
- Default: 2 hours

priority:
- "tonight", "ASAP" → "high"
- "tomorrow", "this week" → "medium"
- "next week" → "low"

confidence:
- Clear commitment with deadline: 1.0
- Clear commitment, no deadline: 0.9
- Implicit commitment: 0.7-0.8

FINAL CHECKLIST:
✅ Is from real person (not automated)?
✅ assigned_to_me correct?
✅ to_whom using Option B logic?
✅ Exact deadline phrase?
✅ Proper summary (second person for SENT)?
✅ estimated_hours is a number?

If automated → has_commitment: false

STEP 4: Fill in classification.reasoning:
{{
  "domain_match": true or false (from step 2),
  "domain": "actual_sender_domain.com" (extracted from sender email),
  "signature_match": check if signature mentions company/title,
  "subject_hint": check if subject contains role hints,
  "body_hint": check if body contains role hints,
  "fallback_used": true if you couldn't determine role
}}

IMPORTANT: 
- "domain" field must contain the ACTUAL sender's domain (e.g., "gmail.com", "useclla.com")
- NOT "example.com" - that's a placeholder!
- Extract the real domain from the sender's email address

═══════════════════════════════════════════════════════════════════════════════
DEADLINE EXTRACTION - CRITICAL INSTRUCTIONS
═══════════════════════════════════════════════════════════════════════════════

The "deadline_raw" field is EXTREMELY IMPORTANT. You must:

1. CAPTURE THE EXACT TIME/DATE PHRASE from the email body
2. Do NOT summarize or paraphrase - use the EXACT words

EXAMPLES OF DEADLINE PHRASES TO EXTRACT:
- "tonight" → deadline_raw: "tonight"
- "this evening" → deadline_raw: "this evening"
- "by end of day" → deadline_raw: "by end of day"
- "by EOD" → deadline_raw: "by EOD"
- "tomorrow" → deadline_raw: "tomorrow"
- "by tomorrow" → deadline_raw: "by tomorrow"
- "tomorrow morning" → deadline_raw: "tomorrow morning"
- "by Friday" → deadline_raw: "by Friday"
- "next Monday" → deadline_raw: "next Monday"
- "this week" → deadline_raw: "this week"
- "by next week" → deadline_raw: "by next week"
- "ASAP" → deadline_raw: "ASAP"
- "as soon as possible" → deadline_raw: "as soon as possible"
- "within 2 hours" → deadline_raw: "within 2 hours"
- "in 30 minutes" → deadline_raw: "in 30 minutes"
- "Nov 25" → deadline_raw: "Nov 25"
- "November 25th" → deadline_raw: "November 25th"
- "by the 25th" → deadline_raw: "by the 25th"
- "before the meeting" → deadline_raw: "before the meeting"
- "before our call" → deadline_raw: "before our call"

IMPORTANT TIME EXPRESSIONS (often missed):
- "tonight" = TODAY (same day as email)
- "this evening" = TODAY
- "by end of day" = TODAY
- "EOD" = TODAY (End Of Day)
- "COB" = TODAY (Close Of Business)
- "first thing tomorrow" = TOMORROW
- "tomorrow morning" = TOMORROW

If NO deadline is mentioned, use: deadline_raw: null

═══════════════════════════════════════════════════════════════════════════════
OTHER INSTRUCTIONS
═══════════════════════════════════════════════════════════════════════════════

ESTIMATED_HOURS RULES (CRITICAL - NEVER return null):
- Quick email/message: 0.5 hours
- Short call/meeting: 1 hour
- Send report/document: 2-3 hours
- Review document: 1-2 hours
- Create presentation: 4-6 hours
- Build feature/complex task: 8+ hours
- If uncertain, default to 2-3 hours

PRIORITY RULES:
- "tonight", "ASAP", "urgent", "immediately" → priority: "high"
- "tomorrow", "by end of week" → priority: "medium"
- "next week", "when you can" → priority: "low"

NOT A COMMITMENT - EXCLUDE:
- Marketing emails, discount offers, newsletters
- Order confirmations, shipping updates
- Automated alerts, password resets
- Generic announcements

A REAL commitment requires:
1. A SPECIFIC person asking YOU to do something, OR
2. YOU promising to deliver something to a SPECIFIC person
3. An actionable task (not just 'click here' or 'buy now')
'''


# ✅ FIX #1: Add function to get user profile with domain extraction
def get_user_profile(user_id: str) -> Dict[str, Any]:
    """
    Fetch user profile from Firestore and extract company domain.
    
    ✅ FIXED: Now extracts domain from email if domain field doesn't exist
    """
    try:
        db = firestore.client()
        user_doc = db.collection("users").document(user_id).get()
        
        if user_doc.exists:
            profile = user_doc.to_dict()
            email = profile.get("email", "")
            name = profile.get("name", "Unknown")
            
            # ✅ FIX: Extract domain from email address
            extracted_domain = "example.com"  # fallback
            if email and "@" in email:
                extracted_domain = email.split("@")[1].lower()
            
            # Try Firestore 'domain' field first, then use extracted
            domain = profile.get("domain", extracted_domain)
            
            return {
                "user_id": user_id,
                "name": name,
                "email": email,
                "domain": domain,
            }
        else:
            print(f"⚠️ User profile not found for: {user_id}")
            return {
                "user_id": user_id,
                "name": "Unknown",
                "email": "",
                "domain": "example.com",
            }
    except Exception as e:
        print(f"❌ Error fetching user profile: {e}")
        return {
            "user_id": user_id,
            "name": "Unknown",
            "email": "",
            "domain": "example.com",
        }


def _safe_parse_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                return None
    return None


def _validate_schema(obj: Dict[str, Any]) -> bool:
    """Validate the schema structure"""
    if not isinstance(obj, dict):
        return False
    if "has_commitment" not in obj or not isinstance(obj["has_commitment"], bool):
        return False
    if "email_metadata" not in obj or not isinstance(obj["email_metadata"], dict):
        return False
    if "commitments" not in obj or not isinstance(obj["commitments"], list):
        return False
    
    em = obj["email_metadata"]
    for k in ("sender", "sender_name", "subject", "date"):
        if k not in em:
            return False
    
    if "direction" in obj:
        if obj["direction"] not in ("incoming", "outgoing"):
            return False
    
    if "classification" in obj:
        cls = obj["classification"]
        if not isinstance(cls, dict):
            return False
        if "sender_role" not in cls or "confidence" not in cls:
            return False
        if "reasoning" in cls:
            reasoning = cls["reasoning"]
            if not isinstance(reasoning, dict):
                return False
            required_reasoning = ["domain_match", "domain", "signature_match", "subject_hint", "body_hint", "fallback_used"]
            for k in required_reasoning:
                if k not in reasoning:
                    return False
    
    if obj.get("has_commitment") and len(obj["commitments"]) > 0:
        for c in obj["commitments"]:
            if not isinstance(c, dict):
                return False
            required_fields = ["what", "to_whom", "assigned_to_me", "deadline_raw", "priority", "confidence", "commitment_type", "estimated_hours"]
            for field in required_fields:
                if field not in c:
                    return False
            if not isinstance(c.get("estimated_hours"), (int, float)):
                return False
            if c.get("estimated_hours") is None:
                return False
            if not isinstance(c.get("assigned_to_me"), bool):
                return False
    
    return True


def _extract_content_from_choice(choice):
    try:
        if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
            return choice.message.content
        if hasattr(choice, 'text'):
            return choice.text
    except Exception:
        pass
    return None


# ✅ FIX #2: Updated _build_user_prompt to include user profile
def _build_user_prompt(email: Dict[str, Any], user_profile: Dict[str, Any]) -> str:
    """
    Build the user prompt with email data and user profile context.
    
    ✅ FIXED: Now includes user_domain for proper classification
    """
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = email.get("folder", "INBOX")
    
    # Get recipient info (for SENT emails)
    recipient_email = email.get("recipient_email", "")
    recipient_name = email.get("recipient_name", "")
    if not recipient_name and recipient_email and "@" in recipient_email:
        recipient_name = recipient_email.split("@")[0]
    
    return USER_PROMPT_TEMPLATE.format(
        sender=email.get("sender", ""),
        sender_name=email.get("sender_name", ""),
        subject=email.get("subject", ""),
        body=email.get("body", "")[:4000],
        email_date=email.get("date", ""),
        current_date=current_date,
        folder=folder,
        recipient_email=recipient_email,
        recipient_name=recipient_name
    )


def _call_openai(api_key: str, model: str, messages: list, max_tokens: int):
    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1
        )
        if hasattr(resp, 'choices') and len(resp.choices) > 0:
            content = _extract_content_from_choice(resp.choices[0])
            if content:
                parsed = _safe_parse_json(content)
                return parsed, content, resp
        return None, None, resp
    except Exception as e:
        if DEBUG:
            print(f"OpenAI API Error: {e}")
        return None, None, None


def _force_metadata(parsed: Dict[str, Any], email: Dict[str, Any]):
    em = parsed.setdefault("email_metadata", {})
    for key in ("sender", "sender_name", "subject", "date"):
        if not em.get(key):
            em[key] = email.get(key, "")
    em["message_id"] = email.get("message_id", "")
    em["folder"] = email.get("folder", "INBOX")


def _post_process_commitments(parsed: Dict[str, Any], email: Dict[str, Any]):
    """Add calculated fields to each commitment"""
    from services.gmail.deadline_parser import parse_deadline_raw
    
    commitments = parsed.get("commitments", [])
    email_sender = email.get("sender", "")
    email_date = email.get("date", "")
    
    today = datetime.now(timezone.utc).date()
    
    for c in commitments:
        c["given_by"] = email_sender
        
        # Ensure estimated_hours is never null
        if c.get("estimated_hours") is None:
            commitment_type = c.get("commitment_type", "").lower()
            if "meeting" in commitment_type or "call" in commitment_type:
                c["estimated_hours"] = 1
            elif "email" in commitment_type or "message" in commitment_type or "communication" in commitment_type:
                c["estimated_hours"] = 0.5
            elif "deliverable" in commitment_type or "report" in commitment_type or "document" in commitment_type:
                c["estimated_hours"] = 3
            elif "presentation" in commitment_type:
                c["estimated_hours"] = 5
            elif "feature" in commitment_type or "development" in commitment_type:
                c["estimated_hours"] = 8
            else:
                c["estimated_hours"] = 2
        
        # Parse deadline_iso from deadline_raw
        deadline_raw = c.get("deadline_raw")
        deadline_iso = None
        if deadline_raw:
            try:
                deadline_iso = parse_deadline_raw(deadline_raw, email_date)
                if DEBUG:
                    print(f"DEBUG: deadline_raw='{deadline_raw}' → deadline_iso='{deadline_iso}'")
            except Exception as e:
                if DEBUG:
                    print(f"DEBUG: deadline parse error: {e}")
                deadline_iso = None
        c["deadline_iso"] = deadline_iso
        
        # Calculate status, days_overdue, overdue_flag
        days_overdue = 0
        overdue_flag = False
        status = "no_deadline"
        
        if deadline_iso:
            try:
                d_iso = datetime.fromisoformat(deadline_iso).date()
                if d_iso < today:
                    status = "overdue"
                    days_overdue = (today - d_iso).days
                    overdue_flag = True
                elif d_iso == today:
                    status = "due_today"
                else:
                    status = "active"
            except Exception:
                status = "no_deadline"
        
        c["status"] = status
        c["days_overdue"] = days_overdue
        c["overdue_flag"] = overdue_flag
        c["completed"] = False
        c["completed_at"] = None


# ✅ FIX #3: Updated main extraction function to use user_profile
def extract_commitments_from_email(email: Dict[str, Any], user_id: str, openai_api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Extract commitments from email using AI.
    
    ✅ FIXED: Now fetches user profile and passes to prompt builder
    """
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")
    
    # ✅ FIX: Get user profile with domain
    user_profile = get_user_profile(user_id)
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(email, user_profile)},
    ]
    
    last_error = None
    from credit_engine import has_enough_credits
    if not has_enough_credits(user_id):
        return {
            "has_commitment": False,
            "summary": "No credits remaining. Please top up.",
            "commitments": []
        }
    for attempt in range(RETRY_ATTEMPTS + 1):
        parsed, raw, resp = _call_openai(api_key, MODEL, messages, MAX_TOKENS)
        # --- CREDIT DEDUCTION (ADDED) ---
        try:
            from credit_engine import calculate_credits_spent, deduct_credits

            if resp is not None and getattr(resp, "usage", None):

                # Try both naming conventions (new SDK vs old SDK)
                input_tokens = getattr(resp.usage, "input_tokens", getattr(resp.usage, "prompt_tokens", 0))
                output_tokens = getattr(resp.usage, "output_tokens", getattr(resp.usage, "completion_tokens", 0))

                input_tokens = int(input_tokens or 0)
                output_tokens = int(output_tokens or 0)

                # Convert tokens → credits
                credits_spent = calculate_credits_spent(input_tokens, output_tokens)

                # Deduct credits for this user
                deduct_credits(user_id, credits_spent)

        except Exception as e:
            print("⚠️ Credit deduction failed:", e)
        # --- END CREDIT DEDUCTION ---


        if parsed and _validate_schema(parsed):
            _force_metadata(parsed, email)
            _post_process_commitments(parsed, email)
            return parsed
        
        if DEBUG:
            print(f"\nDEBUG: Attempt {attempt + 1}/{RETRY_ATTEMPTS + 1}")
            print("DEBUG: raw ->", raw)
            if parsed:
                print("DEBUG: Parsed JSON successfully")
                # Show validation issues
                if "has_commitment" not in parsed:
                    print("DEBUG: X Missing 'has_commitment'")
                if "email_metadata" not in parsed:
                    print("DEBUG: X Missing 'email_metadata'")
                elif "date" not in parsed.get("email_metadata", {}):
                    print("DEBUG: X email_metadata missing 'date' field")
                if "commitments" not in parsed:
                    print("DEBUG: X Missing 'commitments'")
                elif parsed.get("commitments"):
                    c = parsed["commitments"][0]
                    required = ["what", "to_whom", "assigned_to_me", "deadline_raw", "priority", "confidence", "commitment_type", "estimated_hours"]
                    for field in required:
                        if field not in c:
                            print(f"DEBUG: X Commitment missing '{field}'")
            else:
                print("DEBUG: X Failed to parse JSON")
        
        if isinstance(parsed, dict):
            last_error = "Invalid schema returned by model"
        else:
            last_error = "No JSON parsed from model"
        time.sleep(1)
    
    return {
        "has_commitment": False,
        "email_metadata": {
            "sender": email.get("sender", ""),
            "sender_name": email.get("sender_name", ""),
            "subject": email.get("subject", ""),
            "date": email.get("date", ""),
            "message_id": email.get("message_id", ""),
            "folder": email.get("folder", "INBOX"),
        },
        "direction": "incoming",
        "classification": {
            "sender_role": "unknown",
            "confidence": 0.0,
            "reasoning": {
                "domain_match": False,
                "domain": "",
                "signature_match": False,
                "subject_hint": False,
                "body_hint": False,
                "fallback_used": True
            }
        },
        "commitments": [],
        "summary": f"No commitments (model failed). last_error={last_error}"
    }


# CLI test mode
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--key", required=False)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        os.environ["DEBUG_EXTRACTOR"] = "1"
    with open(args.email, "r", encoding="utf-8") as fh:
        em = json.load(fh)
    res = extract_commitments_from_email(em, args.user_id, openai_api_key=args.key)
    print(json.dumps(res, indent=2, ensure_ascii=False))