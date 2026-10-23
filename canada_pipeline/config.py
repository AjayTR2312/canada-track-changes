import os
from dotenv import load_dotenv
load_dotenv()

import requests
import pandas as pd
import anthropic
import httpx
import json

API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# MODEL NAMES — Change here when a model gets deprecated
# ─────────────────────────────────────────────────────────────────────────────
MODEL_OPUS = "claude-opus-4-6"        # Primary model (vision + complex tasks)
MODEL_SONNET = "claude-sonnet-4-6"    # Lighter model (section matching, quick checks)
MODEL_OPUS_FALLBACK = "claude-opus-4-8"  # Stronger fallback used on the final retry
# ─────────────────────────────────────────────────────────────────────────────

# ─── LLM call counter ────────────────────────────────────────────────────────
# Every Anthropic client built by build_anthropic_client() increments this on
# each messages.create call, so the pipeline can report how many LLM calls a run
# made. Process-global (the pipeline runs in a single worker process per job).
import threading
_LLM_CALLS = 0
_LLM_LOCK = threading.Lock()

def reset_llm_calls():
    global _LLM_CALLS
    with _LLM_LOCK:
        _LLM_CALLS = 0

def get_llm_calls() -> int:
    return _LLM_CALLS
# ─────────────────────────────────────────────────────────────────────────────


def get_section_pattern(chapter_number):
    """Build the section regex pattern for a given chapter number.
    Use this everywhere instead of hardcoding the pattern."""
    return rf'§?\s*{chapter_number}[:.][\dA-Za-z]+[^§\n]*' 


# ─────────────────────────────────────────────────────────────────────────────
# VOLUME MODE support
# ─────────────────────────────────────────────────────────────────────────────
# When a document is a *volume* (many chapters) rather than a single *chapter*,
# the pipeline must NOT restrict section matching to one chapter number. In that
# case the ``chapter_number`` threaded through the pipeline is set to this
# wildcard regex fragment, which matches ANY chapter number (e.g. "8", "14").
import re as _re

VOLUME_CHAPTER_WILDCARD = r'\d+'


def chapter_pattern(chapter_number) -> str:
    """Return a regex fragment for the chapter designator.

    - Chapter mode: the literal chapter number is regex-escaped (a no-op for the
      digit/roman-numeral chapter ids we actually get, so behaviour is unchanged).
    - Volume mode: ``chapter_number`` is already ``VOLUME_CHAPTER_WILDCARD``
      (``\\d+``) and is returned as-is so it stays a wildcard instead of being
      escaped into a literal.
    """
    s = '' if chapter_number is None else str(chapter_number)
    if s == VOLUME_CHAPTER_WILDCARD:
        return s
    return _re.escape(s)


def build_anthropic_client(model_name=MODEL_OPUS):
    """
    Fetches credentials from the TR Anthropic token endpoint and returns
    a configured Anthropic client plus the resolved model_name.
    """
    workspace_id = os.getenv("TR_WORKSPACE_ID", "")
    token_url = os.getenv("TR_ANTHROPIC_TOKEN_URL", "")

    # Fail fast with an actionable message when the deployment is missing these.
    # Without this, an empty token_url makes requests.post("") raise the cryptic
    # "Invalid URL '': No scheme supplied", which hides the real cause.
    missing = [name for name, val in
               (("TR_ANTHROPIC_TOKEN_URL", token_url), ("TR_WORKSPACE_ID", workspace_id))
               if not val.strip()]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set them in the deployment environment (e.g. the Plexus Deployment Job "
            "Environment tab) so the pipeline can fetch Anthropic credentials."
        )

    payload = {"workspace_id": workspace_id}
    url = token_url

    resp = requests.post(url, headers=None, json=payload)
    credentials = json.loads(resp.content)

    if "anthropic_api_key" not in credentials:
        raise RuntimeError(
            f"Failed to retrieve Anthropic credentials. Response: {credentials}"
        )

    client = anthropic.Anthropic(
        api_key=credentials["anthropic_api_key"],
        http_client=httpx.Client(
            verify=False,
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=60.0),
        ),
    )

    # Use llm_metering (same as forms) — tracks calls, tokens, and cost
    from .llm_metering import attach_meter
    attach_meter(client)

    # Also increment legacy counter for backward compat
    _orig_create = client.messages.create
    def _counted_create(*args, **kwargs):
        global _LLM_CALLS
        with _LLM_LOCK:
            _LLM_CALLS += 1
        return _orig_create(*args, **kwargs)
    client.messages.create = _counted_create

    print(f"[INFO] Anthropic client built successfully. Model: {model_name}")
    return client


def find_section_key(section_str: str, section_dict: dict,
                     client=None, model_name: str = MODEL_SONNET) -> str:
    """
    Finds the correct key from section_dict that matches section_str
    by sending ALL keys in a single LLM call and asking it to pick
    the exact match.

    Falls back to substring matching only when no client is provided.
    """
    if client is None:
        # Legacy fallback — plain substring check
        for key in section_dict:
            if key in section_str:
                return key
        return None

    all_keys = list(section_dict.keys())
    if not all_keys:
        print(f"  [SECTION MATCH] section_dict is empty")
        return None

    keys_list_str = "\n".join(f"  - '{k}'" for k in all_keys)

    prompt = (
        "You are matching a section identifier to the correct designator key.\n\n"
        f"Section value: '{section_str}'\n\n"
        f"Available designator keys:\n{keys_list_str}\n\n"
        "Which designator key EXACTLY matches the section value?\n"
        "Rules:\n"
        "- A partial or substring match does NOT count "
        "(e.g. '5:1' is NOT the same as '5:13', '3:12' is NOT '3:12A').\n"
        "- The section value may contain extra text after the designator "
        "(e.g. '3:12 breach of contract' matches key '3:12').\n"
        "- Reply with ONLY the matching key exactly as shown above, "
        "or 'NONE' if no key matches.\n"
        "- Do not add any explanation."
    )

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=30,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = response.content[0].text.strip().strip("'\"")
            print(f"    [LLM MATCH] attempt {attempt} → '{answer}'")

            if answer.upper() == "NONE":
                print(f"  [SECTION MATCH] LLM said NONE for '{section_str}'")
                return None

            if answer in section_dict:
                print(f"  [SECTION MATCH] LLM picked key='{answer}' for '{section_str}'")
                return answer

            # Answer wasn't a valid key — retry
            print(f"    [LLM MATCH] '{answer}' not in keys, retrying...")
            prompt = (
                f"You gave '{answer}' which is not one of the available keys.\n"
                "Reply with ONLY one of the keys listed below, or 'NONE'.\n\n"
                f"Section value: '{section_str}'\n\n"
                f"Available designator keys:\n{keys_list_str}\n"
            )
        except Exception as e:
            print(f"    [LLM MATCH] attempt {attempt} error: {e}")

    print(f"  [SECTION MATCH] LLM failed after {max_retries} retries for '{section_str}'")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Model selection + model-fallback helpers (used by the agent modules:
# correction_chat, revision_resolver, triage_agent). Added so those modules can
# resolve a per-phase model chain without hardcoding a model literal. Wired to
# this region's own MODEL_* constants above — retuning is a config edit here.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_PRIMARY_MODEL = MODEL_OPUS
DEFAULT_ESCALATION_MODEL = MODEL_OPUS_FALLBACK
DEFAULT_PROMPT_CACHING = True

# Per-phase overrides. "chat" = interactive correction chat + resolver + triage:
# a user is waiting, so start with the lighter Sonnet and move up to Opus when a
# proposal fails its own DTD check (the "escalation" model). "processing" mirrors
# the pipeline's own primary (Opus) with a cheaper/stronger fallback chain.
_PHASE_OVERRIDES = {
    "chat": {
        "primary": MODEL_SONNET,
        "escalation": MODEL_OPUS,
        "fallback": [MODEL_OPUS, MODEL_OPUS_FALLBACK],
        "prompt_caching": None,
    },
    "processing": {
        "primary": MODEL_OPUS,
        "escalation": MODEL_OPUS_FALLBACK,
        "fallback": [MODEL_SONNET, MODEL_OPUS_FALLBACK],
        "prompt_caching": None,
    },
    "validation": {
        "primary": MODEL_SONNET,
        "escalation": MODEL_OPUS,
        "fallback": [],
        "prompt_caching": None,
    },
}


def get_primary_model(phase: str = "processing") -> str:
    """Resolve the PRIMARY model for a phase. Call sites should NEVER hardcode a
    model literal — call this instead, so retuning is a config edit."""
    v = _PHASE_OVERRIDES.get(phase, {}).get("primary")
    return v or DEFAULT_PRIMARY_MODEL


def get_escalation_model(phase: str = "processing") -> str:
    """Resolve the ESCALATION model — used when the primary model's output fails
    this phase's own quality check (e.g. a proposal that fails its DTD check)."""
    v = _PHASE_OVERRIDES.get(phase, {}).get("escalation")
    return v or DEFAULT_ESCALATION_MODEL


def get_prompt_caching(phase: str = "processing") -> bool:
    v = _PHASE_OVERRIDES.get(phase, {}).get("prompt_caching")
    return DEFAULT_PROMPT_CACHING if v is None else v


def get_fallback_models(phase: str = "processing") -> list:
    """Resolve the ordered FALLBACK models for a phase — tried in order, after the
    primary, when a call keeps failing (see call_with_model_fallback)."""
    return list(_PHASE_OVERRIDES.get(phase, {}).get("fallback") or [])


def get_model_chain(phase: str = "processing") -> list:
    """Primary followed by its fallbacks, as one ordered list — what
    call_with_model_fallback actually walks."""
    return [get_primary_model(phase)] + get_fallback_models(phase)


def call_with_model_fallback(client, model_chain, create_kwargs: dict,
                             max_retries_per_model: int = 3,
                             quality_check=None):
    """Call client.messages.create() walking model_chain in order: retries a
    RETRYABLE failure (connection error, rate limit, overloaded, 5xx, timeout)
    up to max_retries_per_model times with exponential backoff on the CURRENT
    model before moving to the next model in the chain. A non-retryable error
    (e.g. a genuine 400 unrelated to overload) raises immediately, since no
    model in the chain would fix that.

    quality_check(message) -> bool, if given, lets a caller reject an
    otherwise-successful response that fails ITS OWN quality bar — treated
    exactly like a retryable failure, so the same chain-advancement serves both
    transient API errors and quality escalation with one mechanism. Returns the
    message quality_check accepted.

    Raises the last exception (or, for an exhausted quality_check with no
    exception, a RuntimeError) if every model in the chain is exhausted.
    """
    import time as _time_fallback
    last_exc = None
    for model_idx, model in enumerate(model_chain):
        for attempt in range(1, max_retries_per_model + 1):
            try:
                message = client.messages.create(model=model, **create_kwargs)
                if quality_check is not None and not quality_check(message):
                    raise RuntimeError(f"quality_check rejected the response from {model}")
                return message
            except (anthropic.APIConnectionError, anthropic.RateLimitError,
                    anthropic.InternalServerError, httpx.TimeoutException, RuntimeError) as e:
                last_exc = e
                if attempt < max_retries_per_model:
                    _time_fallback.sleep(min(2 ** attempt, 30))
                    continue
                more_models = model_idx < len(model_chain) - 1
                print(f"[MODEL-FALLBACK] {model} exhausted {max_retries_per_model} attempt(s) ({e})."
                      + (f" Trying next model: {model_chain[model_idx + 1]}." if more_models else " No more models in chain."))
    raise last_exc or RuntimeError("call_with_model_fallback: empty model_chain")


def extract_text_from_message(message) -> str:
    """Return the first TEXT block's content from an Anthropic `messages.create`
    response. NEVER assume `message.content[0]` is the text block — newer/thinking-
    capable models can prepend a non-text block at index 0, so indexing [0]
    directly can raise AttributeError. Call this instead of `message.content[0].text`."""
    for block in (getattr(message, "content", None) or []):
        if getattr(block, "type", None) == "text" and hasattr(block, "text"):
            return block.text
    return ""