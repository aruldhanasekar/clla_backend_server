# routes/credit_routes.py
"""
Credit Management API Routes
"""

from fastapi import APIRouter, Request, HTTPException
from firebase_admin import firestore, auth

router = APIRouter()

# ✅ FIX: Don't initialize db at module level
# db = firestore.client()  ← REMOVED


def _get_db():
    """Get Firestore client (lazy initialization)"""
    return firestore.client()


def verify_token(request: Request):
    """Verify Firebase JWT token"""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")
    
    if auth_header.startswith("Bearer INTERNAL_CALL_"):
        internal_uid = auth_header.replace("Bearer INTERNAL_CALL_", "")
        return {"uid": internal_uid}
    
    token = auth_header.replace("Bearer ", "")
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except Exception as e:
        print(f"Token verification error: {e}")
        raise HTTPException(status_code=401, detail="Invalid Firebase Id token")


@router.get("/status")
def get_credit_status(request: Request):
    """
    Get user's current credit status
    
    Returns:
        {
            "user_id": str,
            "credits_total": float,
            "credits_used": float,
            "credits_remaining": float,
            "percentage_used": float,
            "tokens_per_credit_input": int,
            "tokens_per_credit_output": int,
            "plan_type": str,
            "warning": str (optional)
        }
    """
    db = _get_db()  # ← Initialize here
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    from credit_engine import initialize_credits_if_missing
    initialize_credits_if_missing(user_id)
    
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    data = user_doc.to_dict()
    
    credits_total = float(data.get("credits_total", 2500))
    credits_used = float(data.get("credits_used", 0))
    credits_remaining = float(data.get("credits_remaining", credits_total))
    
    percentage_used = (credits_used / credits_total * 100) if credits_total > 0 else 0
    
    response = {
        "user_id": user_id,
        "credits_total": credits_total,
        "credits_used": round(credits_used, 2),
        "credits_remaining": round(credits_remaining, 2),
        "percentage_used": round(percentage_used, 2),
        "tokens_per_credit_input": data.get("tokens_per_credit_input", 6703),
        "tokens_per_credit_output": data.get("tokens_per_credit_output", 1671),
        "plan_type": "free"
    }
    
    # Add warnings
    if percentage_used >= 100:
        response["warning"] = "credits_exhausted"
        response["warning_message"] = "You have used all your credits. Please upgrade to continue."
    elif percentage_used >= 90:
        response["warning"] = "low_credits"
        response["warning_message"] = f"Only {round(credits_remaining, 2)} credits remaining ({round(100-percentage_used, 1)}%)"
    elif percentage_used >= 75:
        response["warning"] = "approaching_limit"
        response["warning_message"] = f"{round(credits_remaining, 2)} credits remaining"
    
    return response


@router.post("/reset")
def reset_credits_admin(request: Request):
    """
    Reset user credits (for testing or manual top-up)
    """
    db = _get_db()  # ← Initialize here
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    from credit_config import DEFAULT_FREE_TRIAL_CREDITS
    
    user_ref = db.collection("users").document(user_id)
    user_ref.update({
        "credits_used": 0.0,
        "credits_remaining": float(DEFAULT_FREE_TRIAL_CREDITS)
    })
    
    return {
        "success": True,
        "user_id": user_id,
        "credits_reset_to": DEFAULT_FREE_TRIAL_CREDITS,
        "message": "Credits reset successfully"
    }