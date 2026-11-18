"""Shared Claude settings for the haggling tools: model, client and cost of each response."""

import os

import anthropic

from processing.processors import claude_condition

DEFAULT_MODEL = "claude-opus-5-5"
# On a safety decline, the API retries on the model Anthropic picks for that category.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def model() -> str:
    return os.getenv("HAGGLE_MODEL", DEFAULT_MODEL)


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def cost_usd(response) -> float:
    return claude_condition.cost_usd(response, DEFAULT_MODEL)


CACHED = {"type": "ephemeral"}  # marks the end of a prompt part worth caching (kept 5 minutes)


def cached_text(text: str) -> dict:
    """A text block that ends a cached part of the prompt: a later request that starts with
    the same text up to here reads it at the cache price."""
    return {"type": "text", "text": text, "cache_control": CACHED}


def check(response) -> None:
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
