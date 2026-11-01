"""Subscription dialogue contracts; HTTP/local-model adapters are excluded."""
from __future__ import annotations

from typing import Any, Mapping, Protocol


class ProviderError(RuntimeError):
    """A provider failed; its response body and credentials are not logged."""


class ProviderUnavailable(ProviderError):
    pass


class ProviderImageUnsupported(ProviderUnavailable):
    """The selected provider/model cannot consume an actual screen image."""


class ProviderCancelled(ProviderError):
    """A requested dialogue turn was cancelled; this is not a provider failure."""


class ProactiveEventRejected(ProviderCancelled):
    """An expired/already-handled system event must not produce another utterance."""


class ChatProvider(Protocol):
    mode: str
    model: str
    base_url: str

    def complete(
        self, messages: list[dict[str, str]], schema: dict[str, Any], *, timeout_seconds: float,
    ) -> str: ...


def select_provider(config: Mapping[str, Any]) -> tuple[ChatProvider | None, dict[str, Any]]:
    """Select subscription dialogue or the explicitly labelled fixed-line demo."""
    mode = config.get("mode", "codex_subscription")
    if mode == "codex_subscription":
        from .codex_subscription import CodexSubscriptionProvider
        try:
            provider = CodexSubscriptionProvider(config)
        except ProviderUnavailable as exc:
            return None, {"provider_mode": "unavailable", "llm_ready": False,
                          "reason": str(exc), "probes": []}
        login = provider.login_status()
        status = {
            "provider_mode": "codex_subscription", "llm_ready": None,
            "model": provider.model, "configured": True, **login, "probes": [],
        }
        if not login["authenticated"]:
            status["provider_mode"] = "unavailable"
            status["llm_ready"] = False
            return None, status
        return provider, status
    if mode == "offline_demo":
        return None, {"provider_mode": "offline_demo", "llm_ready": False,
                      "reason": "Explicit offline demonstration; fixed original replies", "probes": []}
    raise ValueError("provider.mode must be codex_subscription or explicit offline_demo; no local LLM or paid API fallback is supported")
