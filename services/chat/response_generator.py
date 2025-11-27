# services/chat/response_generator.py
"""
Response Generator - Uses LLM to generate conversational responses.
"""

import json
from datetime import date
from dataclasses import dataclass, asdict
from typing import Optional
from openai import OpenAI

from services.chat.prompts import get_response_generation_prompt, HELP_RESPONSE, UNCLEAR_RESPONSE


@dataclass
class ResponseContext:
    """Context for generating a response."""
    user_query: str
    parsed_date_label: Optional[str]
    commitments: list[dict]
    summary: dict
    total_found: int
    is_empty: bool
    filter_description: str = ""
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass 
class GeneratedResponse:
    """LLM-generated response."""
    message: str
    context_used: dict
    tokens_used: int = 0
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "context_used": self.context_used,
            "tokens_used": self.tokens_used,
            "error": self.error
        }


class ResponseGenerator:
    """
    Generates conversational responses using LLM.
    """
    
    def __init__(self, openai_api_key: str, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
    
    def generate(self, context: ResponseContext) -> GeneratedResponse:
        """
        Generate a conversational response based on context.
        
        Args:
            context: ResponseContext with query and commitment data
            
        Returns:
            GeneratedResponse with message
        """
        try:
            # Prepare commitment data for LLM
            commitment_summary = self._prepare_commitments_for_llm(context.commitments)
            
            # Build user message
            user_message = self._build_user_message(context, commitment_summary)
            
            # Call LLM
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": get_response_generation_prompt()},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.7,  # Slightly creative for natural responses
                max_tokens=800
            )
            
            message = response.choices[0].message.content.strip()
            tokens_used = response.usage.total_tokens if response.usage else 0
            
            return GeneratedResponse(
                message=message,
                context_used=context.to_dict(),
                tokens_used=tokens_used
            )
            
        except Exception as e:
            # Fallback to simple response
            fallback = self._generate_fallback_response(context)
            return GeneratedResponse(
                message=fallback,
                context_used=context.to_dict(),
                error=str(e)
            )
    
    def _prepare_commitments_for_llm(self, commitments: list[dict]) -> list[dict]:
        """Prepare commitment data for LLM (only relevant fields)."""
        simplified = []
        for c in commitments[:10]:  # Limit to 10 for token efficiency
            simplified.append({
                "what": c.get("what", "Unknown task"),
                "deadline_iso": c.get("deadline_iso"),
                "status": c.get("status", "active"),
                "days_overdue": c.get("days_overdue", 0),
                "priority": c.get("priority", "medium"),
                "estimated_hours": c.get("estimated_hours"),
                "from": c.get("email_sender_name") or c.get("email_sender", "Unknown"),
                "to_whom": c.get("to_whom"),
            })
        return simplified
    
    def _build_user_message(self, context: ResponseContext, commitments: list[dict]) -> str:
        """Build the user message for LLM."""
        data = {
            "user_query": context.user_query,
            "parsed_date_label": context.parsed_date_label,
            "filter_description": context.filter_description,
            "total_found": context.total_found,
            "is_empty": context.is_empty,
            "summary": context.summary,
            "commitments": commitments
        }
        return f"Generate a response for this query:\n{json.dumps(data, indent=2)}"
    
    def _generate_fallback_response(self, context: ResponseContext) -> str:
        """Generate a simple fallback response without LLM."""
        if context.is_empty:
            if context.parsed_date_label:
                return f"✅ No commitments {context.parsed_date_label}.\n\n📊 Summary:\n• Total active: {context.summary.get('total', 0)}"
            return f"✅ No matching commitments found.\n\n📊 You have {context.summary.get('total', 0)} active commitments."
        
        lines = [f"📋 Found {context.total_found} commitment(s):\n"]
        
        for c in context.commitments[:5]:
            priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(c.get("priority", "medium"), "🟡")
            line = f"{priority_emoji} {c.get('what', 'Unknown')}"
            if c.get("deadline_iso"):
                line += f" (Due: {c['deadline_iso']})"
            if c.get("email_sender_name"):
                line += f" → {c['email_sender_name']}"
            lines.append(f"• {line}")
        
        if context.total_found > 5:
            lines.append(f"\n... and {context.total_found - 5} more")
        
        return "\n".join(lines)
    
    def generate_help(self) -> GeneratedResponse:
        """Generate help response."""
        return GeneratedResponse(
            message=HELP_RESPONSE,
            context_used={"type": "help"}
        )
    
    def generate_unclear(self) -> GeneratedResponse:
        """Generate unclear query response."""
        return GeneratedResponse(
            message=UNCLEAR_RESPONSE,
            context_used={"type": "unclear"}
        )
    
    def generate_greeting(self, summary: dict) -> GeneratedResponse:
        """Generate greeting response with summary."""
        today = date.today()
        
        overdue = summary.get("overdue", 0)
        due_today = summary.get("due_today", 0)
        upcoming = summary.get("upcoming", 0)
        total = summary.get("total", 0)
        
        lines = [f"👋 Hello! Here's your snapshot for {today.strftime('%A, %B %d')}:\n"]
        
        if overdue > 0:
            lines.append(f"🔴 {overdue} overdue - needs attention!")
        if due_today > 0:
            lines.append(f"🟡 {due_today} due today")
        if upcoming > 0:
            lines.append(f"🟢 {upcoming} upcoming this week")
        
        if total == 0:
            lines.append("✨ You're all clear! No active commitments.")
        
        lines.append("\n💬 What would you like to know? Try 'show all' or 'what's urgent?'")
        
        return GeneratedResponse(
            message="\n".join(lines),
            context_used={"type": "greeting", "summary": summary}
        )


def create_response_generator(openai_api_key: str) -> ResponseGenerator:
    """Factory function to create a ResponseGenerator."""
    return ResponseGenerator(openai_api_key=openai_api_key)