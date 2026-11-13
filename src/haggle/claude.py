"""Shared Claude settings for the haggling tools: model, client and cost of each response."""

import os

import anthropic

from processing.processors.claude_condition import PRICES

DEFAULT_MODEL = "claude-opus-5-5"
# On a safety decline, the API retries on the model Anthropic picks for that category.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def model() -> str:
    return os.getenv("HAGGLE_MODEL", DEFAULT_MODEL)


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def cost_usd(response) -> float:
    usage = response.usage
    price_in, price_out = PRICES.get(response.model, PRICES[DEFAULT_MODEL])
    fresh = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (fresh * price_in + cached * price_in * 0.1 + (usage.output_tokens or 0) * price_out) / 1e6


def check(response) -> None:
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
