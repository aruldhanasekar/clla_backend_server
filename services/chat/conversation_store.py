# services/chat/conversation_store.py
"""
Conversation Store - Manages chat history with Firestore + Upstash Redis.

UPDATED: Now saves and loads commitments array for chat history display.

Firestore Structure:
└── users/{user_id}/
    └── chats/{chat_page_id}/
        └── conversations/{conversation_id}/
"""

import os
import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional, List
import requests

from firebase_admin import firestore


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Message:
    """Single message in conversation."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", "")
        )


@dataclass
class Conversation:
    """Single conversation exchange (user + assistant)."""
    conversation_id: str
    user_message: str
    assistant_message: str
    timestamp: str
    intent: str = ""
    function_called: Optional[str] = None
    filters_applied: Optional[dict] = None
    commitments_found: int = 0
    commitments: List[dict] = field(default_factory=list)  # ← NEW: Store actual commitments
    summary: Optional[dict] = None  # ← NEW: Store summary data
    
    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "user_message": self.user_message,
            "assistant_message": self.assistant_message,
            "timestamp": self.timestamp,
            "intent": self.intent,
            "function_called": self.function_called,
            "filters_applied": self.filters_applied,
            "commitments_found": self.commitments_found,
            "commitments": self.commitments,  # ← NEW
            "summary": self.summary  # ← NEW
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        return cls(
            conversation_id=data.get("conversation_id", ""),
            user_message=data.get("user_message", ""),
            assistant_message=data.get("assistant_message", ""),
            timestamp=data.get("timestamp", ""),
            intent=data.get("intent", ""),
            function_called=data.get("function_called"),
            filters_applied=data.get("filters_applied"),
            commitments_found=data.get("commitments_found", 0),
            commitments=data.get("commitments", []),  # ← NEW
            summary=data.get("summary")  # ← NEW
        )


@dataclass
class ChatPage:
    """Chat page container."""
    chat_page_id: str
    user_id: str
    title: str
    created_at: str
    updated_at: str
    conversations: list[Conversation] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "chat_page_id": self.chat_page_id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }
    
    @classmethod
    def from_dict(cls, data: dict, conversations: list = None) -> "ChatPage":
        return cls(
            chat_page_id=data.get("chat_page_id", ""),
            user_id=data.get("user_id", ""),
            title=data.get("title", "New Chat"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            conversations=conversations or []
        )


# ═══════════════════════════════════════════════════════════════════════════════
# UPSTASH REDIS CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class UpstashRedis:
    """Simple Upstash Redis REST client."""
    
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
    
    def _request(self, command: list) -> any:
        """Execute Redis command via REST API."""
        try:
            response = requests.post(
                f"{self.url}",
                headers=self.headers,
                json=command,
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("result")
            return None
        except Exception as e:
            print(f"Redis error: {e}")
            return None
    
    def get(self, key: str) -> Optional[str]:
        """Get value by key."""
        return self._request(["GET", key])
    
    def set(self, key: str, value: str, ex: int = None) -> bool:
        """Set value with optional expiry (seconds)."""
        if ex:
            result = self._request(["SET", key, value, "EX", str(ex)])
        else:
            result = self._request(["SET", key, value])
        return result == "OK"
    
    def delete(self, key: str) -> bool:
        """Delete key."""
        result = self._request(["DEL", key])
        return result == 1
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        result = self._request(["EXISTS", key])
        return result == 1


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION STORE
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationStore:
    """
    Manages conversation history with Firestore + Upstash Redis.
    
    Firestore Path: users/{user_id}/chats/{chat_page_id}/conversations/{conv_id}
    """
    
    CACHE_TTL = 1800  # 30 minutes
    MAX_HISTORY_MESSAGES = 20  # Max messages to keep in context
    
    def __init__(self, redis_url: str = None, redis_token: str = None):
        self.db = firestore.client()
        
        # Initialize Redis if credentials provided
        redis_url = redis_url or os.getenv("UPSTASH_REDIS_REST_URL")
        redis_token = redis_token or os.getenv("UPSTASH_REDIS_REST_TOKEN")
        
        if redis_url and redis_token:
            self.redis = UpstashRedis(redis_url, redis_token)
            print("✅ Upstash Redis connected")
        else:
            self.redis = None
            print("⚠️ Redis not configured, using Firestore only")
    
    def _cache_key(self, user_id: str, chat_page_id: str) -> str:
        """Generate cache key."""
        return f"chat:{user_id}:{chat_page_id}"
    
    def _generate_id(self, prefix: str = "") -> str:
        """Generate unique ID."""
        unique = uuid.uuid4().hex[:12]
        return f"{prefix}_{unique}" if prefix else unique
    
    def _generate_title(self, first_message: str) -> str:
        """Generate chat title from first message."""
        title = first_message.strip()[:50]
        if len(first_message) > 50:
            title += "..."
        return title or "New Chat"
    
    def _get_user_chats_ref(self, user_id: str):
        """Get reference to user's chats subcollection."""
        return self.db.collection("users").document(user_id).collection("chats")
    
    def _get_chat_ref(self, user_id: str, chat_page_id: str):
        """Get reference to a specific chat document."""
        return self._get_user_chats_ref(user_id).document(chat_page_id)
    
    def _get_conversations_ref(self, user_id: str, chat_page_id: str):
        """Get reference to conversations subcollection."""
        return self._get_chat_ref(user_id, chat_page_id).collection("conversations")
    
    # ───────────────────────────────────────────────────────────────────────────
    # CHAT PAGE OPERATIONS
    # ───────────────────────────────────────────────────────────────────────────
    
    def create_chat_page(self, user_id: str, first_message: str = None) -> ChatPage:
        """Create a new chat page under user's chats subcollection."""
        now = datetime.now(timezone.utc).isoformat()
        
        chat_page = ChatPage(
            chat_page_id=self._generate_id("chat"),
            user_id=user_id,
            title=self._generate_title(first_message) if first_message else "New Chat",
            created_at=now,
            updated_at=now,
            conversations=[]
        )
        
        # Save to Firestore: users/{user_id}/chats/{chat_page_id}
        self._get_chat_ref(user_id, chat_page.chat_page_id).set(chat_page.to_dict())
        
        print(f"✅ Created chat page: {chat_page.chat_page_id} under user/{user_id}")
        return chat_page
    
    def get_chat_page(self, user_id: str, chat_page_id: str) -> Optional[ChatPage]:
        """Get chat page by ID."""
        doc = self._get_chat_ref(user_id, chat_page_id).get()
        if not doc.exists:
            return None
        
        data = doc.to_dict()
        conversations = self.get_conversations(user_id, chat_page_id)
        return ChatPage.from_dict(data, conversations)
    
    def update_chat_title(self, user_id: str, chat_page_id: str, title: str):
        """Update chat page title."""
        self._get_chat_ref(user_id, chat_page_id).update({
            "title": title,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
    
    def get_user_chat_pages(self, user_id: str, limit: int = 20) -> list[ChatPage]:
        """Get all chat pages for a user."""
        docs = (
            self._get_user_chats_ref(user_id)
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        
        return [ChatPage.from_dict(doc.to_dict()) for doc in docs]
    
    def delete_chat_page(self, user_id: str, chat_page_id: str):
        """Delete a chat page and all its conversations."""
        # Delete all conversations first
        convs_ref = self._get_conversations_ref(user_id, chat_page_id)
        for doc in convs_ref.stream():
            doc.reference.delete()
        
        # Delete chat page
        self._get_chat_ref(user_id, chat_page_id).delete()
        
        # Clear cache
        self.clear_cache(user_id, chat_page_id)
        
        print(f"🗑️ Deleted chat page: {chat_page_id}")
    
    # ───────────────────────────────────────────────────────────────────────────
    # CONVERSATION OPERATIONS (UPDATED)
    # ───────────────────────────────────────────────────────────────────────────
    
    def add_conversation(
        self,
        user_id: str,
        chat_page_id: str,
        user_message: str,
        assistant_message: str,
        intent: str = "",
        function_called: str = None,
        filters_applied: dict = None,
        commitments_found: int = 0,
        commitments: List[dict] = None,  # ← NEW PARAMETER
        summary: dict = None  # ← NEW PARAMETER
    ) -> Conversation:
        """Add a conversation exchange to a chat page."""
        now = datetime.now(timezone.utc).isoformat()
        
        conversation = Conversation(
            conversation_id=self._generate_id("conv"),
            user_message=user_message,
            assistant_message=assistant_message,
            timestamp=now,
            intent=intent,
            function_called=function_called,
            filters_applied=filters_applied,
            commitments_found=commitments_found,
            commitments=commitments or [],  # ← NEW
            summary=summary  # ← NEW
        )
        
        # Save to Firestore: users/{user_id}/chats/{chat_page_id}/conversations/{conv_id}
        self._get_conversations_ref(user_id, chat_page_id).document(
            conversation.conversation_id
        ).set(conversation.to_dict())
        
        # Update chat page timestamp
        self._get_chat_ref(user_id, chat_page_id).update({
            "updated_at": now
        })
        
        # Update Redis cache
        self._update_cache(user_id, chat_page_id)
        
        print(f"💾 Saved conversation with {len(commitments or [])} commitments")
        return conversation
    
    def get_conversations(self, user_id: str, chat_page_id: str, limit: int = None) -> list[Conversation]:
        """Get all conversations for a chat page."""
        limit = limit or self.MAX_HISTORY_MESSAGES
        
        docs = (
            self._get_conversations_ref(user_id, chat_page_id)
            .order_by("timestamp", direction=firestore.Query.ASCENDING)
            .limit(limit)
            .stream()
        )
        
        return [Conversation.from_dict(doc.to_dict()) for doc in docs]
    
    # ───────────────────────────────────────────────────────────────────────────
    # MESSAGE HISTORY FOR LLM
    # ───────────────────────────────────────────────────────────────────────────
    
    def get_message_history(self, user_id: str, chat_page_id: str) -> list[Message]:
        """
        Get message history for LLM context.
        Tries Redis first, falls back to Firestore.
        """
        # Try Redis cache first
        if self.redis:
            cache_key = self._cache_key(user_id, chat_page_id)
            cached = self.redis.get(cache_key)
            if cached:
                try:
                    data = json.loads(cached)
                    messages = [Message.from_dict(m) for m in data.get("messages", [])]
                    print(f"📦 Cache hit: {len(messages)} messages")
                    return messages[-self.MAX_HISTORY_MESSAGES:]
                except json.JSONDecodeError:
                    pass
        
        # Fallback to Firestore
        print("📂 Fetching from Firestore...")
        conversations = self.get_conversations(user_id, chat_page_id)
        
        messages = []
        for conv in conversations:
            messages.append(Message(role="user", content=conv.user_message, timestamp=conv.timestamp))
            messages.append(Message(role="assistant", content=conv.assistant_message, timestamp=conv.timestamp))
        
        # Update cache
        self._set_cache(user_id, chat_page_id, messages)
        
        return messages[-self.MAX_HISTORY_MESSAGES:]
    
    def _update_cache(self, user_id: str, chat_page_id: str):
        """Update Redis cache after new conversation."""
        if not self.redis:
            return
        
        # Get fresh conversations and update cache
        conversations = self.get_conversations(user_id, chat_page_id)
        messages = []
        for conv in conversations:
            messages.append(Message(role="user", content=conv.user_message, timestamp=conv.timestamp))
            messages.append(Message(role="assistant", content=conv.assistant_message, timestamp=conv.timestamp))
        
        self._set_cache(user_id, chat_page_id, messages)
    
    def _set_cache(self, user_id: str, chat_page_id: str, messages: list[Message]):
        """Set Redis cache."""
        if not self.redis:
            return
        
        cache_key = self._cache_key(user_id, chat_page_id)
        data = {
            "messages": [m.to_dict() for m in messages[-self.MAX_HISTORY_MESSAGES:]],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        self.redis.set(cache_key, json.dumps(data), ex=self.CACHE_TTL)
        print(f"📦 Cache updated: {len(messages)} messages")
    
    def clear_cache(self, user_id: str, chat_page_id: str):
        """Clear Redis cache for a chat."""
        if self.redis:
            cache_key = self._cache_key(user_id, chat_page_id)
            self.redis.delete(cache_key)


def create_conversation_store(redis_url: str = None, redis_token: str = None) -> ConversationStore:
    """Factory function to create ConversationStore."""
    return ConversationStore(redis_url=redis_url, redis_token=redis_token)