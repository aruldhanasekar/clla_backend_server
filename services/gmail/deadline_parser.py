# services/gmail/deadline_parser.py
"""
Deterministic deadline parser.

Function: parse_deadline_raw(deadline_raw: str, email_date_iso: str) -> Optional[str]
- Returns ISO date "YYYY-MM-DD" or None

UPDATED: Added support for:
- "tonight", "this evening" → same day as email
- "ASAP", "as soon as possible", "immediately", "urgent" → same day
- "within X hours/minutes" → same day
- "first thing tomorrow", "tomorrow morning" → next day
- "COB", "close of business" → same day
- "before the meeting", "before our call" → same day (conservative)
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

try:
    from dateutil import parser as dateutil_parser
except Exception:
    dateutil_parser = None

WEEKDAY_MAP = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6
}


def _normalize(text: str) -> str:
    """Normalize text for matching."""
    return (text or "").strip().lower()


def _try_parse_explicit_date(text: str, reference: datetime) -> Optional[datetime]:
    """Try to parse an explicit date using dateutil."""
    if not dateutil_parser:
        return None
    try:
        dt = dateutil_parser.parse(text, default=reference)
        return dt
    except Exception:
        return None


def _weekday_after(reference: datetime, target_wd: int, which: str = 'next') -> datetime:
    """Return the next or this week's weekday date."""
    ref = reference.date()
    days_ahead = (target_wd - ref.weekday() + 7) % 7
    if which == 'next':
        days_ahead = days_ahead if days_ahead != 0 else 7
    elif which == 'this':
        days_ahead = days_ahead
    return datetime.combine(ref + timedelta(days=days_ahead), datetime.min.time(), tzinfo=reference.tzinfo)


def parse_deadline_raw(deadline_raw: str, email_date_iso: str) -> Optional[str]:
    """
    Convert a natural-language deadline into an ISO date string (YYYY-MM-DD) or None.
    
    Handles:
    - today/tonight/this evening/end of day/EOD/COB
    - tomorrow/first thing tomorrow/tomorrow morning
    - ASAP/immediately/urgent/as soon as possible
    - weekdays: monday, by friday, next tuesday
    - explicit dates: Nov 25, 2025-11-25
    - relative: next week, this week, within X hours
    """
    if not deadline_raw:
        return None

    txt = _normalize(deadline_raw)
    
    # Handle null-like values
    if txt in ('null', 'none', 'n/a', 'na', 'no deadline', 'no date', 'tbd', 'to be determined'):
        return None

    # Parse email date as reference
    try:
        email_dt = datetime.fromisoformat(email_date_iso.replace('Z', '+00:00'))
    except Exception:
        email_dt = datetime.now(timezone.utc)

    # ═══════════════════════════════════════════════════════════════════
    # TODAY patterns (same day as email)
    # ═══════════════════════════════════════════════════════════════════
    
    # "tonight", "this evening"
    if 'tonight' in txt or 'this evening' in txt:
        return email_dt.date().isoformat()
    
    # "today" (but not "yesterday")
    if 'today' in txt and 'yesterday' not in txt:
        return email_dt.date().isoformat()
    
    # "end of day", "eod", "by end of day"
    if 'end of day' in txt or 'eod' in txt or 'by eod' in txt:
        return email_dt.date().isoformat()
    
    # "close of business", "cob", "by cob"
    if 'close of business' in txt or 'cob' in txt:
        return email_dt.date().isoformat()
    
    # "ASAP", "as soon as possible", "immediately", "right away", "urgent"
    if any(phrase in txt for phrase in ['asap', 'as soon as possible', 'immediately', 
                                          'right away', 'right now', 'urgent', 
                                          'urgently', 'at your earliest']):
        return email_dt.date().isoformat()
    
    # "within X hours/minutes" → same day
    if re.search(r'within\s+\d+\s*(hour|hr|minute|min)', txt):
        return email_dt.date().isoformat()
    
    # "in X hours/minutes" → same day
    if re.search(r'in\s+\d+\s*(hour|hr|minute|min)', txt):
        return email_dt.date().isoformat()
    
    # "before the meeting", "before our call", "before the demo" → conservative: today
    if re.search(r'before\s+(the|our|my)\s+(meeting|call|demo|presentation|review)', txt):
        return email_dt.date().isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # TOMORROW patterns
    # ═══════════════════════════════════════════════════════════════════
    
    if 'tomorrow' in txt:
        return (email_dt.date() + timedelta(days=1)).isoformat()
    
    # "first thing in the morning" without "tomorrow" → assume next business day
    if 'first thing' in txt and 'morning' in txt:
        return (email_dt.date() + timedelta(days=1)).isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # WEEKDAY patterns
    # ═══════════════════════════════════════════════════════════════════
    
    # "by monday", "due friday", "on tuesday", "next monday", "this friday"
    m = re.search(r'(?:(?:by|due|on|before)\s+)?(?:(next|this)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', txt)
    if m:
        which = m.group(1) or 'this'
        wd = m.group(2)
        wd_index = WEEKDAY_MAP[wd]
        dt = _weekday_after(email_dt, wd_index, which=which)
        return dt.date().isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # RELATIVE WEEK patterns
    # ═══════════════════════════════════════════════════════════════════
    
    # "next week" → 7 days from email
    if 'next week' in txt:
        return (email_dt.date() + timedelta(days=7)).isoformat()
    
    # "this week" → end of week (Sunday)
    if 'this week' in txt:
        days_to_sun = (6 - email_dt.weekday())
        return (email_dt.date() + timedelta(days=days_to_sun)).isoformat()
    
    # "end of week" → Friday of current week
    if 'end of week' in txt or 'end of the week' in txt:
        days_to_fri = (4 - email_dt.weekday())
        if days_to_fri < 0:
            days_to_fri += 7
        return (email_dt.date() + timedelta(days=days_to_fri)).isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # DAYS patterns
    # ═══════════════════════════════════════════════════════════════════
    
    # "in X days", "within X days"
    m = re.search(r'(?:in|within)\s+(\d+)\s*days?', txt)
    if m:
        days = int(m.group(1))
        return (email_dt.date() + timedelta(days=days)).isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # EXPLICIT DATE patterns (using dateutil)
    # ═══════════════════════════════════════════════════════════════════
    
    if dateutil_parser:
        dt = _try_parse_explicit_date(deadline_raw, email_dt)
        if dt:
            return dt.date().isoformat()

    # ═══════════════════════════════════════════════════════════════════
    # ORDINAL DATE patterns (22nd Nov, 25th of November)
    # ═══════════════════════════════════════════════════════════════════
    
    m2 = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', txt)
    if m2:
        try:
            day = int(m2.group(1))
            mon = m2.group(2)
            year = email_dt.year
            parsed = dateutil_parser.parse(f"{day} {mon} {year}") if dateutil_parser else None
            if parsed:
                return parsed.date().isoformat()
        except Exception:
            pass
    
    # "the 25th" → assume current month
    m3 = re.search(r'(?:the|by)\s+(\d{1,2})(?:st|nd|rd|th)', txt)
    if m3:
        try:
            day = int(m3.group(1))
            result_date = email_dt.replace(day=day).date()
            # If the day has passed, assume next month
            if result_date < email_dt.date():
                if email_dt.month == 12:
                    result_date = result_date.replace(year=email_dt.year + 1, month=1)
                else:
                    result_date = result_date.replace(month=email_dt.month + 1)
            return result_date.isoformat()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    # FALLBACK: Return None if no pattern matched
    # ═══════════════════════════════════════════════════════════════════
    
    return None


# ═══════════════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    test_date = "2025-11-24T09:00:00Z"
    
    test_cases = [
        # TODAY patterns
        ("tonight", test_date),
        ("this evening", test_date),
        ("by tonight", test_date),
        ("today", test_date),
        ("by today", test_date),
        ("end of day", test_date),
        ("by end of day", test_date),
        ("EOD", test_date),
        ("by EOD", test_date),
        ("COB", test_date),
        ("close of business", test_date),
        ("ASAP", test_date),
        ("as soon as possible", test_date),
        ("immediately", test_date),
        ("urgent", test_date),
        ("within 2 hours", test_date),
        ("in 30 minutes", test_date),
        ("before the meeting", test_date),
        ("before our call", test_date),
        
        # TOMORROW patterns
        ("tomorrow", test_date),
        ("by tomorrow", test_date),
        ("due tomorrow", test_date),
        ("tomorrow morning", test_date),
        ("first thing tomorrow", test_date),
        
        # WEEKDAY patterns
        ("Friday", test_date),
        ("by Friday", test_date),
        ("on Friday", test_date),
        ("next Monday", test_date),
        ("this Tuesday", test_date),
        
        # RELATIVE patterns
        ("next week", test_date),
        ("this week", test_date),
        ("end of week", test_date),
        ("in 3 days", test_date),
        ("within 5 days", test_date),
        
        # EXPLICIT dates
        ("Nov 25", test_date),
        ("November 25th", test_date),
        ("25th November", test_date),
        ("the 28th", test_date),
        ("by the 30th", test_date),
        
        # NULL patterns
        ("null", test_date),
        ("no deadline", test_date),
        ("TBD", test_date),
    ]
    
    print("=" * 60)
    print("DEADLINE PARSER TEST")
    print(f"Reference date: {test_date} (Monday, Nov 24, 2025)")
    print("=" * 60)
    
    for raw, ref_date in test_cases:
        result = parse_deadline_raw(raw, ref_date)
        print(f"'{raw}' → {result}")