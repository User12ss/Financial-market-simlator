"""LLM access layer: one interface, several providers.

Callers ask for plain text (`complete`) or for a validated Pydantic object
(`complete_structured`). Which provider and model serve a request is
configuration, so agents and the orchestrator never touch a vendor SDK.

Providers:
  ollama         local open-source models through Ollama's native API
  openai_compat  any service exposing the OpenAI chat-completions API
  anthropic      Claude through the official SDK
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

import config

T = TypeVar("T", bound=BaseModel)

PROVIDER_NAMES = ("ollama", "openai_compat", "anthropic")


class LLMError(RuntimeError):
    """A model call failed in a way the caller should see."""


# ── Usage accounting ─────────────────────────────────────────────────────────

@dataclass
class Usage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float | None = None  # None when the provider has no per-token price


class UsageLog:
    """Thread-safe record of every model call made through one LLMClient."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[Usage] = []

    def add(self, usage: Usage) -> None:
        with self._lock:
            self.calls.append(usage)

    def summary(self) -> str:
        with self._lock:
            calls = list(self.calls)
        if not calls:
            return "LLM usage: no calls"
        tokens_in = sum(c.input_tokens for c in calls)
        tokens_out = sum(c.output_tokens for c in calls)
        seconds = sum(c.seconds for c in calls)
        priced = [c.cost_usd for c in calls if c.cost_usd is not None]
        cost = f"about ${sum(priced):.4f}" if priced else "no per-token cost (local model)"
        if priced and len(priced) < len(calls):
            cost += " for the priced calls"
        return (
            f"LLM usage: {len(calls)} calls, {tokens_in:,} input + {tokens_out:,} output tokens, "
            f"{seconds:.0f}s of model time, {cost}"
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may carry reasoning or code fences."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start:end + 1]
    return cleaned


def _inline_schema(schema: dict) -> dict:
    """Resolve $ref pointers and close every object, for strict json_schema hosts."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(defs[name])
            out = {k: walk(v) for k, v in node.items() if k != "$defs"}
            if out.get("type") == "object":
                out.setdefault("additionalProperties", False)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def _schema_instruction(schema: type[BaseModel]) -> str:
    """Schema shown to the model. Constrained decoding fixes the shape of the
    output but does not tell the model what each field means."""
    return (
        "\n\nRespond with a single JSON object that matches this JSON schema. "
        "Output only the JSON object.\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )


# ── Providers ────────────────────────────────────────────────────────────────

class Provider(ABC):
    name: str = ""

    @abstractmethod
    def generate(
        self, model: str, system: str, user: str, schema: type[BaseModel] | None
    ) -> tuple[str, Usage]:
        """Return the reply text (JSON text when a schema is given) and its usage."""

    def check(self, model: str) -> str | None:
        """Return a problem description if the model cannot be used, else None."""
        return None


class _HttpProvider(Provider):
    """Shared transport for the HTTP-based providers."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url
        self._http = httpx.Client(timeout=config.LLM_TIMEOUT_SECONDS, headers=headers or {})

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """The server's own error message when it sent one, else the raw body."""
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, dict):
            error = error.get("message")
        return str(error or response.text)[:300]

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        attempts = max(1, config.MAX_RETRIES)
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            try:
                response = self._http.post(url, json=payload)
            except httpx.ConnectError as e:
                raise LLMError(f"Cannot reach {self.name} at {self.base_url}: {e}") from e
            except httpx.HTTPError as e:  # timeouts, dropped connections
                last_error = e
            else:
                if response.status_code < 400:
                    return response.json()
                detail = self._error_detail(response)
                if response.status_code < 500 and response.status_code != 429:
                    raise LLMError(f"{self.name} rejected the request ({response.status_code}): {detail}")
                last_error = LLMError(f"{self.name} returned {response.status_code}: {detail}")
        raise LLMError(f"{self.name} call failed after {attempts} attempts: {last_error}")


class OllamaProvider(_HttpProvider):
    """Local open-source models through Ollama's native chat API."""

    name = "ollama"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or config.OLLAMA_BASE_URL)
        self._slots = threading.Semaphore(max(1, config.OLLAMA_MAX_PARALLEL))

    @staticmethod
    def _does_not_fit(detail: str) -> LLMError:
        return LLMError(
            f"Prompt does not fit OLLAMA_NUM_CTX={config.OLLAMA_NUM_CTX} ({detail}). "
            "Raise OLLAMA_NUM_CTX or shorten the input."
        )

    def generate(self, model, system, user, schema):
        if schema is not None:
            user = user + _schema_instruction(schema)
        # By default Ollama drops part of a prompt that exceeds the context
        # window and still answers. Three guards, most reliable in the middle:
        # an obvious overflow is refused here, "truncate": False makes the
        # server refuse with an exact token count, and a last check below
        # covers older servers that ignore that flag.
        chars = len(system) + len(user)
        if chars // 4 > config.OLLAMA_NUM_CTX:
            raise self._does_not_fit(f"about {chars // 4:,} tokens")
        payload = {
            "model": model,
            "stream": False,
            "truncate": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {
                "num_ctx": config.OLLAMA_NUM_CTX,
                "num_predict": config.OLLAMA_NUM_PREDICT,
                "temperature": config.LLM_TEMPERATURE,
            },
        }
        if schema is not None:
            payload["format"] = schema.model_json_schema()

        with self._slots:
            started = time.time()
            try:
                data = self._post("/api/chat", payload)
            except LLMError as e:
                detail = str(e)
                if "exceeds the available context size" in detail:
                    raise self._does_not_fit(detail.split("): ", 1)[-1].strip()) from e
                if "not found" in detail:
                    raise LLMError(f"Ollama has no model '{model}'. Run: ollama pull {model}") from e
                raise
            elapsed = time.time() - started

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        # No tokenizer packs ordinary text into fewer than one token per six
        # characters, so a count this low means part of the prompt was dropped.
        if prompt_tokens and prompt_tokens < chars // 6:
            raise LLMError(
                f"Ollama read only {prompt_tokens:,} prompt tokens for {chars:,} characters: the prompt was "
                f"truncated to fit OLLAMA_NUM_CTX={config.OLLAMA_NUM_CTX}. Raise OLLAMA_NUM_CTX."
            )
        if data.get("done_reason") == "length":
            raise LLMError(
                f"Reply from '{model}' was cut off at OLLAMA_NUM_PREDICT={config.OLLAMA_NUM_PREDICT}. Raise it."
            )
        text = (data.get("message") or {}).get("content") or ""
        usage = Usage(self.name, model, prompt_tokens, int(data.get("eval_count") or 0), elapsed)
        return text, usage

    def check(self, model):
        try:
            response = self._http.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError as e:
            return f"Ollama is not reachable at {self.base_url} ({type(e).__name__}). Start it with: ollama serve"
        installed = {m.get("name", "") for m in response.json().get("models", [])}
        if model not in installed and f"{model}:latest" not in installed:
            return f"Ollama has no model '{model}'. Run: ollama pull {model}"
        return None


class OpenAICompatProvider(_HttpProvider):
    """Any service exposing the OpenAI chat-completions API (hosted open models, vLLM, ...)."""

    name = "openai_compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        base_url = base_url or config.OPENAI_COMPAT_BASE_URL
        if not base_url:
            raise LLMError("OPENAI_COMPAT_BASE_URL is required for the openai_compat provider.")
        key = api_key or config.OPENAI_COMPAT_API_KEY
        super().__init__(base_url, {"Authorization": f"Bearer {key}"} if key else None)

    def generate(self, model, system, user, schema):
        if schema is not None:
            user = user + _schema_instruction(schema)
        payload = {
            "model": model,
            "temperature": config.LLM_TEMPERATURE,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": _inline_schema(schema.model_json_schema()),
                    "strict": True,
                },
            }
        started = time.time()
        data = self._post("/chat/completions", payload)
        elapsed = time.time() - started

        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise LLMError(f"Reply from '{model}' was cut off by the provider's output limit.")
        text = (choice.get("message") or {}).get("content") or ""
        counts = data.get("usage") or {}
        usage = Usage(
            self.name, model,
            int(counts.get("prompt_tokens") or 0), int(counts.get("completion_tokens") or 0), elapsed,
        )
        return text, usage

    def check(self, model):
        return None if model else "LLM_MODEL is required for the openai_compat provider."


class AnthropicProvider(Provider):
    """Claude through the official SDK.

    No sampling parameters and no forced tool choice are sent: current Claude
    models reject both. Structured output goes through `messages.parse`.
    """

    name = "anthropic"

    # USD per million tokens (input, output, cache read). Used only for the
    # cost estimate. Cache writes are priced at 1.25x input.
    PRICES: dict[str, tuple[float, float, float]] = {
        "claude-fable-5-2": (8.0, 40.0, 0.20),
        "claude-opus-5-5": (4.0, 20.0, 0.20),
        "claude-sonnet-5-5": (2.0, 10.0, 0.10),
        "claude-haiku-5-5": (0.10, 0.50, 0.01),
    }
    CACHE_WRITE_MULTIPLIER = 1.25

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        key = api_key or config.ANTHROPIC_API_KEY
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is required for the anthropic provider.")
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=key, max_retries=config.MAX_RETRIES)

    def generate(self, model, system, user, schema):
        request = {
            "model": model,
            "max_tokens": config.ANTHROPIC_MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        started = time.time()
        try:
            if schema is None:
                response = self._client.messages.create(**request)
            else:
                response = self._client.messages.parse(output_format=schema, **request)
        except self._sdk.NotFoundError as e:
            raise LLMError(f"Claude model '{model}' was not found. It may be retired or misspelled.") from e
        except self._sdk.AuthenticationError as e:
            raise LLMError("ANTHROPIC_API_KEY was rejected.") from e
        except self._sdk.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except self._sdk.APIConnectionError as e:
            raise LLMError(f"Cannot reach the Claude API: {e}") from e
        elapsed = time.time() - started

        if response.stop_reason == "refusal":
            raise LLMError(f"Claude declined this request (model '{model}').")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                f"Claude's reply was cut off at ANTHROPIC_MAX_TOKENS={config.ANTHROPIC_MAX_TOKENS}. Raise it."
            )

        parsed = getattr(response, "parsed_output", None)
        if schema is not None and parsed is not None:
            text = parsed.model_dump_json()
        else:
            text = "".join(block.text for block in response.content if block.type == "text")

        counts = response.usage
        uncached = counts.input_tokens or 0
        cache_read = getattr(counts, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(counts, "cache_creation_input_tokens", 0) or 0
        output_tokens = counts.output_tokens or 0
        cost = None
        if model in self.PRICES:
            price_in, price_out, price_cache_read = self.PRICES[model]
            cost = (
                uncached * price_in
                + cache_read * price_cache_read
                + cache_write * price_in * self.CACHE_WRITE_MULTIPLIER
                + output_tokens * price_out
            ) / 1_000_000
        input_tokens = uncached + cache_read + cache_write
        return text, Usage(self.name, model, input_tokens, output_tokens, elapsed, cost)


def make_provider(name: str, api_key: str | None = None) -> Provider:
    if name == "ollama":
        return OllamaProvider()
    if name == "openai_compat":
        return OpenAICompatProvider(api_key=api_key)
    if name == "anthropic":
        return AnthropicProvider(api_key=api_key)
    raise LLMError(f"Unknown LLM provider '{name}'. Use one of: {', '.join(PROVIDER_NAMES)}.")


# ── Client ───────────────────────────────────────────────────────────────────

class LLMClient:
    """What the rest of the system uses to talk to a model.

    Holds a main tier and a fast tier. Each tier is a (provider, model) pair
    taken from config unless overridden here.
    """

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        fast_provider: str | None = None,
        fast_model: str | None = None,
    ) -> None:
        self.provider_name = (provider or config.LLM_PROVIDER).lower()
        if model:
            self.model = model
        elif self.provider_name == config.LLM_PROVIDER:
            self.model = config.LLM_MODEL
        else:
            self.model = config.provider_default(self.provider_name, "model")

        if fast_provider:
            self.fast_provider_name = fast_provider.lower()
        elif provider:
            self.fast_provider_name = self.provider_name
        else:
            self.fast_provider_name = config.LLM_PROVIDER_FAST
        if fast_model:
            self.fast_model = fast_model
        elif self.fast_provider_name == config.LLM_PROVIDER_FAST:
            self.fast_model = config.LLM_MODEL_FAST
        else:
            self.fast_model = config.provider_default(self.fast_provider_name, "fast") or self.model

        if not self.model:
            raise LLMError(f"No model configured for provider '{self.provider_name}'. Set LLM_MODEL.")

        self._providers: dict[str, Provider] = {}
        for name in {self.provider_name, self.fast_provider_name}:
            self._providers[name] = make_provider(name, api_key)
        self.usage = UsageLog()

    def _tier(self, fast: bool) -> tuple[Provider, str]:
        if fast:
            return self._providers[self.fast_provider_name], self.fast_model
        return self._providers[self.provider_name], self.model

    def describe(self) -> str:
        main = f"{self.provider_name} / {self.model}"
        fast = f"{self.fast_provider_name} / {self.fast_model}"
        return main if fast == main else f"{main} (fast tier: {fast})"

    def problems(self) -> list[str]:
        """Reasons the configured models cannot be used right now. Empty when ready."""
        found = []
        for fast in (False, True):
            provider, model = self._tier(fast)
            issue = provider.check(model)
            if issue and issue not in found:
                found.append(issue)
        return found

    def complete(self, system_prompt: str, user_message: str, fast: bool = False) -> str:
        """Plain-text completion."""
        provider, model = self._tier(fast)
        text, usage = provider.generate(model, system_prompt, user_message, None)
        self.usage.add(usage)
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def complete_structured(
        self, system_prompt: str, user_message: str, schema: type[T], fast: bool = False
    ) -> T:
        """Completion validated against a Pydantic model.

        A reply that does not validate is retried with the validation error
        shown to the model, up to LLM_SCHEMA_RETRIES extra attempts.
        """
        provider, model = self._tier(fast)
        message = user_message
        last_error = ""
        for _ in range(config.LLM_SCHEMA_RETRIES + 1):
            text, usage = provider.generate(model, system_prompt, message, schema)
            self.usage.add(usage)
            try:
                return schema.model_validate_json(_extract_json(text))
            except ValidationError as e:
                last_error = str(e)[:400]
                message = (
                    f"{user_message}\n\nYour previous reply did not match the required schema:\n"
                    f"{last_error}\nReply again with only the corrected JSON object."
                )
        raise LLMError(
            f"'{model}' did not return valid {schema.__name__} after "
            f"{config.LLM_SCHEMA_RETRIES + 1} attempts: {last_error}"
        )
