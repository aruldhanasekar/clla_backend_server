# services/gmail/commitments/fetcher.py
"""
Main commitment fetcher service.

Fetches commitments from Firestore, recalculates status,
applies filters, categorizes, and returns structured results.

Usage:
    from services.commitments.fetcher import fetch_commitments
    from services.commitments.filters import CommitmentFilters
    
    # Fetch all active
    result = fetch_commitments(user_id="xxx")
    
    # Fetch with filters
    result = fetch_commitments(
        user_id="xxx",
        filters=CommitmentFilters(status=["overdue"], sender_role=["investor"])
    )
"""

from __future__ import annotations
import os
from datetime import date, datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

# Firebase import - optional for testing
try:
    from firebase_admin import firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    firestore = None

from .filters import CommitmentFilters
from .models import (
    CommitmentItem,
    CommitmentSummary,
    CommitmentResult,
    create_empty_result,
)
from .status_calculator import (
    recalculate_status,
    categorize_by_deadline,
    sort_commitments,
)


# Configuration
UPCOMING_DAYS = int(os.getenv("COMMITMENT_UPCOMING_DAYS", "7"))
DEFAULT_LIMIT = int(os.getenv("COMMITMENT_DEFAULT_LIMIT", "100"))


def fetch_commitments(
    user_id: str,
    filters: Optional[CommitmentFilters] = None,
    db=None,
) -> CommitmentResult:
    """
    Fetch and filter user's commitments from Firestore.
    
    Args:
        user_id: Firebase user ID
        filters: Optional filter configuration (defaults to all active)
        db: Optional Firestore client (for testing)
    
    Returns:
        CommitmentResult with categorized commitments and summary
    """
    if filters is None:
        filters = CommitmentFilters()
    
    if db is None:
        if not FIREBASE_AVAILABLE:
            raise RuntimeError("Firebase not available. Use mock_fetch_commitments for testing.")
        db = firestore.client()
    
    today = datetime.now(timezone.utc).date()
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 1: Base Firestore Query
    # ═══════════════════════════════════════════════════════════════
    collection_ref = db.collection("users").document(user_id).collection("commitments")
    
    # Build query based on completion status
    if filters.only_completed:
        query = collection_ref.where("completed", "==", True)
    elif filters.include_completed:
        query = collection_ref  # No filter on completed
    else:
        query = collection_ref.where("completed", "==", False)
    
    # Apply limit
    query = query.limit(filters.limit or DEFAULT_LIMIT)
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 2: Execute Query
    # ═══════════════════════════════════════════════════════════════
    try:
        docs = query.stream()
        raw_commitments = [doc.to_dict() for doc in docs]
    except Exception as e:
        print(f"❌ Firestore query failed: {e}")
        return create_empty_result(
            query_description="Failed to fetch commitments",
            filters_applied=filters.to_dict(),
            user_id=user_id,
            filter_type="general"
        )
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 3: Recalculate Status for Each Commitment
    # ═══════════════════════════════════════════════════════════════
    for c in raw_commitments:
        recalculate_status(c, today)
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 4: Apply Python Filters
    # ═══════════════════════════════════════════════════════════════
    filtered = apply_filters(raw_commitments, filters, today)
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 5: Sort Results
    # ═══════════════════════════════════════════════════════════════
    sorted_commitments = sort_commitments(
        filtered,
        sort_by=filters.sort_by,
        sort_order=filters.sort_order
    )
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 6: Categorize Results
    # ═══════════════════════════════════════════════════════════════
    categorized = categorize_commitments(sorted_commitments, today)
    
    # ═══════════════════════════════════════════════════════════════
    # STEP 7: Build Response
    # ═══════════════════════════════════════════════════════════════
    query_description = filters.describe()
    
    # Handle empty results
    if not sorted_commitments:
        filter_type = determine_filter_type(filters)
        return create_empty_result(
            query_description=query_description,
            filters_applied=filters.to_dict(),
            user_id=user_id,
            filter_type=filter_type
        )
    
    # Convert to CommitmentItem objects
    all_items = [CommitmentItem.from_firestore(c) for c in sorted_commitments]
    
    # Build summary
    summary = CommitmentSummary(
        total=len(sorted_commitments),
        overdue=len(categorized["overdue"]),
        due_today=len(categorized["due_today"]),
        upcoming=len(categorized["upcoming"]),
        later=len(categorized["later"]),
        no_deadline=len(categorized["no_deadline"]),
        completed=len(categorized["completed"]),
    )
    
    return CommitmentResult(
        query_description=query_description,
        filters_applied=filters.to_dict(),
        total_found=len(sorted_commitments),
        summary=summary,
        overdue=[CommitmentItem.from_firestore(c) for c in categorized["overdue"]],
        due_today=[CommitmentItem.from_firestore(c) for c in categorized["due_today"]],
        upcoming=[CommitmentItem.from_firestore(c) for c in categorized["upcoming"]],
        later=[CommitmentItem.from_firestore(c) for c in categorized["later"]],
        no_deadline=[CommitmentItem.from_firestore(c) for c in categorized["no_deadline"]],
        completed=[CommitmentItem.from_firestore(c) for c in categorized["completed"]],
        all_commitments=all_items,
        user_id=user_id,
    )


def apply_filters(
    commitments: List[Dict[str, Any]],
    filters: CommitmentFilters,
    today: date
) -> List[Dict[str, Any]]:
    """
    Apply Python-side filters to commitments.
    
    Args:
        commitments: Raw commitment dicts from Firestore
        filters: Filter configuration
        today: Current date for comparisons
    
    Returns:
        Filtered list of commitments
    """
    result = commitments
    
    # ─────────────────────────────────────────────────────────────────
    # Status filter
    # ─────────────────────────────────────────────────────────────────
    if filters.status:
        result = [c for c in result if c.get("status") in filters.status]
    
    # ─────────────────────────────────────────────────────────────────
    # Sender email filter (partial match, case-insensitive)
    # ─────────────────────────────────────────────────────────────────
    if filters.sender_email:
        search = filters.sender_email.lower()
        result = [
            c for c in result
            if search in (c.get("email_sender") or "").lower()
            or search in (c.get("given_by") or "").lower()
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Sender name filter (partial match, case-insensitive)
    # ─────────────────────────────────────────────────────────────────
    if filters.sender_name:
        search = filters.sender_name.lower()
        result = [
            c for c in result
            if search in (c.get("email_sender_name") or "").lower()
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Sender role filter
    # ─────────────────────────────────────────────────────────────────
    if filters.sender_role:
        roles = [r.lower() for r in filters.sender_role]
        result = [
            c for c in result
            if (c.get("sender_role") or "unknown").lower() in roles
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Direction filter (PHASE 3 - NEW)
    # ─────────────────────────────────────────────────────────────────
    if filters.direction:
        directions = [d.lower() for d in filters.direction]
        result = [
            c for c in result
            if (c.get("direction") or "incoming").lower() in directions
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Assigned to me filter (PHASE 3 - NEW)
    # ─────────────────────────────────────────────────────────────────
    if filters.assigned_to_me is True:
        result = [c for c in result if c.get("assigned_to_me") is True]
    elif filters.assigned_to_me is False:
        result = [c for c in result if c.get("assigned_to_me") is False]
    
    # ─────────────────────────────────────────────────────────────────
    # Created date filters
    # ─────────────────────────────────────────────────────────────────
    if filters.created_after:
        result = [
            c for c in result
            if parse_datetime(c.get("created_at")) and parse_datetime(c.get("created_at")) >= filters.created_after
        ]
    
    if filters.created_before:
        result = [
            c for c in result
            if parse_datetime(c.get("created_at")) and parse_datetime(c.get("created_at")) <= filters.created_before
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Deadline date filters
    # ─────────────────────────────────────────────────────────────────
    if filters.deadline_after:
        result = [
            c for c in result
            if c.get("deadline_iso") and parse_date(c.get("deadline_iso")) and parse_date(c.get("deadline_iso")) >= filters.deadline_after
        ]
    
    if filters.deadline_before:
        result = [
            c for c in result
            if c.get("deadline_iso") and parse_date(c.get("deadline_iso")) and parse_date(c.get("deadline_iso")) <= filters.deadline_before
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Has deadline filter
    # ─────────────────────────────────────────────────────────────────
    if filters.has_deadline is True:
        result = [c for c in result if c.get("deadline_iso")]
    elif filters.has_deadline is False:
        result = [c for c in result if not c.get("deadline_iso")]
    
    # ─────────────────────────────────────────────────────────────────
    # Priority filter
    # ─────────────────────────────────────────────────────────────────
    if filters.priority:
        priorities = [p.lower() for p in filters.priority]
        result = [
            c for c in result
            if (c.get("priority") or "medium").lower() in priorities
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Commitment type filter
    # ─────────────────────────────────────────────────────────────────
    if filters.commitment_type:
        types = [t.lower() for t in filters.commitment_type]
        result = [
            c for c in result
            if (c.get("commitment_type") or "general").lower() in types
        ]
    
    # ─────────────────────────────────────────────────────────────────
    # Text search (in 'what' and 'email_subject', case-insensitive)
    # ─────────────────────────────────────────────────────────────────
    if filters.search_text:
        search = filters.search_text.lower()
        result = [
            c for c in result
            if search in (c.get("what") or "").lower()
            or search in (c.get("email_subject") or "").lower()
        ]
    
    return result


def categorize_commitments(
    commitments: List[Dict[str, Any]],
    today: date
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Categorize commitments into groups.
    
    Returns dict with keys: overdue, due_today, upcoming, later, no_deadline, completed
    """
    categories = {
        "overdue": [],
        "due_today": [],
        "upcoming": [],
        "later": [],
        "no_deadline": [],
        "completed": [],
    }
    
    for c in commitments:
        if c.get("completed"):
            categories["completed"].append(c)
        else:
            category = categorize_by_deadline(c, today, UPCOMING_DAYS)
            categories[category].append(c)
    
    return categories


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parse datetime from string or return None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


def parse_date(value: Any) -> Optional[date]:
    """Parse date from string or return None."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            # Handle both "2025-11-22" and "2025-11-22T00:00:00Z" formats
            return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
        except ValueError:
            return None
    return None


def determine_filter_type(filters: CommitmentFilters) -> str:
    """Determine the primary filter type for empty result messaging."""
    if filters.only_completed:
        return "completed"
    if filters.status:
        return "status"
    if filters.sender_email or filters.sender_name:
        return "sender"
    if filters.sender_role:
        return "sender_role"
    if filters.created_after or filters.created_before:
        return "date"
    if filters.deadline_after or filters.deadline_before:
        return "date"
    if filters.search_text:
        return "search"
    if filters.priority:
        return "priority"
    return "general"


# ═══════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def fetch_all_active(user_id: str, db=None) -> CommitmentResult:
    """Fetch all active (non-completed) commitments."""
    return fetch_commitments(user_id, CommitmentFilters(), db)


def fetch_overdue(user_id: str, db=None) -> CommitmentResult:
    """Fetch only overdue commitments."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(status=["overdue"]),
        db
    )


def fetch_due_today(user_id: str, db=None) -> CommitmentResult:
    """Fetch only commitments due today."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(status=["due_today"]),
        db
    )


def fetch_urgent(user_id: str, db=None) -> CommitmentResult:
    """Fetch overdue + due today commitments."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(status=["overdue", "due_today"]),
        db
    )


def fetch_from_sender(user_id: str, sender_email: str, db=None) -> CommitmentResult:
    """Fetch commitments from a specific sender."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(sender_email=sender_email),
        db
    )


def fetch_by_search(user_id: str, search_text: str, db=None) -> CommitmentResult:
    """Fetch commitments matching search text."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(search_text=search_text),
        db
    )


def fetch_from_investors(user_id: str, db=None) -> CommitmentResult:
    """Fetch commitments from investors."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(sender_role=["investor"]),
        db
    )


def fetch_from_customers(user_id: str, db=None) -> CommitmentResult:
    """Fetch commitments from customers."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(sender_role=["customer"]),
        db
    )


def fetch_high_priority(user_id: str, db=None) -> CommitmentResult:
    """Fetch high priority commitments."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(priority=["high"]),
        db
    )


def fetch_completed(user_id: str, db=None) -> CommitmentResult:
    """Fetch completed commitments."""
    return fetch_commitments(
        user_id,
        CommitmentFilters(only_completed=True),
        db
    )


def fetch_created_today(user_id: str, db=None) -> CommitmentResult:
    """Fetch commitments created today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    return fetch_commitments(
        user_id,
        CommitmentFilters(created_after=today_start, created_before=today_end),
        db
    )


if __name__ == "__main__":
    print("Fetcher module loaded. Run test_fetcher.py for testing.")