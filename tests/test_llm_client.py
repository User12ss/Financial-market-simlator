"""The LLM layer: validation and retries, and what each provider sends on the wire.

HTTP providers are exercised through httpx.MockTransport and the Claude
provider through a stub SDK client, so nothing here reaches a real model.
"""

import json
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

import config
from utils import llm_client
from utils.llm_client import (
    AnthropicProvider, LLMClient, LLMError, OllamaProvider, OpenAICompatProvider,
    Provider, Usage, UsageLog, _extract_json,
)


class Item(BaseModel):
    label: str


class Verdict(BaseModel):
    winner: Literal["bull", "bear"]
    items: list[Item]


GOOD = '{"winner": "bull", "items": [{"label": "a"}]}'


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def generate(self, model, system, user, schema):
        self.prompts.append(user)
        return self.replies.pop(0), Usage(self.name, model, 10, 5, 0.1)


def client_with(provider: Provider, monkeypatch) -> LLMClient:
    monkeypatch.setattr(llm_client, "make_provider", lambda name, api_key=None: provider)
    return LLMClient(provider="ollama", model="m")


# ── LLMClient ────────────────────────────────────────────────────────────────

def test_structured_reply_is_validated(monkeypatch):
    client = client_with(ScriptedProvider([GOOD]), monkeypatch)
    result = client.complete_structured("sys", "user", Verdict)
    assert result.winner == "bull" and result.items[0].label == "a"
    assert len(client.usage.calls) == 1


def test_invalid_reply_is_retried_with_the_error_shown(monkeypatch):
    provider = ScriptedProvider(['{"winner": "draw", "items": []}', GOOD])
    client = client_with(provider, monkeypatch)

    assert client.complete_structured("sys", "user", Verdict).winner == "bull"
    assert "did not match the required schema" in provider.prompts[1]
    assert len(client.usage.calls) == 2


def test_gives_up_after_the_configured_retries(monkeypatch):
    monkeypatch.setattr(config, "LLM_SCHEMA_RETRIES", 1)
    client = client_with(ScriptedProvider(["not json", "still not json"]), monkeypatch)
    with pytest.raises(LLMError, match="did not return valid Verdict"):
        client.complete_structured("sys", "user", Verdict)


def test_plain_completion_drops_reasoning_tags(monkeypatch):
    client = client_with(ScriptedProvider(["<think>hmm</think>The answer."]), monkeypatch)
    assert client.complete("sys", "user") == "The answer."


def test_extract_json_handles_reasoning_and_code_fences():
    assert json.loads(_extract_json("<think>x {y}</think>\n```json\n" + GOOD + "\n```"))["winner"] == "bull"


def test_tiers_default_to_config(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL", "main-model")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAST", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL_FAST", "fast-model")
    monkeypatch.setattr(llm_client, "make_provider", lambda name, api_key=None: ScriptedProvider([]))

    client = LLMClient()
    assert (client.provider_name, client.model) == ("ollama", "main-model")
    assert (client.fast_provider_name, client.fast_model) == ("ollama", "fast-model")


def test_provider_override_brings_its_own_default_models(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_PROVIDER_FAST", "ollama")
    monkeypatch.setattr(llm_client, "make_provider", lambda name, api_key=None: ScriptedProvider([]))

    client = LLMClient(provider="anthropic")
    assert client.model == "claude-sonnet-5-5"
    assert (client.fast_provider_name, client.fast_model) == ("anthropic", "claude-haiku-5-5")


def test_unknown_provider_is_rejected():
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        LLMClient(provider="nope", model="m")


def test_usage_summary_distinguishes_local_from_priced():
    log = UsageLog()
    log.add(Usage("ollama", "m", 100, 50, 2.0))
    assert "no per-token cost" in log.summary()
    log.add(Usage("anthropic", "claude-sonnet-5-5", 1000, 500, 1.0, cost_usd=0.007))
    assert "$0.0070" in log.summary() and "2 calls" in log.summary()


# ── Ollama ───────────────────────────────────────────────────────────────────

def ollama_with(handler) -> OllamaProvider:
    provider = OllamaProvider(base_url="http://ollama.test")
    provider._http = httpx.Client(transport=httpx.MockTransport(handler))
    return provider


def ollama_reply(content=GOOD, prompt_tokens=400, **extra):
    body = {"message": {"content": content}, "prompt_eval_count": prompt_tokens,
            "eval_count": 20, "done_reason": "stop"}
    body.update(extra)
    return httpx.Response(200, json=body)


def test_ollama_sends_schema_context_size_and_field_descriptions():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return ollama_reply()

    text, usage = ollama_with(handler).generate("m", "sys", "question", Verdict)

    assert text == GOOD
    assert seen["format"] == Verdict.model_json_schema()
    assert seen["options"]["num_ctx"] == config.OLLAMA_NUM_CTX
    assert seen["stream"] is False
    assert seen["truncate"] is False
    assert "JSON schema" in seen["messages"][1]["content"]
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (400, 20, None)


def test_ollama_refuses_a_prompt_that_cannot_fit(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 1000)

    def handler(request):
        raise AssertionError("the request should not be sent")

    with pytest.raises(LLMError, match="does not fit OLLAMA_NUM_CTX=1000"):
        ollama_with(handler).generate("m", "sys", "word " * 2000, None)


def test_ollama_server_refusal_of_an_oversized_prompt_is_explained():
    def handler(request):
        return httpx.Response(400, json={
            "error": "request (4843 tokens) exceeds the available context size (2048 tokens), try increasing it"
        })

    with pytest.raises(LLMError, match="does not fit OLLAMA_NUM_CTX.*4843 tokens"):
        ollama_with(handler).generate("m", "sys", "q", None)


def test_ollama_detects_truncation_by_a_server_that_ignores_the_flag():
    long_prompt = "word " * 3000  # 15,000 characters cannot be only 500 tokens
    with pytest.raises(LLMError, match="truncated"):
        ollama_with(lambda r: ollama_reply(prompt_tokens=500)).generate("m", "sys", long_prompt, None)


def test_ollama_accepts_a_normal_token_count():
    long_prompt = "word " * 3000
    text, usage = ollama_with(lambda r: ollama_reply(prompt_tokens=3100)).generate("m", "sys", long_prompt, None)
    assert text == GOOD and usage.input_tokens == 3100


def test_ollama_reports_cut_off_output():
    with pytest.raises(LLMError, match="cut off"):
        ollama_with(lambda r: ollama_reply(done_reason="length")).generate("m", "sys", "q", None)


def test_ollama_missing_model_says_how_to_get_it():
    def handler(request):
        return httpx.Response(404, json={"error": "model 'm' not found"})

    with pytest.raises(LLMError, match="ollama pull m"):
        ollama_with(handler).generate("m", "sys", "q", None)


def test_ollama_check_finds_missing_model():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "other:latest"}]})

    provider = ollama_with(handler)
    assert "ollama pull m" in provider.check("m")
    assert provider.check("other") is None


def test_server_errors_are_retried(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, text="busy") if len(attempts) < 2 else ollama_reply()

    text, _ = ollama_with(handler).generate("m", "sys", "q", None)
    assert text == GOOD and len(attempts) == 2


# ── OpenAI-compatible ────────────────────────────────────────────────────────

def test_openai_compat_sends_strict_inlined_schema_and_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": GOOD}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        })

    provider = OpenAICompatProvider(base_url="http://host.test/v1", api_key="secret")
    provider._http = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer secret"}
    )
    text, usage = provider.generate("m", "sys", "q", Verdict)

    schema = seen["body"]["response_format"]["json_schema"]["schema"]
    assert "$ref" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False
    assert seen["auth"] == "Bearer secret"
    assert (text, usage.input_tokens, usage.output_tokens) == (GOOD, 30, 12)


def test_openai_compat_needs_a_base_url(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", "")
    with pytest.raises(LLMError, match="OPENAI_COMPAT_BASE_URL"):
        OpenAICompatProvider()


# ── Claude ───────────────────────────────────────────────────────────────────

class StubMessages:
    def __init__(self, stop_reason="end_turn", uncached=1000, cache_read=0, cache_write=0):
        self.stop_reason = stop_reason
        self.tokens = (uncached, cache_read, cache_write)
        self.kwargs = None
        self.method = None

    def _response(self, parsed):
        uncached, cache_read, cache_write = self.tokens
        return SimpleNamespace(
            stop_reason=self.stop_reason, parsed_output=parsed,
            content=[SimpleNamespace(type="text", text="plain text")],
            usage=SimpleNamespace(input_tokens=uncached, output_tokens=500,
                                  cache_read_input_tokens=cache_read,
                                  cache_creation_input_tokens=cache_write),
        )

    def parse(self, **kwargs):
        self.method, self.kwargs = "parse", kwargs
        return self._response(Verdict.model_validate_json(GOOD))

    def create(self, **kwargs):
        self.method, self.kwargs = "create", kwargs
        return self._response(None)


def claude_with(messages: StubMessages) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key")
    provider._client = SimpleNamespace(messages=messages)
    return provider


def test_claude_structured_call_uses_parse_without_rejected_parameters():
    messages = StubMessages()
    text, usage = claude_with(messages).generate("claude-sonnet-5-5", "sys", "q", Verdict)

    assert messages.method == "parse"
    assert messages.kwargs["output_format"] is Verdict
    assert messages.kwargs["max_tokens"] == config.ANTHROPIC_MAX_TOKENS
    for rejected in ("temperature", "top_p", "top_k", "tool_choice", "tools", "thinking"):
        assert rejected not in messages.kwargs
    assert Verdict.model_validate_json(text).winner == "bull"
    assert usage.cost_usd == pytest.approx((1000 * 2.0 + 500 * 10.0) / 1_000_000)


def test_claude_cost_prices_cached_tokens_at_their_own_rates():
    messages = StubMessages(uncached=100, cache_read=900, cache_write=200)
    _, usage = claude_with(messages).generate("claude-sonnet-5-5", "sys", "q", Verdict)

    expected = (100 * 2.0 + 900 * 0.10 + 200 * 2.0 * 1.25 + 500 * 10.0) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected)
    assert usage.input_tokens == 1200


def test_claude_unknown_model_has_no_cost_estimate():
    _, usage = claude_with(StubMessages()).generate("claude-some-future-model", "sys", "q", Verdict)
    assert usage.cost_usd is None


def test_claude_plain_call_uses_create():
    messages = StubMessages()
    text, _ = claude_with(messages).generate("claude-sonnet-5-5", "sys", "q", None)
    assert messages.method == "create" and text == "plain text"
    assert "temperature" not in messages.kwargs


@pytest.mark.parametrize("stop_reason, expected", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_claude_bad_stop_reasons_raise(stop_reason, expected):
    with pytest.raises(LLMError, match=expected):
        claude_with(StubMessages(stop_reason)).generate("claude-sonnet-5-5", "sys", "q", Verdict)


def test_claude_needs_a_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()
