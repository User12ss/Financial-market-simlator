"""Anthropic API client with prompt caching, tool_use structured output, and model tiering."""

from __future__ import annotations
import json
import time
import anthropic
from config import ANTHROPIC_API_KEY, MODEL_NAME, MODEL_FAST, MAX_RETRIES, MAX_TOKENS


class LLMClient:
    """Wrapper around the Anthropic API with caching and structured output support."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or MODEL_NAME
        self.model_fast = MODEL_FAST
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required. Set it in .env or pass it directly."
            )
        self.client = anthropic.Anthropic(api_key=self.api_key)

    def _build_system(self, system_prompt: str, cache: bool = False) -> str | list[dict]:
        if cache:
            return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        return system_prompt

    def _call_api(
        self,
        model: str,
        system: str | list[dict],
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> anthropic.types.Message:
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                kwargs = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = tools
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
                return self.client.messages.create(**kwargs)
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

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.4,
        cache: bool = False,
        use_fast_model: bool = False,
    ) -> str:
        """Text completion with optional prompt caching and model tiering."""
        model = self.model_fast if use_fast_model else self.model
        system = self._build_system(system_prompt, cache)
        response = self._call_api(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.content[0].text

    def complete_json(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.3,
        cache: bool = False,
        use_fast_model: bool = False,
    ) -> dict:
        """JSON completion via text — fallback for simple cases."""
        raw = self.complete(system_prompt, user_message, max_tokens, temperature, cache, use_fast_model)
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

    def complete_tool(
        self,
        system_prompt: str,
        user_message: str,
        tool_schema: dict,
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.3,
        cache: bool = True,
        use_fast_model: bool = False,
    ) -> dict:
        """Structured output via tool_use — guarantees valid JSON matching the schema."""
        model = self.model_fast if use_fast_model else self.model
        system = self._build_system(system_prompt, cache)
        response = self._call_api(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
            temperature=temperature,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": tool_schema["name"]},
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError("No tool_use block in response")
