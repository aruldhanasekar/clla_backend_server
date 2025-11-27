# services/chat/chat_service.py
"""
Chat Service V3 - WITH HISTORY COMMITMENTS SUPPORT

UPDATED: Now saves commitments to Firestore so they appear in chat history on reload.

Features:
- Conversation continuation (remembers context)
- Today's snapshot: overdue, due today, received today, due tomorrow
- Deleted commitments retrieval from Redis cache
- Completed today filter
- SAVES COMMITMENTS TO HISTORY for reload display
"""

import os
import json
import requests
from datetime import date, datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
from openai import OpenAI

from .prompts import get_system_prompt, get_tools
from .conversation_store import ConversationStore, Message, create_conversation_store
from credit_engine import calculate_credits_spent, deduct_credits



# Backend URL for commitment API
BACKEND_URL = os.getenv("BACKEND_URL", "https://cllabackendserver-production.up.railway.app")


@dataclass
class ChatRequest:
    """Input request for chat service."""
    user_id: str
    message: str
    chat_page_id: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: dict) -> "ChatRequest":
        return cls(
            user_id=data["user_id"],
            message=data["message"],
            chat_page_id=data.get("chat_page_id")
        )
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatResponse:
    """Output response from chat service."""
    success: bool
    message: str
    chat_page_id: str
    conversation_id: str
    intent: str
    function_called: Optional[str]
    filters_applied: Optional[dict]
    commitments_found: int
    commitments: list[dict]
    summary: dict
    timestamp: str
    tokens_used: int = 0
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "message": self.message,
            "chat_page_id": self.chat_page_id,
            "conversation_id": self.conversation_id,
            "intent": self.intent,
            "function_called": self.function_called,
            "filters_applied": self.filters_applied,
            "commitments_found": self.commitments_found,
            "commitments": self.commitments,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "tokens_used": self.tokens_used,
            "error": self.error
        }
    
    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class ChatServiceV3:
    """Chat Service with Conversation History and Commitment Persistence."""
    
    def __init__(
        self,
        openai_api_key: str,
        commitment_fetcher,
        redis_url: str = None,
        redis_token: str = None,
        model: str = "gpt-4o-mini"
    ):
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
        self.fetch_commitments = commitment_fetcher
        self.store = create_conversation_store(redis_url, redis_token)
        self.redis_url = redis_url
        self.redis_token = redis_token
    
    def process_message(self, request: ChatRequest) -> ChatResponse:
        """Process a user message with conversation context."""
        timestamp = datetime.now(timezone.utc).isoformat()
        total_tokens = 0
        
        try:
            # Get or create chat page
            if request.chat_page_id:
                chat_page = self.store.get_chat_page(request.user_id, request.chat_page_id)
                if not chat_page:
                    chat_page = self.store.create_chat_page(request.user_id, request.message)
            else:
                chat_page = self.store.create_chat_page(request.user_id, request.message)
            
            chat_page_id = chat_page.chat_page_id
            
            # Get conversation history
            history = self.store.get_message_history(request.user_id, chat_page_id)

            from credit_engine import has_enough_credits
            if not has_enough_credits(request.user_id):
                return ChatResponse(
                    success=False,
                    message="⚠️ You have 0 credits remaining. Please top up to continue.",
                    chat_page_id=chat_page_id,
                    conversation_id="",
                    intent="no_credits",
                    function_called=None,
                    filters_applied=None,
                    commitments_found=0,
                    commitments=[],
                    summary={},
                    timestamp=timestamp,
                    tokens_used=0
                )

            
            # Build messages with history
            messages = self._build_messages_with_history(history, request.message)
            
            # First LLM call
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=get_tools(),
                tool_choice="auto",
                temperature=0.7
            )
            
            total_tokens += response.usage.total_tokens if response.usage else 0
            # --- CREDIT METERING (ADD START) ---
            try:
                if response is not None and getattr(response, "usage", None):
                    # Extract tokens robustly (some SDKs use prompt_tokens/completion_tokens)
                    input_tokens = getattr(response.usage, "input_tokens", None)
                    if input_tokens is None:
                        input_tokens = getattr(response.usage, "prompt_tokens", 0)
                    output_tokens = getattr(response.usage, "output_tokens", None)
                    if output_tokens is None:
                        output_tokens = getattr(response.usage, "completion_tokens", 0)

                    input_tokens = int(input_tokens or 0)
                    output_tokens = int(output_tokens or 0)

                    credits_spent = calculate_credits_spent(input_tokens, output_tokens)

                    # Deduct for the calling user
                    deduct_credits(request.user_id, credits_spent)
            except Exception as _e:
                # Do not fail the chat if metering fails; log for debugging
                print(f"⚠️ Credit metering failed during chat processing: {_e}")
            # --- CREDIT METERING (ADD END) ---

            assistant_message = response.choices[0].message
            
            # Check for function calls
            if assistant_message.tool_calls:
                result = self._handle_function_calls(
                    request=request,
                    messages=messages,
                    assistant_message=assistant_message,
                    chat_page_id=chat_page_id,
                    timestamp=timestamp,
                    tokens_so_far=total_tokens
                )
                return result
            
            # Direct response (no function call)
            response_text = assistant_message.content or "How can I help you?"
            
            # Save conversation (no commitments for general responses)
            conversation = self.store.add_conversation(
                user_id=request.user_id,
                chat_page_id=chat_page_id,
                user_message=request.message,
                assistant_message=response_text,
                intent="general",
                commitments=[],  # ← Empty for general
                summary={}
            )
            
            return ChatResponse(
                success=True,
                message=response_text,
                chat_page_id=chat_page_id,
                conversation_id=conversation.conversation_id,
                intent="general",
                function_called=None,
                filters_applied=None,
                commitments_found=0,
                commitments=[],
                summary={},
                timestamp=timestamp,
                tokens_used=total_tokens
            )
            
        except Exception as e:
            print(f"❌ Chat service error: {e}")
            import traceback
            traceback.print_exc()
            
            return ChatResponse(
                success=False,
                message="Sorry, something went wrong. Please try again.",
                chat_page_id=request.chat_page_id or "",
                conversation_id="",
                intent="error",
                function_called=None,
                filters_applied=None,
                commitments_found=0,
                commitments=[],
                summary={},
                timestamp=timestamp,
                tokens_used=total_tokens,
                error=str(e)
            )
    
    def _build_messages_with_history(self, history: list[Message], current_message: str) -> list[dict]:
        """Build messages array with conversation history."""
        messages = [{"role": "system", "content": get_system_prompt()}]
        
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})
        
        messages.append({"role": "user", "content": current_message})
        
        return messages
    
    def _handle_function_calls(
        self,
        request: ChatRequest,
        messages: list,
        assistant_message,
        chat_page_id: str,
        timestamp: str,
        tokens_so_far: int
    ) -> ChatResponse:
        """Handle LLM function call requests."""
        
        tool_call = assistant_message.tool_calls[0]
        function_name = tool_call.function.name
        
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            args = {}
        
        print(f"🔧 Function called: {function_name}")
        print(f"   Arguments: {args}")
        
        if function_name == "get_today_snapshot":
            return self._handle_today_snapshot(
                request, messages, assistant_message, tool_call,
                chat_page_id, timestamp, tokens_so_far
            )
        elif function_name == "get_commitments":
            return self._handle_get_commitments(
                request, messages, assistant_message, tool_call, args,
                chat_page_id, timestamp, tokens_so_far
            )
        elif function_name == "get_deleted_commitments":
            return self._handle_get_deleted_commitments(
                request, messages, assistant_message, tool_call, args,
                chat_page_id, timestamp, tokens_so_far
            )
        else:
            return ChatResponse(
                success=False,
                message="Something went wrong. Please try again.",
                chat_page_id=chat_page_id,
                conversation_id="",
                intent="error",
                function_called=function_name,
                filters_applied=None,
                commitments_found=0,
                commitments=[],
                summary={},
                timestamp=timestamp,
                tokens_used=tokens_so_far,
                error=f"Unknown function: {function_name}"
            )

    # ═══════════════════════════════════════════════════════════════════════════════
    # GET DELETED COMMITMENTS HANDLER
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def _handle_get_deleted_commitments(
        self,
        request: ChatRequest,
        messages: list,
        assistant_message,
        tool_call,
        args: dict,
        chat_page_id: str,
        timestamp: str,
        tokens_so_far: int
    ) -> ChatResponse:
        """Handle get_deleted_commitments function call."""
        
        limit = args.get("limit", 20)
        deleted_items = self._fetch_deleted_from_api(request.user_id, limit)
        
        function_result = {
            "total_found": len(deleted_items),
            "is_empty": len(deleted_items) == 0,
            "message": "Deleted items are kept for 24 hours after deletion.",
            "commitments": deleted_items
        }
        
        messages.append(assistant_message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(function_result)
        })
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7
        )
        
        total_tokens = tokens_so_far + (response.usage.total_tokens if response.usage else 0)
        # --- CREDIT METERING (ADD START) ---
        try:
            if response is not None and getattr(response, "usage", None):
                # Extract tokens robustly (some SDKs use prompt_tokens/completion_tokens)
                input_tokens = getattr(response.usage, "input_tokens", None)
                if input_tokens is None:
                    input_tokens = getattr(response.usage, "prompt_tokens", 0)
                output_tokens = getattr(response.usage, "output_tokens", None)
                if output_tokens is None:
                    output_tokens = getattr(response.usage, "completion_tokens", 0)

                input_tokens = int(input_tokens or 0)
                output_tokens = int(output_tokens or 0)

                credits_spent = calculate_credits_spent(input_tokens, output_tokens)

                # Deduct for the calling user
                deduct_credits(request.user_id, credits_spent)
        except Exception as _e:
            # Do not fail the chat if metering fails; log for debugging
            print(f"⚠️ Credit metering failed during chat processing: {_e}")
        # --- CREDIT METERING (ADD END) ---

        response_text = response.choices[0].message.content or "Here are your deleted items."
        
        commitments_for_frontend = []
        for item in deleted_items:
            commitments_for_frontend.append({
                "commitment_id": item.get("commitment_id"),
                "what": item.get("what", ""),
                "to_whom": item.get("to_whom"),
                "deadline_iso": item.get("deadline_iso"),
                "deadline_raw": item.get("deadline_raw"),
                "status": "deleted",
                "deleted_at": item.get("deleted_at"),
                "priority": item.get("priority"),
                "estimated_hours": item.get("estimated_hours"),
                "email_sender": item.get("email_sender"),
                "email_sender_name": item.get("email_sender_name"),
                "email_subject": item.get("email_subject"),
                "sender_role": item.get("sender_role"),
                "original_status": item.get("original_status"),
            })
        
        summary = {
            "deleted": len(deleted_items),
            "message": "Items kept for 24 hours"
        }
        
        # ✅ Save with commitments
        conversation = self.store.add_conversation(
            user_id=request.user_id,
            chat_page_id=chat_page_id,
            user_message=request.message,
            assistant_message=response_text,
            intent="deleted_query",
            function_called="get_deleted_commitments",
            filters_applied={"type": "deleted"},
            commitments_found=len(deleted_items),
            commitments=commitments_for_frontend,  # ← SAVE COMMITMENTS
            summary=summary  # ← SAVE SUMMARY
        )
        
        return ChatResponse(
            success=True,
            message=response_text,
            chat_page_id=chat_page_id,
            conversation_id=conversation.conversation_id,
            intent="deleted_query",
            function_called="get_deleted_commitments",
            filters_applied={"type": "deleted"},
            commitments_found=len(deleted_items),
            commitments=commitments_for_frontend,
            summary=summary,
            timestamp=timestamp,
            tokens_used=total_tokens
        )
    
    def _fetch_deleted_from_api(self, user_id: str, limit: int = 20) -> list:
        """Fetch deleted commitments from the commitment API."""
        try:
            response = requests.get(
                f"{BACKEND_URL}/api/commitments/deleted",
                headers={
                    "Authorization": f"Bearer INTERNAL_CALL_{user_id}",
                    "Content-Type": "application/json"
                },
                params={"limit": limit},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("commitments", [])
            else:
                print(f"❌ Failed to fetch deleted items: {response.status_code}")
                return []
        except Exception as e:
            print(f"❌ Error fetching deleted items: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════════════════════
    # TODAY SNAPSHOT HANDLER
    # ═══════════════════════════════════════════════════════════════════════════════

    def _handle_today_snapshot(
        self,
        request: ChatRequest,
        messages: list,
        assistant_message,
        tool_call,
        chat_page_id: str,
        timestamp: str,
        tokens_so_far: int
    ) -> ChatResponse:
        """Handle get_today_snapshot function call."""
        from services.gmail.commitments.filters import CommitmentFilters
        
        today = date.today()
        tomorrow = today + timedelta(days=1)
        today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        today_end = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=timezone.utc)
        
        overdue_result = self.fetch_commitments(
            request.user_id,
            CommitmentFilters(status=["overdue"])
        )
        
        due_today_result = self.fetch_commitments(
            request.user_id,
            CommitmentFilters(deadline_after=today, deadline_before=today)
        )
        
        received_today_result = self.fetch_commitments(
            request.user_id,
            CommitmentFilters(created_after=today_start, created_before=today_end)
        )
        
        due_tomorrow_result = self.fetch_commitments(
            request.user_id,
            CommitmentFilters(deadline_after=tomorrow, deadline_before=tomorrow)
        )
        
        function_result = {
            "today_date": today.isoformat(),
            "overdue": {
                "count": overdue_result.total_found,
                "items": self._prepare_commitments_for_llm(overdue_result)
            },
            "due_today": {
                "count": due_today_result.total_found,
                "items": self._prepare_commitments_for_llm(due_today_result)
            },
            "received_today": {
                "count": received_today_result.total_found,
                "items": self._prepare_commitments_for_llm(received_today_result)
            },
            "due_tomorrow": {
                "count": due_tomorrow_result.total_found,
                "items": self._prepare_commitments_for_llm(due_tomorrow_result),
                "total_hours": sum(
                    c.estimated_hours or 0 
                    for c in due_tomorrow_result.all_commitments
                )
            }
        }
        
        overdue_count = overdue_result.total_found
        due_today_count = due_today_result.total_found
        received_today_count = received_today_result.total_found
        tomorrow_count = due_tomorrow_result.total_found
        
        print(f"📊 TodaySnapshot: overdue={overdue_count}, today={due_today_count}, received={received_today_count}, tomorrow={tomorrow_count}")
        
        messages.append(assistant_message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(function_result)
        })
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7
        )
        
        total_tokens = tokens_so_far + (response.usage.total_tokens if response.usage else 0)
        # --- CREDIT METERING (ADD START) ---
        try:
            if response is not None and getattr(response, "usage", None):
                # Extract tokens robustly (some SDKs use prompt_tokens/completion_tokens)
                input_tokens = getattr(response.usage, "input_tokens", None)
                if input_tokens is None:
                    input_tokens = getattr(response.usage, "prompt_tokens", 0)
                output_tokens = getattr(response.usage, "output_tokens", None)
                if output_tokens is None:
                    output_tokens = getattr(response.usage, "completion_tokens", 0)

                input_tokens = int(input_tokens or 0)
                output_tokens = int(output_tokens or 0)

                credits_spent = calculate_credits_spent(input_tokens, output_tokens)

                # Deduct for the calling user
                deduct_credits(request.user_id, credits_spent)
        except Exception as _e:
            # Do not fail the chat if metering fails; log for debugging
            print(f"⚠️ Credit metering failed during chat processing: {_e}")
        # --- CREDIT METERING (ADD END) ---

        response_text = response.choices[0].message.content or "Here's your commitment overview for today."
        
        # Build all commitments with status markers
        all_commitments = []
        
        for c in overdue_result.all_commitments:
            item = self._commitment_to_dict(c)
            item["status"] = "overdue"
            all_commitments.append(item)
        
        for c in due_today_result.all_commitments:
            item = self._commitment_to_dict(c)
            item["status"] = "due_today"
            all_commitments.append(item)
        
        for c in received_today_result.all_commitments:
            item = self._commitment_to_dict(c)
            item["status"] = "received_today"
            all_commitments.append(item)
        
        for c in due_tomorrow_result.all_commitments:
            item = self._commitment_to_dict(c)
            item["status"] = "due_tomorrow"
            all_commitments.append(item)
        
        total_found = len(all_commitments)
        
        summary = {
            "overdue": overdue_count,
            "due_today": due_today_count,
            "received_today": received_today_count,
            "due_tomorrow": tomorrow_count,
            "tomorrow_hours": function_result["due_tomorrow"]["total_hours"]
        }
        
        # ✅ Save with commitments
        conversation = self.store.add_conversation(
            user_id=request.user_id,
            chat_page_id=chat_page_id,
            user_message=request.message,
            assistant_message=response_text,
            intent="today_snapshot",
            function_called="get_today_snapshot",
            filters_applied={"type": "today_snapshot"},
            commitments_found=total_found,
            commitments=all_commitments,  # ← SAVE COMMITMENTS
            summary=summary  # ← SAVE SUMMARY
        )
        
        return ChatResponse(
            success=True,
            message=response_text,
            chat_page_id=chat_page_id,
            conversation_id=conversation.conversation_id,
            intent="today_snapshot",
            function_called="get_today_snapshot",
            filters_applied={"type": "today_snapshot"},
            commitments_found=total_found,
            commitments=all_commitments,
            summary=summary,
            timestamp=timestamp,
            tokens_used=total_tokens
        )

    # ═══════════════════════════════════════════════════════════════════════════════
    # GET COMMITMENTS HANDLER
    # ═══════════════════════════════════════════════════════════════════════════════
        
    def _handle_get_commitments(
        self,
        request: ChatRequest,
        messages: list,
        assistant_message,
        tool_call,
        args: dict,
        chat_page_id: str,
        timestamp: str,
        tokens_so_far: int
    ) -> ChatResponse:
        """Handle get_commitments function call."""
        
        filters = self._build_filters(args)
        result = self.fetch_commitments(request.user_id, filters)
        
        # Filter by completed_today if requested
        completed_today = args.get("completed_today", False)
        if completed_today and args.get("only_completed"):
            today = date.today()
            filtered_commitments = []
            for c in result.all_commitments:
                if hasattr(c, 'completed_at') and c.completed_at:
                    try:
                        completed_date = datetime.fromisoformat(
                            c.completed_at.replace('Z', '+00:00')
                        ).date()
                        if completed_date == today:
                            filtered_commitments.append(c)
                    except:
                        pass
            
            original_count = result.total_found
            result.all_commitments = filtered_commitments
            result.total_found = len(filtered_commitments)
            print(f"📊 Filtered completed items: {original_count} → {result.total_found} (today only)")
        
        function_result = {
            "total_found": result.total_found,
            "is_empty": result.is_empty if hasattr(result, 'is_empty') else result.total_found == 0,
            "summary": {
                "overdue": result.summary.overdue,
                "due_today": result.summary.due_today,
                "upcoming": result.summary.upcoming,
                "later": result.summary.later,
                "no_deadline": result.summary.no_deadline,
                "completed": result.summary.completed
            },
            "commitments": self._prepare_commitments_for_llm(result)
        }
        
        messages.append(assistant_message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(function_result)
        })
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7
        )
        
        total_tokens = tokens_so_far + (response.usage.total_tokens if response.usage else 0)
        # --- CREDIT METERING (ADD START) ---
        try:
            if response is not None and getattr(response, "usage", None):
                # Extract tokens robustly (some SDKs use prompt_tokens/completion_tokens)
                input_tokens = getattr(response.usage, "input_tokens", None)
                if input_tokens is None:
                    input_tokens = getattr(response.usage, "prompt_tokens", 0)
                output_tokens = getattr(response.usage, "output_tokens", None)
                if output_tokens is None:
                    output_tokens = getattr(response.usage, "completion_tokens", 0)

                input_tokens = int(input_tokens or 0)
                output_tokens = int(output_tokens or 0)

                credits_spent = calculate_credits_spent(input_tokens, output_tokens)

                # Deduct for the calling user
                deduct_credits(request.user_id, credits_spent)
        except Exception as _e:
            # Do not fail the chat if metering fails; log for debugging
            print(f"⚠️ Credit metering failed during chat processing: {_e}")
        # --- CREDIT METERING (ADD END) ---

        response_text = response.choices[0].message.content or "Here's what I found."
        
        commitments_list = [self._commitment_to_dict(c) for c in result.all_commitments]
        
        summary = {
            "overdue": result.summary.overdue,
            "due_today": result.summary.due_today,
            "upcoming": result.summary.upcoming,
            "later": result.summary.later,
            "no_deadline": result.summary.no_deadline,
            "completed": result.summary.completed,
            "total": result.total_found
        }
        
        # ✅ Save with commitments
        conversation = self.store.add_conversation(
            user_id=request.user_id,
            chat_page_id=chat_page_id,
            user_message=request.message,
            assistant_message=response_text,
            intent="commitment_query",
            function_called="get_commitments",
            filters_applied=args if args else {"show_all": True},
            commitments_found=result.total_found,
            commitments=commitments_list,  # ← SAVE COMMITMENTS
            summary=summary  # ← SAVE SUMMARY
        )
        
        return ChatResponse(
            success=True,
            message=response_text,
            chat_page_id=chat_page_id,
            conversation_id=conversation.conversation_id,
            intent="commitment_query",
            function_called="get_commitments",
            filters_applied=args if args else {"show_all": True},
            commitments_found=result.total_found,
            commitments=commitments_list,
            summary=summary,
            timestamp=timestamp,
            tokens_used=total_tokens
        )
    
    def _build_filters(self, args: dict):
        """Convert function arguments to CommitmentFilters."""
        from services.gmail.commitments.filters import CommitmentFilters
        
        filters = CommitmentFilters()
        
        if args.get("status"):
            if not args.get("deadline_date") and not args.get("deadline_from") and not args.get("deadline_to"):
                filters.status = args["status"]
        
        if args.get("deadline_date"):
            d = date.fromisoformat(args["deadline_date"])
            filters.deadline_after = d
            filters.deadline_before = d
        else:
            if args.get("deadline_from"):
                filters.deadline_after = date.fromisoformat(args["deadline_from"])
            if args.get("deadline_to"):
                filters.deadline_before = date.fromisoformat(args["deadline_to"])
        
        if args.get("sender_name"):
            filters.sender_name = args["sender_name"]
        if args.get("sender_email"):
            filters.sender_email = args["sender_email"]
        if args.get("sender_role"):
            filters.sender_role = args["sender_role"]
        if args.get("priority"):
            filters.priority = args["priority"]
        if args.get("search_text"):
            filters.search_text = args["search_text"]
        if args.get("has_deadline") is False:
            filters.has_deadline = False
        if args.get("only_completed"):
            filters.only_completed = True
        
        # PHASE 4B: Direction and Assignment filters
        if args.get("direction"):
            filters.direction = args["direction"]
        if args.get("assigned_to_me") is not None:
            filters.assigned_to_me = args["assigned_to_me"]
        
        if args.get("show_all"):
            filters = CommitmentFilters()
        
        return filters
    
    def _prepare_commitments_for_llm(self, result) -> list[dict]:
        """Prepare commitment data for LLM context."""
        commitments = []
        for c in result.all_commitments[:15]:
            sender_name = c.email_sender_name or c.email_sender or "Unknown"
            sender_role = c.sender_role.capitalize() if c.sender_role else "Unknown"
            sender_display = f"{sender_name} ({sender_role})"
            
            status = c.status or "active"
            if status == "overdue" and c.days_overdue:
                status_display = f"Overdue - {c.days_overdue} days!"
            elif status == "due_today":
                status_display = "Due today"
            elif c.completed:
                status_display = "Completed"
            else:
                status_display = status.replace("_", " ").capitalize()
            
            commitments.append({
                "what": c.what,
                "deadline": c.deadline_iso or "No deadline",
                "status": status,
                "status_display": status_display,
                "days_overdue": c.days_overdue if c.days_overdue else None,
                "priority": c.priority,
                "estimated_hours": c.estimated_hours,
                "from": sender_display,
                "sender_name": sender_name,
                "sender_role": sender_role,
                "completed": c.completed,
                "completed_at": c.completed_at if hasattr(c, 'completed_at') else None
            })
        return commitments
    
    def _commitment_to_dict(self, commitment) -> dict:
        """Convert CommitmentItem to dict for API response."""
        return {
            "commitment_id": commitment.commitment_id,
            "what": commitment.what,
            "to_whom": commitment.to_whom,
            "deadline_iso": commitment.deadline_iso,
            "deadline_raw": commitment.deadline_raw,
            "status": commitment.status,
            "days_overdue": commitment.days_overdue,
            "priority": commitment.priority,
            "estimated_hours": commitment.estimated_hours,
            "email_sender": commitment.email_sender,
            "email_sender_name": commitment.email_sender_name,
            "email_subject": commitment.email_subject,
            "sender_role": commitment.sender_role,
            "direction": commitment.direction,
            "assigned_to_me": commitment.assigned_to_me,
            "completed": commitment.completed,
            "completed_at": commitment.completed_at if hasattr(commitment, 'completed_at') else None,
        }
    
    # ───────────────────────────────────────────────────────────────────────────
    # CHAT PAGE MANAGEMENT
    # ───────────────────────────────────────────────────────────────────────────
    
    def create_new_chat(self, user_id: str) -> dict:
        """Create a new chat page."""
        chat_page = self.store.create_chat_page(user_id)
        return {
            "chat_page_id": chat_page.chat_page_id,
            "title": chat_page.title,
            "created_at": chat_page.created_at
        }
    
    def get_chat_history(self, user_id: str, chat_page_id: str) -> dict:
        """Get full conversation history for a chat page."""
        chat_page = self.store.get_chat_page(user_id, chat_page_id)
        if not chat_page:
            return {"error": "Chat not found"}
        
        return {
            "chat_page_id": chat_page.chat_page_id,
            "title": chat_page.title,
            "created_at": chat_page.created_at,
            "conversations": [c.to_dict() for c in chat_page.conversations]
        }
    
    def get_user_chats(self, user_id: str, limit: int = 20) -> list[dict]:
        """Get all chat pages for a user."""
        chat_pages = self.store.get_user_chat_pages(user_id, limit)
        return [cp.to_dict() for cp in chat_pages]
    
    def delete_chat(self, user_id: str, chat_page_id: str) -> dict:
        """Delete a chat page."""
        self.store.delete_chat_page(user_id, chat_page_id)
        return {"deleted": True, "chat_page_id": chat_page_id}


def create_chat_service(
    openai_api_key: str,
    commitment_fetcher,
    redis_url: str = None,
    redis_token: str = None
) -> ChatServiceV3:
    """Factory function to create ChatServiceV3."""
    return ChatServiceV3(
        openai_api_key=openai_api_key,
        commitment_fetcher=commitment_fetcher,
        redis_url=redis_url,
        redis_token=redis_token
    )