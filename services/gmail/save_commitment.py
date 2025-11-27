# service/gmail/save_commitment.py
"""
Save commitment documents to Firestore under users/{uid}/commitments/{commitment_id}.

Behavior:
- Generate unique commitment_id using random UUID
- Each commitment gets a unique ID (no deduplication)
- If document exists (by ID), update 'updated_at' and merge fields
- Return saved document id or False on failure

Requires firebase_admin Firestore initialized in main.py
"""
from __future__ import annotations
from datetime import datetime, timezone
import uuid
from typing import Dict, Any

from firebase_admin import firestore


def _make_commitment_id() -> str:
    """Generate a unique commitment ID using random UUID."""
    unique_id = uuid.uuid4().hex[:16]  # 16 random hex characters
    return f"commitment_{unique_id}"


def save_commitment_to_firestore(user_id: str, doc: Dict[str, Any]) -> str | bool:
    """
    Save or update commitment document under users/{uid}/commitments/{commitment_id}.
    Returns the commitment_id on success, False on failure.
    """
    db = firestore.client()

    # Generate unique commitment ID
    commitment_id = _make_commitment_id()
    doc_ref = db.collection("users").document(user_id).collection("commitments").document(commitment_id)

    now_iso = datetime.now(timezone.utc).isoformat()

    # Set commitment ID and timestamps
    doc["commitment_id"] = commitment_id
    doc["created_at"] = now_iso
    doc["updated_at"] = now_iso

    try:
        # Always create new document (no deduplication with random IDs)
        doc_ref.set(doc)
        return commitment_id
    except Exception as e:
        print(f"Firestore save error: {e}")
        return False