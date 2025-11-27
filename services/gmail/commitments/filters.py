# services/commitments/filters.py
"""
Flexible filter schema for querying commitments.
All filters are optional - None means "don't filter by this".
Multiple filters are combined with AND logic.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import date, datetime, timezone, timedelta


@dataclass
class CommitmentFilters:
    """
    Filter configuration for fetching commitments.
    
    Usage:
        # Show all active
        filters = CommitmentFilters()
        
        # Show overdue from investors
        filters = CommitmentFilters(
            status=["overdue"],
            sender_role=["investor"]
        )
    """
    
    # ═══════════════════════════════════════════════════════════════
    # COMPLETION STATUS
    # ═══════════════════════════════════════════════════════════════
    include_completed: bool = False          # False = only active (completed=False)
    only_completed: bool = False             # True = only completed (completed=True)
    
    # ═══════════════════════════════════════════════════════════════
    # COMMITMENT STATUS (recalculated based on today)
    # ═══════════════════════════════════════════════════════════════
    # Options: "overdue", "due_today", "active", "no_deadline"
    status: Optional[List[str]] = None
    
    # ═══════════════════════════════════════════════════════════════
    # SENDER FILTERS
    # ═══════════════════════════════════════════════════════════════
    sender_email: Optional[str] = None       # Partial match: "sarah@" or "sequoia.com"
    sender_name: Optional[str] = None        # Partial match: "Sarah" or "Chen"
    sender_role: Optional[List[str]] = None  # ["investor", "customer", "teammate", "unknown"]
    
    # ═══════════════════════════════════════════════════════════════
    # DIRECTION FILTERS (PHASE 3 - NEW)
    # ═══════════════════════════════════════════════════════════════
    direction: Optional[List[str]] = None    # ["incoming", "outgoing"]
    assigned_to_me: Optional[bool] = None    # True = user must do it, False = others must do it
    
    # ═══════════════════════════════════════════════════════════════
    # DATE FILTERS - When commitment was CREATED/RECEIVED
    # ═══════════════════════════════════════════════════════════════
    created_after: Optional[datetime] = None   # Commitments created after this
    created_before: Optional[datetime] = None  # Commitments created before this
    
    # ═══════════════════════════════════════════════════════════════
    # DEADLINE FILTERS - When commitment is DUE
    # ═══════════════════════════════════════════════════════════════
    deadline_after: Optional[date] = None    # Due after this date
    deadline_before: Optional[date] = None   # Due before/on this date
    has_deadline: Optional[bool] = None      # True = must have deadline, False = no deadline only
    
    # ═══════════════════════════════════════════════════════════════
    # PRIORITY & TYPE
    # ═══════════════════════════════════════════════════════════════
    priority: Optional[List[str]] = None          # ["high", "medium", "low"]
    commitment_type: Optional[List[str]] = None   # ["deliverable", "meeting", "call", ...]
    
    # ═══════════════════════════════════════════════════════════════
    # TEXT SEARCH (searches in 'what' and 'email_subject')
    # ═══════════════════════════════════════════════════════════════
    search_text: Optional[str] = None        # "investor deck", "contract"
    
    # ═══════════════════════════════════════════════════════════════
    # SORTING & LIMITS
    # ═══════════════════════════════════════════════════════════════
    sort_by: str = "deadline"                # "deadline", "created_at", "priority", "days_overdue"
    sort_order: str = "asc"                  # "asc", "desc"
    limit: int = 100                         # Max results
    
    def to_dict(self) -> dict:
        """Convert filters to dictionary for logging/debugging."""
        result = {}
        if self.include_completed:
            result["include_completed"] = True
        if self.only_completed:
            result["only_completed"] = True
        if self.status:
            result["status"] = self.status
        if self.sender_email:
            result["sender_email"] = self.sender_email
        if self.sender_name:
            result["sender_name"] = self.sender_name
        if self.sender_role:
            result["sender_role"] = self.sender_role
        # PHASE 3 - NEW
        if self.direction:
            result["direction"] = self.direction
        if self.assigned_to_me is not None:
            result["assigned_to_me"] = self.assigned_to_me
        # END PHASE 3
        if self.created_after:
            result["created_after"] = self.created_after.isoformat()
        if self.created_before:
            result["created_before"] = self.created_before.isoformat()
        if self.deadline_after:
            result["deadline_after"] = self.deadline_after.isoformat()
        if self.deadline_before:
            result["deadline_before"] = self.deadline_before.isoformat()
        if self.has_deadline is not None:
            result["has_deadline"] = self.has_deadline
        if self.priority:
            result["priority"] = self.priority
        if self.commitment_type:
            result["commitment_type"] = self.commitment_type
        if self.search_text:
            result["search_text"] = self.search_text
        result["sort_by"] = self.sort_by
        result["sort_order"] = self.sort_order
        result["limit"] = self.limit
        return result
    
    def describe(self) -> str:
        """Generate human-readable description of applied filters."""
        parts = []
        
        if self.only_completed:
            parts.append("completed")
        elif not self.include_completed:
            parts.append("active")
        
        if self.status:
            status_str = " or ".join(self.status)
            parts.append(f"status: {status_str}")
        
        if self.sender_role:
            role_str = " or ".join(self.sender_role)
            parts.append(f"from {role_str}s")
        
        if self.sender_email:
            parts.append(f"from email containing '{self.sender_email}'")
        
        if self.sender_name:
            parts.append(f"from '{self.sender_name}'")
        
        # PHASE 3 - NEW
        if self.direction:
            direction_str = " or ".join(self.direction)
            parts.append(f"direction: {direction_str}")
        
        if self.assigned_to_me is True:
            parts.append("assigned to me")
        elif self.assigned_to_me is False:
            parts.append("assigned to others")
        # END PHASE 3
        
        if self.priority:
            priority_str = " or ".join(self.priority)
            parts.append(f"{priority_str} priority")
        
        if self.commitment_type:
            type_str = " or ".join(self.commitment_type)
            parts.append(f"type: {type_str}")
        
        if self.search_text:
            parts.append(f"matching '{self.search_text}'")
        
        if self.created_after and self.created_before:
            parts.append(f"created between {self.created_after.date()} and {self.created_before.date()}")
        elif self.created_after:
            parts.append(f"created after {self.created_after.date()}")
        elif self.created_before:
            parts.append(f"created before {self.created_before.date()}")
        
        if self.deadline_after and self.deadline_before:
            parts.append(f"due between {self.deadline_after} and {self.deadline_before}")
        elif self.deadline_after:
            parts.append(f"due after {self.deadline_after}")
        elif self.deadline_before:
            parts.append(f"due by {self.deadline_before}")
        
        if self.has_deadline is True:
            parts.append("with deadline")
        elif self.has_deadline is False:
            parts.append("without deadline")
        
        if not parts:
            return "All commitments"
        
        return "Commitments: " + ", ".join(parts)


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS FOR COMMON FILTER PRESETS
# ═══════════════════════════════════════════════════════════════

def all_active() -> CommitmentFilters:
    """Get all active (non-completed) commitments."""
    return CommitmentFilters()


def overdue_only() -> CommitmentFilters:
    """Get only overdue commitments."""
    return CommitmentFilters(status=["overdue"])


def due_today_only() -> CommitmentFilters:
    """Get only commitments due today."""
    return CommitmentFilters(status=["due_today"])


def urgent() -> CommitmentFilters:
    """Get overdue + due today (urgent items)."""
    return CommitmentFilters(status=["overdue", "due_today"])


def from_investors() -> CommitmentFilters:
    """Get commitments from investors."""
    return CommitmentFilters(sender_role=["investor"])


def from_customers() -> CommitmentFilters:
    """Get commitments from customers."""
    return CommitmentFilters(sender_role=["customer"])


def high_priority() -> CommitmentFilters:
    """Get high priority commitments."""
    return CommitmentFilters(priority=["high"])


def created_today() -> CommitmentFilters:
    """Get commitments created today."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    return CommitmentFilters(created_after=today, created_before=tomorrow)


def created_this_week() -> CommitmentFilters:
    """Get commitments created this week (Monday to today)."""
    today = datetime.now(timezone.utc)
    start_of_week = today - timedelta(days=today.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    return CommitmentFilters(created_after=start_of_week)


def due_this_week() -> CommitmentFilters:
    """Get commitments due this week."""
    today = date.today()
    end_of_week = today + timedelta(days=(6 - today.weekday()))
    return CommitmentFilters(deadline_before=end_of_week)


def completed_items() -> CommitmentFilters:
    """Get completed commitments."""
    return CommitmentFilters(only_completed=True)


# ═══════════════════════════════════════════════════════════════
# PHASE 3 - NEW DIRECTION HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def incoming_only() -> CommitmentFilters:
    """Get only incoming commitments (received emails)."""
    return CommitmentFilters(direction=["incoming"])


def outgoing_only() -> CommitmentFilters:
    """Get only outgoing commitments (sent emails)."""
    return CommitmentFilters(direction=["outgoing"])


def assigned_to_me() -> CommitmentFilters:
    """Get tasks assigned to me (I must complete them)."""
    return CommitmentFilters(assigned_to_me=True)


def waiting_on_others() -> CommitmentFilters:
    """Get tasks where I'm waiting on others."""
    return CommitmentFilters(assigned_to_me=False)


def my_action_items() -> CommitmentFilters:
    """Get all items where I need to take action (assigned_to_me=True)."""
    return CommitmentFilters(assigned_to_me=True)


def incoming_assignments() -> CommitmentFilters:
    """Get incoming requests where I'm assigned (Scenario 1)."""
    return CommitmentFilters(direction=["incoming"], assigned_to_me=True)


def incoming_promises() -> CommitmentFilters:
    """Get incoming promises from others (Scenario 2)."""
    return CommitmentFilters(direction=["incoming"], assigned_to_me=False)


def outgoing_promises() -> CommitmentFilters:
    """Get my outgoing promises (Scenario 3)."""
    return CommitmentFilters(direction=["outgoing"], assigned_to_me=True)


def outgoing_requests() -> CommitmentFilters:
    """Get my outgoing requests to others (Scenario 4)."""
    return CommitmentFilters(direction=["outgoing"], assigned_to_me=False)


if __name__ == "__main__":
    # Test filter descriptions
    print("Filter descriptions test:")
    print("-" * 50)
    
    filters = [
        CommitmentFilters(),
        CommitmentFilters(status=["overdue"]),
        CommitmentFilters(status=["overdue", "due_today"], sender_role=["investor"]),
        CommitmentFilters(sender_email="sarah@sequoia.com"),
        CommitmentFilters(search_text="investor deck", priority=["high"]),
        CommitmentFilters(only_completed=True),
        created_today(),
        due_this_week(),
        # PHASE 3 - NEW
        incoming_only(),
        outgoing_only(),
        assigned_to_me(),
        waiting_on_others(),
        incoming_assignments(),
        outgoing_promises(),
    ]
    
    for f in filters:
        print(f"→ {f.describe()}")
        print(f"  Dict: {f.to_dict()}")
        print()