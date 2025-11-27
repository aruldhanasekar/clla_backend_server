# services/commitments/models.py
"""
Data models for commitment fetcher responses.
Uses dataclasses for clean structure and easy serialization.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


@dataclass
class CommitmentItem:
    """
    Single commitment with all fields needed for display.
    This is the cleaned/normalized version returned by the fetcher.
    """
    
    # ═══════════════════════════════════════════════════════════════
    # IDENTIFIERS
    # ═══════════════════════════════════════════════════════════════
    commitment_id: str
    user_id: str
    
    # ═══════════════════════════════════════════════════════════════
    # CORE COMMITMENT DATA
    # ═══════════════════════════════════════════════════════════════
    what: str                              # "Send investor deck"
    to_whom: str                           # "investor", "customer", etc.
    given_by: str                          # Email of person who gave commitment
    
    # ═══════════════════════════════════════════════════════════════
    # DEADLINE INFO
    # ═══════════════════════════════════════════════════════════════
    deadline_raw: Optional[str]            # "by Friday" (original text)
    deadline_iso: Optional[str]            # "2025-11-22" (parsed date)
    
    # ═══════════════════════════════════════════════════════════════
    # STATUS (recalculated based on today)
    # ═══════════════════════════════════════════════════════════════
    status: str                            # "overdue", "due_today", "active", "no_deadline"
    days_overdue: int                      # Number of days past deadline (0 if not overdue)
    overdue_flag: bool                     # True if overdue
    
    # ═══════════════════════════════════════════════════════════════
    # PRIORITY & TYPE
    # ═══════════════════════════════════════════════════════════════
    priority: str                          # "high", "medium", "low"
    commitment_type: str                   # "deliverable", "meeting", "call", etc.
    estimated_hours: float                 # Estimated hours to complete
    confidence: float                      # AI confidence score (0.0 - 1.0)
    
    # ═══════════════════════════════════════════════════════════════
    # SENDER INFO
    # ═══════════════════════════════════════════════════════════════
    email_sender: str                      # "sarah@sequoia.com"
    email_sender_name: str                 # "Sarah Chen"
    sender_role: str                       # "investor", "customer", "teammate", "unknown"

    # ═══════════════════════════════════════════════════════════════
    # DIRECTION & ASSIGNMENT (PHASE 4B - NEW)
    # ═══════════════════════════════════════════════════════════════
    direction: str                         # "incoming" (INBOX) or "outgoing" (SENT)
    assigned_to_me: bool                   # True = I must do it, False = others must do it
    
    # ═══════════════════════════════════════════════════════════════
    # EMAIL CONTEXT
    # ═══════════════════════════════════════════════════════════════
    email_subject: str                     # "Re: Series A deck"
    email_date: str                        # When email was received
    message_id: str                        # Gmail message ID
    
    # ═══════════════════════════════════════════════════════════════
    # COMPLETION STATUS
    # ═══════════════════════════════════════════════════════════════
    completed: bool                        # True if marked complete
    completed_at: Optional[str]            # When it was completed
    
    # ═══════════════════════════════════════════════════════════════
    # TIMESTAMPS
    # ═══════════════════════════════════════════════════════════════
    created_at: str                        # When commitment was extracted
    updated_at: str                        # Last update time
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)
    
    def to_display_string(self, include_details: bool = True) -> str:
        """Format commitment for display in chat."""
        parts = [f"• {self.what}"]
        
        if self.to_whom:
            parts.append(f"  → To: {self.to_whom}")
        
        if self.email_sender_name:
            parts.append(f"  → From: {self.email_sender_name}")
        elif self.email_sender:
            parts.append(f"  → From: {self.email_sender}")
        
        if self.deadline_iso:
            parts.append(f"  → Due: {self.deadline_iso}")
            if self.days_overdue > 0:
                parts.append(f"  ⚠️ {self.days_overdue} day(s) overdue!")
        else:
            parts.append(f"  → Due: No deadline set")
        
        if include_details:
            parts.append(f"  → Priority: {self.priority.capitalize()}")
            if self.estimated_hours:
                parts.append(f"  → Est. time: {self.estimated_hours}h")
        
        return "\n".join(parts)
    
    def to_short_string(self) -> str:
        """Short one-line format."""
        deadline_str = f"Due: {self.deadline_iso}" if self.deadline_iso else "No deadline"
        overdue_str = f" ⚠️ {self.days_overdue}d overdue" if self.days_overdue > 0 else ""
        return f"• {self.what} ({deadline_str}){overdue_str}"
    
    @classmethod
    def from_firestore(cls, doc_dict: Dict[str, Any]) -> CommitmentItem:
        """Create CommitmentItem from Firestore document."""
        return cls(
            commitment_id=doc_dict.get("commitment_id", ""),
            user_id=doc_dict.get("user_id", ""),
            what=doc_dict.get("what", ""),
            to_whom=doc_dict.get("to_whom", ""),
            given_by=doc_dict.get("given_by", ""),
            deadline_raw=doc_dict.get("deadline_raw"),
            deadline_iso=doc_dict.get("deadline_iso"),
            status=doc_dict.get("status", "no_deadline"),
            days_overdue=doc_dict.get("days_overdue", 0),
            overdue_flag=doc_dict.get("overdue_flag", False),
            priority=doc_dict.get("priority", "medium"),
            commitment_type=doc_dict.get("commitment_type", "general"),
            estimated_hours=doc_dict.get("estimated_hours", 0),
            confidence=doc_dict.get("confidence", 0.0),
            email_sender=doc_dict.get("email_sender", ""),
            email_sender_name=doc_dict.get("email_sender_name", ""),
            sender_role=doc_dict.get("sender_role", "unknown"),
            direction=doc_dict.get("direction", "incoming"),  # PHASE 4B: NEW
            assigned_to_me=doc_dict.get("assigned_to_me", False),  # PHASE 4B: NEW
            email_subject=doc_dict.get("email_subject", ""),
            email_date=doc_dict.get("email_date", ""),
            message_id=doc_dict.get("message_id", ""),
            completed=doc_dict.get("completed", False),
            completed_at=doc_dict.get("completed_at"),
            created_at=doc_dict.get("created_at", ""),
            updated_at=doc_dict.get("updated_at", ""),
        )


@dataclass
class CommitmentSummary:
    """Summary statistics for commitments."""
    total: int = 0
    overdue: int = 0
    due_today: int = 0
    upcoming: int = 0          # Next 7 days
    later: int = 0             # Beyond 7 days
    no_deadline: int = 0
    completed: int = 0
    
    def to_dict(self) -> Dict[str, int]:
        return asdict(self)
    
    def to_display_string(self) -> str:
        """Format summary for display."""
        lines = [
            "📊 Commitment Summary:",
            f"  🔴 Overdue: {self.overdue}",
            f"  🟡 Due Today: {self.due_today}",
            f"  🟢 Upcoming (7 days): {self.upcoming}",
            f"  🔵 Later: {self.later}",
            f"  ⚪ No Deadline: {self.no_deadline}",
            f"  ─────────────────",
            f"  📋 Total Active: {self.total}",
        ]
        if self.completed > 0:
            lines.append(f"  ✅ Completed: {self.completed}")
        return "\n".join(lines)


@dataclass
class CommitmentResult:
    """
    Complete response from commitment fetcher.
    Contains categorized commitments, summary, and metadata.
    """
    
    # ═══════════════════════════════════════════════════════════════
    # QUERY INFO
    # ═══════════════════════════════════════════════════════════════
    query_description: str                 # "Overdue commitments from investors"
    filters_applied: Dict[str, Any]        # The filters that were used
    
    # ═══════════════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════════════
    total_found: int                       # Total commitments matching filters
    summary: CommitmentSummary             # Breakdown by status
    
    # Categorized lists (for easy display)
    overdue: List[CommitmentItem] = field(default_factory=list)
    due_today: List[CommitmentItem] = field(default_factory=list)
    upcoming: List[CommitmentItem] = field(default_factory=list)      # Next 7 days
    later: List[CommitmentItem] = field(default_factory=list)         # Beyond 7 days
    no_deadline: List[CommitmentItem] = field(default_factory=list)
    completed: List[CommitmentItem] = field(default_factory=list)     # Only if requested
    
    # Flat list (all results sorted)
    all_commitments: List[CommitmentItem] = field(default_factory=list)
    
    # ═══════════════════════════════════════════════════════════════
    # EMPTY RESULT MESSAGE
    # ═══════════════════════════════════════════════════════════════
    is_empty: bool = False
    empty_message: str = ""
    suggestions: List[str] = field(default_factory=list)
    
    # ═══════════════════════════════════════════════════════════════
    # METADATA
    # ═══════════════════════════════════════════════════════════════
    fetched_at: str = ""
    user_id: str = ""
    
    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "query_description": self.query_description,
            "filters_applied": self.filters_applied,
            "total_found": self.total_found,
            "summary": self.summary.to_dict(),
            "overdue": [c.to_dict() for c in self.overdue],
            "due_today": [c.to_dict() for c in self.due_today],
            "upcoming": [c.to_dict() for c in self.upcoming],
            "later": [c.to_dict() for c in self.later],
            "no_deadline": [c.to_dict() for c in self.no_deadline],
            "completed": [c.to_dict() for c in self.completed],
            "all_commitments": [c.to_dict() for c in self.all_commitments],
            "is_empty": self.is_empty,
            "empty_message": self.empty_message,
            "suggestions": self.suggestions,
            "fetched_at": self.fetched_at,
            "user_id": self.user_id,
        }
    
    def to_display_string(self, verbose: bool = False) -> str:
        """Format results for display in chat."""
        lines = []
        
        # Header
        lines.append(f"📋 {self.query_description}")
        lines.append("=" * 50)
        
        # Handle empty results
        if self.is_empty:
            lines.append("")
            lines.append(f"ℹ️ {self.empty_message}")
            if self.suggestions:
                lines.append("")
                lines.append("💡 Suggestions:")
                for s in self.suggestions:
                    lines.append(f"  • {s}")
            return "\n".join(lines)
        
        # Summary
        lines.append(self.summary.to_display_string())
        lines.append("")
        
        # Overdue
        if self.overdue:
            lines.append("🔴 OVERDUE:")
            for c in self.overdue:
                lines.append(c.to_short_string() if not verbose else c.to_display_string())
            lines.append("")
        
        # Due Today
        if self.due_today:
            lines.append("🟡 DUE TODAY:")
            for c in self.due_today:
                lines.append(c.to_short_string() if not verbose else c.to_display_string())
            lines.append("")
        
        # Upcoming
        if self.upcoming:
            lines.append("🟢 UPCOMING (Next 7 days):")
            for c in self.upcoming:
                lines.append(c.to_short_string() if not verbose else c.to_display_string())
            lines.append("")
        
        # Later
        if self.later:
            lines.append("🔵 LATER:")
            for c in self.later[:5]:  # Limit to 5
                lines.append(c.to_short_string() if not verbose else c.to_display_string())
            if len(self.later) > 5:
                lines.append(f"  ... and {len(self.later) - 5} more")
            lines.append("")
        
        # No Deadline
        if self.no_deadline:
            lines.append("⚪ NO DEADLINE:")
            for c in self.no_deadline[:5]:  # Limit to 5
                lines.append(c.to_short_string() if not verbose else c.to_display_string())
            if len(self.no_deadline) > 5:
                lines.append(f"  ... and {len(self.no_deadline) - 5} more")
            lines.append("")
        
        # Completed (if any)
        if self.completed:
            lines.append("✅ COMPLETED:")
            for c in self.completed[:5]:
                lines.append(c.to_short_string())
            if len(self.completed) > 5:
                lines.append(f"  ... and {len(self.completed) - 5} more")
        
        return "\n".join(lines)


def create_empty_result(
    query_description: str,
    filters_applied: Dict[str, Any],
    user_id: str,
    filter_type: str = "general"
) -> CommitmentResult:
    """
    Create an empty result with helpful message and suggestions.
    
    Args:
        query_description: Description of what was searched
        filters_applied: The filters that were used
        user_id: User ID
        filter_type: Type of filter to customize suggestions
                    Options: "general", "status", "sender", "date", "search", "completed"
    """
    
    # Customize message and suggestions based on filter type
    if filter_type == "status":
        status = filters_applied.get("status", [])
        status_str = " or ".join(status) if status else "matching"
        message = f"No {status_str} commitments found."
        suggestions = [
            "Try checking all active commitments",
            "Maybe you've already completed them? Ask to see completed items",
            "Check if your Gmail is synced with recent emails",
        ]
    
    elif filter_type == "sender":
        sender = filters_applied.get("sender_email") or filters_applied.get("sender_name") or "this sender"
        message = f"No commitments found from '{sender}'."
        suggestions = [
            "Double-check the email address or name spelling",
            "Try a partial match (e.g., just 'sarah' instead of full email)",
            "Ask to see all commitments to find the right sender",
        ]
    
    elif filter_type == "sender_role":
        roles = filters_applied.get("sender_role", [])
        role_str = " or ".join(roles) if roles else "this role"
        message = f"No commitments found from {role_str}s."
        suggestions = [
            "The AI might have classified the sender differently",
            "Try searching by email address instead",
            "Ask to see all commitments to check sender classifications",
        ]
    
    elif filter_type == "date":
        message = "No commitments found in the specified date range."
        suggestions = [
            "Try expanding the date range",
            "Check if you received any emails during that period",
            "Ask to see all commitments without date filters",
        ]
    
    elif filter_type == "search":
        search_text = filters_applied.get("search_text", "")
        message = f"No commitments found matching '{search_text}'."
        suggestions = [
            "Try different keywords",
            "Use partial words (e.g., 'deck' instead of 'investor deck')",
            "Ask to see all commitments and browse through them",
        ]
    
    elif filter_type == "completed":
        message = "No completed commitments found."
        suggestions = [
            "You might not have marked any commitments as complete yet",
            "Try asking to see active commitments instead",
        ]
    
    elif filter_type == "priority":
        priority = filters_applied.get("priority", [])
        priority_str = " or ".join(priority) if priority else ""
        message = f"No {priority_str} priority commitments found."
        suggestions = [
            "Try checking other priority levels",
            "Ask to see all commitments",
        ]
    
    else:  # general
        message = "No commitments found matching your criteria."
        suggestions = [
            "Try broadening your search",
            "Ask to see all active commitments",
            "Check if your Gmail is connected and synced",
            "Try asking in a different way",
        ]
    
    return CommitmentResult(
        query_description=query_description,
        filters_applied=filters_applied,
        total_found=0,
        summary=CommitmentSummary(),
        is_empty=True,
        empty_message=message,
        suggestions=suggestions,
        user_id=user_id,
    )


if __name__ == "__main__":
    # Test models
    print("Testing CommitmentItem...")
    
    item = CommitmentItem(
        commitment_id="test_123",
        user_id="user_456",
        what="Send investor deck",
        to_whom="investor",
        given_by="sarah@sequoia.com",
        deadline_raw="by Friday",
        deadline_iso="2025-11-22",
        status="overdue",
        days_overdue=2,
        overdue_flag=True,
        priority="high",
        commitment_type="deliverable",
        estimated_hours=3,
        confidence=0.9,
        email_sender="sarah@sequoia.com",
        email_sender_name="Sarah Chen",
        sender_role="investor",
        email_subject="Re: Series A materials",
        email_date="2025-11-20T10:00:00Z",
        message_id="msg_abc",
        completed=False,
        completed_at=None,
        created_at="2025-11-20T10:05:00Z",
        updated_at="2025-11-20T10:05:00Z",
    )
    
    print("\nShort format:")
    print(item.to_short_string())
    
    print("\nDetailed format:")
    print(item.to_display_string())
    
    print("\n" + "=" * 50)
    print("Testing empty result...")
    
    empty = create_empty_result(
        query_description="Overdue commitments from investors",
        filters_applied={"status": ["overdue"], "sender_role": ["investor"]},
        user_id="user_456",
        filter_type="sender_role"
    )
    
    print(empty.to_display_string())
