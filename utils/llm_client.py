"""Anthropic API client wrapper with retry logic and structured output parsing."""

from __future__ import annotations
import json
import time
import anthropic
from config import ANTHROPIC_API_KEY, MODEL_NAME, MAX_RETRIES, MAX_TOKENS


class LLMClient:
    """Wrapper around the Anthropic API for agent calls."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or MODEL_NAME
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required. Set it in .env or pass it directly."
            )
        self.client = anthropic.Anthropic(api_key=self.api_key)

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.4,
    ) -> str:
        """Send a completion request and return the text response."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                return response.content[0].text
            except anthropic.RateLimitError as e:
                last_error = e
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited, retrying in {wait}s...")
                time.sleep(wait)
            except anthropic.APIError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue
                break
        raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts: {last_error}")

    def complete_json(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.3,
    ) -> dict:
        """Send a completion request expecting a JSON response."""
        raw = self.complete(system_prompt, user_message, max_tokens, temperature)
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse LLM JSON response: {e}\nRaw: {raw[:500]}")
