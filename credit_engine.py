# credit_engine.py

from firebase_admin import firestore
from credit_config import (
    INPUT_TOKENS_PER_CREDIT,
    OUTPUT_TOKENS_PER_CREDIT,
    DEFAULT_FREE_TRIAL_CREDITS
)

import os
LOCAL_TEST_MODE = os.environ.get("LOCAL_TEST_MODE", "0") == "1"

if LOCAL_TEST_MODE:
    def _get_db():
        # Do NOT initialize Firestore during local tests
        class Dummy:
            def collection(self, *a, **k): return self
            def document(self, *a, **k): return self
            def get(self, *a, **k): return type("X",(object,),{"exists":True,"to_dict":lambda:{"credits_remaining":9999}})()
            def set(self, *a, **k): pass
            def update(self, *a, **k): pass
        return Dummy()


def _get_db():
    """Get Firestore client (lazy initialization)"""
    return firestore.client()


def initialize_credits_if_missing(user_id: str):
    """Creates default credit fields only once per user."""
    db = _get_db()  # ← Initialize here
    user_ref = db.collection("users").document(user_id)
    snap = user_ref.get()
    if snap.exists:
        data = snap.to_dict()
        if "credits_total" in data:
            return  # already initialized

    user_ref.set({
        "credits_total": DEFAULT_FREE_TRIAL_CREDITS,
        "credits_used": 0.0,
        "credits_remaining": float(DEFAULT_FREE_TRIAL_CREDITS),
        "tokens_per_credit_input": INPUT_TOKENS_PER_CREDIT,
        "tokens_per_credit_output": OUTPUT_TOKENS_PER_CREDIT,
        "composio_enabled": True
    }, merge=True)


def calculate_credits_spent(input_tokens: int, output_tokens: int) -> float:
    """Convert token usage to credits, rounding up for safety."""
    input_cost = (input_tokens or 0) / INPUT_TOKENS_PER_CREDIT
    output_cost = (output_tokens or 0) / OUTPUT_TOKENS_PER_CREDIT
    credits = input_cost + output_cost
    return round(credits + 1e-8, 2)


def deduct_credits(user_id: str, credits_spent: float):
    """Atomic Firestore credit deduction."""
    db = _get_db()  # ← Initialize here
    user_ref = db.collection("users").document(user_id)

    @firestore.transactional
    def txn(transaction):
        snap = user_ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError(f"User {user_id} does not exist.")

        data = snap.to_dict()

        remaining = float(data.get("credits_remaining", 0))
        used = float(data.get("credits_used", 0))

        new_remaining = max(remaining - credits_spent, 0)
        new_used = used + credits_spent

        transaction.update(user_ref, {
            "credits_remaining": new_remaining,
            "credits_used": new_used
        })
        
        return new_remaining

    new_remaining = txn(db.transaction())
    
    # ✅ AUTO-PAUSE COMPOSIO IF CREDITS EXHAUSTED
    if new_remaining <= 0:
        try:
            from services.composio.composio_manager import pause_composio_trigger
            pause_composio_trigger(user_id)
            print(f"🔴 User {user_id} exhausted credits - Composio paused")
        except Exception as e:
            print(f"⚠️ Failed to pause Composio: {e}")


def has_enough_credits(user_id: str) -> bool:
    """Return True if user has ANY credits remaining."""
    db = _get_db()  # ← Initialize here
    snap = db.collection("users").document(user_id).get()
    if not snap.exists:
        return False
    data = snap.to_dict()
    return float(data.get("credits_remaining", 0)) > 0