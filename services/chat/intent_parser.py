# services/chat/intent_parser.py
"""
Intent Parser - Uses LLM to extract structured filters from natural language.
"""

import json
import re
from datetime import date, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from openai import OpenAI

from services.chat.prompts import get_intent_extraction_prompt


@dataclass
class ParsedFilters:
    """Structured filters extracted from user query."""
    show_all: bool = False
    status: Optional[list[str]] = None
    deadline_date: Optional[str] = None
    deadline_from: Optional[str] = None
    deadline_to: Optional[str] = None
    sender_email: Optional[str] = None
    sender_name: Optional[str] = None
    sender_role: Optional[list[str]] = None
    direction: Optional[list[str]] = None  # PHASE 4B: NEW
    assigned_to_me: Optional[bool] = None  # PHASE 4B: NEW
    priority: Optional[list[str]] = None
    search_text: Optional[str] = None
    only_completed: bool = False
    
    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != False and v != []}


@dataclass
class ParsedIntent:
    """Complete parsed intent from user query."""
    intent: str  # query, mark_complete, help, greeting, unclear
    filters: Optional[ParsedFilters] = None
    parsed_date_label: Optional[str] = None
    original_query: str = ""
    raw_response: Optional[dict] = None
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        result = {
            "intent": self.intent,
            "filters": self.filters.to_dict() if self.filters else None,
            "parsed_date_label": self.parsed_date_label,
            "original_query": self.original_query,
        }
        if self.error:
            result["error"] = self.error
        return result


class IntentParser:
    """
    Parses user queries into structured intents using LLM.
    """
    
    def __init__(self, openai_api_key: str, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
    
    def parse(self, user_query: str) -> ParsedIntent:
        """
        Parse a user query into structured intent.
        
        Args:
            user_query: Natural language query from user
            
        Returns:
            ParsedIntent with extracted filters
        """
        # Handle empty query
        if not user_query or not user_query.strip():
            return ParsedIntent(
                intent="unclear",
                original_query=user_query,
                error="Empty query"
            )
        
        try:
            # Call LLM for intent extraction
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": get_intent_extraction_prompt()},
                    {"role": "user", "content": user_query}
                ],
                temperature=0.1,  # Low temperature for consistent parsing
                max_tokens=500
            )
            
            # Extract response text
            response_text = response.choices[0].message.content.strip()
            
            # Clean response - remove markdown if present
            response_text = self._clean_json_response(response_text)
            
            # Parse JSON
            parsed_json = json.loads(response_text)
            
            # Extract intent
            intent = parsed_json.get("intent", "unclear")
            
            # Extract filters if present
            filters = None
            if parsed_json.get("filters"):
                filters_data = parsed_json["filters"]
                filters = ParsedFilters(
                    show_all=filters_data.get("show_all", False),
                    status=filters_data.get("status"),
                    deadline_date=filters_data.get("deadline_date"),
                    deadline_from=filters_data.get("deadline_from"),
                    deadline_to=filters_data.get("deadline_to"),
                    sender_email=filters_data.get("sender_email"),
                    sender_name=filters_data.get("sender_name"),
                    sender_role=filters_data.get("sender_role"),
                    direction=filters_data.get("direction"),
                    assigned_to_me=filters_data.get("assigned_to_me"),
                    priority=filters_data.get("priority"),
                    search_text=filters_data.get("search_text"),
                    only_completed=filters_data.get("only_completed", False),
                )
            
            return ParsedIntent(
                intent=intent,
                filters=filters,
                parsed_date_label=parsed_json.get("parsed_date_label"),
                original_query=user_query,
                raw_response=parsed_json
            )
            
        except json.JSONDecodeError as e:
            return ParsedIntent(
                intent="unclear",
                original_query=user_query,
                error=f"Failed to parse LLM response: {str(e)}"
            )
        except Exception as e:
            return ParsedIntent(
                intent="unclear",
                original_query=user_query,
                error=f"LLM error: {str(e)}"
            )
    
    def _clean_json_response(self, text: str) -> str:
        """Remove markdown code blocks if present."""
        # Remove ```json ... ``` or ``` ... ```
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'^```\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return text.strip()


def create_intent_parser(openai_api_key: str) -> IntentParser:
    """Factory function to create an IntentParser."""
    return IntentParser(openai_api_key=openai_api_key)