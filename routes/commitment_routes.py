# routes/commitment_routes.py
"""
FastAPI Routes for Commitment Management - COMPLETE VERSION

Features:
1. Mark as complete/incomplete
2. Delete (with Redis backup)
3. Get completed commitments (with "today" filter)
4. Get deleted commitments from Redis cache
"""

import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException, Query
from firebase_admin import auth, firestore
from pydantic import BaseModel
from typing import Optional, List

# Redis for deleted items backup
try:
    from upstash_redis import Redis
    UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
    UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        redis_client = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
        print("✅ Redis connected for commitment backup")
    else:
        redis_client = None
        print("⚠️ Redis not configured for commitment backup")
except Exception as e:
    redis_client = None
    print(f"⚠️ Redis connection failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER: Get Firestore client
# ═══════════════════════════════════════════════════════════════════════════════
def get_db():
    """Get Firestore client instance."""
    return firestore.client()


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def verify_token(request: Request):
    """Verify Firebase token."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class MarkCompleteRequest(BaseModel):
    completed: bool = True


class MarkCompleteResponse(BaseModel):
    success: bool
    commitment_id: str
    completed: bool
    completed_at: Optional[str] = None
    message: str


class DeleteCommitmentResponse(BaseModel):
    success: bool
    commitment_id: str
    message: str
    backup_expires_in: str = "24 hours"


class CompletedCommitmentsResponse(BaseModel):
    success: bool
    count: int
    commitments: list
    filter_applied: Optional[str] = None


class DeletedCommitmentsResponse(BaseModel):
    success: bool
    count: int
    commitments: list
    message: str


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_commitment_by_field(user_id: str, commitment_id: str):
    """
    Find commitment document by querying the commitment_id FIELD.
    Returns: (doc_ref, doc_snapshot) or (None, None) if not found
    """
    db = get_db()
    collection_ref = db.collection("users").document(user_id).collection("commitments")
    
    print(f"🔍 Searching for commitment_id field = '{commitment_id}'")
    
    # Query by commitment_id field
    query = collection_ref.where("commitment_id", "==", commitment_id).limit(1)
    docs = list(query.stream())
    
    print(f"🔍 Found {len(docs)} documents")
    
    if not docs:
        # Also try using commitment_id as document ID
        print(f"🔍 Trying document ID lookup...")
        doc_ref = collection_ref.document(commitment_id)
        doc = doc_ref.get()
        if doc.exists:
            print(f"✅ Found by document ID!")
            return doc_ref, doc
        return None, None
    
    doc = docs[0]
    print(f"✅ Found document with ID: {doc.id}")
    return doc.reference, doc


def backup_to_redis(user_id: str, commitment_id: str, commitment_data: dict):
    """Backup deleted commitment to Redis for 24 hours."""
    if not redis_client:
        print("⚠️ Redis not available, skipping backup")
        return False
    
    try:
        key = f"deleted_commitment:{user_id}:{commitment_id}"
        data = {
            "commitment_id": commitment_id,
            "user_id": user_id,
            "data": commitment_data,
            "deleted_at": datetime.now(timezone.utc).isoformat()
        }
        # Store for 24 hours (86400 seconds)
        redis_client.setex(key, 86400, json.dumps(data, default=str))
        print(f"✅ Backed up commitment {commitment_id} to Redis (expires in 24h)")
        return True
    except Exception as e:
        print(f"❌ Redis backup failed: {e}")
        return False


def get_deleted_from_redis(user_id: str) -> List[dict]:
    """Get all deleted commitments for a user from Redis."""
    if not redis_client:
        print("⚠️ Redis not available")
        return []
    
    try:
        # Pattern to match all deleted commitments for this user
        pattern = f"deleted_commitment:{user_id}:*"
        
        # Get all keys matching the pattern
        keys = redis_client.keys(pattern)
        print(f"🔍 Found {len(keys)} deleted commitment keys in Redis")
        
        deleted_items = []
        for key in keys:
            try:
                data = redis_client.get(key)
                if data:
                    if isinstance(data, str):
                        item = json.loads(data)
                    else:
                        item = data
                    deleted_items.append(item)
            except Exception as e:
                print(f"⚠️ Error parsing Redis key {key}: {e}")
                continue
        
        # Sort by deleted_at (most recent first)
        deleted_items.sort(
            key=lambda x: x.get("deleted_at", ""),
            reverse=True
        )
        
        return deleted_items
    except Exception as e:
        print(f"❌ Error fetching from Redis: {e}")
        return []


def is_today(iso_string: str) -> bool:
    """Check if an ISO timestamp is from today."""
    if not iso_string:
        return False
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        today = datetime.now(timezone.utc).date()
        return dt.date() == today
    except:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════════
# MARK COMPLETE
# ═══════════════════════════════════════════════════════════════════════════════

@router.patch("/{commitment_id}/complete", response_model=MarkCompleteResponse)
async def mark_commitment_complete(
    request: Request, 
    commitment_id: str, 
    body: MarkCompleteRequest
):
    """Mark a commitment as completed or reopen it."""
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"\n{'='*60}")
    print(f"📝 MARK COMPLETE REQUEST")
    print(f"   User: {user_id}")
    print(f"   Commitment ID: {commitment_id}")
    print(f"   Completed: {body.completed}")
    print(f"{'='*60}\n")
    
    try:
        doc_ref, doc_snapshot = get_commitment_by_field(user_id, commitment_id)
        
        if not doc_ref:
            print(f"❌ Commitment NOT FOUND: {commitment_id}")
            raise HTTPException(status_code=404, detail="Commitment not found")
        
        now = datetime.now(timezone.utc).isoformat()
        update_data = {
            "completed": body.completed,
            "status": "completed" if body.completed else "active",
            "updated_at": now
        }
        
        if body.completed:
            update_data["completed_at"] = now
        else:
            update_data["completed_at"] = None
        
        doc_ref.update(update_data)
        
        action = "completed" if body.completed else "reopened"
        print(f"✅ Commitment {commitment_id} marked as {action}")
        
        return MarkCompleteResponse(
            success=True,
            commitment_id=commitment_id,
            completed=body.completed,
            completed_at=now if body.completed else None,
            message=f"Commitment marked as {action}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE COMMITMENT
# ═══════════════════════════════════════════════════════════════════════════════

@router.delete("/{commitment_id}", response_model=DeleteCommitmentResponse)
async def delete_commitment(request: Request, commitment_id: str):
    """Delete a commitment (backs up to Redis for 24 hours)."""
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"\n{'='*60}")
    print(f"🗑️ DELETE REQUEST")
    print(f"   User: {user_id}")
    print(f"   Commitment ID: {commitment_id}")
    print(f"{'='*60}\n")
    
    try:
        doc_ref, doc_snapshot = get_commitment_by_field(user_id, commitment_id)
        
        if not doc_ref:
            print(f"❌ Commitment NOT FOUND: {commitment_id}")
            raise HTTPException(status_code=404, detail="Commitment not found")
        
        commitment_data = doc_snapshot.to_dict()
        backup_to_redis(user_id, commitment_id, commitment_data)
        doc_ref.delete()
        
        print(f"✅ Commitment {commitment_id} deleted")
        
        return DeleteCommitmentResponse(
            success=True,
            commitment_id=commitment_id,
            message="Commitment deleted successfully",
            backup_expires_in="24 hours"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# GET COMPLETED COMMITMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/completed", response_model=CompletedCommitmentsResponse)
async def get_completed_commitments(
    request: Request, 
    limit: int = Query(default=50, ge=1, le=100),
    today_only: bool = Query(default=False, description="Only show items completed today")
):
    """
    Get completed commitments for the user.
    
    Query params:
    - limit: Max number of items (default: 50)
    - today_only: If true, only return items completed today
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"\n{'='*60}")
    print(f"📋 GET COMPLETED REQUEST")
    print(f"   User: {user_id}")
    print(f"   Today only: {today_only}")
    print(f"{'='*60}\n")
    
    try:
        db = get_db()
        commitments_ref = db.collection("users").document(user_id).collection("commitments")
        query = commitments_ref.where("completed", "==", True).limit(limit)
        
        docs = query.stream()
        
        commitments = []
        for doc in docs:
            data = doc.to_dict()
            
            # Filter by today if requested
            if today_only:
                completed_at = data.get("completed_at")
                if not is_today(completed_at):
                    continue
            
            commitments.append({
                "commitment_id": data.get("commitment_id", doc.id),
                "what": data.get("what", ""),
                "to_whom": data.get("to_whom"),
                "deadline_iso": data.get("deadline_iso"),
                "deadline_raw": data.get("deadline_raw"),
                "status": "completed",
                "completed": True,
                "completed_at": data.get("completed_at"),
                "priority": data.get("priority"),
                "estimated_hours": data.get("estimated_hours"),
                "email_sender": data.get("email_sender"),
                "email_sender_name": data.get("email_sender_name"),
                "email_subject": data.get("email_subject"),
                "sender_role": data.get("sender_role"),
            })
        
        filter_msg = "completed today" if today_only else "all completed"
        print(f"📋 Found {len(commitments)} {filter_msg} commitments")
        
        return CompletedCommitmentsResponse(
            success=True,
            count=len(commitments),
            commitments=commitments,
            filter_applied=filter_msg
        )
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# GET DELETED COMMITMENTS (FROM REDIS)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/deleted", response_model=DeletedCommitmentsResponse)
async def get_deleted_commitments(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50)
):
    """
    Get deleted commitments from Redis cache.
    These are kept for 24 hours after deletion.
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"\n{'='*60}")
    print(f"🗑️ GET DELETED REQUEST")
    print(f"   User: {user_id}")
    print(f"{'='*60}\n")
    
    if not redis_client:
        return DeletedCommitmentsResponse(
            success=True,
            count=0,
            commitments=[],
            message="Redis not configured - deleted items cannot be retrieved"
        )
    
    try:
        deleted_items = get_deleted_from_redis(user_id)
        
        # Limit results
        deleted_items = deleted_items[:limit]
        
        # Format for response
        commitments = []
        for item in deleted_items:
            data = item.get("data", {})
            commitments.append({
                "commitment_id": item.get("commitment_id"),
                "what": data.get("what", ""),
                "to_whom": data.get("to_whom"),
                "deadline_iso": data.get("deadline_iso"),
                "deadline_raw": data.get("deadline_raw"),
                "status": "deleted",
                "deleted_at": item.get("deleted_at"),
                "priority": data.get("priority"),
                "estimated_hours": data.get("estimated_hours"),
                "email_sender": data.get("email_sender"),
                "email_sender_name": data.get("email_sender_name"),
                "email_subject": data.get("email_subject"),
                "sender_role": data.get("sender_role"),
                "original_status": data.get("status"),
            })
        
        print(f"🗑️ Found {len(commitments)} deleted commitments in Redis")
        
        if len(commitments) == 0:
            message = "No deleted commitments found (items are kept for 24 hours after deletion)"
        else:
            message = f"Found {len(commitments)} deleted commitment(s) - these will expire 24 hours after deletion"
        
        return DeletedCommitmentsResponse(
            success=True,
            count=len(commitments),
            commitments=commitments,
            message=message
        )
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# RESTORE DELETED COMMITMENT
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/restore/{commitment_id}")
async def restore_deleted_commitment(request: Request, commitment_id: str):
    """
    Restore a deleted commitment from Redis backup.
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"\n{'='*60}")
    print(f"♻️ RESTORE REQUEST")
    print(f"   User: {user_id}")
    print(f"   Commitment ID: {commitment_id}")
    print(f"{'='*60}\n")
    
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis not configured - cannot restore")
    
    try:
        # Get from Redis
        key = f"deleted_commitment:{user_id}:{commitment_id}"
        data = redis_client.get(key)
        
        if not data:
            raise HTTPException(status_code=404, detail="Deleted commitment not found in backup")
        
        if isinstance(data, str):
            item = json.loads(data)
        else:
            item = data
        
        commitment_data = item.get("data", {})
        
        # Restore to Firestore
        db = get_db()
        doc_ref = db.collection("users").document(user_id).collection("commitments").document(commitment_id)
        
        # Update timestamps
        commitment_data["restored_at"] = datetime.now(timezone.utc).isoformat()
        commitment_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        commitment_data["completed"] = False
        commitment_data["status"] = "active"
        
        doc_ref.set(commitment_data)
        
        # Remove from Redis
        redis_client.delete(key)
        
        print(f"✅ Commitment {commitment_id} restored from backup")
        
        return {
            "success": True,
            "commitment_id": commitment_id,
            "message": "Commitment restored successfully",
            "commitment": {
                "what": commitment_data.get("what"),
                "deadline_iso": commitment_data.get("deadline_iso"),
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def commitment_health():
    """Health check."""
    return {
        "status": "healthy",
        "service": "commitments",
        "redis_available": redis_client is not None,
        "features": [
            "mark_complete",
            "delete_with_backup",
            "get_completed",
            "get_completed_today",
            "get_deleted",
            "restore_deleted"
        ]
    }