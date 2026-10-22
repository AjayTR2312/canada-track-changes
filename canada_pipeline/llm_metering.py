"""
Exact LLM cost metering for Anthropic Claude calls.

A single process-wide :class:`UsageMeter` accumulates token usage from every
``client.messages.create`` call. Both ``build_anthropic_client`` factories
(in ``config.py`` and ``inline_footnotes_insertion.py``) route their returned
client through :func:`attach_meter`, so every LLM call in the pipeline is
metered automatically without touching individual call sites.

Pricing is per *million* tokens and overridable via environment variables so it
can be corrected without code changes:

    ANTHROPIC_INPUT_PRICE_PER_MTOK   (default 15.0)
    ANTHROPIC_OUTPUT_PRICE_PER_MTOK  (default 75.0)

Anthropic prompt-cache tokens are billed differently from normal input tokens:
cache *reads* at 0.1x the input price and cache *writes* at 1.25x. Those
multipliers are captured as constants below.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Price per 1,000,000 tokens (USD). Defaults reflect Claude Opus list pricing.
INPUT_PRICE_PER_MTOK = _env_float("ANTHROPIC_INPUT_PRICE_PER_MTOK", 15.0)
OUTPUT_PRICE_PER_MTOK = _env_float("ANTHROPIC_OUTPUT_PRICE_PER_MTOK", 75.0)

# Anthropic prompt-cache multipliers relative to the input price.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class UsageSnapshot:
    """Immutable view of meter totals at a point in time."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    call_count: int = 0
    cost_usd: float = 0.0


@dataclass
class UsageMeter:
    """Thread-safe accumulator of Anthropic token usage and cost."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    call_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)
            self.cache_read_tokens += int(cache_read_tokens or 0)
            self.cache_write_tokens += int(cache_write_tokens or 0)
            self.call_count += 1

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return self._cost_locked()

    def _cost_locked(self) -> float:
        in_price = INPUT_PRICE_PER_MTOK / 1_000_000.0
        out_price = OUTPUT_PRICE_PER_MTOK / 1_000_000.0
        return (
            self.input_tokens * in_price
            + self.output_tokens * out_price
            + self.cache_read_tokens * in_price * CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * in_price * CACHE_WRITE_MULTIPLIER
        )

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            return UsageSnapshot(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_read_tokens=self.cache_read_tokens,
                cache_write_tokens=self.cache_write_tokens,
                call_count=self.call_count,
                cost_usd=self._cost_locked(),
            )

    def reset(self) -> None:
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.cache_read_tokens = 0
            self.cache_write_tokens = 0
            self.call_count = 0


# Process-wide default meter.
_GLOBAL_METER = UsageMeter()


def get_global_meter() -> UsageMeter:
    return _GLOBAL_METER


def reset_global_meter() -> None:
    _GLOBAL_METER.reset()


def _record_response(meter: UsageMeter, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    meter.record(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def attach_meter(client, meter: UsageMeter | None = None):
    """
    Wrap ``client.messages.create`` so every call records token usage into
    ``meter`` (default: the process-wide global meter). Idempotent: wrapping an
    already-metered client is a no-op. Returns the same client for chaining.
    """
    meter = meter or _GLOBAL_METER
    messages = client.messages
    original_create = messages.create

    if getattr(original_create, "_is_metered", False):
        return client

    def metered_create(*args, **kwargs):
        response = original_create(*args, **kwargs)
        try:
            _record_response(meter, response)
        except Exception:
            # Metering must never break the pipeline.
            pass
        return response

    metered_create._is_metered = True
    messages.create = metered_create
    return client


def install_global_metering(meter: UsageMeter | None = None) -> None:
    """
    Patch ``anthropic.Anthropic.__init__`` so EVERY client constructed anywhere
    in the process is automatically metered, including clients built directly
    (not via the ``build_anthropic_client`` factories) and module-level clients.

    Idempotent and version-robust: it only relies on the public
    ``client.messages.create`` surface. Call this as early as possible (before
    any module constructs its client) — e.g. at the top of the pipeline entry
    point and the web app.
    """
    import anthropic

    if getattr(anthropic.Anthropic, "_metering_installed", False):
        return

    original_init = anthropic.Anthropic.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            attach_meter(self, meter)
        except Exception:
            pass

    anthropic.Anthropic.__init__ = patched_init
    anthropic.Anthropic._metering_installed = True
