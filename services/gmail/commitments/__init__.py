# services/gmail/commitments/__init__.py
"""
Commitment fetcher service.

This module provides functions to fetch, filter, and categorize
commitments from Firestore.

Usage:
    from services.gmail.commitments import fetch_commitments, CommitmentFilters
    
    # Fetch all active commitments
    result = fetch_commitments(user_id="xxx")
    
    # Fetch with specific filters
    result = fetch_commitments(
        user_id="xxx",
        filters=CommitmentFilters(
            status=["overdue"],
            sender_role=["investor"]
        )
    )
    
    # Display results
    print(result.to_display_string())
"""

from .filters import (
    CommitmentFilters,
    # Preset filter helpers
    all_active,
    overdue_only,
    due_today_only,
    urgent,
    from_investors,
    from_customers,
    high_priority,
    created_today,
    created_this_week,
    due_this_week,
    completed_items,
)

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
    get_urgency_score,
    get_priority_score,
)

from .fetcher import (
    fetch_commitments,
    # Convenience functions
    fetch_all_active,
    fetch_overdue,
    fetch_due_today,
    fetch_urgent,
    fetch_from_sender,
    fetch_by_search,
    fetch_from_investors,
    fetch_from_customers,
    fetch_high_priority,
    fetch_completed,
    fetch_created_today,
)

__all__ = [
    # Main fetcher
    "fetch_commitments",
    
    # Filter class
    "CommitmentFilters",
    
    # Models
    "CommitmentItem",
    "CommitmentSummary",
    "CommitmentResult",
    "create_empty_result",
    
    # Filter presets
    "all_active",
    "overdue_only",
    "due_today_only",
    "urgent",
    "from_investors",
    "from_customers",
    "high_priority",
    "created_today",
    "created_this_week",
    "due_this_week",
    "completed_items",
    
    # Convenience fetchers
    "fetch_all_active",
    "fetch_overdue",
    "fetch_due_today",
    "fetch_urgent",
    "fetch_from_sender",
    "fetch_by_search",
    "fetch_from_investors",
    "fetch_from_customers",
    "fetch_high_priority",
    "fetch_completed",
    "fetch_created_today",
    
    # Status helpers
    "recalculate_status",
    "categorize_by_deadline",
    "sort_commitments",
    "get_urgency_score",
    "get_priority_score",
]