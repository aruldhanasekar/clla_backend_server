# services/commitments/status_calculator.py
"""
Recalculate commitment status based on current date.

The status stored in Firestore may be stale (calculated at extraction time).
This module recalculates status on every fetch to ensure accuracy.

Status values:
- "overdue": deadline_iso < today
- "due_today": deadline_iso == today
- "active": deadline_iso > today (has future deadline)
- "no_deadline": deadline_iso is None/empty
"""

from __future__ import annotations
from datetime import date, datetime, timezone, timedelta
from typing import Dict, Any, Tuple


def recalculate_status(commitment: Dict[str, Any], today: date = None) -> Dict[str, Any]:
    """
    Recalculate status, days_overdue, and overdue_flag for a commitment.
    
    Args:
        commitment: Commitment dict from Firestore
        today: Date to compare against (defaults to today)
    
    Returns:
        Updated commitment dict with recalculated status fields
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    
    # Get deadline_iso
    deadline_iso = commitment.get("deadline_iso")
    
    # Default values
    status = "no_deadline"
    days_overdue = 0
    overdue_flag = False
    
    # If already completed, don't change status calculation
    if commitment.get("completed"):
        commitment["status"] = commitment.get("status", "no_deadline")
        commitment["days_overdue"] = commitment.get("days_overdue", 0)
        commitment["overdue_flag"] = commitment.get("overdue_flag", False)
        return commitment
    
    if deadline_iso:
        try:
            # Parse deadline date
            if isinstance(deadline_iso, str):
                # Handle both "2025-11-22" and "2025-11-22T00:00:00Z" formats
                deadline_date = datetime.fromisoformat(deadline_iso.replace('Z', '+00:00')).date()
            elif isinstance(deadline_iso, date):
                deadline_date = deadline_iso
            else:
                deadline_date = None
            
            if deadline_date:
                if deadline_date < today:
                    status = "overdue"
                    days_overdue = (today - deadline_date).days
                    overdue_flag = True
                elif deadline_date == today:
                    status = "due_today"
                    days_overdue = 0
                    overdue_flag = False
                else:
                    status = "active"
                    days_overdue = 0
                    overdue_flag = False
        except (ValueError, TypeError) as e:
            # If parsing fails, treat as no deadline
            status = "no_deadline"
            days_overdue = 0
            overdue_flag = False
    
    # Update commitment
    commitment["status"] = status
    commitment["days_overdue"] = days_overdue
    commitment["overdue_flag"] = overdue_flag
    
    return commitment


def categorize_by_deadline(
    commitment: Dict[str, Any],
    today: date = None,
    upcoming_days: int = 7
) -> str:
    """
    Categorize commitment into: overdue, due_today, upcoming, later, no_deadline.
    
    Args:
        commitment: Commitment dict with recalculated status
        today: Date to compare against
        upcoming_days: Number of days to consider "upcoming" (default: 7)
    
    Returns:
        Category string: "overdue", "due_today", "upcoming", "later", "no_deadline"
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    
    status = commitment.get("status", "no_deadline")
    
    if status == "overdue":
        return "overdue"
    elif status == "due_today":
        return "due_today"
    elif status == "no_deadline":
        return "no_deadline"
    elif status == "active":
        # Further categorize active into "upcoming" vs "later"
        deadline_iso = commitment.get("deadline_iso")
        if deadline_iso:
            try:
                if isinstance(deadline_iso, str):
                    deadline_date = datetime.fromisoformat(deadline_iso.replace('Z', '+00:00')).date()
                else:
                    deadline_date = deadline_iso
                
                upcoming_end = today + timedelta(days=upcoming_days)
                if deadline_date <= upcoming_end:
                    return "upcoming"
                else:
                    return "later"
            except (ValueError, TypeError):
                return "no_deadline"
        return "no_deadline"
    
    return "no_deadline"


def get_urgency_score(commitment: Dict[str, Any], today: date = None) -> int:
    """
    Calculate urgency score for sorting.
    Lower score = more urgent.
    
    Scoring:
    - Overdue: 0 + (100 - days_overdue) → Most overdue first
    - Due Today: 100
    - Active (upcoming): 200 + days_until_due
    - Active (later): 300 + days_until_due
    - No Deadline: 1000
    
    Args:
        commitment: Commitment dict with recalculated status
        today: Date to compare against
    
    Returns:
        Urgency score (lower = more urgent)
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    
    status = commitment.get("status", "no_deadline")
    days_overdue = commitment.get("days_overdue", 0)
    deadline_iso = commitment.get("deadline_iso")
    
    if status == "overdue":
        # More overdue = lower score (more urgent)
        return max(0, 100 - days_overdue)
    
    elif status == "due_today":
        return 100
    
    elif status == "active" and deadline_iso:
        try:
            if isinstance(deadline_iso, str):
                deadline_date = datetime.fromisoformat(deadline_iso.replace('Z', '+00:00')).date()
            else:
                deadline_date = deadline_iso
            
            days_until = (deadline_date - today).days
            
            if days_until <= 7:
                return 200 + days_until
            else:
                return 300 + days_until
        except (ValueError, TypeError):
            return 1000
    
    # No deadline = lowest priority
    return 1000


def get_priority_score(priority: str) -> int:
    """
    Convert priority string to numeric score for sorting.
    
    Args:
        priority: "high", "medium", or "low"
    
    Returns:
        Score: high=0, medium=1, low=2
    """
    priority_map = {
        "high": 0,
        "medium": 1,
        "low": 2,
    }
    return priority_map.get(priority.lower(), 1)


def sort_commitments(
    commitments: list,
    sort_by: str = "deadline",
    sort_order: str = "asc"
) -> list:
    """
    Sort commitments by specified field.
    
    Args:
        commitments: List of commitment dicts
        sort_by: Field to sort by ("deadline", "created_at", "priority", "days_overdue")
        sort_order: "asc" or "desc"
    
    Returns:
        Sorted list of commitments
    """
    reverse = sort_order.lower() == "desc"
    
    if sort_by == "deadline":
        # Sort by urgency score (combines status + deadline)
        return sorted(commitments, key=get_urgency_score, reverse=reverse)
    
    elif sort_by == "priority":
        # Sort by priority, then by deadline
        def priority_then_deadline(c):
            return (get_priority_score(c.get("priority", "medium")), get_urgency_score(c))
        return sorted(commitments, key=priority_then_deadline, reverse=reverse)
    
    elif sort_by == "created_at":
        def created_at_key(c):
            created = c.get("created_at", "")
            if isinstance(created, str):
                try:
                    return datetime.fromisoformat(created.replace('Z', '+00:00'))
                except ValueError:
                    return datetime.min.replace(tzinfo=timezone.utc)
            return datetime.min.replace(tzinfo=timezone.utc)
        return sorted(commitments, key=created_at_key, reverse=reverse)
    
    elif sort_by == "days_overdue":
        def days_key(c):
            return c.get("days_overdue", 0)
        return sorted(commitments, key=days_key, reverse=not reverse)  # Most overdue first by default
    
    else:
        # Default: by deadline urgency
        return sorted(commitments, key=get_urgency_score, reverse=reverse)


if __name__ == "__main__":
    from datetime import timedelta
    
    # Test status recalculation
    print("Testing status recalculation...")
    print("=" * 50)
    
    today = date.today()
    print(f"Today: {today}")
    print()
    
    test_cases = [
        {
            "name": "Overdue (3 days)",
            "commitment": {
                "what": "Send report",
                "deadline_iso": (today - timedelta(days=3)).isoformat(),
                "status": "active",  # Stale status
                "days_overdue": 0,
            }
        },
        {
            "name": "Due today",
            "commitment": {
                "what": "Call investor",
                "deadline_iso": today.isoformat(),
                "status": "active",
            }
        },
        {
            "name": "Due in 3 days (upcoming)",
            "commitment": {
                "what": "Review contract",
                "deadline_iso": (today + timedelta(days=3)).isoformat(),
                "status": "active",
            }
        },
        {
            "name": "Due in 14 days (later)",
            "commitment": {
                "what": "Quarterly review",
                "deadline_iso": (today + timedelta(days=14)).isoformat(),
                "status": "active",
            }
        },
        {
            "name": "No deadline",
            "commitment": {
                "what": "Optional task",
                "deadline_iso": None,
                "status": "active",
            }
        },
    ]
    
    for tc in test_cases:
        print(f"Test: {tc['name']}")
        result = recalculate_status(tc["commitment"].copy(), today)
        category = categorize_by_deadline(result, today)
        urgency = get_urgency_score(result, today)
        
        print(f"  Status: {result['status']}")
        print(f"  Days overdue: {result['days_overdue']}")
        print(f"  Category: {category}")
        print(f"  Urgency score: {urgency}")
        print()
    
    # Test sorting
    print("=" * 50)
    print("Testing sorting...")
    print()
    
    commitments = [tc["commitment"].copy() for tc in test_cases]
    for c in commitments:
        recalculate_status(c, today)
    
    sorted_by_deadline = sort_commitments(commitments, sort_by="deadline")
    print("Sorted by deadline (most urgent first):")
    for c in sorted_by_deadline:
        print(f"  • {c['what']} - {c['status']} (urgency: {get_urgency_score(c)})")
