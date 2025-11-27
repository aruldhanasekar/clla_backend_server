# routes/chat_routes.py
"""
FastAPI Routes for Chat Service V3 - WITH GMAIL CONNECTION CHECK

NEW FEATURE:
- Checks Gmail connection before processing commitment queries
- Returns natural "connect Gmail first" response if not connected
- Uses LLM to generate contextual messages

Add to your main.py:
    from routes.chat_routes import router as chat_router
    app.include_router(chat_router, prefix="/api/chat", tags=["Chat"])
"""

import os
from fastapi import APIRouter, Request, HTTPException
from firebase_admin import auth, firestore
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from services.chat import ChatServiceV3, ChatRequest as ServiceChatRequest, create_chat_service
from services.gmail.commitments import fetch_commitments

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# Chat service singleton
_chat_service: Optional[ChatServiceV3] = None


def get_firestore_client():
    """Get Firestore client (lazy initialization)."""
    return firestore.client()


def get_chat_service() -> ChatServiceV3:
    """Get or create chat service singleton."""
    global _chat_service
    if _chat_service is None:
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
        _chat_service = create_chat_service(
            openai_api_key=OPENAI_API_KEY,
            commitment_fetcher=fetch_commitments,
            redis_url=UPSTASH_REDIS_REST_URL,
            redis_token=UPSTASH_REDIS_REST_TOKEN
        )
    return _chat_service


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def verify_token(request: Request):
    """Verify Firebase token or internal backend call."""
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
# GMAIL CONNECTION CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def is_gmail_connected(user_id: str) -> bool:
    """
    Check if user has Gmail connected via Composio.
    
    Returns:
        bool: True if Gmail is connected and active, False otherwise
    """
    try:
        from composio import Composio
        
        # Initialize Composio client with API key
        composio_client = Composio(api_key=COMPOSIO_API_KEY)
        
        # List connected accounts for this user, filtering for Gmail
        connected_accounts = composio_client.connected_accounts.list(
            user_ids=[user_id],
            toolkit_slugs=["GMAIL"]
        )
        
        # Check if there's an active Gmail connection
        for account in connected_accounts.items:
            if account.status == "ACTIVE":
                print(f"✅ Gmail connected for user {user_id}: {account.id}")
                return True
            else:
                print(f"⚠️ Gmail connection exists but status is: {account.status}")
                
        # No active connections found
        print(f"📭 Gmail NOT connected for user {user_id}")
        return False
        
    except Exception as e:
        print(f"❌ Error checking Gmail connection: {e}")
        return False


def is_commitment_query(message: str) -> bool:
    """
    Detect if message is a commitment-related query that needs Gmail.
    
    Args:
        message: User's message text
        
    Returns:
        bool: True if this is a commitment query
    """
    message_lower = message.lower()
    
    # Keywords that indicate commitment queries
    commitment_keywords = [
        "today", "tomorrow", "overdue", "due", "deadline", "urgent", "priority",
        "show", "list", "find", "get", "commitments", "tasks",
        "what's", "whats", "do i have", "have anything", "anything",
        "plate", "my plate", "on my plate",
        "from investor", "from customer", "from teammate",
        "waiting", "inbox", "sent", "received",
        "completed", "done", "finished", "deleted"
    ]
    
    return any(keyword in message_lower for keyword in commitment_keywords)


def generate_gmail_needed_response(user_message: str) -> str:
    """
    Generate natural response using OpenAI when Gmail is not connected.
    
    Args:
        user_message: User's original query
        
    Returns:
        str: Natural language response asking user to connect Gmail
    """
    try:
        import openai
        
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        
        prompt = f"""You are a helpful assistant. The user asked: "{user_message}"

To answer this question, you need access to their Gmail to check their commitments and emails.

Generate a friendly, natural response (2-3 sentences max) that:
1. Acknowledges what they're asking for
2. Explains you need Gmail access to help
3. Asks them to connect Gmail

Be conversational and helpful. Don't use bullet points or formal language.

Examples:
- User: "Do I have anything today?" → "I'd love to help you check what's on your plate today! To do that, I'll need access to your Gmail to scan your emails for commitments. Could you connect your Gmail account?"
- User: "Show me overdue tasks" → "I can help you find overdue items! First, I'll need to connect to your Gmail so I can check your emails. Mind connecting it real quick?"
- User: "What's due tomorrow?" → "Let me help you see what's coming up tomorrow! I'll need Gmail access to check your commitments from emails. Can you connect your Gmail?"

Now generate a response for: "{user_message}"

Response (no quotes, just the text):"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        print(f"❌ Error generating Gmail response: {e}")
        # Fallback response
        return "I'd love to help you with that! To check your commitments, I'll need access to your Gmail. Could you connect it first?"


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST/RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class ChatMessageRequest(BaseModel):
    """Request for sending a message."""
    message: str
    chat_page_id: Optional[str] = None  # None = create new chat


class ChatMessageResponse(BaseModel):
    """Response from sending a message."""
    success: bool
    message: str
    chat_page_id: str
    conversation_id: str
    intent: str
    function_called: Optional[str] = None
    filters_applied: Optional[dict] = None
    commitments_found: int = 0
    commitments: list = []
    summary: dict = {}
    timestamp: str
    tokens_used: int = 0
    error: Optional[str] = None


class NewChatResponse(BaseModel):
    """Response for creating a new chat."""
    chat_page_id: str
    title: str
    created_at: str


class ChatHistoryResponse(BaseModel):
    """Response for chat history."""
    chat_page_id: str
    title: str
    created_at: str
    conversations: list


class UserChatsResponse(BaseModel):
    """Response for user's chat list."""
    chats: list


class DeleteChatResponse(BaseModel):
    """Response for deleting a chat."""
    deleted: bool
    chat_page_id: str


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

router = APIRouter()


@router.post("/new", response_model=NewChatResponse)
async def create_new_chat(request: Request):
    """
    Create a new chat page.
    
    Response:
    {
        "chat_page_id": "chat_abc123",
        "title": "New Chat",
        "created_at": "2025-11-22T10:00:00Z"
    }
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    chat_service = get_chat_service()
    result = chat_service.create_new_chat(user_id)
    
    print(f"✅ New chat created: {result['chat_page_id']} for user {user_id}")
    
    return NewChatResponse(**result)


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(request: Request, body: ChatMessageRequest):
    """
    Send a message and get response.
    
    ✅ NEW: Checks Gmail connection for commitment queries
    
    Request:
    {
        "message": "What do I have today?",
        "chat_page_id": "chat_abc123"  // null for new chat
    }
    
    Response:
    {
        "success": true,
        "message": "Here's your snapshot...",
        "chat_page_id": "chat_abc123",
        "conversation_id": "conv_xyz789",
        "intent": "today_snapshot",
        ...
    }
    
    OR if Gmail not connected:
    {
        "success": true,
        "message": "I'd love to help! First, please connect Gmail...",
        "intent": "gmail_not_connected",
        ...
    }
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    print(f"💬 Chat | User: {user_id} | Page: {body.chat_page_id or 'NEW'}")
    print(f"📝 Message: {body.message}")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # ✅ NEW: CHECK GMAIL CONNECTION FOR COMMITMENT QUERIES
    # ═══════════════════════════════════════════════════════════════════════════════
    
    if is_commitment_query(body.message):
        print("🔍 Detected commitment query - checking Gmail connection...")
        
        if not is_gmail_connected(user_id):
            print("⚠️ Gmail NOT connected - returning connection prompt")
            
            # Generate natural response
            natural_message = generate_gmail_needed_response(body.message)
            
            # Create or get chat page ID
            chat_service = get_chat_service()
            if not body.chat_page_id:
                new_chat = chat_service.create_new_chat(user_id)
                chat_page_id = new_chat["chat_page_id"]
            else:
                chat_page_id = body.chat_page_id
            
            # Create conversation ID
            conversation_id = f"conv_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}"
            
            # Store in Firestore (optional - for history)
            try:
                db = get_firestore_client()
                db.collection("users").document(user_id)\
                    .collection("chats").document(chat_page_id)\
                    .collection("conversations").document(conversation_id).set({
                        "user_message": body.message,
                        "assistant_message": natural_message,
                        "intent": "gmail_not_connected",
                        "timestamp": datetime.utcnow().isoformat(),
                        "gmail_connected": False
                    })
            except Exception as e:
                print(f"⚠️ Failed to store conversation: {e}")
            
            # Return special response
            return ChatMessageResponse(
                success=True,
                message=natural_message,
                chat_page_id=chat_page_id,
                conversation_id=conversation_id,
                intent="gmail_not_connected",
                function_called=None,
                filters_applied=None,
                commitments_found=0,
                commitments=[],
                summary={},
                timestamp=datetime.utcnow().isoformat(),
                tokens_used=0,
                error=None
            )
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # GMAIL CONNECTED OR NON-COMMITMENT QUERY - PROCESS NORMALLY
    # ═══════════════════════════════════════════════════════════════════════════════
    
    chat_service = get_chat_service()
    
    service_request = ServiceChatRequest(
        user_id=user_id,
        message=body.message,
        chat_page_id=body.chat_page_id
    )
    
    response = chat_service.process_message(service_request)
    
    print(f"✅ Response | Intent: {response.intent} | Function: {response.function_called}")
    
    return ChatMessageResponse(
        success=response.success,
        message=response.message,
        chat_page_id=response.chat_page_id,
        conversation_id=response.conversation_id,
        intent=response.intent,
        function_called=response.function_called,
        filters_applied=response.filters_applied,
        commitments_found=response.commitments_found,
        commitments=response.commitments,
        summary=response.summary,
        timestamp=response.timestamp,
        tokens_used=response.tokens_used,
        error=response.error
    )


@router.get("/history/{chat_page_id}", response_model=ChatHistoryResponse)
async def get_chat_history(request: Request, chat_page_id: str):
    """
    Get full conversation history for a chat page.
    
    Response:
    {
        "chat_page_id": "chat_abc123",
        "title": "Commitments - Nov 22",
        "created_at": "2025-11-22T10:00:00Z",
        "conversations": [
            {
                "conversation_id": "conv_001",
                "user_message": "What do I have today?",
                "assistant_message": "Here's your snapshot...",
                "timestamp": "2025-11-22T10:01:00Z"
            }
        ]
    }
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    chat_service = get_chat_service()
    result = chat_service.get_chat_history(user_id, chat_page_id)
    
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    
    return ChatHistoryResponse(**result)


@router.get("/list", response_model=UserChatsResponse)
async def get_user_chats(request: Request, limit: int = 20):
    """
    Get all chat pages for current user.
    
    Response:
    {
        "chats": [
            {
                "chat_page_id": "chat_abc123",
                "title": "Commitments - Nov 22",
                "created_at": "2025-11-22T10:00:00Z",
                "updated_at": "2025-11-22T12:00:00Z"
            }
        ]
    }
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    chat_service = get_chat_service()
    chats = chat_service.get_user_chats(user_id, limit)
    
    return UserChatsResponse(chats=chats)


@router.delete("/{chat_page_id}", response_model=DeleteChatResponse)
async def delete_chat(request: Request, chat_page_id: str):
    """
    Delete a chat page and all its conversations.
    
    Response:
    {
        "deleted": true,
        "chat_page_id": "chat_abc123"
    }
    """
    decoded = verify_token(request)
    user_id = decoded.get("uid")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    
    chat_service = get_chat_service()
    result = chat_service.delete_chat(user_id, chat_page_id)
    
    print(f"🗑️ Deleted chat: {chat_page_id} for user {user_id}")
    
    return DeleteChatResponse(**result)


@router.get("/health")
async def chat_health():
    """Health check for chat service."""
    try:
        service = get_chat_service()
        return {
            "status": "healthy",
            "service": "chat_v3",
            "features": [
                "conversation_history",
                "today_snapshot",
                "redis_cache",
                "function_calling",
                "gmail_connection_check"  # NEW
            ],
            "firestore_path": "users/{user_id}/chats/{chat_page_id}/conversations/{conv_id}",
            "openai_configured": bool(OPENAI_API_KEY),
            "redis_configured": bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN),
            "composio_configured": bool(COMPOSIO_API_KEY)  # NEW
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }