#!/usr/bin/env python3
"""
PHASE 2 TEST - AI Prompt Validation

Tests the updated AI prompt with sample emails.
Verifies:
1. Direction detection (incoming/outgoing)
2. assigned_to_me detection (true/false)
3. All 4 scenarios work correctly
"""

import os
import sys
import json
from datetime import datetime, timezone

# ════════════════════════════════════════════════════════════════
# CONFIGURATION - Set your user ID here
# ════════════════════════════════════════════════════════════════
TEST_USER_ID = "oOXqwITMVPNlAHfdaXmuvbNJ5wL2"  # Your Firebase user ID with credits

# Mock email samples for testing
TEST_EMAILS = [
    {
        "name": "SCENARIO 1: Incoming Assignment",
        "email": {
            "sender": "investor@sequoia.com",
            "sender_name": "Sarah Chen",
            "subject": "Re: Q4 Deck",
            "body": "Hi, can you send me the Q4 investor deck by Friday? We need it for our partner meeting. Thanks!",
            "date": "2025-11-25T10:00:00Z",
            "message_id": "msg_001",
            "folder": "INBOX"
        },
        "expected": {
            "direction": "incoming",
            "assigned_to_me": True,
            "explanation": "User received a request and must send the deck"
        }
    },
    {
        "name": "SCENARIO 2: Incoming Promise",
        "email": {
            "sender": "vendor@lawfirm.com",
            "sender_name": "John Smith",
            "subject": "Contract Update",
            "body": "Hi, I will send you the signed contract by tomorrow morning. Just waiting for final approval.",
            "date": "2025-11-25T14:00:00Z",
            "message_id": "msg_002",
            "folder": "INBOX"
        },
        "expected": {
            "direction": "incoming",
            "assigned_to_me": False,
            "explanation": "Sender promises to send contract, user waits"
        }
    },
    {
        "name": "SCENARIO 3: Outgoing Promise",
        "email": {
            "sender": "founder@startup.com",
            "sender_name": "You",
            "subject": "Re: Investment Materials",
            "body": "Thanks for the interest! I will send you the full pitch deck and financials by Monday.",
            "date": "2025-11-25T16:00:00Z",
            "message_id": "msg_003",
            "folder": "SENT"
        },
        "expected": {
            "direction": "outgoing",
            "assigned_to_me": True,
            "explanation": "User promised to send materials"
        }
    },
    {
        "name": "SCENARIO 4: Outgoing Request",
        "email": {
            "sender": "founder@startup.com",
            "sender_name": "You",
            "subject": "Contract Request",
            "body": "Hi, could you please send me the updated contract by end of week? Let me know if you need anything.",
            "date": "2025-11-25T18:00:00Z",
            "message_id": "msg_004",
            "folder": "SENT"
        },
        "expected": {
            "direction": "outgoing",
            "assigned_to_me": False,
            "explanation": "User requested, recipient must send"
        }
    }
]


def test_phase2():
    """Test Phase 2: AI prompt with direction and assigned_to_me detection."""
    
    print("\n" + "="*80)
    print("🧪 PHASE 2 TEST: AI Prompt Validation")
    print("="*80)
    print("Testing updated AI prompt with 4 scenarios")
    print("Each email will be sent to OpenAI for extraction")
    print("="*80 + "\n")
    
    # Check for OpenAI API key
    if not os.getenv("OPENAI_API_KEY"):
        print("❌ ERROR: OPENAI_API_KEY not found in environment")
        print("Please set OPENAI_API_KEY in your .env file")
        return False
    
    # Import the updated extractor
    try:
        from services.gmail.extract_initial_commitments import extract_commitments_from_email
    except ImportError:
        print("❌ ERROR: Could not import extract_commitments_from_email")
        print("Make sure extract_initial_commitments.py is in the same directory")
        return False
    
    # Initialize Firebase (required for credit system)
    import firebase_admin
    from firebase_admin import credentials
    import base64
    
    if not firebase_admin._apps:
        service_account_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64")
        
        if service_account_b64:
            service_account_json = base64.b64decode(service_account_b64).decode()
            service_account_dict = json.loads(service_account_json)
            cred = credentials.Certificate(service_account_dict)
        else:
            SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT", "serviceAccountKey.json")
            cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        
        firebase_admin.initialize_app(cred)
    
    # Test each scenario
    test_results = []
    
    for idx, test in enumerate(TEST_EMAILS, 1):
        print(f"\n{'─'*80}")
        print(f"TEST {idx}/4: {test['name']}")
        print(f"{'─'*80}")
        print(f"Email folder: {test['email']['folder']}")
        print(f"Body: {test['email']['body'][:60]}...")
        print()
        print(f"Expected:")
        print(f"  • direction: {test['expected']['direction']}")
        print(f"  • assigned_to_me: {test['expected']['assigned_to_me']}")
        print(f"  • Explanation: {test['expected']['explanation']}")
        print()
        
        # Call AI extractor
        print("🤖 Calling OpenAI API...")
        
        try:
            result = extract_commitments_from_email(test['email'], user_id=TEST_USER_ID)
        except Exception as e:
            print(f"❌ Extraction failed: {e}")
            test_results.append({
                "test": test['name'],
                "passed": False,
                "error": str(e)
            })
            continue
        
        # Check if commitment found
        if not result.get("has_commitment"):
            print("⚠️  No commitment found (unexpected)")
            test_results.append({
                "test": test['name'],
                "passed": False,
                "error": "No commitment found"
            })
            continue
        
        # Get actual values
        actual_direction = result.get("direction")
        commitments = result.get("commitments", [])
        
        if not commitments:
            print("⚠️  Commitments array is empty")
            test_results.append({
                "test": test['name'],
                "passed": False,
                "error": "Empty commitments array"
            })
            continue
        
        actual_assigned = commitments[0].get("assigned_to_me")
        
        print(f"Actual:")
        print(f"  • direction: {actual_direction}")
        print(f"  • assigned_to_me: {actual_assigned}")
        print()
        
        # Validate
        direction_ok = actual_direction == test['expected']['direction']
        assigned_ok = actual_assigned == test['expected']['assigned_to_me']
        
        if direction_ok and assigned_ok:
            print("✅ TEST PASSED")
            test_results.append({
                "test": test['name'],
                "passed": True
            })
        else:
            print("❌ TEST FAILED")
            if not direction_ok:
                print(f"   Direction mismatch: expected '{test['expected']['direction']}', got '{actual_direction}'")
            if not assigned_ok:
                print(f"   Assigned_to_me mismatch: expected {test['expected']['assigned_to_me']}, got {actual_assigned}")
            
            test_results.append({
                "test": test['name'],
                "passed": False,
                "expected_direction": test['expected']['direction'],
                "actual_direction": actual_direction,
                "expected_assigned": test['expected']['assigned_to_me'],
                "actual_assigned": actual_assigned
            })
    
    # Final results
    print("\n" + "="*80)
    print("PHASE 2 TEST RESULTS")
    print("="*80)
    
    passed = sum(1 for r in test_results if r['passed'])
    total = len(test_results)
    
    print(f"Tests run: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print()
    
    for result in test_results:
        status = "✅" if result['passed'] else "❌"
        print(f"{status} {result['test']}")
        if not result['passed'] and 'error' not in result:
            print(f"   Expected: direction={result.get('expected_direction')}, assigned_to_me={result.get('expected_assigned')}")
            print(f"   Got:      direction={result.get('actual_direction')}, assigned_to_me={result.get('actual_assigned')}")
    
    print()
    
    if passed == total:
        print("🎉 ALL TESTS PASSED!")
        print()
        print("Phase 2 Complete:")
        print("  ✅ AI correctly detects direction (incoming/outgoing)")
        print("  ✅ AI correctly detects assigned_to_me (true/false)")
        print("  ✅ All 4 scenarios working")
        print()
        print("Ready for Phase 3 (Filter Updates)")
        return True
    else:
        print("⚠️  SOME TESTS FAILED")
        print()
        print("Possible issues:")
        print("  • AI prompt may need adjustment")
        print("  • Model may need more examples")
        print("  • Check DEBUG output for details")
        return False


if __name__ == "__main__":
    success = test_phase2()
    sys.exit(0 if success else 1)