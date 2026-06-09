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
# ─────────────────────────────────────────────────────────────────────────────

def get_section_pattern(chapter_number):
    """Build the section regex pattern for a given chapter number.
    Use this everywhere instead of hardcoding the pattern."""
    return rf'§?\s*{chapter_number}[:.][\dA-Za-z]+[^§\n]*' 

def build_anthropic_client(model_name=MODEL_OPUS):
    """
    Fetches credentials from the TR Anthropic token endpoint and returns
    a configured Anthropic client plus the resolved model_name.
    """
    workspace_id = os.getenv("TR_WORKSPACE_ID", "")
    token_url = os.getenv("TR_ANTHROPIC_TOKEN_URL", "")
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