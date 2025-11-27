# services/chat/__init__.py
"""
Chat Service V3 - With Conversation History and Today's Snapshot.

Firestore Path: users/{user_id}/chats/{chat_page_id}/conversations/{conv_id}

Features:
- Conversation continuation (LLM remembers context)
- Today's snapshot: overdue, due today, received today, due tomorrow
- Redis cache for fast history access
- Firestore storage under users/{user_id}/chats/
- Chat page management

Usage:
    from services.chat import ChatServiceV3, ChatRequest, create_chat_service
    
    # Create service
    chat_service = create_chat_service(
        openai_api_key="sk-...",
        commitment_fetcher=fetch_commitments,
        redis_url="https://...",
        redis_token="..."
    )
    
    # Process a message (creates new chat if chat_page_id is None)
    request = ChatRequest(
        user_id="user123",
        message="What do I have today?",
        chat_page_id=None  # or existing chat_page_id
    )
    
    response = chat_service.process_message(request)
    print(response.message)
    print(response.chat_page_id)  # Use this for continuation
"""

from .chat_service import (
    ChatServiceV3,
    ChatRequest,
    ChatResponse,
    create_chat_service,
)

from .conversation_store import (
    ConversationStore,
    Message,
    Conversation,
    ChatPage,
    create_conversation_store,
)

from .prompts import (
    get_system_prompt,
    get_tools,
    COMMITMENT_FUNCTION,
    TODAY_SNAPSHOT_FUNCTION,
)

__all__ = [
    # Main service
    "ChatServiceV3",
    "ChatRequest",
    "ChatResponse",
    "create_chat_service",
    
    # Conversation store
    "ConversationStore",
    "Message",
    "Conversation",
    "ChatPage",
    "create_conversation_store",
    
    # Prompts
    "get_system_prompt",
    "get_tools",
    "COMMITMENT_FUNCTION",
    "TODAY_SNAPSHOT_FUNCTION",
]
