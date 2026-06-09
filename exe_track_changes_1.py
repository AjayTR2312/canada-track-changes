"""
Track Change Processing System for DOCX to XML Integration

This system processes track changes from DOCX files and integrates them into XML files
with appropriate XML tagging using GPT-4 and post-processing repositioning.

Author: Track Change Processing System
Version: 1.4 - Enhanced Repositioning for long <trace> tags
"""
import sys

import os

from datetime import datetime

import shutil

import logging

from pathlib import Path

from pathlib import Path

from typing import Optional
import textwrap
import html
import re
from bs4 import BeautifulSoup
import zipfile
import json
import os
import shutil
import tempfile
import base64
import logging
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_original_session_request = requests.Session.request
def _no_verify_request(self, *args, **kwargs):
    kwargs["verify"] = False
    return _original_session_request(self, *args, **kwargs)
requests.Session.request = _no_verify_request
from pathlib import Path
from lxml import etree
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any
from tqdm import tqdm
from openai import AzureOpenAI
import subprocess
import time
from pdf2image import convert_from_path
import pdfplumber
import tiktoken
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures
from config import MODEL_OPUS, MODEL_SONNET, build_anthropic_client


# Optional imports with fallback
try:
    import win32com.client
    import win32com.client.gencache
    # Clear stale gen_py cache to avoid CLSIDToClassMap errors
    try:
        win32com.client.gencache.EnsureModule('{00020905-0000-0000-C000-000000000046}', 0, 8, 7)
    except Exception:
        # If cache is corrupted, wipe the gen_py directory and rebuild
        try:
            import shutil as _shutil_genpy
            gen_py_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Temp', 'gen_py')
            if not os.path.isdir(gen_py_path):
                gen_py_path = win32com.__gen_path__
            if os.path.isdir(gen_py_path):
                _shutil_genpy.rmtree(gen_py_path, ignore_errors=True)
                logging.info("Cleared corrupted win32com gen_py cache at %s", gen_py_path)
        except Exception as _e:
            logging.warning("Could not clear gen_py cache: %s", _e)
    WIN32COM_AVAILABLE = True
except ImportError:
    WIN32COM_AVAILABLE = False
    logging.warning("win32com.client not available. DOCX to image conversion requires Windows with MS Word.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('track_change_processing.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class FallbackModelConfig:
    """Configuration for a single model in the fallback chain."""
    model_name: str
    api_type: str  # "azure_openai" or "anthropic"
    max_retries: int = 3
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_api_version: str = "2024-03-01-preview"
    anthropic_api_key: str = ""
    anthropic_api_url: str = "https://api.anthropic.com/v1/messages"
    temperature: Optional[float] = None
    max_tokens: int = 4096


class LLMFallbackHandler:
    """Reusable LLM caller with retry, exponential backoff, and multi-model fallback.

    Accepts messages in OpenAI format and transparently converts when calling
    the Anthropic REST API.  Each model in the chain is attempted up to
    ``max_retries`` times with exponential backoff (2 s, 4 s, 8 s) before
    moving to the next model.
    """

    MAX_BACKOFF = 60

    # Shared Azure OpenAI defaults — loaded from environment variables
    _DEFAULT_AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    _DEFAULT_AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
    _DEFAULT_AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-03-01-preview")
    _DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, fallback_chain: List[FallbackModelConfig]):
        if not fallback_chain:
            raise ValueError("fallback_chain must contain at least one FallbackModelConfig")
        self.fallback_chain = fallback_chain

    # ------------------------------------------------------------------ #
    #  Factory helpers — build pre-configured chains                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def build_main_chain(
        cls,
        anthropic_api_key: str,
        model_name: str = "gpt-5",
        azure_endpoint: str = "",
        azure_api_key: str = "",
        azure_api_version: str = "",
        temperature: Optional[float] = 0.2,
    ) -> "LLMFallbackHandler":
        """Build the Main-Pipeline fallback chain: <selected model> → claude-sonnet-4-6 → gpt-5.4.

        Only ``anthropic_api_key`` is required; Azure credentials default to
        the values already hard-coded in *run_complete_pipeline*.
        """
        az_ep = azure_endpoint or cls._DEFAULT_AZURE_ENDPOINT
        az_key = azure_api_key or cls._DEFAULT_AZURE_API_KEY
        az_ver = azure_api_version or cls._DEFAULT_AZURE_API_VERSION

        is_anthropic_model = model_name.startswith("claude")

        if is_anthropic_model:
            chain = [
                FallbackModelConfig(
                    model_name=model_name, api_type="anthropic",
                    anthropic_api_key=anthropic_api_key,
                    anthropic_api_url=cls._DEFAULT_ANTHROPIC_URL,
                    temperature=temperature,
                ),
                FallbackModelConfig(
                    model_name="gpt-5", api_type="azure_openai",
                    azure_endpoint=az_ep, azure_api_key=az_key,
                    azure_api_version=az_ver, temperature=temperature,
                ),
                FallbackModelConfig(
                    model_name="gpt-5.4", api_type="azure_openai",
                    azure_endpoint=az_ep, azure_api_key=az_key,
                    azure_api_version=az_ver, temperature=temperature,
                ),
            ]
        else:
            chain = [
                FallbackModelConfig(
                    model_name=model_name, api_type="azure_openai",
                    azure_endpoint=az_ep, azure_api_key=az_key,
                    azure_api_version=az_ver, temperature=temperature,
                ),
                FallbackModelConfig(
                    model_name=MODEL_OPUS, api_type="anthropic",
                    anthropic_api_key=anthropic_api_key,
                    anthropic_api_url=cls._DEFAULT_ANTHROPIC_URL,
                    temperature=temperature,
                ),
                FallbackModelConfig(
                    model_name="gpt-5.4", api_type="azure_openai",
                    azure_endpoint=az_ep, azure_api_key=az_key,
                    azure_api_version=az_ver, temperature=temperature,
                ),
            ]
        return cls(chain)

    @classmethod
    def build_validation_chain(
        cls,
        anthropic_api_key: str,
        azure_endpoint: str = "",
        azure_api_key: str = "",
        azure_api_version: str = "",
    ) -> "LLMFallbackHandler":
        """Build the Validation-Pipeline fallback chain: claude-opus-4-6 → claude-sonnet-4-6 → gpt-5.4.

        Only ``anthropic_api_key`` is required; Azure credentials default to
        the values already hard-coded in *run_complete_pipeline*.
        """
        az_ep = azure_endpoint or cls._DEFAULT_AZURE_ENDPOINT
        az_key = azure_api_key or cls._DEFAULT_AZURE_API_KEY
        az_ver = azure_api_version or cls._DEFAULT_AZURE_API_VERSION

        chain = [
            FallbackModelConfig(
                model_name=MODEL_OPUS, api_type="anthropic",
                max_retries=5,
                anthropic_api_key=anthropic_api_key,
                anthropic_api_url=cls._DEFAULT_ANTHROPIC_URL,
            ),
            FallbackModelConfig(
                model_name=MODEL_SONNET, api_type="anthropic",
                max_retries=5,
                anthropic_api_key=anthropic_api_key,
                anthropic_api_url=cls._DEFAULT_ANTHROPIC_URL,
            ),
            FallbackModelConfig(
                model_name="gpt-5.4", api_type="azure_openai",
                max_retries=5,
                azure_endpoint=az_ep, azure_api_key=az_key,
                azure_api_version=az_ver,
            ),
        ]
        return cls(chain)

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def call_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
    ) -> Optional[Dict[str, Any]]:
        """Try each model in the chain, returning the first successful result.

        Returns a dict ``{"text": str, "usage": dict, "model": str}`` on
        success, or ``None`` when every model has exhausted its retries.
        """
        for config in self.fallback_chain:
            effective_temp = temperature if temperature is not None else config.temperature
            effective_max_tokens = max_tokens or config.max_tokens

            for attempt in range(1, config.max_retries + 1):
                try:
                    result = self._dispatch(config, messages, effective_temp, effective_max_tokens)
                    logger.info(
                        "[LLM Fallback] Success with model '%s' on attempt %d/%d",
                        config.model_name, attempt, config.max_retries,
                    )
                    return result
                except Exception as exc:
                    backoff = min(2 ** attempt, self.MAX_BACKOFF)
                    logger.warning(
                        "[LLM Fallback] Model '%s' attempt %d/%d failed: %s. Retrying in %ds...",
                        config.model_name, attempt, config.max_retries, exc, backoff,
                    )
                    if attempt < config.max_retries:
                        time.sleep(backoff)

            logger.warning(
                "[LLM Fallback] Model '%s' exhausted retries. Falling back to next model.",
                config.model_name,
            )

        logger.error("[LLM Fallback] All models exhausted. Returning failure.")
        return None

    # ------------------------------------------------------------------ #
    #  Dispatch                                                           #
    # ------------------------------------------------------------------ #

    def _dispatch(
        self,
        config: FallbackModelConfig,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: int,
    ) -> Dict[str, Any]:
        if config.api_type == "azure_openai":
            return self._call_openai(config, messages, temperature, max_tokens)
        if config.api_type == "anthropic":
            return self._call_anthropic(config, messages, temperature, max_tokens)
        raise ValueError(f"Unknown api_type: {config.api_type!r}")

    # ------------------------------------------------------------------ #
    #  Azure OpenAI                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _call_openai(
        config: FallbackModelConfig,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: int,
    ) -> Dict[str, Any]:
        client = AzureOpenAI(
            azure_endpoint=config.azure_endpoint,
            api_key=config.azure_api_key,
            api_version=config.azure_api_version,
        )
        kwargs: Dict[str, Any] = {"model": config.model_name, "messages": messages}
        if temperature is not None and not config.model_name.startswith("gpt-5"):
            kwargs["temperature"] = temperature

        response = client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content.strip()

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
        return {"text": text, "usage": usage, "model": config.model_name}

    # ------------------------------------------------------------------ #
    #  Anthropic REST API                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _call_anthropic(
        config: FallbackModelConfig,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: int,
    ) -> Dict[str, Any]:
        system_text, anthropic_messages = LLMFallbackHandler._openai_to_anthropic(messages)

        headers = {
            "x-api-key": config.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": config.model_name,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = temperature

        max_rate_limit_retries = 3
        for rl_attempt in range(max_rate_limit_retries):
            resp = requests.post(
                config.anthropic_api_url, headers=headers, json=payload, timeout=600,
            )
            if resp.status_code in (429, 529):
                retry_after = resp.headers.get("retry-after")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(2 ** (rl_attempt + 1) * 2, 60)
                logger.warning(
                    "[Anthropic] Rate limited (HTTP %d), waiting %ds (rate-limit retry %d/%d)",
                    resp.status_code, wait, rl_attempt + 1, max_rate_limit_retries,
                )
                time.sleep(wait)
                continue
            break

        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code}: {resp.text[:2000]}")

        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                break

        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_raw.get("input_tokens", 0),
            "completion_tokens": usage_raw.get("output_tokens", 0),
            "total_tokens": (
                usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0)
            ),
        }
        return {"text": text, "usage": usage, "model": config.model_name}

    # ------------------------------------------------------------------ #
    #  Format conversion helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _openai_to_anthropic(
        messages: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Convert an OpenAI-format message list to Anthropic format.

        Returns ``(system_text, anthropic_messages)``.
        System-role messages are extracted to a top-level string (Anthropic
        requires ``system`` as a separate parameter).  ``image_url`` content
        blocks are converted to Anthropic ``image`` blocks with base64 source.
        """
        system_text = ""
        anthropic_messages: List[Dict[str, Any]] = []

        for msg in messages:
            if msg.get("role") == "system":
                system_text = msg.get("content", "")
                continue

            content = msg.get("content", "")
            if isinstance(content, str):
                anthropic_messages.append({"role": msg["role"], "content": content})
            elif isinstance(content, list):
                converted: List[Dict[str, Any]] = []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        converted.append({"type": "text", "text": block["text"]})
                    elif btype == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            header, b64_data = url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                            converted.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            })
                    elif btype == "image":
                        converted.append(block)
                anthropic_messages.append({"role": msg["role"], "content": converted})

        return system_text, anthropic_messages

    @staticmethod
    def _anthropic_to_openai(
        system_text: str,
        anthropic_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert Anthropic-format messages to OpenAI format.

        Anthropic ``image`` blocks with base64 source are converted to OpenAI
        ``image_url`` data-URI blocks.  The ``system`` string is prepended as
        a system-role message.
        """
        openai_messages: List[Dict[str, Any]] = []
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

        for msg in anthropic_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                openai_messages.append({"role": msg["role"], "content": content})
            elif isinstance(content, list):
                converted: List[Dict[str, Any]] = []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        converted.append({"type": "text", "text": block["text"]})
                    elif btype == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            data_uri = f"data:{source['media_type']};base64,{source['data']}"
                            converted.append({
                                "type": "image_url",
                                "image_url": {"url": data_uri, "detail": "high"},
                            })
                    elif btype == "image_url":
                        converted.append(block)
                openai_messages.append({"role": msg["role"], "content": converted})

        return openai_messages


class PipelineReport:
    """Collects metrics and timing data for each pipeline run."""

    def __init__(self, input_file_name: str = ""):
        self.input_file_name = input_file_name
        self.pipeline_start_time = None
        self.pipeline_end_time = None
        self.total_execution_time_seconds = 0.0
        self.document_pages = 0
        self.total_track_changes = 0
        self.total_insertions = 0
        self.total_deletions = 0
        self.total_llm_calls = 0
        self.llm_calls_skipped = 0
        self.llm_calls_failed = 0
        self.llm_calls_succeeded = 0
        self.llm_model = ""
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.total_llm_cost_usd = 0.0
        self.steps: List[Dict[str, Any]] = []
        self.llm_call_details: List[Dict[str, Any]] = []
        self._step_timers: Dict[str, float] = {}
        self._cost_per_1k_prompt = 0.0
        self._cost_per_1k_completion = 0.0

    def start_pipeline(self):
        self.pipeline_start_time = time.time()

    def end_pipeline(self):
        self.pipeline_end_time = time.time()
        self.total_execution_time_seconds = round(
            self.pipeline_end_time - self.pipeline_start_time, 3
        )

    def start_step(self, step_name: str):
        self._step_timers[step_name] = time.time()

    def end_step(self, step_name: str, details: Optional[Dict[str, Any]] = None):
        elapsed = round(time.time() - self._step_timers.get(step_name, time.time()), 3)
        step_entry = {
            "step_name": step_name,
            "duration_seconds": elapsed,
            "status": "completed",
        }
        if details:
            step_entry["details"] = details
        self.steps.append(step_entry)

    def set_cost_rates(self, cost_per_1k_prompt: float, cost_per_1k_completion: float):
        self._cost_per_1k_prompt = cost_per_1k_prompt
        self._cost_per_1k_completion = cost_per_1k_completion

    def record_llm_call(self, segment: str, duration: float, status: str,
                        pages_used: List[int] = None, skipped: bool = False,
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        total_tokens: int = 0):
        call_cost = (prompt_tokens / 1000.0) * self._cost_per_1k_prompt + \
                    (completion_tokens / 1000.0) * self._cost_per_1k_completion
        entry = {
            "segment": segment,
            "duration_seconds": round(duration, 3),
            "status": status,
            "pages_used": pages_used or [],
            "skipped": skipped,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(call_cost, 6),
        }
        self.llm_call_details.append(entry)
        self.total_llm_calls += 1
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.total_llm_cost_usd += call_cost
        if skipped:
            self.llm_calls_skipped += 1
        elif status == "success":
            self.llm_calls_succeeded += 1
        else:
            self.llm_calls_failed += 1

    def to_dict(self) -> Dict[str, Any]:
        total_llm_time = sum(c["duration_seconds"] for c in self.llm_call_details)
        non_llm_time = self.total_execution_time_seconds - total_llm_time
        return {
            "input_file": self.input_file_name,
            "document_pages": self.document_pages,
            "total_track_changes": self.total_track_changes,
            "total_insertions": self.total_insertions,
            "total_deletions": self.total_deletions,
            "total_execution_time_seconds": self.total_execution_time_seconds,
            "total_execution_time_formatted": self._fmt(self.total_execution_time_seconds),
            "total_llm_time_seconds": round(total_llm_time, 3),
            "total_llm_time_formatted": self._fmt(total_llm_time),
            "total_non_llm_time_seconds": round(non_llm_time, 3),
            "total_non_llm_time_formatted": self._fmt(non_llm_time),
            "llm_summary": {
                "model": self.llm_model,
                "total_calls": self.total_llm_calls,
                "succeeded": self.llm_calls_succeeded,
                "failed": self.llm_calls_failed,
                "skipped_short_text": self.llm_calls_skipped,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "total_cost_usd": round(self.total_llm_cost_usd, 4),
            },
            "pipeline_steps": self.steps,
            "llm_call_details": self.llm_call_details,
        }

    @staticmethod
    def _fmt(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{round(seconds, 2)}s"

    def save(self, output_path: str, file_name: str):
        report_path = os.path.join(output_path, file_name)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f"Pipeline report saved: {report_path}")
        return report_path

class DOCXProcessor:
    """Handles DOCX file processing and track change extraction."""
    
    def __init__(self, docx_path: str):
        """
        Initialize DOCX processor.
        
        Args:
            docx_path: Path to the DOCX file
        """
        self.docx_path = docx_path
        self.doc_tree = None
        self.footnote_tree = None
    
    def extract_manual_footnotes(self, output_txt_path: str):
        """
        Parses the document XML to find manual footnotes (superscripted text 
        without proper footnoteReference links) and saves them to a text file.
        
        Args:
            output_txt_path: Path to save the resulting text file.
        """
        logger.info(f"Extracting manual footnotes to {output_txt_path}")
        
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        
        # 1. Pre-process paragraphs to find potential footnote definitions 
        # (Paragraphs starting with numbers/symbols that might be the footer text)
        para_definitions = []
        body = self.doc_tree.find('.//w:body', namespaces=ns)
        
        if body is None:
            logger.error("No body found in DOCX XML")
            return

        # Collect all paragraphs text to search for definitions later
        # We store: (stripped_text, full_xml_element)
        all_paras = body.findall('.//w:p', namespaces=ns)
        for p in all_paras:
            text = "".join([t.text for t in p.findall('.//w:t', namespaces=ns) if t.text])
            if text:
                para_definitions.append(text.strip())

        manual_footnotes = []
        current_page = 1
        
        # Helper to recursively traverse and track pages
        def traverse_for_superscripts(elem):
            nonlocal current_page
            
            # Update page count based on rendered page breaks
            # Note: This relies on Word having saved the page break tags.
            if elem.tag.endswith('lastRenderedPageBreak'):
                current_page += 1
            
            # Check for Run elements <w:r>
            if elem.tag.endswith('r'):
                rPr = elem.find('w:rPr', namespaces=ns)
                if rPr is not None:
                    # Check for superscript
                    vertAlign = rPr.find('w:vertAlign', namespaces=ns)
                    if vertAlign is not None and vertAlign.get(f'{{{ns["w"]}}}val') == 'superscript':
                        
                        # Check if this run contains a footnoteReference (if so, it's a REAL footnote, ignore it)
                        if elem.find('w:footnoteReference', namespaces=ns) is not None:
                            return
                        
                        # Extract the text of the superscript
                        t_elem = elem.find('w:t', namespaces=ns)
                        if t_elem is not None and t_elem.text:
                            ref_text = t_elem.text.strip()
                            
                            # Filter out empty or non-footnote looking superscripts (optional heuristic)
                            # e.g., ignore superscripts like "st", "nd", "rd", "th"
                            if not ref_text or ref_text.lower() in ['st', 'nd', 'rd', 'th']:
                                return

                            # Attempt to find the "content" - heuristic matching
                            # We look for a paragraph that starts with this reference number
                            found_content = "Content not found"
                            
                            # Heuristic: Find a paragraph that starts with the ref_text 
                            # and is NOT the paragraph we are currently inside (context check omitted for speed)
                            for p_text in para_definitions:
                                # Check if paragraph starts with "1 " or "1." or "1\t"
                                if p_text.startswith(f"{ref_text} ") or \
                                   p_text.startswith(f"{ref_text}.") or \
                                   p_text.startswith(f"{ref_text}\t"):
                                    # Avoid matching the reference itself in the text flow
                                    if len(p_text) > len(ref_text) + 5: 
                                        found_content = p_text
                                        break
                            
                            manual_footnotes.append({
                                "no": ref_text,
                                "content": found_content,
                                "page": str(current_page)
                            })

            # Recurse
            for child in elem:
                traverse_for_superscripts(child)

        # Start traversal
        traverse_for_superscripts(body)

        # Write to file
        try:
            with open(output_txt_path, 'w', encoding='utf-8') as f:
                if not manual_footnotes:
                    f.write("No manual footnotes found.")
                
                for note in manual_footnotes:
                    # Format: Footnote No "9.50" , "footnote_content" on page no "5".
                    # Sanitize quotes in content to avoid breaking the format
                    safe_content = note['content'].replace('"', "'").replace('\n', ' ')
                    line = f'Footnote No "{note["no"]}" , on page no "{note["page"]} needs review".\n'
                    f.write(line)
            
            logger.info(f"Successfully wrote {len(manual_footnotes)} manual footnotes to {output_txt_path}")
            
        except Exception as e:
            logger.error(f"Failed to write manual footnotes file: {e}")

        
    def extract_xml_from_docx(self) -> Tuple[etree.Element, etree.Element]:
        """
        Extract document and footnote XML from DOCX file.
        
        Returns:
            Tuple of (document_tree, footnote_tree)
        """
        logger.info(f"Extracting XML from DOCX: {self.docx_path}")
        
        try:
            with zipfile.ZipFile(self.docx_path, 'r') as docx_zip:
                # 1. Read Document XML (Always exists)
                document_xml = docx_zip.read('word/document.xml')
                self.doc_tree = etree.fromstring(document_xml)
                
                # 2. Check and Read Footnotes XML (Only exists if footnotes present)
                if 'word/footnotes.xml' in docx_zip.namelist():
                    footnotes_xml = docx_zip.read('word/footnotes.xml')
                    self.footnote_tree = etree.fromstring(footnotes_xml)
                else:
                    logger.info("No footnotes.xml found in archive. creating empty tree.")
                    # Create an empty element so downstream code (like findall) doesn't crash
                    self.footnote_tree = etree.Element("root")
            
            logger.info("Successfully extracted XML from DOCX")
            return self.doc_tree, self.footnote_tree
            
        except Exception as e:
            logger.error(f"Error extracting XML from DOCX: {e}")
            raise
    
    def _get_formatted_text(self, elem, ns: Dict[str, str]) -> str:
        """
        Helper to extract text from a <w:t> element and wrap it in formatting tags.
        
        Args:
            elem: XML element
            ns: Namespace dictionary
            
        Returns:
            Formatted text with tags
        """
        text = elem.text or ""
        parent = elem.getparent()
        
        if parent is not None and parent.tag.endswith('r'):
            rPr = parent.find('w:rPr', namespaces=ns)
            if rPr is not None:
                # if rPr.find('w:b', namespaces=ns) is not None:
                #     text = f"<b>{text}</b>"
                if rPr.find('w:i', namespaces=ns) is not None:
                    text = f"<ital>{text}</ital>"
                    
        return text
    
    def process_footnote_body(self, elem, ns: Dict[str, str], inside_ins: bool = False, inside_del: bool = False) -> str:
        """
        Recursively process footnote XML, capturing inserted and deleted text.
        
        Args:
            elem: XML element to process
            ns: Namespace dictionary
            inside_ins: Whether we're inside an insertion
            inside_del: Whether we're inside a deletion
            
        Returns:
            Processed footnote text
        """
        if elem is None:
            return ""
            
        tag = elem.tag
        
        # Skip field code XML elements
        if tag.endswith('fldChar') or tag.endswith('instrText') or tag.endswith('fldSimple'):
            return ""

        # Handle insertions
        if tag.endswith('ins') or tag.endswith('moveTo'):
            ins_parts = [self.process_footnote_body(child, ns, inside_ins=True, inside_del=inside_del) for child in elem]
            # ins_parts = [self.process_footnote_body(child, ns, inside_ins=True, inside_del=False) 
            #              for child in elem]
            return f"<trace>{''.join(ins_parts)}</trace>" if not inside_ins else ''.join(ins_parts)

        # Handle deletions
        if tag.endswith('del') or tag.endswith('moveFrom'):
            # del_parts = [self.process_footnote_body(child, ns, inside_ins=False, inside_del=True) 
            #              for child in elem]
            del_parts = [self.process_footnote_body(child, ns, inside_ins=inside_ins, inside_del=True) for child in elem]
            joined_parts = "".join(del_parts)
            if "Drafter's Note" in joined_parts:
                return joined_parts
            return f"<DEL_MARKER>{joined_parts}</DEL_MARKER>" if not inside_del else joined_parts

        if tag.endswith('p'):
            p_content = [self.process_footnote_body(child, ns, inside_ins, inside_del) for child in elem]
            return f"<para>{''.join(p_content)}</para>"

        # Handle inline elements
        result = []
        if (tag.endswith('t') or tag.endswith('delText')) and elem.text:
            result.append(self._get_formatted_text(elem, ns))
        
        # Default: Process children of other container tags (like 'footnote')
        for child in elem:
            result.append(self.process_footnote_body(child, ns, inside_ins, inside_del))

        return ''.join(result)

    def extract_full_text_with_track_changes(self) -> str:
        """
        Extract full text from document with track changes marked.
        
        Returns:
            Text with track change markers
        """
        logger.info("Extracting full text with track changes")
        
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        footnote_map = {fn.get(f'{{{ns["w"]}}}id'): fn 
                        for fn in self.footnote_tree.findall('.//w:footnote', namespaces=ns)}

     
        def process_element(elem, inside_ins=False, inside_del=False, state=None):
            if state is None:
                state = {'wrap_next_t_with_url': False}
            
            tag = elem.tag

            # Terminal elements (skip)
            if tag.endswith('fldChar') or tag.endswith('instrText') or tag.endswith('fldSimple'):
                return []

            # Container elements
            if tag.endswith('ins') or tag.endswith('moveTo'):
                ins_parts = [part for child in elem for part in process_element(child, inside_ins=True, inside_del=inside_del, state=state)]
                # ins_parts = [part for child in elem for part in process_element(child, inside_ins=True, inside_del=False, state=state)]
                if not inside_ins and ins_parts:
                    return ['<trace>', *ins_parts, '</trace>']
                return ins_parts

            if tag.endswith('del') or tag.endswith('moveFrom'):
                del_parts = [part for child in elem for part in process_element(child, inside_ins=inside_ins, inside_del=True, state=state)]
                # del_parts = [part for child in elem for part in process_element(child, inside_ins=False, inside_del=True, state=state)]
                if not inside_del and del_parts:
                    joined_parts = ''.join(del_parts)
                    if "Drafter's Note" in joined_parts:
                        return del_parts
                    return ['<DEL_MARKER>', *del_parts, '</DEL_MARKER>']
                return del_parts

            if tag.endswith('p'):
                p_content = [part for child in elem for part in process_element(child, inside_ins, inside_del, state=state)]
                return ['<para>', *p_content, '</para>']

            # Inline elements
            if tag.endswith('footnoteReference'):
                fn_id = elem.get(f'{{{ns["w"]}}}id')
                footnote_elem = footnote_map.get(fn_id)
                traced_body = self.process_footnote_body(footnote_elem, ns, inside_ins, inside_del)
                return [f'<footnote><footnote.body>{traced_body}</footnote.body></footnote>']
            if tag.endswith('t'):
                parent = elem.getparent()
                if parent is not None and parent.tag.endswith('r'):
                    # Already-handled case: parent run contains <w:footnoteReference>
                    if parent.find('.//w:footnoteReference', namespaces=ns) is not None:
                        return []
                    # NEW: skip text from runs with FootnoteReference character style
                    # (handles custom marks where the footnote number is split across runs)
                    rPr = parent.find('w:rPr', namespaces=ns)
                    if rPr is not None:
                        rStyle = rPr.find('w:rStyle', namespaces=ns)
                        if rStyle is not None and rStyle.get(f'{{{ns["w"]}}}val') == 'FootnoteReference':
                            return []
                if elem.text:
                    formatted_text = self._get_formatted_text(elem, ns)
                    if state.get('wrap_next_t_with_url', False):
                        state['wrap_next_t_with_url'] = False
                        return [f'<url>{formatted_text}</url>']
                    else:
                        return [formatted_text]
                return [] # Is footnote ref text, skip
            
            if tag.endswith('delText'):
                if elem.text:
                    formatted_text = self._get_formatted_text(elem, ns)
                    return [formatted_text]
                return []

            if tag.endswith('br'):
                return ['<br>']

            # Default: Process children of other container tags (like 'body', 'tbl', 'tr', 'tc')
            result = []
            for child in elem:
                result.extend(process_element(child, inside_ins, inside_del, state=state))
            return result

        body = self.doc_tree.find('.//w:body', namespaces=ns)
        full_text_parts = process_element(body) if body is not None else []
        
        logger.info("Successfully extracted text with track changes")
        return ''.join(full_text_parts)
        
class TextProcessor:
    """Handles text cleaning and processing operations."""
    
    @staticmethod
    def clean_text(input_text: str) -> str:
        """
        Clean text by removing unwanted tags and replacing entities.
        
        Args:
            input_text: Raw text to clean
            
        Returns:
            Cleaned text
        """
        # Protect allowed tags
        placeholder_map = {
            "<trace>": "___TRACE_OPEN___", "</trace>": "___TRACE_CLOSE___",
            "<b>": "___B_OPEN___", "</b>": "___B_CLOSE___",
            "<ital>": "___I_OPEN___", "</ital>": "___I_CLOSE___",
            "<DEL_MARKER>": "___DEL_OPEN___", "</DEL_MARKER>": "___DEL_CLOSE___",
            "<footnote>": "___FOOTNOTE_OPEN___", "</footnote>": "___FOOTNOTE_CLOSE___",
            "<para>": "___PARA_OPEN___",
            "</para>": "___PARA_CLOSE___"
        }

        text = input_text
        for tag, placeholder in placeholder_map.items():
            text = text.replace(tag, placeholder)

        # Remove all other XML/HTML tags
        text = re.sub(r"<[^>]+>", "", text)

        # Restore protected tags
        for tag, placeholder in placeholder_map.items():
            text = text.replace(placeholder, tag)

        # Apply replacements
        replacements = [
            ("&s;§", "&sect;"), ("&ss;§§", "&dblsect;"), ("&p;", "&para;"),
            ("'", "&apos;"), ("&ldquo;“", "&ldquo;"), ("&rdquo;”", "&rdquo;"),
            ("&ndash;-", "&ndash;"), ("&rsquo;’", "&rsquo;"), ("&lsquo;‘", "&lsquo;"),
            ("--", "&mdash;"), ("$", "&dollar;"), ("[", "&lsqb;"),
            ("]", "&rsqb;"),("‘", "&lsquo;"),("’", "&rsquo;"),("=", "&equals;"),
            ("§", "&sect"), ("*", "&ast;"), ("►", ""),("♦",""),("West's Key Number Digest, Trial ","West's Key Number Digest, Trial &key;")
        ]

        for old, new in replacements:
            text = text.replace(old, new)
        # This looks for '&sect' NOT followed by a ';' and adds the ';'
        text = re.sub(r'&sect(?!;)', '&sect;', text)

        return text
    
    @staticmethod
    def flatten_trace_tags(text: str) -> str:
        """
        Normalize and merge adjacent trace and formatting tags.
        
        Args:
            text: Text with trace tags
            
        Returns:
            Flattened text
        """
        # Normalize trace tags
        text = re.sub(r"<\s*trace\s*>", "<trace>", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*trace\s*>", "</trace>", text, flags=re.IGNORECASE)
    
        # Collapse multiple deletion markers
        text = re.sub(r"(?:\s*<trace\.deleted\s*/>\s*)+", "<trace.deleted/>", text, flags=re.IGNORECASE)
    
        # Iteratively merge adjacent trace and formatting tags until no more changes can be made.
        while True:
            original_text = text
    
            # 1. Merge adjacent traces with only whitespace
            text = re.sub(r"</trace>\s*<trace>", "", text, flags=re.IGNORECASE)
            
            # 2. Merge adjacent traces separated by full, empty para blocks
            # <para>\s*</para> handles empty paragraphs
            text = re.sub(r"</trace>(\s*<para>\s*</para>\s*)+<trace>", r"\1", text, flags=re.IGNORECASE)

            # 3. Merge adjacent traces separated by <para> boundaries
            text = re.sub(r"</trace></para>\s*<para><trace>", r"</para><para>", text, flags=re.IGNORECASE)
            
            # 4. Merge around DEL_MARKER with only whitespace
            # text = re.sub(
            #     r"</trace>\s*(<DEL_MARKER>[^<]*</DEL_MARKER>)\s*<trace>",
            #     r"\1",
            #     text,
            #     flags=re.IGNORECASE
            # )
            
            # 5. Merge around DEL_MARKER at para boundaries (when DEL_MARKER is inside its own <para>)
            text = re.sub(
                r"</trace></para>\s*<para>(<DEL_MARKER>[^<]*</DEL_MARKER>)</para>\s*<para><trace>",
                r"</para><para>\1</para><para>",
                text,
                flags=re.IGNORECASE
            )

            # Merge adjacent formatting tags (bold/italic)
            text = re.sub(r'</b>\s*<b>', '', text, flags=re.IGNORECASE)
            text = re.sub(r'</ital>\s*<ital>', '', text, flags=re.IGNORECASE)
    
            # If the text is no longer changing, exit the loop
            if text == original_text:
                break
 
    
        # Final cleanup
        text = re.sub(r"\s*<trace\.deleted\/>\s*", "<trace.deleted/>", text)
        text = re.sub(r"<trace></trace>", "", text)
        text = re.sub(r"\s+([.,;:])", r"\1", text)
        text = re.sub(r"[ ]{2,}", " ", text)
    
        return text.strip()

    
    @staticmethod
    def remove_field_code_artifacts(text: str) -> str:
        """
        Remove field code artifact patterns also add key to west key.
        Args:
            text: Text potentially containing artifacts
        Returns:
            Cleaned text
        """
        # Updated to dynamically catch pstart, updated, uuid, ct.id, rowsep, style, resize and other leftover XML attribute fragments
        artifact_pattern = re.compile(r'\b(?:(?:\w+\.)*(?:id|fnref|pstart|updated|rowsep|style|resize)|uuid)="[^"]*"\s*')
        text = artifact_pattern.sub("", text)
        pattern = re.compile(r"(West(?:&apos;|['’])s Key Number Digest,.*?)(\d+)", re.IGNORECASE | re.DOTALL)
        result = pattern.sub(r"\1&key;\2", text)
        return result.strip()   
        
class MapperGenerator:
    """Generates mapping instructions for track changes."""
    
    @staticmethod
    def find_top_level_changes(text: str):
        """
        Generator that finds top-level trace and deletion marker tags.
        
        Args:
            text: Text to analyze
            
        Yields:
            Tuple of (tag_type, content_string, start_index, end_index)
        """
        tag_pattern = re.compile(r"(<trace>)|(</trace>)|(<DEL_MARKER>)|(</DEL_MARKER>)")
        matches = list(tag_pattern.finditer(text))
        
        level = 0
        start_match = None
        current_tag_type = None
    
        for match in matches:
            match_str = match.group(0)
    
            if match_str == "<trace>":
                if level == 0:
                    start_match = match
                    current_tag_type = "insertion"
                level += 1
            elif match_str == "</trace>":
                level -= 1
                if level == 0 and current_tag_type == "insertion" and start_match:
                    content_start = start_match.end()
                    content_end = match.start()
                    yield ("insertion", text[content_start:content_end], start_match.start(), match.end())
                    start_match = None
                    current_tag_type = None
            elif match_str == "<DEL_MARKER>":
                if level == 0:
                    start_match = match
                    current_tag_type = "deletion"
                level += 1
            elif match_str == "</DEL_MARKER>":
                level -= 1
                if level == 0 and current_tag_type == "deletion" and start_match:
                    content_start = start_match.end()
                    content_end = match.start()
                    yield ("deletion", text[content_start:content_end], start_match.start(), match.end())
                    start_match = None
                    current_tag_type = None
            
            if level < 0:
                level = 0
                start_match = None
                current_tag_type = None
    
    @staticmethod
    def create_mapper_instructions(processed_text: str) -> List[Dict[str, Any]]:
        """
        Create mapping instructions from processed text.
        
        Args:
            processed_text: Text with resolved track changes
            
        Returns:
            List of mapping instructions
        """
        logger.info("Creating mapper instructions")
        
        mapper_instructions = []
        ins_counter = 1
        del_counter = 1
        
        total_changes = len(list(MapperGenerator.find_top_level_changes(processed_text)))
        progress_bar = tqdm(total=total_changes, desc="Creating mapper", unit="change")
        # This regex will find and remove entire trace/deletion blocks (tags + content)
        change_block_pattern = re.compile(r'<DEL_MARKER>.*?</DEL_MARKER>', re.DOTALL)
        # This regex will clean remaining formatting/structure tags
        cleanup_tag_pattern = re.compile(r'<[^>]+>')
        new_para = True

        def clean_content_tags(content: str) -> str:  
            # 1) From the start  
            open_idx = content.find("<para>")  
            close_idx = content.find("</para>")  
            # remove the first occurring </para> only if it occurs before any <para>  
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):  
                content = content[:close_idx] + content[close_idx + len("</para>"):]
          
            # 2) From the end  
            open_last = content.rfind("<para>")  
            close_last = content.rfind("</para>")  
            # remove the last occurring <para> only if it occurs after any </para>  
            if open_last != -1 and (close_last == -1 or open_last > close_last):  
                content = content[:open_last] + content[open_last + len("<para>"):]
  
            return content 
        
        for change_type, content, start_idx, end_idx in MapperGenerator.find_top_level_changes(processed_text):
            content = content.replace("1<footnote>","<footnote>")
            if change_type == "insertion":
                # Look back directly from source string
                prev_text = processed_text[max(0, start_idx - 200):start_idx]
                next_text = processed_text[end_idx:]
                text_before = processed_text[:start_idx]
                # Find the last occurrence of opening and closing para tags
                last_open_para = -1
                for match in re.finditer(r"(?<!<footnote>)<para>", text_before):
                    last_open_para = match.start()
                # Match </para> ONLY if it is NOT followed by </footnote>
                last_close_para = -1
                for match in re.finditer(r"</para>(?!</footnote>)", text_before):
                    last_close_para = match.start()
                if last_open_para > last_close_para and not prev_text.endswith("<para>"):
                    # The last tag seen was <para>, so we are inside a paragraph
                    new_para = False
                else:
                    # The last tag seen was </para> (or no tags found yet), so we are outside/starting new
                    new_para = True
                if prev_text.endswith("</footnote>") or prev_text.endswith("</footnote></DEL_MARKER>"):
                    new_para = False
                if prev_text.endswith("<para>"):
                    content = "<para>" + content
                if next_text.startswith("</para>"):
                    content = content + "</para>"
                         
                # prev_text = prev_text.replace("</trace>","</trace> ")
                # Clean *all* tags (trace, del, html) to get previous context
                prev_text_clean = re.sub(r'<[^>]+>', '', prev_text)
                add_space = ""
                if prev_text_clean.endswith(" ") or content.startswith(" "):
                    add_space = " "
                words = prev_text_clean.split() # Define words
                # prev_text_clean_anchor = " ".join(words[-10:])
                
                if " " in prev_text_clean:
                    prev_text_clean = prev_text_clean.split(" ", 1)[1]

                content = clean_content_tags(content)
                if content == "<trace.deleted/>" or content == " ":
                    continue
                # Skip insertions that contain only empty tags and no actual text
                if not re.sub(r'<[^>]+>', '', content).strip():
                    continue
                mapper_instructions.append({
                    "type": "insertion",
                    "Track Change Segment": str(ins_counter),
                    "Track changes": f"<trace>{add_space}{content}</trace>".replace("<trace.deleted/>",""),
                    "Previous Content": prev_text_clean.replace("<b>","").replace("</b>","").replace("<ital>"," ").replace("</ital>"," ")
                                                    .replace("&s;&sect","&sect;").replace("–","&ndash;")
                                                    .replace("&ldquo;“", "&ldquo;").replace("&rdquo;”", "&rdquo;").replace("‘", "&lsquo;").replace("’", "&rsquo;")
                                                    .replace("&ss;&sect&sect","&dblsect;").replace("—","&mdash;").replace("#","&num;").replace("....","&hellip4;").replace("...","&hellip;").replace("&b;•","&bull;")
                                                    .replace("<footnote>"," ").replace("</footnote>"," ").replace("&sect","&sect;").replace("&sect;;","&sect;").replace("–","&ndash;")
                                                    .replace("<para>","").replace("</para>","").replace("¶","&para;").replace("\"","&quot;").replace("”", "&rdquo;").replace("“","&ldquo;")
                                                    .replace("&sect;&sect;","&dblsect;").replace("&s;","&sect;").replace("&ast;&ast;&ast;&ast;&ast;", "&ast;&ast;&ast;").replace("&nbsp;",""),
                    "is_new_para":new_para
                })
                ins_counter += 1

            elif change_type == "deletion":
                # Correct pattern using capture groups
                pattern = r'(<footnote>\d+) (.*?</footnote>)'
                # The replacement string '\1\2' reassembles the captured groups without the space
                content = re.sub(pattern, r'\1\2', content)
                prev_text = processed_text[max(0, start_idx - 200):start_idx]
                # prev_text = prev_text.replace("</trace>","</trace> ") # Keep consistency
                # prev_text_clean = re.sub(r'<[^>]+>', '', prev_text).strip()
                # 1. First, remove all *previous* change blocks (tags and content)
                prev_text_no_changes = change_block_pattern.sub('', prev_text)
                if "</DEL_MARKER>" in prev_text_no_changes:
                # split on the tag and take the last part (the text AFTER the deleted block)
                    prev_text_no_changes = prev_text_no_changes.split("</DEL_MARKER>")[-1]
                # 2. Now, clean *remaining* formatting tags (<ital>, <para>, etc.)
                prev_text_clean = cleanup_tag_pattern.sub('', prev_text_no_changes).strip()
                # --- MODIFICATION END ---
                # words = prev_text_clean.split() # Define words
                # prev_text_clean_anchor = " ".join(words[-10:])

                # if len(prev_text_clean) < 100:
                #     logger.info(f"For Track Change Segment: {str(del_counter)} Prev content length is less than 200 char - {len(prev_text_clean)}")
                #     value = 300
                #     prev_text_clean_new = prev_text_clean
                #     while len(prev_text_clean_new) < 100:
                #         prev_text = processed_text[max(0, start_idx - value):start_idx]
                #         prev_text_no_changes = change_block_pattern.sub('', prev_text)
                #         if "</DEL_MARKER>" in prev_text_no_changes:
                #             prev_text_no_changes = prev_text_no_changes.split("</DEL_MARKER>")[-1]
                #         prev_text_clean_new = cleanup_tag_pattern.sub('', prev_text_no_changes).strip()
                #         if len(prev_text_clean_new) == len(prev_text_clean) and value > 300:
                #             break
                #         value = value + 100
                #     prev_text_clean = prev_text_clean_new
                #     logger.info(f"increasing length to {len(prev_text_clean_new)}")
                MAX_LOOKBACK = 2000  # Set a reasonable maximum search range
                TARGET_LENGTH = 100
                INCREMENT = 100
                if len(prev_text_clean) < TARGET_LENGTH:
                    logger.info(f"Segment {del_counter}: Initial length {len(prev_text_clean)} is under {TARGET_LENGTH}. Expanding search.")
                    value = 300
                    while len(prev_text_clean) < TARGET_LENGTH:
                        # 1. Calculate slice boundaries
                        current_start = max(0, start_idx - value)
                        prev_text = processed_text[current_start:start_idx]
                        # 2. Process text (regex and cleaning)
                        prev_text_no_changes = change_block_pattern.sub('', prev_text)
                        if "</DEL_MARKER>" in prev_text_no_changes:
                            prev_text_no_changes = prev_text_no_changes.split("</DEL_MARKER>")[-1]
                        new_clean_text = cleanup_tag_pattern.sub('', prev_text_no_changes).strip()
                        # 3. Exit Conditions (The Safety Net)
                        # Check if we've reached the start of the document
                        reached_doc_start = (current_start == 0)
                        # Check if increasing 'value' actually gave us more text
                        no_new_data = (len(new_clean_text) <= len(prev_text_clean)) and value > 300
                        # Hard limit to prevent infinite processing
                        hit_max_limit = (value >= MAX_LOOKBACK)
                        prev_text_clean = new_clean_text
                        if reached_doc_start or no_new_data or hit_max_limit:
                            logger.info(f"Search stopped. Reason: start_reached={reached_doc_start}, no_new_data={no_new_data}, max_limit={hit_max_limit}")
                            break
                        value += INCREMENT
                        logger.debug(f"Extended lookback to {value} chars. Current clean length: {len(prev_text_clean)}")
                    logger.info(f"Final expanded length: {len(prev_text_clean)}")
                
                if " " in prev_text_clean:
                    prev_text_clean = prev_text_clean.split(" ", 1)[1]
             
                    
                if content.strip():
                    mapper_instructions.append({
                        "type": "deletion",
                        "Track Change Segment": str(del_counter),
                        "Deleted Content": content.replace("<b>","").replace("</b>","").replace("<ital>","").replace("</ital>","")
                                                .replace("<footnote>","").replace("</footnote>","")
                                                .replace("&s;&sect","&sect;").replace("&ndash;-","&ndash;")
                                                .replace("&ldquo;“", "&ldquo;").replace("&rdquo;”", "&rdquo;").replace("#","&num;").replace("....","&hellip4;").replace("...","&hellip;")
                                                .replace("&ss;&sect&sect","&dblsect;").replace(" & "," &amp; ").replace("¶","&para;")
                                                .replace("“", "&ldquo;").replace("”", "&rdquo;").replace("\"","&quot;").replace("%","&percnt;")
                                                .replace("‘", "&lsquo;").replace("’", "&rsquo;").replace("—","&mdash;").replace("<para>","").replace("</para>","").replace("&b;•","&bull;")
                                                .replace("&sect&sect","&dblsect;").replace("&sect","&sect;").replace("&sect;;","&sect;").replace("&sect;&sect;","&dblsect;").replace("&s;","&sect;").replace("&nbsp;",""),
                        "Previous Content": prev_text_clean.replace("<b>"," ").replace("</b>"," ").replace("<ital>"," ").replace("</ital>"," ").replace("“","&ldquo;")
                                                    .replace("&s;&sect","&sect;").replace("&ndash;-","&ndash;").replace("#","&num;")
                                                    .replace("&ldquo;“", "&ldquo;").replace("&rdquo;”", "&rdquo;").replace("‘", "&lsquo;").replace("’", "&rsquo;")
                                                    .replace("&ss;&sect&sect","&dblsect;").replace("—","&mdash;").replace("¶","&para;").replace("....","&hellip4;").replace("...","&hellip;").replace("&b;•","&bull;")
                                                    .replace("<footnote>"," ").replace("</footnote>"," ").replace("<para>","").replace("</para>","").replace("–","&ndash;")
                                                    .replace("&sect","&sect;").replace("&sect;;","&sect;").replace("\"","&quot;").replace("”", "&rdquo;").replace("“","&ldquo;")
                                                    .replace("&sect;&sect;","&dblsect;").replace("&s;","&sect;").replace("&nbsp;",""),
                    })
                    del_counter += 1
                    
            # --- REMOVED ---
            # last_end_index = end_idx
            progress_bar.update(1)
            
        progress_bar.close()
        logger.info(f"Created {len(mapper_instructions)} mapper instructions")
        mapper_instructions = MapperGenerator.merge_consecutive_deletions(mapper_instructions)
        return mapper_instructions
    
    @staticmethod
    def merge_consecutive_deletions(mapper_instructions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Merge consecutive deletion segments that have the same "Previous Content".
        Updates segment numbers after merging.
        
        Args:
            mapper_instructions: List of mapping instructions
            
        Returns:
            Updated list with merged deletions and renumbered segments
        """
        logger.info("Checking for consecutive deletions to merge")
        
        merged_instructions = []
        i = 0
        deletions_merged = 0
        
        while i < len(mapper_instructions):
            current = mapper_instructions[i]
            
            # Check if this is a deletion
            if current["type"] == "deletion":
                # Look ahead for consecutive deletions with same Previous Content
                deleted_contents = [current["Deleted Content"]]
                j = i + 1
                
                while j < len(mapper_instructions):
                    next_item = mapper_instructions[j]
                    
                    # Check if next item is also a deletion with same Previous Content
                    if (next_item["type"] == "deletion" and 
                        next_item["Previous Content"] == current["Previous Content"]):
                        deleted_contents.append(next_item["Deleted Content"])
                        j += 1
                        deletions_merged += 1
                    else:
                        break
                
                # Create merged deletion entry
                merged_deletion = {
                    "type": "deletion",
                    "Track Change Segment": current["Track Change Segment"],
                    "Deleted Content": "".join(deleted_contents),
                    "Previous Content": current["Previous Content"]
                }
                merged_instructions.append(merged_deletion)
                
                if j > i + 1:
                    logger.info(f"Merged {j - i} consecutive deletion segments (original segments {current['Track Change Segment']}-{mapper_instructions[j-1]['Track Change Segment']}) with same Previous Content")
                
                i = j
            else:
                merged_instructions.append(current)
                i += 1
        
        # Renumber all segments
        insertion_counter = 1
        deletion_counter = 1
        
        for instruction in merged_instructions:
            if instruction["type"] == "insertion":
                instruction["Track Change Segment"] = str(insertion_counter)
                insertion_counter += 1
            else:  # deletion
                instruction["Track Change Segment"] = str(deletion_counter)
                deletion_counter += 1
        
        if deletions_merged > 0:
            logger.info(f"Merged {deletions_merged} deletion segments. New total: {len(merged_instructions)} instructions")
        else:
            logger.info("No consecutive deletions with same Previous Content found")
        
        return merged_instructions



class XMLUpdater:
    """Handles XML content updates for insertions and deletions."""
    
    def __init__(self, initial_xml_with_tags: str, mapper_instructions: List[Dict[str, Any]]):
        """
        Initialize XML updater.
        
        Args:
            initial_xml_with_tags: Original XML content with tags
            mapper_instructions: List of mapping instructions
        """
        self.xml_with_tags = initial_xml_with_tags
        self.xml_without_tags = re.sub(r'<[^>]+>', '', initial_xml_with_tags)
        self.instructions = sorted(mapper_instructions, key=lambda x: int(x['Track Change Segment']))
        self.search_start_pos = 0

    def run_updates(self) -> str:
        """
        Run all update operations.
        
        Returns:
            Updated XML content
        """
        logger.info(f"Processing {len(self.instructions)} update instructions")
        
        progress_bar = tqdm(total=len(self.instructions), desc="Processing updates", unit="instruction")
        
        for instruction in self.instructions:
            segment_num = instruction['Track Change Segment']
            is_new_para = instruction['is_new_para']
            logger.info(f"Processing update segment: {segment_num}")
            
            clean_previous_content = re.sub(r'<[^>]+>', '', instruction['Previous Content'])
            
            found_pos, final_prev_content = self._find_previous_content(clean_previous_content)
            if found_pos == -1:
                logger.error(f"Update insertion point not found for Segment {segment_num}")
                progress_bar.update(1)
                continue
            insertion_point_with_tags = self._calculate_tagged_insertion_point(found_pos, final_prev_content)
            safe_insertion_point = self._find_safe_insertion_point(insertion_point_with_tags,is_new_para)
            self._perform_insertion(instruction, found_pos, final_prev_content, safe_insertion_point)
            instruction['final_end_pos_no_tags'] = self.search_start_pos
            
            progress_bar.update(1)
            
        progress_bar.close()
        logger.info("All update segments processed")
        return self.xml_with_tags

    def _find_previous_content(self, previous_content: str) -> Tuple[int, Optional[str]]:
            """
            Find content using three-pass strategy.
            Args:
                previous_content: Content to find
            Returns:
                Tuple of (position, found_content) or (-1, None) if not found
            """
            original_content = previous_content.strip()        
            # Pass 1: Trim from start, search from last position
            current_search_string = original_content.replace("–","-")
            search_pos = max(0,self.search_start_pos-100)
            content_to_search = self.xml_without_tags.replace("__sep__","&mdash;")

            # --- Pass 0: Fuzzy Regex Match ---
            # This pass handles differences in spacing, punctuation, and HTML entities.
            logger.info("Attempting Pass 0 (Fuzzy Regex Match)...")
            try:
                # 1. Create a "fuzzy" regex from the previous_content
                #    - Replace all non-word blocks with a single space
                #    - Split into a list of words
                all_words = original_content.split()
    
                # 2. Make sure we're not dealing with an empty string
                if all_words:
                    # 3. Grab the last word, punctuation and all, and set it aside.
                    last_word_with_punctuation = all_words[-1]
                    # 4. Re-join everything ELSE into a single string.
                    head_string = ' '.join(all_words[:-1])
                    # 5. NOW run your regex logic, but *only* on the head string.
                    # 5a. First, neutralize HTML entities in the head string
                    #     so they act as separators, just like punctuation.
                    head_string_no_entities = re.sub(r'&\w+;', ' ', head_string)
                    # 5b. This strips all punctuation from the main body.
                    words_from_head = re.sub(r'\W+', ' ', head_string_no_entities).split()
                    # 6. Combine the clean list with the untouched last word.
                    words = words_from_head + [last_word_with_punctuation]
                else:
                    # Handle the empty case
                    words = []
                if not words:
                    raise ValueError("No words to search for in fuzzy pass")
    
                # 2. Join words with an entity-aware separator
                #    This matches \W* (zero or more non-word chars) OR
                #    &\w+; (HTML entities like &period; &dollar; etc.)
                #    This matches "Word1Word2", "Word1 Word2", "Word1&period;Word2", etc.
                entity_aware_separator = r'(?:\W|&\w+;)*'
                regex_pattern = entity_aware_separator.join(re.escape(word) for word in words)
                # 3. Search in the recommended window (same as Pass 3)
                search_pos = max(0,self.search_start_pos)
                # We search in a slice of the string for efficiency
                # This version searches content_to_search directly
                match = re.search(regex_pattern, content_to_search[search_pos:])
                if match:
                    # Adjust found position by the search_pos offset
                    found_pos = match.start() + search_pos
                    # We return original_content (like Pass 3) for consistency
                    found_string = match.group(0) 
                    logger.info(f"Found content (Pass 0 Fuzzy): '{found_string[:]}...' at pos {found_pos}")
                    return found_pos, found_string
                else:
                    lookback_pos = max(0, self.search_start_pos - 500)
                    if lookback_pos < search_pos:
                        match = re.search(regex_pattern, content_to_search[lookback_pos:])
                        if match:
                            found_pos = match.start() + lookback_pos
                            found_string = match.group(0)
                            logger.info(f"Found content (Pass 0 Fuzzy lookback): '{found_string[:]}...' at pos {found_pos}")
                            return found_pos, found_string
                    logger.info("Pass 0 Fuzzy search did not find a match.")
            except Exception as e:
                # If regex fails or anything else goes wrong, just log and fall through
                logger.warning(f"Pass 0 Fuzzy search failed: {e}. Proceeding to exact passes.")
            # --- End of Pass 0 ---

            while len(current_search_string.split()) > 1:
                try:
                    pos = content_to_search.index(current_search_string, search_pos)
                    logger.info(f"Found content (Pass 1): '{current_search_string[:60]}...'")
                    logger.info(f"CONTENT:{content_to_search[pos-100:pos+100]}")
                    return pos, current_search_string
                except ValueError:
                    words = current_search_string.split()
                    current_search_string = " ".join(words[1:])
                    search_pos = max(0,self.search_start_pos-100)

            # Try last word from Pass 1
            # if current_search_string:
            #     try:
            #         pos = content_to_search.index(current_search_string, self.search_start_pos)
            #         logger.info(f"Found content (Pass 1 final): '{current_search_string}'")
            #         logger.info(f"\nContent:{content_to_search[pos-100:pos+10]}")
            #         return pos, current_search_string
            #     except ValueError:
            #         pass
            # Pass 2: Trim from start, search from beginning
            current_search_string = original_content
            while len(current_search_string.split()) > 3:
                try:
                    pos = content_to_search.index(current_search_string, 0)
                    logger.info(f"Found content (Pass 2): '{current_search_string[:60]}...'")
                    return pos, current_search_string
                except ValueError:
                    words = current_search_string.split()
                    current_search_string = " ".join(words[1:])
            # Try last word from Pass 2
            if current_search_string:
                try:
                    pos = content_to_search.index(current_search_string, self.search_start_pos-100)
                    logger.info(f"Found content (Pass 2 final): '{current_search_string}'")
                    return pos, current_search_string
                except ValueError:
                    pass
    
            # Pass 3: Trim from end, search from beginning with adjustment
            current_search_string = original_content
            while len(current_search_string.split()) > 1:
                try:
                    pos = content_to_search.index(current_search_string, max(0, self.search_start_pos - 100))
                    chars_trimmed = len(original_content) - len(current_search_string)
                    adjusted_pos = pos
                    logger.info(f"Found content (Pass 3): '{current_search_string[:]}...', adjusted pos: {adjusted_pos}")
                    logger.info(f"\nContent:{content_to_search[adjusted_pos:adjusted_pos+200]}")
                    return adjusted_pos, original_content
                except ValueError:
                    words = current_search_string.split()
                    current_search_string = " ".join(words[:-1])
            # Try last word from Pass 3
            if current_search_string:
                try:
                    pos = content_to_search.index(current_search_string, max(0, self.search_start_pos - 100))
                    chars_trimmed = len(original_content) - len(current_search_string)
                    adjusted_pos = pos
                    logger.info(f"Found content (Pass 3 final): '{current_search_string}', adjusted pos: {adjusted_pos}")
                    return adjusted_pos, original_content
                except ValueError:
                    pass
            logger.warning("All three passes failed - content not found")
            return -1, None

    def _calculate_tagged_insertion_point(self, found_pos_no_tags: int, final_prev_content: str) -> Optional[int]:
        """
        Calculate the insertion point in the tagged XML string that corresponds to a
        position in the plain-text version.
        
        Args:
            found_pos_no_tags: The starting position of the preceding content in the plain-text string.
            final_prev_content: The plain-text content that comes just before the desired insertion point.
    
        Returns:
            The calculated character index in the tagged XML string, or None if not found.
        """
        # This is the target coordinate in the plain-text string.
        end_pos_no_tags = found_pos_no_tags + len(final_prev_content)
        
        cumulative_text_len = 0
        cumulative_tag_len = 0
        parts = re.split(r'(<[^>]+>)', self.xml_with_tags)
        
        for part in parts:
            if not part:
                continue
                
            if re.match(r'<[^>]+>', part):
                # This is a tag, so we only add its length to the tag counter.
                cumulative_tag_len += len(part)
            else:
                # This is a plain-text chunk.
                current_part_len = len(part)
                if cumulative_text_len <= end_pos_no_tags <= cumulative_text_len + current_part_len:
                    offset_in_part = end_pos_no_tags - cumulative_text_len
                    insertion_point = cumulative_tag_len + cumulative_text_len + offset_in_part
                    
                    logger.info(f"Calculated insertion point: {insertion_point}")
                    return insertion_point
                    
                cumulative_text_len += current_part_len
                
        # This edge case handles when the insertion point is at the very end of the document's text content.
        if end_pos_no_tags >= cumulative_text_len:
            insertion_point = cumulative_tag_len + cumulative_text_len
            logger.info(f"Calculated insertion point (at end): {insertion_point}")
            return insertion_point
            
        return None


    def _find_safe_insertion_point(self, start_point: int,is_new_para: bool) -> int:
        """Find safe insertion point avoiding tag boundaries."""
        point = start_point
        try:
            next_open = self.xml_with_tags.index('<', point)
            next_close = self.xml_with_tags.index('>', point)
            if next_open > next_close:
                point = next_close + 1
        except ValueError:
            pass
            
        # Skip closing tags
        # while True:
        #     match = re.match(r'\s*</[^>]+>', self.xml_with_tags[point:])
        #     if match:
        #         point += len(match.group(0))
        #     else:
        #         break
        while True:
            if is_new_para:
                # Original behavior: Skip *all* closing tags
                match = re.match(r'\s*(?:</[^>]+>|'  # Closing tags
                                 r'<section\.body[^>]*>\s*<para\b[^>]*>\s*<para\.text[^>]*>|'
                                 r'<author\.block[^>]*>\s*<author\.byline[^>]*>)+',
                                 self.xml_with_tags[point:])
            else:
                # New behavior: Skip tags *until* </para.text> is found.
                # This regex matches any closing tag *except* </para.text>.
                match = re.match(r'\s*</(?!(?:para\.text|name)\s*>)[^>]+>', self.xml_with_tags[point:])
            if match:
                # If we found a tag we are allowed to skip, advance the pointer
                point += len(match.group(0))
            else:
                # No match means we either hit a non-tag, an opening tag,
                # or (in the 'is_new_para == False' case) we hit </para.text>
                break
                
        if point != start_point:
            logger.info(f"Adjusted to safe insertion point: {point} , insertion at new paragraph = {is_new_para}")
            
        return point

    def _perform_insertion(self, instruction: Dict[str, Any], found_pos: int, 
                           final_prev_content: str, safe_insertion_point: int):
        """Perform insertion with footnote handling."""
        track_changes_text = instruction['Track changes']
        
        clean_text_to_insert = re.sub(r'<[^>]+>', '', track_changes_text)
        
        end_pos_no_tags = found_pos + len(final_prev_content)

        # Check for preceding footnote tags
        preceding_text = self.xml_with_tags[:safe_insertion_point]
        footnote_pattern = re.compile(r'((?:\s*</footnote.body>\s*|\s*</footnote>\s*)+)$')
        match = footnote_pattern.search(preceding_text)
        
        # Convert trace tags to trace.ins tags
        start_tag = '<trace>'
        end_tag = '</trace>'
        if track_changes_text.startswith(start_tag) and track_changes_text.endswith(end_tag):
            content = track_changes_text[len(start_tag):-len(end_tag)]
            track_changes_text = f'<trace.ins>{content}</trace.ins>'

        if match:
            tags_to_move_after = match.group(1)
            target_point = safe_insertion_point #- len(tags_to_move_after)
            logger.info(f"Footnote detected - shifting insertion from {safe_insertion_point} to {target_point}")
            
        else:
            target_point = safe_insertion_point

        # --- NEW CHECK: Ensure we are not inside an existing <trace.ins> block ---
        # Look backwards to see if we are inside an open trace tag
        last_trace_open = self.xml_with_tags.rfind('<trace.ins>', 0, target_point)
        last_trace_close = self.xml_with_tags.rfind('</trace.ins>', 0, target_point)

        if last_trace_open > last_trace_close:
            logger.info(f"Insertion point {target_point} is inside an existing <trace.ins> block.")
            logger.info("\n\n\n\n\n\nNEED MANUAL REVIEW\n\n\n\n\n\n")
            
            # Find the closing tag for this block
            next_trace_close = self.xml_with_tags.find('</trace.ins>', target_point)
            
            if next_trace_close != -1:
                # Shift target point to immediately after the closing tag
                target_point = next_trace_close + len('</trace.ins>')
                logger.info(f"Shifted insertion point to {target_point} (after </trace.ins>)")
        # ------------------------------------------------------------------------

        # Perform the insertion at the calculated target_point
        self.xml_with_tags = (self.xml_with_tags[:target_point] + 
                              track_changes_text + 
                              self.xml_with_tags[target_point:])
        
        # Update tag-less version and search position
        self.xml_without_tags = (self.xml_without_tags[:end_pos_no_tags] + 
                                 clean_text_to_insert + 
                                 self.xml_without_tags[end_pos_no_tags:])
        
        self.search_start_pos = end_pos_no_tags + len(clean_text_to_insert)
        logger.info(f"Next search starts at position: {self.search_start_pos}")


class XMLDeleter:
    """Handles XML content deletions."""
    
    def __init__(self, initial_xml_with_tags: str, deletion_instructions: List[Dict[str, Any]],
                 all_instructions: List[Dict[str, Any]] = None):
        """
        Initialize XML deleter.
        
        Args:
            initial_xml_with_tags: XML content with tags
            deletion_instructions: List of deletion instructions
            all_instructions: Full mapper instructions (insertions + deletions) for predecessor lookup
        """
        self.xml_with_tags = initial_xml_with_tags
        self.instructions = sorted(deletion_instructions, key=lambda x: int(x['Track Change Segment']))
        self.all_instructions = all_instructions or []
        self.search_start_pos = 0

    def run_deletions(self) -> str:
        """
        Run all deletion operations.
        
        Returns:
            Updated XML content
        """
        logger.info(f"Processing {len(self.instructions)} deletion instructions")
        
        progress_bar = tqdm(total=len(self.instructions), desc="Processing deletions", unit="deletion")
        
        for instruction in self.instructions:
            segment_num = int(instruction['Track Change Segment'])
            logger.info(f"Processing deletion segment: {segment_num}")
            # LOGIC CHANGE: Look at the previous segment (segment_num - 1) 

            # to find where the search should actually start.

            if segment_num > 1:

                # We look for the position stored by the immediate predecessor

                prev_instr = next((opt for opt in self.all_instructions if int(opt['Track Change Segment']) == segment_num - 1), None)

                if prev_instr and 'final_end_pos_no_tags' in prev_instr:
                    self.search_start_pos = prev_instr['final_end_pos_no_tags']
                    logger.info(f"Previous instruction was insertion. Backtracking search start to {self.search_start_pos}")
    
            
            # Regenerate tag-less version for current state
            xml_without_tags = re.sub(r'<[^>]+>', '', self.xml_with_tags)

            clean_prev_content = re.sub(r'<[^>]+>', '', instruction['Previous Content']).strip()
            clean_deleted_content = re.sub(r'<[^>]+>', '', instruction['Deleted Content']).strip()
            
            found_pos, found_deleted_text = self._find_deletion_target(
                xml_without_tags, clean_prev_content, clean_deleted_content)
            
            if found_pos == -1:
                logger.error(f"Deletion target not found for Segment {segment_num}")
                # Even if failed, we approximate the start pos for the next one

                instruction['final_end_pos_no_tags'] = self.search_start_pos
                progress_bar.update(1)
                continue

            start_tagged, end_tagged = self._calculate_tagged_points(found_pos, len(found_deleted_text))
            if start_tagged is not None and end_tagged is not None:

                self._perform_replacement(start_tagged, end_tagged)

                # RECORD THE END POSITION:

                # search_start_pos is updated inside _find_deletion_target

                instruction['final_end_pos_no_tags'] = self.search_start_pos
            progress_bar.update(1)

        progress_bar.close()
        logger.info("All deletion segments processed")
        return self.xml_with_tags

    def _find_deletion_target(self, xml_without_tags: str, prev_content: str, 
                              deleted_content: str) -> Tuple[int, Optional[str]]:
        """Find deletion target using anchor-based search."""
        # Find anchor (prev_content) using multi-pass strategy
        logger.info(f"\nDELETED:{deleted_content}\n")
        original_anchor = prev_content.strip()
        anchor_pos = -1
        found_anchor_text = ""
        content_to_search = xml_without_tags.replace("__sep__","&mdash;")
        if len(deleted_content.split()) > 3:
            logger.info(f"---Attempting Direct Deletion, searching from position {self.search_start_pos} ---")
            del_pos = content_to_search.find(deleted_content, self.search_start_pos,self.search_start_pos+len(deleted_content)+100)
            if del_pos != -1:
                print(deleted_content)
                print(xml_without_tags[del_pos:len(deleted_content)])
                return del_pos, deleted_content
        logger.info(f"---Direct Deletion, not possible")
        # --- Anchor Pass 0: Fuzzy Regex Match ---
        
        logger.info("Attempting Anchor Pass 0 (Fuzzy Regex Match)...")
        try:
            words = re.sub(r'\W+', ' ', original_anchor).split()
            if not words:
                raise ValueError("No words for fuzzy anchor search")
            
            regex_pattern = r'\W*'.join(re.escape(word) for word in words)
            # Search from self.search_start_pos
            match = re.search(regex_pattern, content_to_search[self.search_start_pos:])
            logger.info(f"\n{match}\n")
            
            if match:
                anchor_pos = match.start()+self.search_start_pos
                found_anchor_text = match.group(0) # <-- The actual found text
                logger.info(f"Found anchor (Pass 0 Fuzzy): '{found_anchor_text[:]}...' at pos {anchor_pos}")
                logger.info(f"Found anchor (Pass 0 Fuzzy): '{content_to_search[anchor_pos:anchor_pos+len(found_anchor_text)]}...' at pos {anchor_pos}")
            else:
                match = re.search(regex_pattern, content_to_search)
                if match:
                    anchor_pos = match.start() 
                    found_anchor_text = match.group(0) # <-- The actual found text
                    logger.info(f"Found anchor (Pass 0 Fuzzy) with full file: '{found_anchor_text[:]}...' at pos {anchor_pos}")
                    logger.info(f"Found anchor (Pass 0 Fuzzy) with full file: '{content_to_search[anchor_pos:anchor_pos+len(found_anchor_text)]}...' at pos {anchor_pos}")
                    
            
        except Exception as e:
            anchor_pos = -1
            logger.warning(f"Anchor Pass 0 Fuzzy search failed: {e}")                    
        # --- End of Anchor Pass 0 ---
        
        # Anchor Pass 1: Trim from start, search from current position
        if anchor_pos == -1:
            
            logger.info("Attempting Anchor Pass 1(Trim from start)...")
            logger.info(f"content:{content_to_search[self.search_start_pos:self.search_start_pos+1000]}")
            current_search_anchor = original_anchor
            while len(current_search_anchor.split()) > 2:
                try:
                    pos = content_to_search.index(current_search_anchor, self.search_start_pos)
                    logger.info(f"Found anchor (Pass 1): '{current_search_anchor[:60]}...'")
                    anchor_pos = pos
                    found_anchor_text = current_search_anchor
                    break
                except ValueError:
                    words = current_search_anchor.split()
                    current_search_anchor = " ".join(words[1:])
        
        # if anchor_pos == -1 and current_search_anchor:
        #     try:
        #         pos = content_to_search.index(current_search_anchor, self.search_start_pos)
        #         logger.info(f"Found anchor (Pass 1 final): '{current_search_anchor}'")
        #         anchor_pos = pos
        #         found_anchor_text = current_search_anchor
        #     except ValueError:
        #         pass

        # Anchor Pass 2: From beginning if Pass 1 failed
        if anchor_pos == -1:
            logger.info("Attempting Anchor Pass 2(Trim from start,position 0)...")
            current_search_anchor = original_anchor
            while len(current_search_anchor.split()) > 2:
                try:
                    pos = content_to_search.index(current_search_anchor, 0)
                    logger.info(f"Found anchor (Pass 2): '{current_search_anchor[:60]}...'")
                    anchor_pos = pos
                    found_anchor_text = current_search_anchor
                    break
                except ValueError:
                    words = current_search_anchor.split()
                    current_search_anchor = " ".join(words[1:])
            
            # if anchor_pos == -1 and current_search_anchor:
            #     try:
            #         pos = content_to_search.index(current_search_anchor, 0)
            #         logger.info(f"Found anchor (Pass 2 final): '{current_search_anchor}'")
            #         anchor_pos = pos
            #         found_anchor_text = current_search_anchor
            #     except ValueError:
            #         pass

        # Anchor Pass 3: Trim from end if Pass 2 failed
        if anchor_pos == -1:
            logger.info("Attempting Anchor Pass 3(Trim from end)...")
            current_search_anchor = original_anchor
            while len(current_search_anchor.split()) > 2:
                try:
                    pos = content_to_search.index(current_search_anchor, 0)
                    logger.info(f"Found anchor (Pass 3): '{content_to_search[pos:pos+200]}...'")
                    anchor_pos = pos
                    found_anchor_text = current_search_anchor
                    break
                except ValueError:
                    words = current_search_anchor.split()
                    current_search_anchor = " ".join(words[:-1])
            
            if anchor_pos == -1 and current_search_anchor:
                try:
                    pos = content_to_search.index(current_search_anchor, 0)
                    logger.info(f"Found anchor (Pass 3 final): '{current_search_anchor}'")
                    anchor_pos = pos
                    found_anchor_text = current_search_anchor
                except ValueError:
                    pass

        if anchor_pos == -1:
            logger.warning(f"Could not find anchor content: '{original_anchor[:60]}...'")
            return -1, None

        anchor_end_pos = anchor_pos + len(found_anchor_text)

        # Find deleted content after anchor
        original_deleted = deleted_content.strip()
        deleted_pos = -1
        found_deleted_text = ""

        # --- Deletion Pass 0: Fuzzy Regex Match ---

        logger.info("Attempting Deletion Pass 0 (Fuzzy Regex Match)...")

        try:
            # all_words = original_deleted.split()
            
            
            # if not all_words:
            #     words = []
            # elif len(all_words) == 1:
            #     # Case 1: Only one word. Use it as-is.
            #     words = all_words
            # elif len(all_words) == 2:
            #     # Case 2: Only two words. Use them both as-is.
            #     # This correctly handles your original "&sect; 3:2" example.
            #     words = all_words
            # else:
            #     # Case 3: More than 2 words.
            #     # Preserve the first and last, sanitize the middle.
            #     first_word = all_words[0]
            #     last_word = all_words[-1]
            #     middle_words_list = all_words[1:-1]
                
            #     # Sanitize the middle part, just like the old logic did
            #     # for the "head"
            #     middle_string = ' '.join(middle_words_list)
            #     middle_string_no_entities = re.sub(r'&\w+;', ' ', middle_string)
            #     words_from_middle = re.sub(r'\W+', ' ',\
            #                              middle_string_no_entities).split()
                
            #     # Combine them: [exact_first] + [sanitized_middle] + [exact_last]
            #     words = [first_word] + words_from_middle + [last_word]
                
            # if not words:
            #     raise ValueError("No words to search for in fuzzy pass")
        
            # entity_aware_separator = r'(?:\W|&\w+;)*'
            all_words = original_deleted.split()
            
            if not all_words:
                words = []
            else:
                # 2. Use this *exact* list. No more special first/last/middle.
                # The 'words' list will be:
                # [');', 'Irwin', 'v.', 'Mascott,', '186', 'F.R.D.', ...]
                words = all_words
                
            if not words:
                raise ValueError("No words to search for in fuzzy pass")
    
            # 3. Change the separator from \W (any non-word) to \s (any whitespace).
            # Since our words list now contains 'v.' and 'Mascott,',
            # the separator *only* needs to match the spaces between them.
            entity_aware_separator = r'(?:\s|&\w+;)*'
            regex_pattern = entity_aware_separator.join(re.escape(word) for word in words)
            
            # Search in a window *after* the anchor
            search_window_start = max(0, anchor_end_pos) # Allow small overlap/gap
            match = re.search(regex_pattern, content_to_search[search_window_start:])
            logger.info(f"\nORIGINAL:{content_to_search[search_window_start-50:search_window_start+50]}")
            
            if match:
                match_start_relative = match.start()
                match_start_absolute = match_start_relative + search_window_start
                
                # Check if the match is "close" to the anchor
                if (match_start_absolute - anchor_end_pos) < 1000000: # Max 1000 chars gap
                    deleted_pos = match_start_absolute
                    found_deleted_text = match.group(0) # <-- The actual found text
                    logger.info(f"Found deletion target (Pass 0 Fuzzy): '{found_deleted_text[:]}...'")
                else:
                    logger.info(f"Deletion Pass 0 Fuzzy found a match, but it was too far from anchor (Gap: {match_start_absolute - anchor_end_pos}).")
            else:
                   logger.info("Deletion Pass 0 Fuzzy search did not find a match.")
        except Exception as e:
            logger.warning(f"Deletion Pass 0 Fuzzy search failed: {e}")

        # --- End of Deletion Pass 0 ---
        
        # Deletion Pass 1: Exact match
        if deleted_pos == -1:
            try:
                pos = content_to_search.index(original_deleted, anchor_end_pos)
                logger.info(f"Found deletion target (Pass 1): '{original_deleted[:60]}...'")
                deleted_pos = pos
                found_deleted_text = original_deleted
            except ValueError:
                logger.info("Exact match not found, trying trimming passes")
        
        # Deletion Pass 2: Trim from end
        if deleted_pos == -1:
            current_search_deleted = original_deleted
            while len(current_search_deleted.split()) > 1:
                try:
                    pos = content_to_search.index(current_search_deleted, anchor_end_pos - 500)
                    logger.info(f"Found deletion target (Pass 2): '{current_search_deleted[:60]}...'")
                    logger.info(f"Found deletion target (Pass 2)_original: '{content_to_search[pos:pos+len(original_deleted)]}...'")
                    deleted_pos = pos
                    found_deleted_text = original_deleted
                    break 
                except ValueError:
                    words = current_search_deleted.split()
                    current_search_deleted = " ".join(words[:-1])
            
            if deleted_pos == -1 and current_search_deleted:
                try:
                    pos = content_to_search.index(current_search_deleted, anchor_end_pos - 500)
                    logger.info(f"Found deletion target (Pass 2 final): '{current_search_deleted}'")
                    deleted_pos = pos
                    found_deleted_text = original_deleted
                except ValueError:
                    pass

        if deleted_pos != -1:
            if deleted_pos - anchor_end_pos > 100:
                logger.warning(f"Deleted content is far from anchor: '{found_deleted_text[:30]}...'")
            
            self.search_start_pos = deleted_pos
            return deleted_pos, found_deleted_text
        else:
            logger.warning(f"Could not find deletion target: '{original_deleted[:60]}...'")
            return -1, None

    def _calculate_tagged_points(self, start_pos_no_tags: int, length_no_tags: int) -> Tuple[Optional[int], Optional[int]]:
        """Calculate start and end points in tagged XML."""
        end_pos_no_tags = start_pos_no_tags + length_no_tags
        start_point_tagged, end_point_tagged = None, None
        cumulative_text_len = 0
        cumulative_tag_len = 0
        parts = re.split(r'(<[^>]+>)', self.xml_with_tags)

        for part in parts:
            if not part:
                continue
                
            if re.match(r'<[^>]+>', part):
                cumulative_tag_len += len(part)
            else:
                current_part_len = len(part)
                
                if start_point_tagged is None and cumulative_text_len <= start_pos_no_tags < cumulative_text_len + current_part_len:
                    offset = start_pos_no_tags - cumulative_text_len
                    start_point_tagged = cumulative_text_len + cumulative_tag_len + offset
                    
                if end_point_tagged is None and cumulative_text_len < end_pos_no_tags <= cumulative_text_len + current_part_len:
                    offset = end_pos_no_tags - cumulative_text_len
                    end_point_tagged = cumulative_text_len + cumulative_tag_len + offset
                    logger.info(f"Calculated tagged points: Start={start_point_tagged}, End={end_point_tagged}")
                    return start_point_tagged, end_point_tagged
                    
                cumulative_text_len += current_part_len
        
        if start_point_tagged is not None and end_point_tagged is None and end_pos_no_tags >= cumulative_text_len:
            end_point_tagged = cumulative_text_len + cumulative_tag_len
            return start_point_tagged, end_point_tagged

        return start_point_tagged, end_point_tagged

    def _perform_replacement(self, start_point: int, end_point: int):
        """
        Replace content with deletion marker, preserving internal XML tags.
        Places the <trace.deleted/> marker at the location of the 
        largest text chunk being deleted.
        """
        
        slice_to_process = self.xml_with_tags[start_point:end_point]
        logger.info(f"Processing deletion for slice: {slice_to_process[:100]}...")

        # --- UPDATED CHECK: Ensure we are not inside an existing <trace> OR <trace.ins> block ---
        # Look backwards to see if we are inside an open trace tag
        last_trace_open = self.xml_with_tags.rfind('<trace>', 0, start_point)
        last_trace_close = self.xml_with_tags.rfind('</trace>', 0, start_point)
        
        last_trace_ins_open = self.xml_with_tags.rfind('<trace.ins>', 0, start_point)
        last_trace_ins_close = self.xml_with_tags.rfind('</trace.ins>', 0, start_point)

        if (last_trace_open > last_trace_close) or (last_trace_ins_open > last_trace_ins_close):
            logger.info("Deletion point is inside an existing <trace> or <trace.ins> block. Abandoning Deletion.")
            return
        # ---------------------------------------------------------------------

        # Keep the original check to avoid deleting insertion markers
        # Also prevent deleting across or inside standard <trace> tags
        if any(tag in slice_to_process for tag in ["<trace.ins>", "</trace.ins>", "<trace>", "</trace>"]):
            logger.info("Slice contains <trace.ins> or <trace> tags. Abandoning Deletion.")
            return

        # Split the slice by its tags, keeping the tags as delimiters
        parts = re.split(r'(<[^>]+>)', slice_to_process)
        
        # --- Pass 1: Find the largest text chunk ---
        max_len = -1
        target_index = -1
        
        for i, part in enumerate(parts):
            # Check if the part is text (not a tag and not just whitespace)
            is_tag = re.match(r'<[^>]+>', part)
            if not is_tag and part.strip():
                if len(part) > max_len:
                    max_len = len(part)
                    target_index = i
        
        # --- Pass 2: Rebuild the slice ---
        new_parts = []
        for i, part in enumerate(parts):
            if not part:
                continue
            
            is_tag = re.match(r'<[^>]+>', part)
            
            if is_tag:
                # Keep all tags
                new_parts.append(part)
            elif i == target_index:
                # This is the largest text chunk, replace it
                new_parts.append('<trace.deleted/>')
            else:
                # This is other text or whitespace, remove it
                new_parts.append('')
                
        # --- Edge Case Handling ---
        # If no text was found (target_index == -1), 
        # e.g., the slice was just "<ital></ital>" or whitespace.
        if target_index == -1:
            if new_parts:
                # Slice contains tags but no text. Insert marker after the first tag.
                new_parts.insert(1, '<trace.deleted/>')
            else:
                # Slice was empty or just whitespace
                new_parts.append('<trace.deleted/>')

        # Reconstruct the slice
        replaced_slice = "".join(new_parts)
        
        # Clean up any empty tag pairs created by the replacement
        # e.g., <para></para> becomes ""
        # This regex will not match <trace.deleted/>
        replaced_slice = re.sub(
            r'<([a-zA-Z0-9\.]+)((?:\s+[^>]+)?)>\s*</\1>', 
            '', 
            replaced_slice, 
            flags=re.IGNORECASE
        )

        logger.info(f"Replaced slice with: {replaced_slice}")

        # Perform the final replacement in the main XML string
        self.xml_with_tags = (self.xml_with_tags[:start_point] +
                              replaced_slice +
                              self.xml_with_tags[end_point:])
        
        logger.info(f"Replaced content in tagged XML, preserving tags.")

class GPTProcessor:
    """Handles GPT-4 processing for XML tagging."""
    
    def __init__(self, docx_file_path: str, output_folder: Path, context_window_size: int = 10, 
                 model_name: str = "gpt-5", temperature: float = 0.2,
                 anthropic_api_key: str = ""):
        """
        Initialize GPT processor.
        
        Args:
            docx_file_path: Path to DOCX file
            output_folder: Output folder for images
            context_window_size: Context window size for search
            model_name: Primary model name
            temperature: LLM temperature
            anthropic_api_key: Anthropic API key for fallback chain (sonnet model)
        """
        self.docx_file_path = docx_file_path
        self.output_folder = Path(output_folder) if not isinstance(output_folder, Path) else output_folder
        self.context_window_size = context_window_size
        self.doc_images = []
        self.doc_text_by_page = {}
        self.system_prompt = ""

        self.model_name = model_name

        self.temperature = temperature

        self._fallback_handler = LLMFallbackHandler.build_main_chain(
            anthropic_api_key=anthropic_api_key,
            model_name=model_name,
            temperature=temperature,
        )
        logger.info("[GPTProcessor] Fallback chain: %s",
                    " -> ".join(c.model_name for c in self._fallback_handler.fallback_chain))

        if not os.path.exists(self.output_folder):
            os.makedirs(self.output_folder)

    def get_llm_credentials(self, model_name: str, workspace_id: str, open_ai_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Get LLM credentials from API."""
        try:
            payload = {
                "workspace_id": workspace_id,
                "model_name": model_name,
                "oai_access": "apim"
            }
            resp = requests.post(open_ai_url, headers=None, json=payload)
            credentials = json.loads(resp.content)
            logger.info("LLM credentials retrieved successfully")
            
            if "openai_key" in credentials:
                return credentials["openai_endpoint"], credentials["openai_key"]
            else:
                logger.error("OpenAI key not found in credentials")
                return None, None
                
        except Exception as e:
            logger.error(f"Error getting LLM credentials: {e}")
            return None, None

    def _encode_image_to_base64(self, image_path: str) -> Optional[str]:
        """Encode image file to base64 string."""
        try:
            with open(image_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"Error encoding image {image_path}: {e}")
            return None
    
    def _find_poppler_in_directory(self, search_path: Path) -> Optional[str]:

        """

        Helper to recursively search for the Poppler 'bin' directory 

        containing pdftoppm.exe within a specific path.

        """

        logger.info(f"Looking for Poppler binaries in: {search_path}")

        try:

            # Recursively look for the main executable required by pdf2image

            for file_path in search_path.rglob("pdftoppm.exe"):

                # Found the executable, return its parent directory (the 'bin' folder)

                bin_path = str(file_path.parent)

                logger.info(f"Poppler found at: {bin_path}")
                return bin_path

        except Exception as e:

            logger.warning(f"Error searching for Poppler in {search_path}: {e}")

        

        return None

    def convert_docx_to_images(self):
        """
        Convert DOCX file to images with track changes accepted and comments removed
        """
        if not WIN32COM_AVAILABLE:
            logger.error("win32com.client is not available. Cannot convert DOCX to images.")
            raise RuntimeError("win32com.client not found. Please install it and ensure MS Word is on your system.")

        word = None
        temp_pdf_path = None
        
        import pythoncom
        pythoncom.CoInitialize()
        
        try:
            # Kill any existing Word processes first
            try:
                subprocess.call("taskkill /f /im WINWORD.EXE", shell=True)
                logger.info("Killed existing Word processes")
            except:
                pass

            time.sleep(2)

            logger.info("Starting MS Word application to convert DOCX.")
            # Use dynamic dispatch to avoid gen_py CLSIDToClassMap errors
            try:
                word = win32com.client.dynamic.Dispatch("Word.Application")
            except Exception:
                # Fallback: clear gen_py cache and retry with plain Dispatch
                try:
                    import shutil as _shutil_retry
                    gen_py_path = getattr(win32com, '__gen_path__', None)
                    if gen_py_path and os.path.isdir(gen_py_path):
                        _shutil_retry.rmtree(gen_py_path, ignore_errors=True)
                        logger.info("Cleared gen_py cache at %s, retrying Dispatch", gen_py_path)
                except Exception:
                    pass
                word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False

            abs_docx_path = os.path.abspath(self.docx_file_path)

            # Create a copy of the document to work with
            temp_docx = os.path.join(tempfile.gettempdir(), f"temp_{os.path.basename(self.docx_file_path)}")
            shutil.copy2(abs_docx_path, temp_docx)
            logger.info(f"Created temporary copy of document: {temp_docx}")

            try:
                # Open document
                doc = word.Documents.Open(temp_docx, ReadOnly=False, Visible=False, 
                                          ConfirmConversions=False, AddToRecentFiles=False)
                logger.info("Document opened successfully")

                # Make document active
                try:
                    doc.Activate()
                    logger.info("Document activated")
                except Exception as e:
                    logger.warning(f"Could not activate document: {e}")

                # Set view to hide markup BEFORE accepting changes
                try:
                    # Set the view type to Print Layout
                    doc.ActiveWindow.View.Type = 3  # wdPrintView

                    # Hide all markup - these are the key settings
                    doc.ActiveWindow.View.ShowRevisionsAndComments = False
                    doc.ActiveWindow.View.RevisionsView = 0  # wdRevisionsViewFinal

                    # Additional view settings to hide markup
                    doc.ActiveWindow.View.ShowComments = False
                    doc.ActiveWindow.View.ShowInsertionsAndDeletions = False
                    doc.ActiveWindow.View.ShowFormatChanges = False

                    logger.info("Track changes display turned off")
                except Exception as e:
                    logger.warning(f"Could not change some revision view settings: {e}")

                # Turn off track changes mode
                try:
                    doc.TrackRevisions = False
                    logger.info("Track changes mode turned off")
                except Exception as e:
                    logger.warning(f"Could not turn off track changes: {e}")

                # Accept all revisions
                try:
                    revision_count = doc.Revisions.Count
                    if revision_count > 0:
                        doc.Revisions.AcceptAll()
                        logger.info(f"Accepted {revision_count} revisions")
                    else:
                        logger.info("No revisions to accept")
                except Exception as e:
                    logger.warning(f"Could not accept revisions: {e}. Continuing...")

                # Delete all comments
                try:
                    comment_count = doc.Comments.Count
                    while doc.Comments.Count > 0:
                        doc.Comments.Item(1).Delete()
                    logger.info(f"Deleted {comment_count} comments")
                except Exception as e:
                    logger.warning(f"Could not delete comments: {e}")

                # Update fields
                try:
                    doc.Fields.Update()
                    logger.info("Document fields updated")
                except:
                    pass

                # Create PDF path — save in the output folder so validation can find it later
                temp_pdf_path = os.path.join(str(self.output_folder), "docx_pdf_clean.pdf")

                # Export as PDF with specific settings to exclude markup
                try:
                    logger.info(f"Exporting clean DOCX to PDF: {temp_pdf_path}")
                    # Try ExportAsFixedFormat with all available parameters
                    doc.ExportAsFixedFormat(
                        OutputFileName=temp_pdf_path,
                        ExportFormat=17,  # wdExportFormatPDF
                        OpenAfterExport=False,
                        OptimizeFor=0,  # wdExportOptimizeForStandard
                        BitmapMissingFonts=True,
                        DocStructureTags=False,
                        UseISO19005_1=False
                    )
                    logger.info("PDF created successfully without comments and track changes")
                except Exception as e:
                    logger.warning(f"ExportAsFixedFormat failed with some parameters: {e}")
                    # Try simpler SaveAs2 method
                    try:
                        doc.SaveAs2(FileName=temp_pdf_path, FileFormat=17)
                        logger.info("PDF created using SaveAs2 method")
                    except Exception as e2:
                        logger.error(f"SaveAs2 also failed: {e2}")
                        raise

                # Store the PDF path on the instance so the pipeline can retrieve it
                self.clean_pdf_path = temp_pdf_path

                doc.Close(SaveChanges=False)

                # Process the PDF if it exists
                if os.path.exists(temp_pdf_path):
                    try:
                        # <--- [MODIFIED] POPPLER DETECTION LOGIC START ---

                        poppler_path = None

                        # 0. Check in project directory's poppler folder
                        if getattr(sys, 'frozen', False):
                            _script_dir = Path(os.path.dirname(sys.executable))
                        else:
                            _script_dir = Path(os.path.dirname(os.path.abspath(__file__)))
                        _project_poppler = _script_dir / "poppler" / "Library" / "bin"
                        logger.info(f"Checking for poppler at: {_project_poppler}")
                        if _project_poppler.exists():
                            poppler_path = str(_project_poppler)
                            logger.info(f"Found poppler in project dir: {poppler_path}")

                        # 1. Identify the directory of the current DOCX file

                        current_file_dir = Path(self.docx_file_path).parent

                        

                        # 2. Identify the main Input Directory (Parent of the task folder)

                        # Assumes structure: Input_Dir/Task_Name/file.docx

                        main_input_dir = current_file_dir.parent



                        # 3. Search for Poppler in the Main Input Directory first (most likely location for a shared folder)

                        if not poppler_path and main_input_dir.exists():

                            poppler_path = self._find_poppler_in_directory(main_input_dir)

                        

                        # 4. If not found, search inside the specific Task directory

                        if not poppler_path and current_file_dir.exists():

                            poppler_path = self._find_poppler_in_directory(current_file_dir)



                        if not poppler_path:

                            logger.warning("Poppler not found in Input directory. Falling back to system PATH or checking bundled.")

                            # Fallback to bundled check if frozen (for EXE support)

                            if getattr(sys, 'frozen', False):

                                base_path = sys._MEIPASS

                                temp_poppler = os.path.join(base_path, 'poppler_bin')

                                if os.path.exists(temp_poppler):

                                    poppler_path = temp_poppler



                        if poppler_path:

                            logger.info(f"Using Poppler path: {poppler_path}")

                        else:

                            logger.warning("Poppler binary path could not be determined. PDF conversion requires 'poppler' folder in Input directory.")

                        # <--- [MODIFIED] POPPLER DETECTION LOGIC END ---
                        logger.info(f"Converting clean PDF to images using pdf2image with high quality")

                        # Use enhanced settings for better quality
                        conversion_params = {
                            "dpi": 200,  # High resolution
                            "fmt": "png",
                            "thread_count": 4,
                            "grayscale": False,
                            "transparent": False,
                            "strict": False
                        }

                        if poppler_path:
                            images = convert_from_path(temp_pdf_path, poppler_path=poppler_path, **conversion_params)
                        else:
                            images = convert_from_path(temp_pdf_path, **conversion_params)

        

                        # Process images with pdfplumber for text extraction
                        with pdfplumber.open(temp_pdf_path) as pdf:
                            base_filename = os.path.splitext(os.path.basename(self.docx_file_path))[0]
                            # Sanitize filename to avoid issues with parentheses/special chars
                            # safe_base_filename = re.sub(r'[()&\[\]{}]', '_', base_filename)
                            safe_base_filename = re.sub(r'[()&\[\]{}\.]', '_', base_filename)
                            safe_base_filename = re.sub(r'_+', '_', safe_base_filename).strip('_')

                            # Use a short fixed folder name to avoid Windows MAX_PATH (260 chars) issues.
                            # The base path alone (output_folder) can already be 150+ chars, so the
                            # subfolder + filename must stay well under ~100 chars combined.
                            safe_base_filename = "doc_images"

                            task_image_dir = self.output_folder / safe_base_filename

                            task_image_dir.mkdir(exist_ok=True)

                            for page_num, (page, image) in enumerate(tqdm(zip(pdf.pages, images), total=len(images),

                                                                          desc="Converting PDF pages to images",

                                                                          unit="page",disable=True)):
                                # Extract text
                                page_text = page.extract_text()
                                self.doc_text_by_page[page_num] = page_text

                                # Save clean image
                                image_filename = f"{safe_base_filename}_page_{page_num + 1}.png"
                                os.makedirs(f"{self.output_folder}/{safe_base_filename}", exist_ok=True)
                                image_path = os.path.join(self.output_folder,safe_base_filename, image_filename)

                                # Save with high quality
                                image.save(
                                    image_path,
                                    format="PNG",
                                    optimize=False,
                                    compress_level=6
                                )

                                self.doc_images.append({
                                    "page_num": page_num,
                                    "filename": image_filename,
                                    "path": str(image_path),
                                    "text": page_text
                                })

                                logger.info(f"Created clean image for page {page_num + 1}: {image_filename}")

                            logger.info(f"Successfully converted {len(self.doc_images)} pages to clean images")

                    except Exception as e:
                        logger.error(f"Error processing PDF: {e}")
                        raise
                else:
                    logger.error("PDF was not created successfully")
                    raise Exception("PDF conversion failed")

            except Exception as e:
                logger.error(f"Error processing document: {e}")
                raise

        except Exception as e:
            logger.error(f"Error converting DOCX to images: {e}")
            raise
        finally:
            # Clean up
            if word:
                try:
                    word.Quit()
                except:
                    pass
            pythoncom.CoUninitialize()

            # Clean up temp files
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                try:
                    pass
                    # os.remove(temp_pdf_path)
                except:
                    pass

            if "temp_docx" in locals() and os.path.exists(temp_docx):
                try:
                    os.remove(temp_docx)
                except:
                    pass
    
    def load_images_from_folder(self, images_folder_path: str, pdf_path: str):
        """
        Load images from a folder (instead of zip) and pair with PDF page text.
        Walks the folder for image files, pairs them with PDF pages for text extraction.
        """
        logger.info(f"Loading images from folder: {images_folder_path}")
        logger.info(f"Using PDF for page text: {pdf_path}")

        valid_exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
        image_paths = []
        for root, _, files in os.walk(images_folder_path):
            for f in files:
                if f.lower().endswith(valid_exts):
                    image_paths.append(os.path.join(root, f))
        image_paths.sort()

        if not image_paths:
            raise RuntimeError(f"No image files found under {images_folder_path}")

        self.doc_images = []
        self.doc_text_by_page = {}

        with pdfplumber.open(pdf_path) as pdf:
            num_pages = len(pdf.pages)
            num_images = len(image_paths)
            min_len = min(num_pages, num_images)

            if num_pages != num_images:
                logger.warning(
                    f"PDF pages ({num_pages}) and images ({num_images}) differ; "
                    f"only pairing the first {min_len} pages"
                )

            for page_num in range(min_len):
                page = pdf.pages[page_num]
                image_path = image_paths[page_num]
                page_text = page.extract_text() or ""
                self.doc_text_by_page[page_num] = page_text

                self.doc_images.append({
                    "page_num": page_num,
                    "filename": os.path.basename(image_path),
                    "path": image_path,
                    "text": page_text,
                })

            logger.info(f"Successfully loaded {len(self.doc_images)} images and page texts from folder and PDF")

    def call_llm(self, client, system_prompt: str, track_change_text: str,
                 previous_content: str, pages: List[int],
                 pre_context: str, post_context: str) -> Tuple[Optional[str], Dict[str, int]]:
        """Call LLM with track change context and images using fallback chain."""
        empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            if len(track_change_text) < 50 and not track_change_text.startswith("<trace>_"):
                return track_change_text, empty_usage
            MAX_IMAGES = 40  # Safely below the hard limit of 50
            if len(pages) > MAX_IMAGES:
                logger.warning(f"Track change spans {len(pages)} pages. Trimming to first and last {MAX_IMAGES//2} pages to respect API limits.")
                half_limit = MAX_IMAGES // 2
                # Grab the start and end of the span, remove any accidental overlaps, and sort
                trimmed_pages = pages[:half_limit] + pages[-half_limit:]
                pages = sorted(list(set(trimmed_pages)))

            page_numbers_display = [p + 1 for p in pages]
            print(f"{track_change_text}\n")
            user_prompt_text = f"""
            Please analyze the following track change and add appropriate XML tags based on the document structure visible in the provided images from page(s) {page_numbers_display} and the surrounding text context.

            Here is the surrounding context from the document:
            --- START OF CONTEXT BEFORE THE CHANGE(pre_context) ---
            {pre_context}
            --- END OF CONTEXT BEFORE THE CHANGE ---

            <<<THE CHANGE TO BE TAGGED IS HERE>>>

            --- START OF CONTEXT AFTER THE CHANGE(post_context) ---
            {post_context}
            --- END OF CONTEXT AFTER THE CHANGE ---

            Track Change Text to be tagged: {track_change_text}
            - Closely follow the surrounding context to check where the Track Change Text is getting added, for example:- if you find that the track change text is a part of a table from the surrounding text, add it as:-
            <row><entry>Content</entry></row>
            - If you find the pre_context endswith a closing tag(</tag_name>) and post_context beginning with a opening tag(<tag_name>), the Track Change Text to be tagged is most probably a new addition and have date.trace = "1" attribute after tag_name.
            - Child tags like footnote.body,section.body,ref,para.text cannot have date.trace = "1" attribute.
            - Inline inserts marked with <trace> tags can also have citations and other xml codings in them.EX:-<trace><cite type="periodical" normalpub="lr" reftext="&ldquo;Installment loans have increasingly replaced traditional payday loans in the short-term, small-dollar credit market.&rdquo; Carlie Malone, Paige Marta Skiba, Installment Loans">&ldquo;Installment loans have increasingly replaced traditional payday loans in the short-term, small-dollar credit market.&rdquo; Carlie Malone, Paige Marta Skiba, Installment Loans, 53 J. Legal Stud. 369 (2024)</cite>. See also</trace>
            
            Please return only the track change text with appropriate XML structural tags added. Maintain the original content but wrap it with proper XML tags based on the document's structure. Do not add any new content text, just add tags.
            Below are the images for reference:
            """
            
            content_payload = [{"type": "text", "text": user_prompt_text.strip()}]

            for page_num in pages:
                if page_num < len(self.doc_images):
                    image_path = self.doc_images[page_num]['path']
                    base64_image = self._encode_image_to_base64(image_path)
                    if base64_image:
                        content_payload.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}",
                                "detail": "high"
                            }
                        })
                else:
                    logger.warning(f"Page number {page_num} out of bounds for document images")

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_payload}
            ]

            result = self._fallback_handler.call_with_fallback(
                messages=messages,
                max_tokens=4096,
            )

            if result is None:
                logger.error("All models in fallback chain exhausted for this segment")
                return None, empty_usage

            logger.info("[GPTProcessor] Success with model: %s", result.get("model", "unknown"))
            output = result["text"]
            usage = result.get("usage", empty_usage)
            
            if output.startswith("```xml"):
                output = output[len("```xml"):].strip()
            if output.endswith("```"):
                output = output[:-3].strip()
            
            return output, usage
            
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
            return None, empty_usage

    def clean_text_for_search(self, text: str) -> str:
        """Remove HTML tags and entities for searching."""
        if not text:
            return ""
        
        clean_text = re.sub(r'<[^>]+>', '', text)
        
        html_entities = {
            '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
            '&apos;': "'", '&nbsp;': ' ', '&ndash;': '–', '&mdash;': '—',
            '&sect;': '§', '&para;': '¶'
        }
        
        for entity, replacement in html_entities.items():
            clean_text = clean_text.replace(entity, replacement)
        
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        return clean_text

    def create_context_windows(self, text: str, window_size: int) -> List[str]:
        """Create overlapping context windows from text."""
        words = text.split()
        if len(words) <= window_size:
            return [text]
        
        windows = []
        for i in range(len(words) - window_size + 1):
            window = ' '.join(words[i:i + window_size])
            windows.append(window)
        
        return windows

    def search_with_context_windows(self, search_text: str, window_size: int) -> Dict[int, int]:
        """Search for text using overlapping context windows."""
        page_matches = defaultdict(int)
        context_windows = self.create_context_windows(search_text, window_size)
        
        for window in context_windows:
            for page_num, page_text in self.doc_text_by_page.items():
                clean_page_text = self.clean_text_for_search(page_text).lower()
                clean_window = window.lower()
                
                if clean_window in clean_page_text:
                    page_matches[page_num] += 1
        
        return dict(page_matches)

    def intelligent_search(self, search_text: str) -> List[int]:
        """Fallback search based on word matches."""
        page_scores = defaultdict(float)
        search_words = set(search_text.lower().split())
        
        for page_num, page_text in self.doc_text_by_page.items():
            clean_page_text = self.clean_text_for_search(page_text).lower()
            found_words = search_words.intersection(clean_page_text.split())
            page_scores[page_num] = len(found_words)
        
        if page_scores:
            max_score = max(page_scores.values())
            if max_score > 0:
                threshold = max(max_score * 0.6, 2)
                best_pages = [page for page, score in page_scores.items() if score >= threshold]
                best_pages.sort()
                return best_pages
        
        return []

    def _search_for_text(self, text_to_search: str) -> List[int]:
        """Internal helper method for multi-step search."""
        if not text_to_search:
            logger.warning("No text provided for searching")
            return []

        clean_search_text = self.clean_text_for_search(text_to_search)
        if not clean_search_text:
            logger.warning("Search text empty after cleaning")
            return []
        
        # Try different context window sizes
        for window_size in range(min(self.context_window_size, len(clean_search_text.split())), 2, -1):
            page_matches = self.search_with_context_windows(clean_search_text, window_size)
            
            if page_matches:
                max_matches = max(page_matches.values())
                best_pages = [page for page, count in page_matches.items() if count >= max_matches * 0.7]
                
                if best_pages:
                    best_pages.sort()
                    logger.info(f"Found pages {[p + 1 for p in best_pages]} using window size {window_size}")
                    return best_pages
        
        # Fallback to intelligent search
        logger.info("Context window search failed, using intelligent fallback")
        return self.intelligent_search(clean_search_text)

    def _filter_continuous_pages(self, pages: List[int]) -> List[int]:
        """
        Ensure the list of pages is continuous.
        If pages are disjoint (e.g., [5, 8]), select the longest continuous sequence.
        If there is a small gap (e.g. [1, 3]) in a multi-page scenario, fill it ([1, 2, 3]).
        """
        if not pages:
            return []
        
        unique_pages = sorted(list(set(pages)))
        if len(unique_pages) == 1:
            return unique_pages
            
        # 1. Check for small gaps (missing 1 page) which might be a search miss in a span
        # e.g., if we have [1, 3], we assume [1, 2, 3] if it's a single track change
        filled_pages = []
        if unique_pages:
            filled_pages.append(unique_pages[0])
            for i in range(1, len(unique_pages)):
                prev = unique_pages[i-1]
                curr = unique_pages[i]
                gap = curr - prev
                
                # If gap is 2 (e.g., 1 -> 3, missing 2), fill it
                if gap == 2:
                    filled_pages.append(prev + 1)
                
                # If gap is huge (e.g. 5 -> 8), don't fill yet, handle in sequence grouping
                filled_pages.append(curr)
        
        # 2. Group into continuous sequences
        sequences = []
        if filled_pages:
            current_seq = [filled_pages[0]]
            for i in range(1, len(filled_pages)):
                if filled_pages[i] == filled_pages[i-1] + 1:
                    current_seq.append(filled_pages[i])
                else:
                    sequences.append(current_seq)
                    current_seq = [filled_pages[i]]
            sequences.append(current_seq)
            
        # 3. Select the best sequence
        # We assume the longest sequence is the correct match for the track change.
        # If ties, we prefer the first one (earlier in document).
        best_sequence = max(sequences, key=len)
        
        logger.info(f"Filtered pages from {unique_pages} to continuous sequence {best_sequence}")
        return best_sequence
    
    def find_track_change_pages(self, track_change_text: str, previous_content: str) -> List[int]:
        """
        Find pages containing track change with support for multi-page spans.
        """
        found_pages = []
        
        clean_tc_text = self.clean_text_for_search(track_change_text)
        text_len = len(clean_tc_text)
        
        # Strategy 1: For Long Text (> 300 chars), check Start and End separately
        # This handles cases where text starts on Page 1 and ends on Page 2 or 3
        if text_len > 300:
            logger.info("Track change is long (>300 chars) - attempting multi-page span detection")
            
            # Search for the first ~200 chars
            start_chunk = clean_tc_text[:200]
            start_pages = self._search_for_text(start_chunk)
            
            # Search for the last ~200 chars
            end_chunk = clean_tc_text[-200:]
            end_pages = self._search_for_text(end_chunk)
            
            if start_pages and end_pages:
                # Find the logical span: min start page to max end page
                # We filter to ensure End Page >= Start Page
                
                min_start = min(start_pages)
                # Filter end pages that make sense (must be after or on start page)
                valid_ends = [p for p in end_pages if p >= min_start]
                
                if valid_ends:
                    max_end = max(valid_ends)
                    # Create range [min_start, ... , max_end]
                    found_pages = list(range(min_start, max_end + 1))
                    logger.info(f"Detected multi-page span: {found_pages}")
                    return found_pages # Range is inherently continuous
        
        # Strategy 2: Short text or Split Search failed
        if not found_pages:
            # 2a. Try Anchor (Previous Content) - High Confidence
            anchor_pages = []
            if previous_content:
                anchor_pages = self._search_for_text(previous_content)
                
            # 2b. Try Full Text Search
            # If text is very short, relying solely on it might produce false positives (e.g. "the")
            # So we prefer anchor if text is short.
            text_matches = []
            if len(clean_tc_text) > 30: 
                text_matches = self._search_for_text(track_change_text)
                
            # Combine logic
            if text_matches:
                found_pages = text_matches
                # If we have anchor pages, use them to filter outliers from text matches?
                # For now, just trust text matches if found.
            elif anchor_pages:
                logger.info("Track change text not found using full search - falling back to previous content location")
                found_pages = anchor_pages
            else:
                # Last resort: if text was short and we skipped it in 2b, try it now
                if len(clean_tc_text) <= 30 and clean_tc_text:
                     found_pages = self._search_for_text(track_change_text)

        # Enforce Continuity on the resulting list
        final_pages = self._filter_continuous_pages(found_pages)
        return final_pages

    def check_content_drop(self, original: str, llm_output: str, segment_num: str) -> Optional[Dict[str, Any]]:
        """
        Check if the LLM output dropped more than 10% of the content from the original text.
        Ignores tags, footnote numbers, entities, and punctuation for the length calculation.
        """
        import html
        import difflib

        clean_orig = re.sub(r'<[^>]+>', '', original).strip()
        clean_llm = re.sub(r'<[^>]+>', '', llm_output).strip()

        replacements = [("&percnt;", "%"), ("&dollar;", "$"), ("&mdash;", "—"), ("&ndash;", "–"),
                        ("&sect;", "§"), ("&dblsect;", "§§"), ("&para;", "¶")]
        for old, new in replacements:
            clean_orig = clean_orig.replace(old, new)
            clean_llm = clean_llm.replace(old, new)

        clean_orig = html.unescape(clean_orig)
        clean_llm = html.unescape(clean_llm)

        norm_orig = re.sub(r'\s+', ' ', clean_orig)
        norm_llm = re.sub(r'\s+', ' ', clean_llm)

        alpha_orig = re.sub(r'[\W\d]+', '', norm_orig)
        alpha_llm = re.sub(r'[\W\d]+', '', norm_llm)

        if len(alpha_orig) == 0:
            return None

        drop_ratio = (len(alpha_orig) - len(alpha_llm)) / len(alpha_orig)

        if drop_ratio > 0:
            matcher = difflib.SequenceMatcher(None, norm_orig.split(), norm_llm.split())
            dropped_chunks = []
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag in ('delete', 'replace'):
                    chunk = " ".join(norm_orig.split()[i1:i2])
                    if re.search(r'[a-zA-Z]', chunk):
                        dropped_chunks.append(chunk)

            if not dropped_chunks:
                return None

            dropped_text = " ... ".join(dropped_chunks)

            logger.error(f"Major content drop detected in Segment {segment_num}. Drop ratio: {drop_ratio:.2%}")

            return {
                "segment_num": segment_num,
                "drop_ratio": f"{drop_ratio:.2%}",
                "original_text": original,
                "llm_text": llm_output,
                "dropped_content": dropped_text
            }
        return None

    # def process_track_changes(self, mapper_json: List[Dict[str, Any]], client: AzureOpenAI, xml_content: str) -> List[Dict[str, Any]]:
    def process_track_changes(self, mapper_json: List[Dict[str, Any]], client: AzureOpenAI, xml_content: str, report: Optional["PipelineReport"] = None, max_workers: int = 10) -> List[Dict[str, Any]]:
        """Process all track changes from mapper JSON in parallel, using context from the intermediate XML."""
        # Initialize updated_mapper with all original items
        updated_mapper = list(mapper_json)
        
        # Create a map from insertion segment number to its index in the updated_mapper list
        insertion_map = {
            item['Track Change Segment']: i 
            for i, item in enumerate(updated_mapper) 
            if item.get("type") == "insertion"
        }

        # Find all <trace.ins> placeholders in the intermediate XML
        trace_ins_matches = list(re.finditer(r'<trace.ins>(.*?)</trace.ins>', xml_content, re.DOTALL))

        if len(insertion_map) != len(trace_ins_matches):
            logger.error(f"Mismatch between insertion instructions{len(insertion_map)} and placeholders{len(trace_ins_matches)} in XML. Aborting GPT processing.")
            return mapper_json # Return original mapper on error

        # --- STEP 1: Pre-process tasks sequentially to gather context safely ---
        tasks = []
        for i, match in enumerate(trace_ins_matches):
            segment_num = str(i + 1)
            
            if segment_num not in insertion_map:
                logger.warning(f"Could not find instruction for insertion segment {segment_num}. Skipping.")
                continue

            mapper_index = insertion_map[segment_num]
            item = updated_mapper[mapper_index]
            
            track_change_text_to_tag = match.group(1)
            track_change_text_for_llm = f"<trace>{track_change_text_to_tag}</trace>"
            previous_content = item.get("Previous Content", "")

            # --- CONTEXT EXTRACTION (from intermediate XML) ---
            start_idx = match.start()
            end_idx = match.end()

            pre_context_raw = xml_content[max(0, start_idx - 1000):start_idx]
            open_tag_matches = list(re.finditer(r'<[a-zA-Z]', pre_context_raw))
            pre_context_start_index = 0
            if len(open_tag_matches) >= 2:
                pre_context_start_index = open_tag_matches[-2].start()
            elif len(open_tag_matches) == 1:
                pre_context_start_index = open_tag_matches[0].start()
            pre_context = pre_context_raw[pre_context_start_index:]

            post_context_raw = xml_content[end_idx : end_idx + 1000]
            close_tag_matches = list(re.finditer(r'</', post_context_raw))
            post_context_end_index = len(post_context_raw)
            if len(close_tag_matches) >= 2:
                second_close_tag_start = close_tag_matches[1].start()
                end_of_tag = post_context_raw.find('>', second_close_tag_start)
                if end_of_tag != -1:
                    post_context_end_index = end_of_tag + 1
            elif len(close_tag_matches) == 1:
                first_close_tag_start = close_tag_matches[0].start()
                end_of_tag = post_context_raw.find('>', first_close_tag_start)
                if end_of_tag != -1:
                    post_context_end_index = end_of_tag + 1
            post_context = post_context_raw[:post_context_end_index]
            # --- END OF CONTEXT EXTRACTION ---

            logger.info(f"Preparing track change segment {segment_num} for LLM queue")
            pages = self.find_track_change_pages(track_change_text_to_tag, previous_content)
            
            if not pages:
                logger.warning(f"Could not find pages for segment {segment_num}")
                if report:
                    report.record_llm_call(segment_num, 0.0, "skipped_no_pages", skipped=True)
                continue
                
            # Store the extracted parameters as a task dict
            tasks.append({
                'segment_num': segment_num,
                'mapper_index': mapper_index,
                'track_change_text_for_llm': track_change_text_for_llm,
                'previous_content': previous_content,
                'pages': pages,
                'pre_context': pre_context,
                'post_context': post_context
            })

        # --- STEP 2: Process API calls in Parallel ---
        def worker(task):
            """Worker function to execute the LLM call"""
            llm_start = time.time()
            updated_track, token_usage = self.call_llm(
                client, self.system_prompt, task['track_change_text_for_llm'],
                task['previous_content'], task['pages'], task['pre_context'], task['post_context']
            )
            llm_elapsed = time.time() - llm_start
            return task, updated_track, token_usage, llm_elapsed

        progress_bar = tqdm(total=len(tasks), desc="Processing with GPT", unit="change")
        content_drops = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks to the thread pool
            future_to_task = {executor.submit(worker, task): task for task in tasks}
            
            # Process results as they complete
            for future in concurrent.futures.as_completed(future_to_task):
                task, updated_track_change, token_usage, llm_elapsed = future.result()
                mapper_index = task['mapper_index']
                segment_num = task['segment_num']
                original_track_change_text = task['track_change_text_for_llm']

                if updated_track_change:
                    is_short_skip = (updated_track_change == task['track_change_text_for_llm'])
                    if is_short_skip and report:
                        report.record_llm_call(segment_num, llm_elapsed, "skipped_short_text",
                                               pages_used=[p + 1 for p in task['pages']], skipped=True)
                    elif report:
                        report.record_llm_call(segment_num, llm_elapsed, "success",
                                               pages_used=[p + 1 for p in task['pages']],
                                               prompt_tokens=token_usage.get("prompt_tokens", 0),
                                               completion_tokens=token_usage.get("completion_tokens", 0),
                                               total_tokens=token_usage.get("total_tokens", 0))

                    drop_info = self.check_content_drop(original_track_change_text, updated_track_change, segment_num)
                    if drop_info:
                        content_drops.append(drop_info)

                    first_tag_end = updated_track_change.find('>')
                    opening_tag = updated_track_change[:first_tag_end]
                    
                    if 'date.trace' in opening_tag:
                        updated_mapper[mapper_index]["Track changes"] = updated_track_change.replace("\n","")
                    elif updated_track_change.startswith('<trace'):
                        updated_mapper[mapper_index]["Track changes"] = updated_track_change.replace("\n","")
                    elif first_tag_end == -1:
                        updated_mapper[mapper_index]["Track changes"] = updated_track_change.replace("\n","")
                    else: 
                        modified_string = updated_track_change[:first_tag_end] + ' date.trace="1"' + updated_track_change[first_tag_end:]
                        updated_mapper[mapper_index]["Track changes"] = modified_string.replace("\n","")
                    
                    logger.info(f"Successfully updated segment {segment_num}")
                else:
                    logger.error(f"Failed to update segment {segment_num}")
                    if report:
                        report.record_llm_call(segment_num, llm_elapsed, "failed",
                                               pages_used=[p + 1 for p in task['pages']],
                                               prompt_tokens=token_usage.get("prompt_tokens", 0),
                                               completion_tokens=token_usage.get("completion_tokens", 0),
                                               total_tokens=token_usage.get("total_tokens", 0))
                    
                progress_bar.update(1)

        progress_bar.close()

        self.content_drops = content_drops
        if content_drops:
            logger.warning(f"Content drop detected in {len(content_drops)} segment(s)")

        return updated_mapper


    def set_system_prompt(self, system_prompt: str):
        """Set system prompt for LLM."""
        self.system_prompt = system_prompt

class FootnoteAnalyzer:
    """Analyzes insertions to determine footnote parts."""
    
    @staticmethod
    def create_insertion_json(processed_text: str) -> List[Dict[str, Any]]:
        """
        Parse processed text to find insertions and mark footnote parts.
        """
        logger.info("Creating insertion JSON with footnote analysis")
        results = []
        segment_id = 1
        combined_pattern = re.compile(r'(<footnote>(?:.|\n)*?</footnote>)|(<trace>(?:.|\n)*?</trace>)', re.IGNORECASE)
        trace_inside_pattern = re.compile(r'<trace>.*?</trace>', re.DOTALL | re.IGNORECASE)

        # Helper to check if the track change content is just a trace-wrapped deletion marker
        def is_invalid_trace(text: str) -> bool:
            # Remove trace tags and whitespace to see what's inside
            inner = re.sub(r'</?trace>', '', text, flags=re.IGNORECASE).strip()
            # If inner is empty or only contains a deleted marker, it's invalid for insertion JSON
            return not inner or inner == "<trace.deleted/>"

        matches = list(combined_pattern.finditer(processed_text))
        progress_bar = tqdm(total=len(matches), desc="Analyzing footnotes", unit="match")

        for match in matches:
            footnote_block = match.group(1)
            toplevel_trace = match.group(2)
            if footnote_block:
                trace_changes_in_footnote = trace_inside_pattern.findall(footnote_block)
                for change in trace_changes_in_footnote:
                    if change and not is_invalid_trace(change): # Check added here
                        results.append({
                            "type": "insertion",
                            "Track Change Segment": str(segment_id),
                            "Track changes": change,
                            "is_footnote_part": 1
                        })
                        segment_id += 1
            elif toplevel_trace:
                if toplevel_trace and not is_invalid_trace(toplevel_trace): # Check added here
                    results.append({
                        "type": "insertion",
                        "Track Change Segment": str(segment_id),
                        "Track changes": toplevel_trace,
                        "is_footnote_part": 0
                    })
                    segment_id += 1
            progress_bar.update(1)
        progress_bar.close()
        logger.info(f"Created {len(results)} insertion records")
        return results

class PostProcessor:
    """Handles post-processing operations for XML repositioning."""
    
    @staticmethod
    def update_xml_with_footnote_logic(xml_content: str, updated_mapper: List[Dict[str, Any]], 
                                       insertion_info: List[Dict[str, Any]]) -> str:
        """
        Update XML content with footnote positioning logic.
        
        Args:
            xml_content: XML content to update
            updated_mapper: Updated mapper with GPT tags
            insertion_info: Insertion information with footnote flags
            
        Returns:
            Updated XML content
        """
        logger.info("Updating XML with footnote logic")
        
        # Prepare and combine insertion data
        mapper_insertions = sorted(
            [item for item in updated_mapper if item.get('type') == 'insertion'],
            key=lambda x: int(x.get('Track Change Segment', 0))
        )
        info_insertions = sorted(
            [item for item in insertion_info if item.get('type') == 'insertion'],
            key=lambda x: int(x.get('Track Change Segment', 0))
        )
        
        if len(mapper_insertions) != len(info_insertions):
            logger.error("Mismatch in insertion counts between mapper and info files")
            return xml_content
            
        # Combine insertion data
        combined_insertions = []
        for i in range(len(mapper_insertions)):
            combined_insertions.append({
                'content': mapper_insertions[i]['Track changes'],
                'is_footnote': info_insertions[i]['is_footnote_part'],
                'segment_no': mapper_insertions[i]['Track Change Segment']
            })

        progress_bar = tqdm(total=len(combined_insertions), desc="Processing replacements", unit="replacement",disable=True)

        

        # --- FORWARD REPLACEMENT LOGIC ---

        iteration_index = 0

        

        while iteration_index < len(combined_insertions):

            # Re-scan for current positions

            trace_matches = list(re.finditer(r'<trace.ins>.*?</trace.ins>', xml_content, re.DOTALL))

            if len(trace_matches) == 0:

                logger.error(f"Mismatch: No more <trace.ins> tags found at iteration {iteration_index + 1}")

                break

                

            match = trace_matches[0] 

            task = combined_insertions[iteration_index]

            

            start_pos, end_pos = match.span()

            replacement_content = task['content'].replace("\n", "")

            # For cleaner logging, we show a snippet of the content

            log_content = (replacement_content[:50] + '...') if len(replacement_content) > 50 else replacement_content

            log_segment = task['segment_no']

            

            # Formatting logic

            if replacement_content.startswith('<para date.trace="1">'):

                replacement_content = "<p.ins>" + replacement_content + "</p.ins>"

            if replacement_content.startswith('<head date.trace="1">'):

                replacement_content = "<p.ins>" + replacement_content + "</p.ins>"

            # logic for non-footnote or specific footnote start

            if task['is_footnote'] == 0 or replacement_content.startswith('<footnote date.trace="1">'):

                if replacement_content.endswith("</trace>"):

                    text_after_match = xml_content[end_pos:]

                    first_footnote_tag_match = re.search(r'</?footnote(?:.body)?[^>]*>', text_after_match)

        

                    if first_footnote_tag_match and first_footnote_tag_match.group(0).startswith('</'):

                        logger.info(f"Segment {log_segment} [{log_content}]: first footnote tag to right is a closing tag. Relocating *after* </footnote>.")

                        

                        close_footnote_match = re.search(r'</footnote>', text_after_match)

                        if close_footnote_match:

                            insertion_point = end_pos + close_footnote_match.end()

                            part1 = xml_content[:start_pos]

                            part2 = xml_content[end_pos:insertion_point]

                            part3 = replacement_content

                            part4 = xml_content[insertion_point:]

                            xml_content = part1 + part2 + part3 + part4

                            logger.info(f"Segment {log_segment} relocated after first </footnote> tag.")

                            iteration_index += 1

                            progress_bar.update(1)
                            continue 

                        else:
                            logger.warning(f"Segment {log_segment} [{log_content}]: met adjacent footnote criteria, but no </footnote> was found. Fallback to standard.")



                # Standard replacement / Para-relocation logic

                relocate_content = False

                next_footnote_match = re.search(r'<\s*(/?)\s*footnote', xml_content[end_pos:])

                if next_footnote_match and next_footnote_match.group(1) == '/':

                    footnote_close_pos = end_pos + next_footnote_match.start()

                    para_close_match = re.search(r'</para>', xml_content[footnote_close_pos:])

                    

                    if para_close_match:

                        relocate_content = True

                        insertion_point = footnote_close_pos + para_close_match.end()

                        xml_content = xml_content[:start_pos] + xml_content[end_pos:insertion_point] + replacement_content + xml_content[insertion_point:]

                        logger.info(f"Segment {log_segment} [{log_content}] relocated after </para> following a </footnote>")

                    else:

                        logger.warning(f"Segment {log_segment} [{log_content}] followed by </footnote> but no subsequent </para>. Standard replacement used.")



                if not relocate_content:

                    logger.info(f"Segment {log_segment} [{log_content}] received standard replacement.")

                    xml_content = xml_content[:start_pos] + replacement_content + xml_content[end_pos:]

            elif task['is_footnote'] == 1:

                next_footnote_tag_match = re.search(r'<\s*(/?)\s*footnote(?=[\s>])', xml_content[end_pos:])

                is_already_in_footnote = True if (next_footnote_tag_match and next_footnote_tag_match.group(1) == '/') else False


                if is_already_in_footnote:

                    logger.info(f"Segment {log_segment} [{log_content}] already in footnote - performing standard replacement.")

                    xml_content = xml_content[:start_pos] + replacement_content + xml_content[end_pos:]

                    if replacement_content.rstrip().endswith('</trace>'):

                        content_after_replacement = xml_content[start_pos + len(replacement_content):]

                        if content_after_replacement.lstrip().startswith('</footnote.body>'):

                            logger.info(f"Segment {log_segment} [{log_content}]: Found <trace> adjacent to </footnote.body>. Attempting relocation.")

                            

                            search_area = xml_content[:start_pos]

                            para_text_close_pos = search_area.rfind('</para.text>')

                            if para_text_close_pos != -1:

                                logger.info(f"Segment {log_segment}: Found preceding </para.text>. Relocating content.")

                                part_before = xml_content[:start_pos]

                                part_after = xml_content[start_pos + len(replacement_content):]

                                temp_xml = part_before + part_after

                                xml_content = temp_xml[:para_text_close_pos] + replacement_content + temp_xml[para_text_close_pos:]

                                logger.info(f"Segment {log_segment} successfully relocated trace content.")

                            else:

                                logger.warning(f"Segment {log_segment} [{log_content}]: No preceding </para.text> tag found. No relocation performed.")

                else:

                    logger.info(f"Segment {log_segment} [{log_content}] is footnote but outside structure. Relocating to preceding footnote.")

                    footnote_body_close_tag = '</footnote.body>'

                    insertion_point = xml_content.rfind(footnote_body_close_tag, 0, start_pos)
                    

                    if insertion_point == -1:

                        logger.warning(f"Could not find preceding '{footnote_body_close_tag}' for segment {log_segment}. Standard replacement fallback.")

                        xml_content = xml_content[:start_pos] + replacement_content + xml_content[end_pos:]

                    else:

                        xml_content = xml_content[:insertion_point] + replacement_content + xml_content[insertion_point:start_pos] + xml_content[end_pos:]

                        logger.info(f"Segment {log_segment} successfully relocated to preceding footnote body.")



            iteration_index += 1

            progress_bar.update(1)

        progress_bar.close()

        logger.info("XML footnote logic update completed")

        return xml_content


    
    @staticmethod
    def process_xml_repositioning(input_file_path: str) -> str:
        """
        Process XML file for tag repositioning based on structure.
        Uses a two-pass approach:
        1. Reposition all container tags.
        2. Reposition all inline <trace> tags.
        
        Args:
            input_file_path: Path to input XML file
            
        Returns:
            Path to processed file
        """
        logger.info(f"Processing XML repositioning for: {input_file_path}")
        
        if not os.path.isfile(input_file_path):
            logger.error(f"File not found: {input_file_path}")
            return input_file_path

        try:
            base, _ = os.path.splitext(input_file_path)
            output_file_path = f"{base}_repositioned.xml"
            shutil.copyfile(input_file_path, output_file_path)
            
            with open(output_file_path, 'r', encoding='utf-8') as file:
                content = file.read()

            # --- PASS 1: Process CONTAINER tags ---
            logger.info("--- Starting Pass 1: Repositioning Container Tags ---")

            # Define patterns (excluding 'trace_pattern')
            para_trace_pattern = re.compile(r'(<p.ins>.*?</p.ins>)', re.DOTALL | re.IGNORECASE)
            section_trace_pattern = re.compile(r'(<(section|appendix\.section)\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</\2>)', re.DOTALL | re.IGNORECASE)
            # generic_trace_pattern = re.compile(r'<(?!para\b|section\b|feature.para.block\b|footnote\b|row\b)([^\s>]+)\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE)
            #generic_trace_pattern = re.compile(r'<(?!para\b|section\b|feature.para.block\b|footnote\b|row\b)([^\s>]+)\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE)
            feature_para_block_pattern = re.compile(r'(<feature\.para\.block\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</feature\.para\.block>)', re.DOTALL | re.IGNORECASE)
            footnote_trace_pattern = re.compile(r'(<footnote\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</footnote>)', re.DOTALL | re.IGNORECASE)
            row_trace_pattern = re.compile(r'(<row\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</row>)', re.DOTALL | re.IGNORECASE)
            research_reference_pattern = re.compile(r'(<research\.reference\.block\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</research\.reference\.block>)', re.DOTALL | re.IGNORECASE)
            online_view_pattern = re.compile(r'(<online\.view(?:\s+[^>]*?)?\s+date\.trace\s*=\s*["\']1["\'][^>]*?>.*?</online\.view>)', re.DOTALL | re.IGNORECASE)
            # Find all container matches
            matches = []
            for match in para_trace_pattern.finditer(content):
                matches.append({'type': 'para_trace', 'match': match, 'priority': 1})
            for match in section_trace_pattern.finditer(content):
                matches.append({'type': 'section_trace', 'match': match, 'priority': 3})
            for match in row_trace_pattern.finditer(content):
                matches.append({'type': 'row_trace', 'match': match, 'priority': 2})
            # for match in generic_trace_pattern.finditer(content):
                # tag_name = match.group(1)
                # matches.append({'type': 'generic_trace', 'match': match, 'tag_name': tag_name, 'priority': 2})
            for match in feature_para_block_pattern.finditer(content):
                matches.append({'type': 'feature_para_block', 'match': match, 'priority': 2})
            for match in footnote_trace_pattern.finditer(content):
                matches.append({'type': 'footnote_trace', 'match': match, 'priority': 2})
            for match in research_reference_pattern.finditer(content):
                matches.append({'type': 'research_ref', 'match': match, 'priority': 2})
            for match in online_view_pattern.finditer(content):
                matches.append({'type': 'online_view', 'match': match, 'priority': 2})


            # Sort matches by start position (ascending), then by end position (descending)
            # to identify top-level containers.
            matches.sort(key=lambda x: (x['match'].start(), -x['match'].end()))
            
            if not matches:
                logger.info("No matching container tags found in Pass 1.")
            else:
                logger.info(f"Found {len(matches)} total container tags. Filtering nested tags...")

                filtered_matches = []
                last_end = -1 # Keep track of the end of the last *top-level* match

                for item in matches:
                    match = item['match']
                    start = match.start()
                    
                    if start < last_end:
                        logger.info(f"Skipping nested {item['type']} match at {start}-{match.end()} as it is inside a container ending at {last_end}")
                        continue
                    
                    filtered_matches.append(item)
                    last_end = match.end()
                    
                logger.info(f"Processing {len(filtered_matches)} top-level (non-nested) container tags.")
                
                progress_bar = tqdm(total=len(filtered_matches), desc="Repositioning containers", unit="tag", disable=True)

                # Process container matches from END to START
                for item in reversed(filtered_matches):
                    match = item['match']
                    match_type = item['type']
                    
                    full_match_text = match.group(0)
                    start_index = match.start()
                    end_index = match.end()
                    
                    logger.info(f"Processing {match_type} at position {start_index}-{end_index}")
                    logger.info(f"\nTrack Change:\n{content[start_index-50:start_index]}...{full_match_text[:60]}...{content[end_index:end_index+50]}")
                

                    if match_type == 'footnote_trace':
                        trimmed_text_before = content[:start_index].rstrip()
                        is_outside_para = trimmed_text_before.endswith('</para.text>') or trimmed_text_before.endswith('</para>')
                        
                        if is_outside_para:
                            content = content[:start_index] + content[end_index:]
                            text_before_where_match_was = content[:start_index]
                            insertion_point = text_before_where_match_was.rfind('</para.text>')
                            
                            if insertion_point != -1:
                                content = content[:insertion_point] + " " + full_match_text + content[insertion_point:]
                                logger.info(f"Moved footnote_trace before the last </para.text> tag at position {insertion_point}")
                            else:
                                content = content[:start_index] + full_match_text + content[start_index:]
                                logger.warning(f"Could not find a preceding </para.text> tag. Kept footnote_trace in original place.")
                        else:
                            logger.info(f"Skipped moving footnote_trace as it is already inside a paragraph.")
                                
                    elif match_type == 'feature_para_block':
                        text_immediately_before = content[:start_index].rstrip()
                        if not text_immediately_before or not text_immediately_before.endswith('>'):
                            logger.info(f"Keeping feature.para.block in place as it is preceded by text, not a tag.")
                            progress_bar.update(1)
                            continue
                        else:
                            content = content[:start_index] + content[end_index:]
                            text_before_where_match_was = content[:start_index]
                            target_tag = '</para>'
                            last_tag_start_index = text_before_where_match_was.rfind(target_tag)
                            if last_tag_start_index != -1:
                                insertion_point = last_tag_start_index + len(target_tag)
                                content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                logger.info(f"Moved feature.para.block after the last {target_tag} tag at position {insertion_point}")
                            else:
                                content = content[:start_index] + full_match_text + content[start_index:]
                                logger.warning(f"Could not find a preceding {target_tag} tag. Kept feature.para.block in original place.")
                    
                    # --- Updated Logic: Research Reference and Online View Blocks ---
                    elif match_type in ['research_ref', 'online_view']:
                        logger.info(f"Processing {match_type} at {start_index}-{end_index}")
                        
                        text_before = content[:start_index]
                        # Check the tag immediately to the left
                        if text_before.rstrip().endswith('</outline.name.block>'):
                            logger.info(f"Skipping shift for {match_type}: already adjacent to </outline.name.block>")
                            progress_bar.update(1)
                            continue

                        target_tag = '</section.front>'
                        
                        # Find the LAST occurrence of </section.front> to the left
                        insertion_point = text_before.rfind(target_tag)
                        
                        if insertion_point != -1:
                            # 1. Remove the block from its current (wrong) position
                            content_without_block = content[:start_index] + content[end_index:]
                            
                            # 2. Re-calculate the insertion point on the NEW content 
                            # string to avoid index mismatch after removal.
                            # We search up to start_index because that's where our 'text_before' ended.
                            new_text_before = content_without_block[:start_index]
                            final_insertion_point = new_text_before.rfind(target_tag)
                            
                            if final_insertion_point != -1:
                                # 3. Insert the block at the final_insertion_point
                                # This places it exactly where </section.front> starts, 
                                # pushing the tag to the right.
                                content = (
                                    content_without_block[:final_insertion_point] + 
                                    full_match_text + 
                                    content_without_block[final_insertion_point:]
                                )
                                logger.info(f"Successfully moved {match_type} inside the section before {target_tag}")
                            else:
                                # Fallback if recalculation fails (unlikely)
                                content = content[:start_index] + full_match_text + content[start_index:]
                                logger.warning(f"Recalculation failed for {match_type}. Reverting to original position.")
                        else:
                            logger.warning(f"Could not find preceding {target_tag} for {match_type}. Keeping in place.")


                    elif match_type == 'para_trace':
                        # 1. Remove the block from its current position
                        content = content[:start_index] + content[end_index:]
                        text_before_match = content[:start_index]
                        text_after_match = content[start_index:]
                        # --- NEW CONDITION: Check if sandwiched between </footnote.reference> and <footnote.body> ---
                        footnote_ref_match = re.search(r'</footnote\.reference>\s*$', text_before_match)
                        footnote_body_match = re.match(r'\s*<footnote\.body[^>]*>', text_after_match)
                        if footnote_ref_match and footnote_body_match:
                            # Calculate insertion point just after the <footnote.body ...> tag
                            insertion_point = start_index + footnote_body_match.end()
                            content = content[:insertion_point] + full_match_text + content[insertion_point:]
                            logger.info(f"Moved para_trace after <footnote.body> at position {insertion_point}")
                        else:
                            # --- EXISTING LOGIC ---
                            close_para_matches = list(re.finditer(r'</para></feature.para.block>|</para>|</head>|</list>|<footnote.body[^>]*>|</stat.para>', text_before_match))
                            last_close_para_pos = close_para_matches[-1].start() if close_para_matches else -1
                    
                            open_section_matches = list(re.finditer(r'<(?:appendix\.)?section\.body\b[^>]*>|<(?:appendix\.)?section\b>', text_before_match))
                            last_open_section_match = open_section_matches[-1] if open_section_matches else None
                            last_open_section_pos = last_open_section_match.start() if last_open_section_match else -1
                    
                            moved_as_first_para = False
                    
                            if last_open_section_pos > last_close_para_pos:
                                last_match_text = last_open_section_match.group(0)
                    
                                if 'section.body' in last_match_text:
                                    insertion_point = last_open_section_match.end()
                                    content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                    logger.info(f"Moved first para_trace after <section.body> tag at position {insertion_point}")
                                    moved_as_first_para = True
                                else:
                                    section_body_re = re.compile(r'<(?:appendix\.)?section\.body[^>]*>')
                                    # Search *after* the original start index
                                    section_body_match = section_body_re.search(content, pos=start_index) 
                                    if section_body_match:
                                        insertion_point = section_body_match.end()
                                        content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                        logger.info(f"Moved first para_trace para_trace after <section.body> (found after <section>) at pos {insertion_point}")
                                        moved_as_first_para = True
                    
                            if not moved_as_first_para:
                                if close_para_matches:
                                    insertion_point = close_para_matches[-1].end()
                                    content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                    logger.info(f"Moved para_trace after </para> tag at position {insertion_point}")
                                else:
                                    content = content[:start_index] + full_match_text + content[start_index:]
                                    logger.info(f"No preceding </para> tag found - keeping para_trace in its original place.")
                                
                    elif match_type == 'section_trace':
                        content = content[:start_index] + content[end_index:]
                        text_before_match = content[:start_index]
                        close_section_positions = [m.end() for m in re.finditer(r'</section>', text_before_match)]
                        
                        if close_section_positions:
                            insertion_point = close_section_positions[-1]
                            content = content[:insertion_point] + full_match_text + content[insertion_point:]
                            logger.info(f"Moved section_trace after </section> at position {insertion_point}")
                        else:
                            text_after_match = content[start_index:]
                            match_after = re.search(r'</section>', text_after_match)
                            
                            if match_after:
                                insertion_point = start_index + match_after.end()
                                content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                logger.info(f"Moved section_trace after next </section> at position {insertion_point}")
                            else:
                                content = content[:start_index] + full_match_text + content[start_index:]
                                logger.info(f"No </section> found before or after - keeping section_trace in original place")

                    elif match_type == 'row_trace':
                        logger.info(f"Processing row_trace at position {start_index}-{end_index}")
                        text_before = content[:start_index]
                        text_after = content[end_index:] # Text *after* the match
                        
                        # --- 1. "First row check" ---
                        last_table_match = re.search(r'<table[^>]*>', text_before, re.IGNORECASE)
                        last_row_match = re.search(r'<row[^>]*>', text_before, re.IGNORECASE)
                        last_table_pos = last_table_match.start() if last_table_match else -1
                        last_row_pos = last_row_match.start() if last_row_match else -1

                        is_first_row = (last_table_pos > last_row_pos)

                        if is_first_row:
                            logger.info("row_trace identified as 'first row' of a table.")
                            text_immediately_before = text_before.rstrip()
                            
                            # "if the full_match_text is right adjacent to a thead tag keep the position as is"
                            if text_immediately_before.endswith('<thead>'):
                                 logger.info("Already adjacent to <thead>. Keeping position.")
                                 # Do nothing, progress_bar.update(1) is at the end
                            
                            else:
                                # "else shift it after <thead> tag."
                                # Find <thead> *within the current table* (after last_table_pos, before start_index)
                                table_context_text = content[last_table_pos:start_index]
                                thead_match = re.search(r'<thead>', table_context_text, re.IGNORECASE)
                                
                                if thead_match:
                                    # Remove from old position *first*
                                    content = content[:start_index] + content[end_index:]
                                    
                                    # Calculate *absolute* insertion point
                                    # This is the start of the table + end of <thead> *relative* to table
                                    insertion_point = last_table_pos + thead_match.end()
                                    content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                    logger.info(f"Moved 'first row' row_trace after <thead> at {insertion_point}")
                                else:
                                    logger.warning("row_trace is 'first row' but no <thead> found in its table. Keeping in place.")
                        
                        else: # --- 2. "Not the first row" ---
                            logger.info("row_trace is not 'first row'. Checking sibling tags.")
                            
                            # Find *last* occurrence of either tag
                            last_close_row_pos = -1
                            last_close_row_match_obj = None
                            for m in re.finditer(r'</row>', text_before, re.IGNORECASE):
                                last_close_row_pos = m.start()
                                last_close_row_match_obj = m

                            last_open_row_pos = -1
                            for m in re.finditer(r'<row[^>]*>', text_before, re.IGNORECASE):
                                last_open_row_pos = m.start()
                                
                            if last_close_row_pos > last_open_row_pos:
                                # --- Logic 3: `</row>` is nearest ---
                                logger.info("Nearest tag to the left is </row>.")
                                
                                # "if it is right adjacent to </row> keep it as is."
                                if text_before.rstrip().endswith('</row>'):
                                    logger.info("Already adjacent to </row>. Keeping position.")
                                else:
                                    # "move the full match text after it"
                                    if last_close_row_match_obj:
                                        # Remove from old position
                                        content = content[:start_index] + content[end_index:]
                                        # Find insertion point
                                        insertion_point = last_close_row_match_obj.end() 
                                        content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                        logger.info(f"Moved row_trace after preceding </row> at {insertion_point}")
                                    else:
                                        logger.warning("Logic error: found </row> pos but not match object. Keeping in place.")

                            elif last_open_row_pos > last_close_row_pos:
                                # --- Logic 4: `<row...>` is nearest (nested) ---
                                logger.info("Nearest tag to the left is <row...>. This implies nesting.")
                                
                                # "search for the first occurence of a </row> tag to the right of full_match_text"
                                first_close_row_match_in_after = re.search(r'</row>', text_after, re.IGNORECASE)

                                if first_close_row_match_in_after:
                                    # Find absolute insertion point *before* modifying content.
                                    insertion_point_relative_to_after = first_close_row_match_in_after.end()
                                    absolute_insertion_point = end_index + insertion_point_relative_to_after
                                    content = content[:start_index] + content[end_index:]
                                    removal_length = end_index - start_index
                                    new_insertion_point = absolute_insertion_point - removal_length
                                    
                                    # 3. Insert at new position.
                                    content = content[:new_insertion_point] + full_match_text + content[new_insertion_point:]
                                    logger.info(f"Moved nested row_trace after its parent's </row> at new pos {new_insertion_point}")
                                    
                                else:
                                    logger.warning("row_trace is nested but no closing </row> found after it. Keeping in place.")
                            
                            else:
                                logger.warning(f"Could not determine row_trace context (open_pos={last_open_row_pos}, close_pos={last_close_row_pos}). Keeping in place.")
                    
                    elif match_type == 'generic_trace':
                        continue
                        tag_name = item['tag_name']
                        logger.info(f"Processing generic_trace <{tag_name}> at position {start_index}-{end_index}")

                        content = content[:start_index] + content[end_index:]
                        text_before_match = content[:start_index]
                        closing_tag = f"</{tag_name}>"
                        
                        positions = [m.end() for m in re.finditer(re.escape(closing_tag), text_before_match)]
                        
                        if positions:
                            insertion_point = positions[-1]
                            content = content[:insertion_point] + full_match_text + content[insertion_point:]
                            logger.info(f"Moved <{tag_name}> tag after its last sibling at position {insertion_point}")
                        else:
                            last_tag_start_index = content.rfind('</')
                            if last_tag_start_index != -1:
                                last_tag_end_index = content.find('>', last_tag_start_index)
                                if last_tag_end_index != -1:
                                    insertion_point = last_tag_end_index + 1
                                    content = content[:insertion_point] + full_match_text + content[insertion_point:]
                                    logger.info(f"Moved <{tag_name}> tag after the last closing tag in the document at position {insertion_point}")
                                else:
                                    content = content[:start_index] + full_match_text + content[start_index:]
                                    logger.info(f"Found a partial closing tag, but it was malformed. Keeping tag in its original place.")
                            else:
                                content = content[:start_index] + full_match_text + content[start_index:]
                                logger.info(f"No closing tags of any kind found - keeping tag in its original place.")
                                
                    progress_bar.update(1)
                
                progress_bar.close()

            # --- Pass 1.5: Grouping and Repositioning Dislocated Sections (Forward Pass) ---
            logger.info("--- Starting Pass 1.5: Grouping and Repositioning Sections ---")
            
            section_pattern = re.compile(r'(<(section|appendix\.section)\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</\2>)', re.DOTALL | re.IGNORECASE)
            # generic_trace_pattern = re.compile(r'<(?!para\b|section\b|feature.para.block\b|footnote\b|row\b)([^\s>]+)\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE)
            all_sections = list(section_pattern.finditer(content))
            
            if all_sections:
                cumulative_offset = 0
                
                # 1. Forward Pass with Offset Correction
                for m in all_sections:
                    # Calculate current indices based on cumulative changes to the string
                    current_start = m.start() + cumulative_offset
                    current_end = m.end() + cumulative_offset
                    
                    text_before = content[:current_start].rstrip()
                    
                    # Check dislocation
                    if not text_before.endswith('</section>'):
                        tag_open = "<s_ins>"
                        tag_close = "</s_ins>"
                        
                        # Wrap the content
                        content = (content[:current_start] + 
                                   tag_open + m.group(0) + tag_close + 
                                   content[current_end:])
                        
                        # Update offset: we added len("<s_ins>") + len("</s_ins>") = 14 chars
                        cumulative_offset += len(tag_open) + len(tag_close)
            
                # 2. COLLATING STEP: Merge adjacent groups
                content = re.sub(r'</s_ins>\s*<s_ins>', '', content)
            
                # 3. SHIFTING STEP: Forward Pass for Movement
                s_ins_pattern = re.compile(r'<s_ins>(.*?)</s_ins>', re.DOTALL)
                
                # We must re-find matches because the string length changed in step 2
                s_ins_matches = list(s_ins_pattern.finditer(content))
                cumulative_move_offset = 0
            
                for s_match in s_ins_matches:
                    # Adjust for any shifts caused by previous moves in this loop
                    curr_s_start = s_match.start() 
                    curr_s_end = s_match.end()
                    inner_content = s_match.group(1)
                    
                    text_after = content[curr_s_end:]
                    first_closing_section = re.search(r'</section>', text_after, re.IGNORECASE)
                    
                    if first_closing_section:
                        # Insertion point relative to the end of the group
                        rel_insertion = first_closing_section.end()
                        abs_insertion = curr_s_end + rel_insertion
                        
                        part_before = content[:curr_s_start]
                        part_between = content[curr_s_end:abs_insertion]
                        part_after = content[abs_insertion:]
                        
                        # Move: [Before] + [Gap Content] + [The Section Group] + [After]
                        content = part_before + part_between + inner_content + part_after
                        
                        # Note: Moving content internally does NOT change the total length of the 
                        # string, but it DOES change where the "marker" tags for the NEXT matches 
                        # would have been. However, because we are moving the group TO THE RIGHT, 
                        # and we are iterating forward, this logic remains stable.
                        logger.info(f"Forward shifted section group.")
                    else:
                        logger.info(f"couldn't find section.")

            # Final Cleanup
            content = content.replace("<s_ins>", "").replace("</s_ins>", "")
            logger.info("--- Starting Pass 2: Repositioning 'trace' Tags ---")
            # Re-define the pattern and find all matches in the *modified* content
            # trace_pattern = re.compile(r'(<trace>.*?</trace>)', re.DOTALL | re.IGNORECASE)
            # trace_matches = []
            # for match in trace_pattern.finditer(content):
            #     trace_matches.append(match) # Just need the match object
            

            trace_pattern = re.compile(r'(<trace>.*?</trace>)', re.DOTALL | re.IGNORECASE)
            
            # Regex to find a tag touching the LEFT side (ends exactly at the current position)
            left_tag_check = re.compile(r'</?[^>]+>$')
            
            # Regex to find a tag touching the RIGHT side (starts exactly at the current position)
            right_tag_check = re.compile(r'^</?[^>]+>')
            
            # Define the tags that are allowed to be adjacent
            # We use lowercase to ensure case-insensitive matching matches your logic
            exception_tags = {'<trace>', '</trace>', '<trace.deleted/>'}
            
            trace_matches = []
            
            for match in trace_pattern.finditer(content):
                start, end = match.span()
                
                # --- Check Left Side ---
                # Scan the text immediately before the match
                left_search = left_tag_check.search(content[max(0, start - 1000):start])
                
                left_blocked = False
                if left_search:
                    # A tag was found. Is it an exception?
                    found_tag = left_search.group().lower() # Normalize to lowercase
                    if found_tag not in exception_tags:
                        left_blocked = True # It's a tag, and it's NOT an exception -> Block it
            
                # --- Check Right Side ---
                # Scan the text immediately after the match
                right_search = right_tag_check.match(content, pos=end)
                
                right_blocked = False
                if right_search:
                    # A tag was found. Is it an exception?
                    found_tag = right_search.group().lower()
                    if found_tag not in exception_tags:
                        right_blocked = True
            
                # --- Final Decision ---
                # Only append if we are NOT blocked by valid tags on either side
                if left_blocked or right_blocked:
                    trace_matches.append(match)
                
            if not trace_matches:
                logger.info("No 'trace' tags found in Pass 2. Skipping.")
            else:
                logger.info(f"Found {len(trace_matches)} 'trace' tags to process in Pass 2.")
                
                trace_progress_bar = tqdm(total=len(trace_matches), desc="Repositioning trace tags", unit="tag", disable=True)
                
                # Process from END to START to avoid index shifting
                for match in reversed(trace_matches):
                    full_match_text = match.group(0)
                    start_index = match.start()
                    end_index = match.end()

                    logger.info(f"Processing 'trace' at new position {start_index}-{end_index}")
                    logger.info(f"Processing 'trace':{full_match_text}")
                    
                    inner_content_match = re.search(r'<trace>(.*)</trace>', full_match_text, re.DOTALL | re.IGNORECASE)
                    if not inner_content_match:
                        logger.warning(f"Found <trace> tag at {start_index} with malformed/missing inner content. Skipping.")
                        trace_progress_bar.update(1)
                        continue
                    
                    clean_inner_content = re.sub(r'<[^>]+>', '', inner_content_match.group(1)).strip()
                    
                    text_before = content[:start_index]
                    text_after = content[end_index:]
                    
                    reposition_primary = False
                    reposition_secondary = False
                    secondary_tags_match = None

                    # --- NEW CONDITION: Check for any section closing tag immediately to the left ---
                    # This matches </section>, </section.front>, </section.body>, etc.
                    section_left_match = re.search(r'</section\.front\s*>\s*$', text_before, re.IGNORECASE)
                    if section_left_match:
                        logger.info(f"Trace at {start_index} is preceded by a section tag. Moving left.")
                        
                        # We want to move the trace before the sequence of closing tags
                        # Example: </name></outline.name.block></section.front>
                        # We look for the start of this closing tag cluster
                        tags_to_move_past = re.search(r'((?:</[\w\.-]+>\s*)+)</section(\.[\w\.-]+)?\s*>\s*$', text_before, re.IGNORECASE)
                        
                        if tags_to_move_past:
                            # Get the start index of the first closing tag in that sequence
                            move_to_index = tags_to_move_past.start(1)
                            
                            part1 = content[:move_to_index]
                            part2 = full_match_text
                            part3 = content[move_to_index:start_index]
                            part4 = content[end_index:]
                            
                            content = part1 + part2 + part3 + part4
                            logger.info(f"Successfully moved trace left to index {move_to_index}")
                            
                            # Skip the rest of the repositioning logic for this trace
                            reposition_primary = False
                            reposition_secondary = False
                        else:
                            # Fallback if we can't find a sequence, just stay put or proceed
                            logger.info("Could not determine tag cluster start. Proceeding with standard logic.")
                    
                    # --- Condition 1: Check length ---
                    
                    elif len(clean_inner_content) >= 4:
                        # --- Condition 2: Check for a non-section closing tag immediately to the left ---
                        left_match = re.search(r'</\s*(?!section\b)\s*([\w\.-]+)[^>]*>\s*$', text_before, re.IGNORECASE)
                        # --- Condition 3: Check for a non-section opening tag immediately to the right ---
                        right_match = re.search(r'^\s*<\s*(?!/|section\b)\s*([\w\.-]+)[^>]*>', text_after, re.IGNORECASE)
                        if left_match and right_match:
                            # Extract the captured tag names
                            left_tag_name = left_match.group(1)
                            right_tag_name = right_match.group(1)
                            
                            # Compare the tag names
                            if left_tag_name.lower() == right_tag_name.lower():
                                logger.info(f"Trace at {start_index} meets Primary Condition (between </{left_tag_name}> and <{right_tag_name}>).")
                                reposition_primary = True
                            else:
                                adjacent_tags_match = re.search(r'^(\s*(?:<\s*(?![cC]ite[\s>])[^/>][^>]*(?<!/)>\s*)+)', text_after)
                                if adjacent_tags_match:
                                    logger.info(f"Trace at {start_index} meets Secondary Condition (mismatched tags </{left_tag_name}> and <{right_tag_name}>, but adjacent opening tags found).")
                                    reposition_secondary = True
                                    secondary_tags_match = adjacent_tags_match # Store the match
                                else:
                                    logger.info(f"Trace at {start_index} failed Primary & Secondary Conditions (mismatched tags, no adjacent tags or <cite> found).")
                                    
                        else:
                            
                            logger.info(f"Trace at {start_index} failed Primary Condition (not between </tag> and <tag>).")
                    else:
                         logger.info(f"Trace at {start_index} is too short (len {len(clean_inner_content)}). Skipping.")
                    

                    # --- Execute Repositioning Logic ---
                    if reposition_primary:
                        # --- PRIMARY LOGIC: Skip over subsequent opening tags ---
                        logger.info(f"Executing Primary repositioning for trace at {start_index}...")
                        
                        # Find all adjacent opening tags to skip (excluding <cite>)
                        adjacent_tags_match = re.search(r'^(\s*(?:<\s*(?![cC]ite[\s>])[^/>][^>]*(?<!/)>\s*)+)', text_after)
                        
                        if adjacent_tags_match:
                            tags_to_skip = adjacent_tags_match.group(1)
                            insertion_point_abs = end_index + len(tags_to_skip)
                            
                            part1 = content[:start_index]
                            part2 = content[end_index:insertion_point_abs]
                            part3 = full_match_text
                            part4 = content[insertion_point_abs:]
                            
                            content = part1 + part2 + part3 + part4
                            
                            logger.info(f"Moved <trace> tag to position {start_index + len(tags_to_skip)}")
                        else:
                            # --- FALLBACK LOGIC: Move before </para.text> ---
                            logger.warning(f"Trace at {start_index} met Primary Condition but no adjacent opening tags were found to skip.")
                            logger.info(f"Executing Fallback repositioning for trace at {start_index}...")
                            
                            fallback_tag = '</para.text>'
                            # Find the *last* (closest) occurrence of the tag to the left
                            fallback_index = text_before.rfind(fallback_tag)
                            
                            if fallback_index != -1:
                                logger.info(f"Found fallback '{fallback_tag}' at index {fallback_index}. Moving trace block.")
                                
                                # Reconstruct content
                                part1 = content[:fallback_index]             # Text before </para.text>
                                part2 = full_match_text                      # The <trace>...</trace> block
                                part3 = content[fallback_index:start_index]  # The </para.text> and text up to the trace
                                part4 = content[end_index:]                  # The text after the trace
                                
                                content = part1 + part2 + part3 + part4
                                
                            else:
                                logger.info(f"No fallback '{fallback_tag}' found to the left. Leaving trace tag at {start_index}.")      
                    
                    elif reposition_secondary:
                        logger.info(f"Executing Secondary repositioning for trace at {start_index}...")
                        
                        # We already stored the match
                        tags_to_skip = secondary_tags_match.group(1)
                        insertion_point_abs = end_index + len(tags_to_skip)
                        
                        part1 = content[:start_index]
                        part2 = content[end_index:insertion_point_abs] # The tags to skip
                        part3 = full_match_text                        # The trace block
                        part4 = content[insertion_point_abs:]
                        
                        content = part1 + part2 + part3 + part4
                        
                        logger.info(f"Moved <trace> tag to position {start_index + len(tags_to_skip)}")
            
                    else:
                        # This logs the case where len < 4 and primary_reposition was False
                        logger.info(f"Trace at {start_index} did not meet any criteria. Leaving tag in place.")
                    
                    trace_progress_bar.update(1)
                
                trace_progress_bar.close()
            
            # #Remove surrounding tags to <trace.deleted/>
            def clean_xml_deletions(xml_string: str) -> str:
                """
                Cleans an XML string by performing several collapse operations
                and then removing whitespace between adjacent tags.
                """
                logger.info("Cleaning content of stray tags and whitespaces")
                current_string = xml_string.replace("<ins_trace.deleted/>","<trace.deleted/>").replace(" <trace.deleted/> ","<trace.deleted/>")
                return current_string.strip()


            # --- Clean up whitespace between the remaining tags ---
            # cleaned_content = re.sub(r'>\s+<', '><', clean_xml_deletions(content))
            cleaned_content = re.sub(r'>\s+<(?!trace|/trace)', '><', clean_xml_deletions(content))
            cleaned_content = re.sub(r'\s+', ' ', cleaned_content)
            cleaned_content = cleaned_content.replace("__sep__","<sep/>").replace("</footnote><cite","</footnote> <cite").replace("</footnote><ital>","</footnote> <ital>").replace("<p.ins>","").replace("</p.ins>","").replace("<trace> "," <trace>").replace("&rsquo;s","&apos;s").replace("&rsquo;t","&apos;t")
            

            def clean_final_xml_stray_tags(xml_content: str) -> str:
                            """
                            Iteratively cleans final XML to remove empty tags and collapse 
                            tags containing only <trace.deleted/>.
                            This version uses ONLY regular expressions to avoid re-ordering
                            attributes or decoding HTML entities.
                            Args:
                                xml_content: The final XML string to clean.
                            Returns:
                                A cleaned XML string.
                            """
                            print("Starting final XML cleanup (Empty Tags & trace.deleted Collapse) using REGEX")
                            # Pattern to find empty tags (e.g., <para attr="1">\s*</para>)
                            # It ignores <trace.deleted/> which is self-closing and valid.
                            empty_tag_pattern = re.compile(
                                r'<(?!(?:trace\.deleted)\b)([a-zA-Z0-9.:_-]+)(?:[^>]*)>\s*</\1\s*>'
                            )
                            # Pattern to find tags containing ONLY <trace.deleted/> and whitespace
                            # e.g., <para><para.txt> <trace.deleted/> </para.txt></para>
                            collapse_pattern = re.compile(
                                r'<(?!(?:html|body|trace\.deleted)\b)([a-zA-Z0-9.:_-]+)(?:[^>]*)>(\s*(?:<trace\.deleted\s*/>\s*)+)\s*</\1\s*>'
                            )
                            # Pattern to clean up adjacent <trace.deleted/> tags
                            collapse_adjacent_trace_pattern = re.compile(
                                r'(\s*<trace\.deleted\s*/>\s*)+'
                            )
                            previous_content = None
                            current_content = xml_content
                            while previous_content != current_content:
                                changes_made = False
                                previous_content = current_content
                                # --- PASS 1: Remove empty tags ---
                                new_content = re.sub(empty_tag_pattern, '', current_content)
                                if new_content != current_content:
                                    changes_made = True
                                    current_content = new_content
                                # --- PASS 2: Collapse containers with only trace.deleted ---
                                new_content = re.sub(collapse_pattern, ' <trace.deleted/> ', current_content)
                                if new_content != current_content:
                                    changes_made = True
                                    current_content = new_content
                                # --- POST-PASS CLEANUP: Collapse adjacent trace.deleted ---
                                new_content = re.sub(collapse_adjacent_trace_pattern, ' <trace.deleted/> ', current_content)
                                if new_content != current_content:
                                    changes_made = True
                                    current_content = new_content
                                # If neither pass made changes, the document is stable.
                                if not changes_made:
                                    print("Cleanup loop finished, no more changes detected.")
                                    break
                            # --- Final String Cleanup ---
                            # General whitespace cleanup
                            final_xml = re.sub(r'>\s+<', '><', current_content).strip() # Between tags
                            final_xml = re.sub(r'\s+', ' ', final_xml).strip()       # All other whitespace
                            print("Final XML cleanup complete.")
                            return final_xml

            cleaned_content = clean_final_xml_stray_tags(cleaned_content)

            def remove_footnotes_without_body(xml_content: str) -> str:
                """Remove <footnote> blocks that have a <footnote.reference> but no <footnote.body>."""
                footnote_pattern = re.compile(
                    r'<footnote\b[^>]*>.*?</footnote\s*>',
                    re.DOTALL | re.IGNORECASE
                )

                def _should_remove(m: re.Match) -> str:
                    block = m.group(0)
                    has_ref = re.search(r'<footnote\.reference\b', block, re.IGNORECASE)
                    has_body = re.search(r'<footnote\.body\b', block, re.IGNORECASE)
                    if has_ref and not has_body:
                        logger.info(f"Removing footnote without body: {block[:80]}...")
                        return ''
                    return block

                return footnote_pattern.sub(_should_remove, xml_content)

            cleaned_content = remove_footnotes_without_body(cleaned_content)

            def clean_quote_block_traces(xml_content: str) -> str:
                """Inside <quote.block> blocks, strip <st>, <cc>, <x> tags and their
                surrounding <trace>/<trace.deleted/> wrappers, keeping the inner text."""
                quote_pattern = re.compile(
                    r'(<quote\.block\b[^>]*>)(.*?)(</quote\.block\s*>)',
                    re.DOTALL | re.IGNORECASE
                )

                def _clean_block(m: re.Match) -> str:
                    open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
                    if not re.search(r'<(?:st|cc|x|url)\b', inner, re.IGNORECASE):
                        return m.group(0)

                    # Remove <trace.deleted/> that appear right before <st>/<cc>/<x>/<url> tags
                    cleaned = re.sub(
                        r'<trace\.deleted\s*/>\s*(?=<(?:st|cc|x|url)\b)',
                        '', inner, flags=re.IGNORECASE
                    )
                    # Remove <trace>...<st|cc|x|url>...</st|cc|x|url>...</trace> → keep inner text
                    cleaned = re.sub(
                        r'<trace>\s*<(st|cc|x|url)\b[^>]*>(.*?)</\1\s*>\s*</trace>',
                        r'\2', cleaned, flags=re.DOTALL | re.IGNORECASE
                    )
                    # Remove any remaining standalone <st>/<cc>/<x>/<url> tags (not wrapped in trace)
                    cleaned = re.sub(
                        r'<(st|cc|x|url)\b[^>]*>(.*?)</\1\s*>',
                        r'\2', cleaned, flags=re.DOTALL | re.IGNORECASE
                    )
                    logger.info(f"Cleaned quote.block traces: {open_tag[:60]}...")
                    return f"{open_tag}{cleaned}{close_tag}"

                return quote_pattern.sub(_clean_block, xml_content)

            def move_trace_deleted_between_matching_tags(xml_str: str) -> str:
                """
                1) Find </X> <trace.deleted/> <X ...> (same tag name X on both sides),
                and mark ONLY those <trace.deleted/> as candidates.
                2) For those candidates, move left across consecutive closing tags
                so the marker ends up right after the last text.
                3) Do nothing for patterns like </cite><trace.deleted/><footnote>.
                """

                # ---- Phase 1: mark only the good <trace.deleted/> ----
                # Match: </X>  <trace.deleted/>  <X ...>
                mark_pattern = re.compile(
                    r'(</\s*([a-zA-Z0-9\.\-:]+)\s*>)\s*'
                    r'<trace\.deleted\s*/>\s*'
                    r'(<\s*\2\b[^>]*>)',
                    re.IGNORECASE
                )

                def _mark_replacer(m: re.Match) -> str:
                    # m.group(1) = full closing tag </X>
                    # m.group(2) = tag name X
                    # m.group(3) = opening tag <X ...>
                    return f"{m.group(1)}<TRACE_TEMP/>{m.group(3)}"

                xml_str = mark_pattern.sub(_mark_replacer, xml_str)

                # ---- Phase 2: bubble TRACE_TEMP left over closing tags ----
                # One step: </Y> <TRACE_TEMP/>  -->  <TRACE_TEMP/></Y>
                bubble_pattern = re.compile(
                    r'(</[^>]+>\s*)(<TRACE_TEMP\s*/>)',
                    re.IGNORECASE
                )

                while True:
                    new_xml = bubble_pattern.sub(r'\2\1', xml_str)
                    if new_xml == xml_str:
                        break
                    xml_str = new_xml

                # ---- Phase 3: turn TRACE_TEMP back into trace.deleted ----
                xml_str = xml_str.replace('<TRACE_TEMP/>', '<trace.deleted/>')

                return xml_str
            # NEW: handle <cite> that contain trace / trace.deleted:
            # - if no trace/trace.deleted inside cite: leave cite as-is
            # - if there is trace.deleted: <trace.deleted/><st>...</st>
            # - if only trace: <st>...</st>
            # Inner <ital>/<b> are preserved inside <st>.
            def transform_cites_with_traces(xml_str: str) -> str:
                cite_pattern = re.compile(
                    r'(<cite\b[^>]*>)(.*?)(</cite>)',
                    re.IGNORECASE | re.DOTALL
                )

                def _repl(m):
                    open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
                    is_secondaryxref = bool(re.search(r'type\s*=\s*"secondaryxref"', open_tag, flags=re.IGNORECASE))
                    is_case=bool(re.search(r'type\s*=\s*"case"', open_tag, flags=re.IGNORECASE))
                    is_url=bool(re.search(r'type\s*=\s*"url"', open_tag, flags=re.IGNORECASE))

                    # 1) Check if this cite contains any trace / trace.deleted
                    has_trace = re.search(r'<trace[^>]*>', inner, flags=re.IGNORECASE)
                    has_td = re.search(r'<trace\.deleted[^>]*/>', inner, flags=re.IGNORECASE)
                    if not (has_trace or has_td):
                        # Rule 1: no trace/trace.deleted -> leave cite unchanged
                        return m.group(0)

                    # 2) Remove all trace/trace.deleted tags from inside
                    no_trace = re.sub(r'<trace[^>]*>', '', inner, flags=re.IGNORECASE)
                    no_trace = re.sub(r'</trace[^>]*>', '', no_trace, flags=re.IGNORECASE)
                    no_td = re.sub(r'<trace\.deleted[^>]*/>', '', no_trace, flags=re.IGNORECASE)

                    # 3) Strip all remaining tags (ref, para, cite, ital, b, etc.), keep text/entities
                    temp = re.sub(r'<[^>]+>', '', no_td)
                    text = re.sub(r'\s+', ' ', temp).strip()

                    # 4) If nothing left: if there was deletion, keep just <trace.deleted/>, else drop
                    if not text:
                        return '<trace.deleted/>' if has_td else ''

                    # 5) Build final replacement:
                    #    - if trace.deleted present: <trace.deleted/><st>...</st>
                    #    - else: <st>...</st>
                    if is_secondaryxref:
                        wrap_tag = 'x'
                    elif is_case:
                        wrap_tag = 'cc'
                    elif is_url:
                        wrap_tag = 'url'
                    else:
                        wrap_tag = 'st'

                    if has_td:
                        return f'<trace.deleted/><{wrap_tag}>{text}</{wrap_tag}>'
                    else:
                        return f'<{wrap_tag}>{text}</{wrap_tag}>'

                prev = None
                result = xml_str
                while prev != result:
                    prev = result
                    result = cite_pattern.sub(_repl, result)
                return result
 
            def process_traces_around_cites(cleaned_content):
                content = cleaned_content
                # Regex to find the specific target blocks: <trace><st>...</st></trace>, etc.
                target_pattern = re.compile(r'<trace><(st|x|cc|url)>(.*?)</\1></trace>', re.DOTALL)
                # Find all target occurrences and put them in a list
                targets = list(target_pattern.finditer(content))
                # Process from right to left (reversed) to prevent index shifting
                for match in reversed(targets):
                    start, end = match.span()
                    tag_name = match.group(1)      # 'st', 'x', or 'cc'
                    inner_content = match.group(2) # The text inside the tags
                    # Split the document into what's to the left and right of the CURRENT target
                    left_text = content[:start]
                    right_text = content[end:]
                    # Count all opening and closing tags to the left
                    open_left = left_text.count('<trace>')
                    close_left = left_text.count('</trace>')
                    # Count all opening and closing tags to the right
                    open_right = right_text.count('<trace>')
                    close_right = right_text.count('</trace>')
                    # Evaluate Condition: Is it inside a parent <trace>...</trace>?
                    # If open_left > close_left, there is an unclosed parent tag to the left.
                    # If close_right > open_right, there is an unmatched closing parent tag to the right.
                    if (open_left > close_left) and (close_right > open_right):
                        # It IS part of a track change. Strip the outer <trace> wrapper.
                        replacement = f"<{tag_name}>{inner_content}</{tag_name}>"
                        # Rebuild the string
                        content = content[:start] + replacement + content[end:]
                    # If the condition above fails (e.g., open_left == close_left), 
                    # it is not inside a parent tag, and the text is left exactly as is.
                return content

            def remove_formatting_inside_cite_tags(xml_content: str) -> str:
                """Remove <ital>, <b>, and other font formatting tags (both opening
                and closing) from inside <cc>, <x>, <url>, and <st> blocks."""
                formatting_tags_pattern = re.compile(
                    r'</?(?:ital|b|bold|smallcap|sup|sub|underline)\s*/?>', 
                    re.IGNORECASE
                )
                cite_tag_pattern = re.compile(
                    r'<(cc|x|url|st)\b([^>]*)>(.*?)</\1\s*>',
                    re.DOTALL | re.IGNORECASE
                )

                def _outer_repl(m):
                    tag_name = m.group(1)
                    attrs = m.group(2)
                    inner = m.group(3)
                    cleaned_inner = formatting_tags_pattern.sub('', inner)
                    cleaned_inner = re.sub(r'\s+', ' ', cleaned_inner).strip()
                    return f'<{tag_name}{attrs}>{cleaned_inner}</{tag_name}>'

                return cite_tag_pattern.sub(_outer_repl, xml_content)

 
            cleaned_content = transform_cites_with_traces(cleaned_content).replace("<st>","<trace><st>").replace("</st>","</st></trace>").replace("<x>","<trace><x>").replace("</x>","</x></trace>").replace("<cc>","<trace><cc>").replace("</cc>","</cc></trace>").replace("<url>","<trace><url>").replace("</url>","</url></trace>")
            cleaned_content = process_traces_around_cites(cleaned_content)
            cleaned_content = remove_formatting_inside_cite_tags(cleaned_content)
            cleaned_content = clean_quote_block_traces(cleaned_content)
            cleaned_content = move_trace_deleted_between_matching_tags(cleaned_content)
            # --- Pass 1.2: Repositioning wrong <para date.trace=1> insertions ---
            logger.info("--- Starting Pass 1.2: Repositioning <para date.trace=1> tags ---")
            # Using negative lookahead to prevent matching </para> if followed by </footnote>
            para_date_trace_pattern = re.compile(
                r'(<para\s+[^>]*date\.trace\s*=\s*["\']1["\'][^>]*>.*?</para>(?!\s*</footnote.body>))', 
                re.DOTALL | re.IGNORECASE
            )
            needs_processing = True
            while needs_processing:
                needs_processing = False
                # Fetch fresh matches on the currently updated content string
                para_date_matches = list(para_date_trace_pattern.finditer(cleaned_content))
                # Reverse iteration to prevent index shifting while editing within the loop
                for match in reversed(para_date_matches):
                    full_match_text = match.group(0)
                    start_index = match.start()
                    end_index = match.end()
                    text_after = cleaned_content[end_index:]
                    # Matches </para.text></para> or </list> with optional preceding whitespace
                    after_match = re.match(r'^\s*(</para\.text>\s*</para>|</list>|<trace\.deleted/></para\.text></para>)', text_after, re.IGNORECASE)
                    if after_match:
                        shift_len = after_match.end()
                        insertion_point = end_index + shift_len
                        logger.info(f"Moving <para date.trace=1>:{full_match_text} at {start_index} after closing tags at {insertion_point}")
                        part1 = cleaned_content[:start_index]
                        part2 = cleaned_content[end_index:insertion_point]
                        part3 = full_match_text
                        part4 = cleaned_content[insertion_point:]
                        cleaned_content = part1 + part2 + part3 + part4
                        # Set flag to True and break the loop to re-evaluate matches on the new content
                        needs_processing = True
                        break
            with open(output_file_path, 'w', encoding='utf-8') as file:
                file.write(cleaned_content.replace("<p.ins>","").replace("</p.ins>","").replace(" <trace.deleted/>","<trace.deleted/>").replace("<trace.deleted/><trace>","<trace.deleted/> <trace>").replace("</para.text></para><trace.deleted/>","<trace.deleted/></para.text></para>").replace("</trace><trace.deleted/> ","</trace><trace.deleted/>").replace(" </trace> ","</trace> "))

            logger.info(f"XML repositioning complete: {output_file_path}")
            return output_file_path

        except Exception as e:
            logger.error(f"Error during XML repositioning: {e}")
            return input_file_path
            
class TrackChangeProcessingSystem:
    """Main system orchestrating the entire track change processing pipeline."""
    
    def __init__(self, docx_path: str, xml_file_path: str, prompt_file_path: str, output_base_dir: str, 
                 model_name: str = "gpt-5", temperature: float = 0.2, report: Optional[PipelineReport] = None,
                 anthropic_api_key: str = ""):
        """
        Initialize the processing system.
        
        Args:
            docx_path: Path to input DOCX file
            xml_file_path: Path to input XML file
            prompt_file_path: Path to prompt text file
            output_path: Path to output directory
            report: PipelineReport instance for collecting metrics
            anthropic_api_key: Anthropic API key for fallback chain
        """
        self.docx_path = docx_path
        self.xml_file_path = xml_file_path
        self.report = report or PipelineReport()
        self.logger = logger
        self.output_base_dir = Path(output_base_dir)
        self.temp_dir = self.output_base_dir / "temp"

        self.model_name = model_name

        self.temperature = temperature

        self.anthropic_api_key = anthropic_api_key
        # Ensure directories exist

        self.output_base_dir.mkdir(parents=True, exist_ok=True)

        self.temp_dir.mkdir(parents=True, exist_ok=True)



        self.image_output_folder = self.temp_dir / "document_images"

        self.image_output_folder.mkdir(parents=True, exist_ok=True)



        self.logger.info(f"System initialized. Output will be in: {self.output_base_dir}")

        self.logger.info(f"Temporary files will be in: {self.temp_dir}")
        try:
            with open(prompt_file_path, 'r',encoding='utf-8') as file:
                prompt = file.read()
        except FileNotFoundError:
            logger.info(f"Error: The file '{prompt_file_path}' was not found.")
        except Exception as e:
            logger.info(f"An error occurred: {e}")
        self.system_prompt = prompt
        
    def run_complete_pipeline(self, progress_signal=None):
        """
        Run the complete track change processing pipeline.
        
        Returns:
            Dict containing all output file paths
        """
        logger.info("=== Starting Complete Track Change Processing Pipeline ===")
        def emit_progress(value, message):

            """Helper to safely emit progress."""

            if progress_signal:

                progress_signal.emit(value, message)

            self.logger.info(message)



        emit_progress(0, "=== Starting Complete Track Change Processing Pipeline ===")
        self.report.start_pipeline()
        
        output_files = {}
        
        try:
            # Step 1: Extract and process DOCX
            logger.info("Step 1: Processing DOCX file")
            emit_progress(5, "Step 1: Processing DOCX file")
            docx_processor = DOCXProcessor(self.docx_path)
            self.report.start_step("Step 1: Processing DOCX file")
            doc_tree, footnote_tree = docx_processor.extract_xml_from_docx()
            output_files['manual_footnotes'] = self.temp_dir / 'manual_footnotes.txt'
            docx_processor.extract_manual_footnotes(output_files['manual_footnotes'])
            
            # Extract text with track changes
            raw_text = docx_processor.extract_full_text_with_track_changes()
            with open("before_flattening.xml", 'w', encoding='utf-8') as f:
                f.write(raw_text)
            # Clean and process text
            cleaned_text = TextProcessor.remove_field_code_artifacts(raw_text)
            cleaned_text = TextProcessor.clean_text(cleaned_text)
            # with open("before_flattening.xml", 'w', encoding='utf-8') as f:
            #     f.write(cleaned_text)
            cleaned_text = cleaned_text.replace("<para></para>","").replace("</DEL_MARKER></para><para><DEL_MARKER>"," ")
            flattened_text = TextProcessor.flatten_trace_tags(cleaned_text)
            flattened_text = re.sub(r'(<footnote><para>\d+)\s+(?=[a-zA-Z])', r'\1', flattened_text)
            flattened_text = flattened_text.replace("</DEL_MARKER><DEL_MARKER>","").replace("</DEL_MARKER> <DEL_MARKER>"," ").replace("<trace> </trace>","").replace("<para></para>","").replace("<para> </para>","").replace("<para> <trace>","<para><trace>")
            pattern = r'<trace>\s*(?:<[^>]+>\s*)*<\/trace>'

            flattened_text = re.sub(pattern, '', flattened_text)
            def format_urls_in_text(text):
                # Match URLs but stop before trailing sentence punctuation like commas or periods
                url_pattern = re.compile(r'(https?://[^\s<>"]+?)(?=[.,;:?!]*(?:\s|$))')
                def modify_url(match):
                    url = match.group(1)
                    # Rule 1: Replace '&equals;=' with '&equals;'
                    url = url.replace('&equals;=', '&equals;')
                    # Rule 2: Replace '=' with '&equals;'
                    url = url.replace('=', '&equals;')
                    # Rule 3: Replace '&' with '&amp;' ONLY if not already '&amp;' or '&equals;'
                    url = re.sub(r'&(?!amp;|equals;)', '&amp;', url)
                    return url
                # Apply the modification function only to the matched URLs
                text = url_pattern.sub(modify_url, text)

                # Escape bare '&' outside URLs (e.g. Q&A -> Q&amp;A)
                # but leave existing XML entities like &dollar; &rsquo; &amp; intact
                text = re.sub(r'&(?![a-zA-Z0-9#]+;)', '&amp;', text)

                return text
            flattened_text = format_urls_in_text(flattened_text)
            # Save flattened output
            output_files['flattened_test_doc'] = self.temp_dir / 'flattened_test_doc.txt'
            with open(output_files['flattened_test_doc'], 'w', encoding='utf-8') as f:
                f.write(flattened_text)
            logger.info(f"Saved: {output_files['flattened_test_doc']}")
            
            # Resolve nested deletions
            def resolve_nested_deletions(match):
                inner_content = match.group(1)
                processed_inner_content = re.sub(
                    r"<DEL_MARKER>.*?</DEL_MARKER>",
                    "<trace.deleted/>",
                    inner_content,
                    flags=re.DOTALL
                )
                return f"<trace>{processed_inner_content}</trace>"

            text_with_resolved_nesting = re.sub(
                r"<trace>(.*?)</trace>",
                resolve_nested_deletions,
                flattened_text,
                flags=re.DOTALL
            )

            final_flattened_text = re.sub(
                r"<DEL_MARKER>.*?</DEL_MARKER>",
                "<trace.deleted/>",
                text_with_resolved_nesting,
                flags=re.DOTALL
            )
            self.report.end_step("Step 1: Processing DOCX file")

            # Step 2: Create mapper instructions
            logger.info("Step 2: Creating mapper instructions")
            emit_progress(15, "Step 2: Creating mapper instructions")
            mapper_instructions = MapperGenerator.create_mapper_instructions(text_with_resolved_nesting)
            
            output_files['mapper_json'] = self.temp_dir / 'mapper.json'
            Path(output_files['mapper_json']).write_text(
                json.dumps(mapper_instructions, indent=2, ensure_ascii=False), 
                encoding="utf-8"
            )
            logger.info(f"Saved: {output_files['mapper_json']}")
            self.report.total_track_changes = len(mapper_instructions)
            self.report.total_insertions = sum(1 for m in mapper_instructions if m.get("type") == "insertion")
            self.report.total_deletions = sum(1 for m in mapper_instructions if m.get("type") == "deletion")
            self.report.end_step("Step 2: Creating mapper instructions", {
                "total_track_changes": self.report.total_track_changes,
                "insertions": self.report.total_insertions,
                "deletions": self.report.total_deletions,
            })
            # Step 3: Process original XML
            logger.info("Step 3: Processing original XML")
            self.report.start_step("Step 3: Processing original XML")
            emit_progress(20, "Step 3: Processing original XML")
            if not Path(self.xml_file_path).exists():
                logger.error(f"XML file not found: {self.xml_file_path}")
                return output_files
                
            xml_content = Path(self.xml_file_path).read_text(encoding='utf-8').replace("\n","").replace("\t","").replace("<sep/>","__sep__").replace("<label date.updated=\"0\">&sect;</label>","<label date.updated=\"0\"><sect></label>")
            xml_without_tags = re.sub(r'<[^>]+>', '', xml_content)
            with open("xml_without_tags_original.xml", 'w', encoding='utf-8') as f:
                f.write(re.sub(r'<[^>]+>', '', xml_content))
            self.report.end_step("Step 3: Processing original XML")
            # Step 4: Apply insertions and deletions
            logger.info("Step 4: Applying track changes to XML")
            self.report.start_step("Step 4: Applying track changes to XML")
            emit_progress(25, "Step 4: Applying track changes to XML")
            
            # Separate insertions and deletions
            insertions = [instr for instr in mapper_instructions if instr['type'] == 'insertion']
            deletions = [instr for instr in mapper_instructions if instr['type'] == 'deletion']

            # Stage 1: Apply insertions
            logger.info("Step 4a: Applying insertions")
            self.report.start_step("Step 4a: Applying insertions")
            emit_progress(30, "Step 4a: Applying insertions")
            if insertions:
                updater = XMLUpdater(xml_content, insertions)
                xml_after_insertions = updater.run_updates()
            else:
                xml_after_insertions = xml_content
            self.report.end_step("Step 4a: Applying insertions", {"insertion_count": len(insertions)})    
            # Stage 2: Apply deletions
            logger.info("Step 4b: Applying deletions")
            self.report.start_step("Step 4b: Applying deletions")
            emit_progress(40, "Step 4b: Applying deletions")
            if deletions:
                deleter = XMLDeleter(xml_after_insertions, deletions, all_instructions=mapper_instructions)
                xml_after_deletions = deleter.run_deletions()
            else:
                xml_after_deletions = xml_after_insertions
            self.report.end_step("Step 4b: Applying deletions", {"deletion_count": len(deletions)})
            # Clean up multiple deletion markers
            xml_after_deletions = re.sub(
                r"<trace\.deleted/>\s*(?:<trace\.deleted/>\s*)+", 
                "<trace.deleted/>", 
                xml_after_deletions, 
                flags=re.IGNORECASE
            )
            xml_after_deletions = xml_after_deletions.replace("<label date.updated=\"0\"><sect></label>","<label date.updated=\"0\">&sect;</label>")
            # Save intermediate results
            output_files['xml_without_tags_updated'] = self.temp_dir / 'xml_with_tags_updated.txt'
            with open(output_files['xml_without_tags_updated'], 'w', encoding='utf-8') as f:
                f.write(re.sub(r'<[^>]+>', '', xml_after_deletions))
            logger.info(f"Saved: {output_files['xml_without_tags_updated']}")

            output_files['xml_with_tags_updated'] = self.temp_dir / 'xml_with_tags_updated.txt'
            with open(output_files['xml_with_tags_updated'], 'w', encoding='utf-8') as f:
                f.write(xml_after_deletions)
            logger.info(f"Saved: {output_files['xml_with_tags_updated']}")
            self.report.end_step("Step 4: Applying track changes to XML")

            # Step 5: GPT Processing
            logger.info("Step 5: Processing with GPT-5")
            self.report.start_step("Step 5: Processing with GPT-4")
            emit_progress(50, "Step 5: Processing with GPT-5")


            
            try:
                gpt_processor = GPTProcessor(
                    self.docx_path, 
                    output_folder=self.image_output_folder,
                    model_name=self.model_name,
                    temperature=self.temperature,
                    anthropic_api_key=self.anthropic_api_key,
                )
                
                gpt_processor.set_system_prompt(self.system_prompt)
                
                # Convert DOCX to images for GPT processing
                emit_progress(55, "Step 5a: Converting DOCX to images")
                gpt_processor.convert_docx_to_images()

                # Store the clean PDF path for validation later
                output_files['clean_pdf'] = getattr(gpt_processor, 'clean_pdf_path', '')
                
                self.report.llm_model = "gpt-5"
                self.report.set_cost_rates(
                    cost_per_1k_prompt=0.00125,
                    cost_per_1k_completion=0.010,
                )
                
                # Process track changes with GPT, using context from the intermediate XML
                emit_progress(60, "Step 5b: Calling LLM API with fallback (this may take a while)...")
                updated_mapper = gpt_processor.process_track_changes(mapper_instructions, None, xml_after_deletions, report=self.report)
                
                output_files['updated_mapper'] = self.temp_dir / 'updated_mapper.json'
                with open(output_files['updated_mapper'], 'w', encoding='utf-8') as f:
                    json.dump(updated_mapper, f, indent=2, ensure_ascii=False)
                logger.info(f"Saved: {output_files['updated_mapper']}")

                if getattr(gpt_processor, 'content_drops', None):
                    content_drops_path = self.temp_dir / 'content_drops.json'
                    with open(content_drops_path, 'w', encoding='utf-8') as f:
                        json.dump(gpt_processor.content_drops, f, indent=4, ensure_ascii=False)
                    logger.warning(f"Content drop report saved: {content_drops_path} ({len(gpt_processor.content_drops)} segments)")

                emit_progress(70, "Step 5: GPT processing complete.")
                
            except Exception as e:
                logger.warning(f"GPT processing failed: {e}. Using original mapper.")
                updated_mapper = mapper_instructions
            
            self.report.end_step("Step 5: Processing with GPT-4 (LLM)", {
                "total_llm_calls": self.report.total_llm_calls,
                "llm_succeeded": self.report.llm_calls_succeeded,
                "llm_failed": self.report.llm_calls_failed,
                "llm_skipped": self.report.llm_calls_skipped,
                "document_pages": self.report.document_pages,
            })

            # Step 6: Create insertion analysis
            logger.info("Step 6: Creating insertion analysis")
            self.report.start_step("Step 6: Creating insertion analysis")
            insertion_data = FootnoteAnalyzer.create_insertion_json(final_flattened_text)
            
            output_files['insertion_output_json'] = self.temp_dir / 'insertion_output.json'
            Path(output_files['insertion_output_json']).write_text(
                json.dumps(insertion_data, indent=2, ensure_ascii=False), 
                encoding="utf-8"
            )
            logger.info(f"Saved: {output_files['insertion_output_json']}")
            self.report.end_step("Step 6: Creating insertion analysis")


            # Step 7: Post-processing with footnote logic
            logger.info("Step 7: Applying post-processing")
            self.report.start_step("Step 7: Applying post-processing")
            emit_progress(80, "Step 7: Applying post-processing")
            
            xml_with_footnote_logic = PostProcessor.update_xml_with_footnote_logic(
                xml_after_deletions, updated_mapper, insertion_data
            )
            
            output_files['output_XML_before_repositioning'] = self.temp_dir / 'output_XML_before_repositioning.xml'
            with open(output_files['output_XML_before_repositioning'], 'w', encoding='utf-8') as f:
                f.write(xml_with_footnote_logic)
            logger.info(f"Saved: {output_files['output_XML_before_repositioning']}")
            self.report.end_step("Step 7: Applying post-processing")
            # Step 8: Final repositioning
            logger.info("Step 8: Final XML repositioning")
            self.report.start_step("Step 8: Final XML repositioning")
            emit_progress(90, "Step 8: Final XML repositioning")


            
            final_output_path_temp = PostProcessor.process_xml_repositioning(

                output_files['output_XML_before_repositioning']

            )

            # Copy to final output name

            output_files['Final_output_XML'] = self.temp_dir / 'Final_output_XML.txt'
            if final_output_path_temp != output_files['output_XML_before_repositioning']:

                shutil.copy2(final_output_path_temp, output_files['Final_output_XML'])
            else:
                shutil.copy2(output_files['output_XML_before_repositioning'], output_files['Final_output_XML'])
            
            logger.info(f"Saved: {output_files['Final_output_XML']}") 
            self.report.end_step("Step 8: Final XML repositioning")

            # Step 9: Fix spacing around <trace.deleted/> using flattened text
            logger.info("Step 9: Fixing spacing around <trace.deleted/> using flattened text")
            self.report.start_step("Step 9: Fixing spacing around <trace.deleted/> using flattened text")
            try:
                final_xml_path = output_files['Final_output_XML']
                flattened_path = output_files['flattened_test_doc']

                with open(final_xml_path, 'r', encoding='utf-8') as f:
                    final_xml_text = f.read()
                with open(flattened_path, 'r', encoding='utf-8') as f:
                    flattened_text = f.read()

                fixed_xml_text = fix_trace_deleted_spacing_using_flattened(final_xml_text, flattened_text)

                timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
                output_files['Final_output_XML_fixed'] = self.output_base_dir / f'Final_output_XML_fixed_{timestamp}.xml'
                with open(output_files['Final_output_XML_fixed'], 'w', encoding='utf-8') as f:
                    f.write(fixed_xml_text)

                logger.info(f"Saved: {output_files['Final_output_XML_fixed']}")
            except Exception as e:
                logger.warning(f"Spacing fix step failed: {e}")

            self.report.end_step("Step 9: Fixing spacing around trace.deleted")
            self.report.end_pipeline()
            logger.info("=== Pipeline completed successfully ===")
            logger.info(f"Total execution time: {self.report.total_execution_time_seconds}s")
            emit_progress(100, f"Step 10: Final XML saved to {output_files['Final_output_XML']}")
            logger.info("Generated files:")
            for key, path in output_files.items():
                logger.info(f"  {key}: {path}")
                
            return output_files
            
        except Exception as e:
            logger.error(f"Pipeline failed with error: {e}")
            self.report.end_pipeline()
            emit_progress(100, f"Pipeline FAILED: {e}")
            raise

# -------------------------------------------------------------------
SENTINEL = "\uFFF0"  # placeholder for each deletion
def build_flat_profile(flat_text: str) -> Tuple[str, List[int], List[Dict[str, object]]]:
    """
    Replace each <DEL_MARKER>...</DEL_MARKER> with SENTINEL, strip other tags,
    and record spacing/neighbor info + context for each deletion in order.
    """
    # Collapse deletions to a single sentinel each
    def _del_marker_repl(m: re.Match) -> str:
        # content inside <DEL_MARKER>...</DEL_MARKER>
        content = m.group(1)
        has_left_space = bool(content and content[0].isspace())
        has_right_space = bool(content and content[-1].isspace())

        # Move boundary spaces *outside* as context for SENTINEL
        replacement = ""
        if has_left_space:
            replacement += " "
        replacement += SENTINEL
        if has_right_space:
            replacement += " "
        return replacement

    no_del = re.sub(
        r"<DEL_MARKER>(.*?)</DEL_MARKER>",
        _del_marker_repl,
        flat_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove all other tags
    plain = re.sub(r"<[^>]+>", "", no_del)

    positions = [i for i, ch in enumerate(plain) if ch == SENTINEL]
    info_list: List[Dict[str, object]] = []

    for pos in positions:
        n = len(plain)

        # Look left
        i = pos - 1
        flat_L_space = False
        flat_L_char: Optional[str] = None
        while i >= 0:
            ch = plain[i]
            if ch == SENTINEL:
                i -= 1
                continue
            if ch.isspace():
                flat_L_space = True
                i -= 1
                continue
            flat_L_char = ch
            break

        # Look right
        i = pos + 1
        flat_R_space = False
        flat_R_char: Optional[str] = None
        while i < n:
            ch = plain[i]
            if ch == SENTINEL:
                i += 1
                continue
            if ch.isspace():
                flat_R_space = True
                i += 1
                continue
            flat_R_char = ch
            break

        midword = (
            not flat_L_space
            and not flat_R_space
            and flat_L_char is not None
            and flat_R_char is not None
            and flat_L_char.isalnum()
            and flat_R_char.isalnum()
        )

        #If there is space on BOTH sides (and not mid-word),
        # drop the left space and keep only the right.
        eff_L_space = flat_L_space
        eff_R_space = flat_R_space
        if not midword and flat_L_space and flat_R_space:
            eff_L_space = False
            eff_R_space = True

        # Build context snippets for matching (use plain text with spaces normalized)
        window = 40
        start = max(0, pos - window)
        end = min(len(plain), pos + 1 + window)
        left_ctx_raw = plain[start:pos]
        right_ctx_raw = plain[pos + 1:end]
        left_ctx = re.sub(r"\s+", " ", left_ctx_raw)[-window:]
        right_ctx = re.sub(r"\s+", " ", right_ctx_raw)[:window]

        info_list.append(
            {
                "L_space": eff_L_space,
                "R_space": eff_R_space,
                "L_char": flat_L_char,
                "R_char": flat_R_char,
                "midword": midword,
                "left_ctx": left_ctx,
                "right_ctx": right_ctx,
            }
        )

    return plain, positions, info_list


def split_xml_on_trace_deleted(xml: str) -> List[str]:
    """
    Split XML into [text, <trace.deleted/>, text, <trace.deleted/>, text, ...],
    normalizing the tag to <trace.deleted/>.
    """
    tag_re = re.compile(r"<trace\.deleted\s*/>", flags=re.IGNORECASE)
    parts: List[str] = []
    last = 0
    for m in tag_re.finditer(xml):
        parts.append(xml[last:m.start()])
        parts.append("<trace.deleted/>")
        last = m.end()
    parts.append(xml[last:])
    return parts


def get_last_visible_char_and_space(s: str) -> Tuple[Optional[str], bool]:
    """
    From the end of s, ignoring tags, find:
    - last visible non-space char
    - whether there is visible whitespace at the very end before the tag boundary.
    """
    in_tag = False
    last_char: Optional[str] = None
    has_trailing_space = False
    seen_nonspace = False

    i = len(s) - 1
    while i >= 0:
        ch = s[i]
        if in_tag:
            if ch == "<":
                in_tag = False
            i -= 1
            continue
        if ch == ">":
            in_tag = True
            i -= 1
            continue
        # Outside tags
        if ch.isspace():
            if not seen_nonspace:
                has_trailing_space = True
            i -= 1
            continue
        last_char = ch
        seen_nonspace = True
        break

    return last_char, has_trailing_space


def get_first_visible_char_and_space(s: str) -> Tuple[Optional[str], bool]:
    """
    From the start of s, ignoring tags, find:
    - first visible non-space char
    - whether there is visible whitespace immediately after the tag boundary.
    """
    in_tag = False
    first_char: Optional[str] = None
    has_leading_space = False
    seen_nonspace = False

    i = 0
    while i < len(s):
        ch = s[i]
        if in_tag:
            if ch == ">":
                in_tag = False
            i += 1
            continue
        if ch == "<":
            in_tag = True
            i += 1
            continue
        # Outside tags
        if ch.isspace():
            if not seen_nonspace:
                has_leading_space = True
            i += 1
            continue
        first_char = ch
        seen_nonspace = True
        break

    return first_char, has_leading_space


def build_xml_profile(xml: str, window: int = 40) -> Tuple[str, List[int], List[Dict[str, str]]]:
    """
    Build a plain-text representation where each <trace.deleted/> is a SENTINEL,
    plus context snippets around each deletion for matching.
    """
    temp = re.sub(r"<trace\.deleted\s*/>", SENTINEL, xml, flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", temp)
    positions = [i for i, ch in enumerate(plain) if ch == SENTINEL]
    ctx_list: List[Dict[str, str]] = []

    for pos in positions:
        start = max(0, pos - window)
        end = min(len(plain), pos + 1 + window)
        left_ctx_raw = plain[start:pos]
        right_ctx_raw = plain[pos + 1:end]
        left_ctx = re.sub(r"\s+", " ", left_ctx_raw)[-window:]
        right_ctx = re.sub(r"\s+", " ", right_ctx_raw)[:window]
        ctx_list.append({"left_ctx": left_ctx, "right_ctx": right_ctx})

    return plain, positions, ctx_list


def _common_suffix(a: str, b: str) -> int:
    max_len = min(len(a), len(b))
    count = 0
    for i in range(1, max_len + 1):
        if a[-i] != b[-i]:
            break
        count += 1
    return count


def _common_prefix(a: str, b: str) -> int:
    max_len = min(len(a), len(b))
    count = 0
    for i in range(max_len):
        if a[i] != b[i]:
            break
        count += 1
    return count


def compute_mapping(
    flat_info: List[Dict[str, object]],
    xml_ctx_list: List[Dict[str, str]],
    window: int = 10,
) -> List[Optional[int]]:
    """
    For each XML <trace.deleted/>, find the best matching flattened <DEL_MARKER>
    based on left/right context (not just index).
    Returns a list mapping xml_index -> flat_index (or None).
    """
    mapping: List[Optional[int]] = [None] * len(xml_ctx_list)
    used_flat = set()

    for j, xml_ctx in enumerate(xml_ctx_list):
        best_i = None
        best_score = -1
        left_x = xml_ctx.get("left_ctx", "")
        right_x = xml_ctx.get("right_ctx", "")

        # Only search in a window around j to avoid bad global matches
        start_i = max(0, j - window)
        end_i = min(len(flat_info), j + window + 1)

        for i in range(start_i, end_i):
            if i in used_flat:
                continue
            left_f = str(flat_info[i].get("left_ctx", ""))
            right_f = str(flat_info[i].get("right_ctx", ""))
            score = _common_suffix(left_f, left_x) + _common_prefix(right_f, right_x)
            if score > best_score:
                best_score = score
                best_i = i

        # If no strong match, fall back to order-based, but avoid reusing
        if best_i is None or best_score <= 0:
            if j < len(flat_info) and j not in used_flat:
                best_i = j
            else:
                for i in range(len(flat_info)):
                    if i not in used_flat:
                        best_i = i
                        break

        if best_i is not None:
            mapping[j] = best_i
            used_flat.add(best_i)

    return mapping


def fix_trace_deleted_spacing_using_flattened(xml: str, flat_text: str) -> str:
    """
    Adjust spaces around <trace.deleted/> in xml to match the
    spacing patterns inferred from the flattened text, using context matching.
    """
    # Profile flattened text
    _, flat_positions, flat_info = build_flat_profile(flat_text)

    # Normalize XML tag variants and build XML profile
    xml_norm = re.sub(r"<trace\.deleted\s*/>", "<trace.deleted/>", xml, flags=re.IGNORECASE)
    _, xml_positions, xml_ctx_list = build_xml_profile(xml_norm)

    if not flat_positions or not xml_positions:
        # Nothing to do if either side has no deletions
        return xml_norm

    if len(flat_positions) != len(xml_positions):
        print(
            f"Warning: flattened has {len(flat_positions)} deletions, XML has "
            f"{len(xml_positions)} <trace.deleted/> tags. Using context-based mapping."
        )

    parts = split_xml_on_trace_deleted(xml_norm)
    num_tags = (len(parts) - 1) // 2

    # Compute mapping from each XML deletion to a flattened deletion
    mapping = compute_mapping(flat_info, xml_ctx_list)
    max_pairs = min(num_tags, len(mapping))

    for j in range(max_pairs):
        flat_index = mapping[j]
        if flat_index is None or flat_index >= len(flat_info):
            continue

        left_idx = 2 * j
        tag_idx = 2 * j + 1
        right_idx = 2 * j + 2

        left = parts[left_idx]
        tag = parts[tag_idx]  # always "<trace.deleted/>"
        right = parts[right_idx]

        fi = flat_info[flat_index]
        flat_L_space: bool = bool(fi["L_space"])
        flat_R_space: bool = bool(fi["R_space"])
        midword: bool = bool(fi["midword"])

        # Inspect current XML context
        _, left_has_space = get_last_visible_char_and_space(left)
        _, right_has_space = get_first_visible_char_and_space(right)

        if midword:
            # Deletion is inside a word: no spaces on either side.
            left = re.sub(r"\s+$", "", left)
            right = re.sub(r"^\s+", "", right)
        else:
            # Left side: ensure space presence/absence matches flattened (with our rule).
            if flat_L_space and not left_has_space:
                left = left + " "
            elif not flat_L_space and left_has_space:
                left = re.sub(r"\s+$", "", left)

            # Right side: ensure space presence/absence matches flattened.
            if flat_R_space and not right_has_space:
                right = " " + right
            elif not flat_R_space and right_has_space:
                right = re.sub(r"^\s+", "", right)

        parts[left_idx] = left
        parts[tag_idx] = tag
        parts[right_idx] = right

    result = "".join(parts)

    # Final safety pass: never allow a space before punctuation
    # immediately after a <trace.deleted/> tag.
    result = re.sub(
        r"<trace\.deleted\s*/>\s+([.,;:?!])",
        r"<trace.deleted/>\1",
        result,
    )

    return result

class SmartXMLChunker:
    """Token-based XML chunker with structural awareness."""

    _EVENT_RE = re.compile(r"<(/?)(para|section|trace)\b[^>]*?>", re.IGNORECASE)
    _OPEN_ANCHOR_RE = re.compile(r"<(para|section|trace)\b[^>]*>", re.IGNORECASE)

    def __init__(self, target_tokens: int = 20000, overlap_tokens: int = 1000, model: str = "gpt-4"):
        self.target_tokens = int(target_tokens)
        self.overlap_tokens = int(overlap_tokens)
        self._use_tiktoken = False
        try:
            self.encoding = tiktoken.encoding_for_model(model)
            self._use_tiktoken = True
        except (KeyError, Exception):
            try:
                self.encoding = tiktoken.get_encoding("cl100k_base")
                self._use_tiktoken = True
            except Exception:
                logger.warning("tiktoken unavailable (network issue). Using char-based token estimation.")
                self.encoding = None
                self._use_tiktoken = False

    def _encode(self, text: str) -> List[int]:
        if self._use_tiktoken:
            return self.encoding.encode(text)
        # Fallback: treat every 4 chars as 1 token, but store char indices
        return list(range(0, len(text), 4))

    def _decode(self, tokens: List[int]) -> str:
        if self._use_tiktoken:
            return self.encoding.decode(tokens)
        # Fallback: tokens are char-position indices (every 4th char)
        # Use the stored _fallback_text to slice
        if not tokens:
            return ""
        start_char = tokens[0]
        end_char = tokens[-1] + 4
        return self._fallback_text[start_char:end_char] if hasattr(self, '_fallback_text') else ""

    def count_tokens(self, text: str) -> int:
        if self._use_tiktoken:
            return len(self.encoding.encode(text))
        return len(text) // 4

    def _scan_last_safe_break(self, text: str) -> Optional[int]:
        stack: List[str] = []
        last_safe: Optional[int] = None
        for m in self._EVENT_RE.finditer(text):
            is_close = (m.group(1) == "/")
            tag = m.group(2).lower()
            if not is_close:
                stack.append(tag)
            else:
                if stack and stack[-1] == tag:
                    stack.pop()
                if not stack:
                    last_safe = m.end()
        return last_safe

    def _adjust_start_to_opening_tag(self, all_tokens: List[int], desired_start: int, lookback: int = 2000) -> int:
        if desired_start <= 0:
            return 0
        window_start = max(0, desired_start - lookback)
        window_text = self._decode(all_tokens[window_start:desired_start])
        anchors = list(self._OPEN_ANCHOR_RE.finditer(window_text))
        if not anchors:
            return desired_start
        last_anchor = anchors[-1]
        suffix = window_text[last_anchor.start():]
        suffix_tokens = len(self._encode(suffix))
        return max(window_start, desired_start - suffix_tokens)

    def chunk_xml(self, xml: str) -> List[Dict]:
        if not self._use_tiktoken:
            self._fallback_text = xml  # Store for _decode fallback
        all_tokens = self._encode(xml)
        n = len(all_tokens)
        chunks: List[Dict] = []
        start_tok = 0
        chunk_id = 0
        hard_max = max(self.target_tokens * 3, self.target_tokens + 2000)

        while start_tok < n:
            ideal_end = min(n, start_tok + self.target_tokens)
            end_tok = ideal_end
            extended = False

            while True:
                text = self._decode(all_tokens[start_tok:end_tok])
                safe_char = self._scan_last_safe_break(text)
                if safe_char is not None:
                    cut_len = len(self._encode(text[:safe_char]))
                    if cut_len > 0:
                        end_tok = start_tok + cut_len
                        break
                if end_tok >= n:
                    break
                if (end_tok - start_tok) >= hard_max:
                    end_tok = min(n, start_tok + self.target_tokens)
                    break
                end_tok = min(n, end_tok + 250)
                extended = True

            chunk_text = self._decode(all_tokens[start_tok:end_tok])
            chunks.append({
                "chunk_id": chunk_id,
                "text": chunk_text,
                "start_token": start_tok,
                "end_token": end_tok,
                "token_count": (end_tok - start_tok),
                "extended_to_close": extended,
            })

            if end_tok >= n:
                break

            desired_next = max(0, end_tok - self.overlap_tokens)
            next_start = self._adjust_start_to_opening_tag(all_tokens, desired_next)
            # Prevent _adjust from going too far from desired position
            if next_start < max(0, desired_next - 200):
                next_start = desired_next
            # Ensure forward progress: if next_start didn't advance past
            # the current chunk's start, jump to end of current chunk.
            # This prevents both gaps (old bug) and tiny-increment looping.
            if next_start <= start_tok:
                next_start = end_tok
            start_tok = next_start
            chunk_id += 1

        return chunks

    def xml_to_norm(self, s: str) -> str:
        """Public normalized text for comparison."""
        s = html.unescape(s or "")
        s = re.sub(r"<[^>]+>", " ", s)
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s


def _clean_for_pdf_search(text: str) -> str:
    """
    Aggressively clean mapper text so it can be matched against raw PDF text.
    Handles HTML entities, XML tags, and normalizes whitespace.
    """
    if not text:
        return ""
    # Strip XML/HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    entity_map = {
        '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
        '&apos;': "'", '&lsquo;': "'", '&rsquo;': "'",
        '&ldquo;': '"', '&rdquo;': '"',
        '&nbsp;': ' ', '&ndash;': '-', '&mdash;': '-',
        '&sect;': '§', '&dblsect;': '§§', '&para;': '¶',
        '&dollar;': '$', '&lsqb;': '[', '&rsqb;': ']',
        '&ast;': '*', '&num;': '#', '&percnt;': '%',
        '&key;': '', '&comma;': ',',
    }
    for ent, repl in entity_map.items():
        text = text.replace(ent, repl)
    # Catch remaining entities
    text = re.sub(r'&\w+;', ' ', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Replace BOTH normalization functions with a single consistent one:

def _normalize_for_comparison(text: str) -> str:
    """
    Single consistent normalization used for BOTH XML chunks AND search text.
    Must match SmartXMLChunker.xml_to_norm exactly.
    """
    if not text:
        return ""
    # Step 1: Strip XML/HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Step 2: Decode ALL HTML entities (same as html.unescape)
    text = html.unescape(text)
    # Step 3: Also handle any remaining & entities manually
    text = re.sub(r'&\w+;', ' ', text)
    # Step 4: Lowercase
    text = text.lower()
    # Step 5: Keep only alphanumeric, replace everything else with space
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    # Step 6: Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


class ClaudeTrackChangeValidator:
    """Validate applied track changes against page images + final XML using Claude."""

    def __init__(
        self,
        mapper: list[dict],
        final_xml: str,
        gpt_processor,
        claude_api_key: str,
        claude_model: str = MODEL_OPUS,
        claude_api_url: str = "https://api.anthropic.com/v1/messages",
        max_tokens: int = 1024,
        timeout: int = 600,
        output_dir: str = None,
        validation_prompt_path: str = None,
        max_workers: int = 5,
    ) -> None:
        self.mapper = mapper
        self.final_xml = final_xml
        self.gpt_processor = gpt_processor
        self.api_key = claude_api_key
        self.model = claude_model
        self.api_url = claude_api_url
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.chunker = SmartXMLChunker(
            target_tokens=6000,
            overlap_tokens=1000,
            model="gpt-4"
        )
        # Cached chunks and page index
        self._final_xml_chunks = None
        self._page_text_normalized = None

        self._fallback_handler = LLMFallbackHandler.build_validation_chain(
            anthropic_api_key=claude_api_key,
        )
        logger.info("[ClaudeValidator] Fallback chain: %s",
                    " -> ".join(c.model_name for c in self._fallback_handler.fallback_chain))

        if validation_prompt_path:
            try:
                with open(validation_prompt_path, 'r', encoding='utf-8') as file:
                    self.validation_prompt_template = file.read()
                logger.info("[ClaudeValidator] Loaded validation prompt from: %s", validation_prompt_path)
            except FileNotFoundError:
                logger.error("[ClaudeValidator] Validation prompt file not found: %s", validation_prompt_path)
                raise
            except Exception as e:
                logger.error("[ClaudeValidator] Error reading validation prompt file: %s", e)
                raise
        else:
            raise ValueError("validation_prompt_path is required for ClaudeTrackChangeValidator")

        logger.info("[ClaudeValidator] Initialized with %d mapper entries", len(mapper))

    # ------------------------------------------------------------------ helpers

    def _log_snippet(self, label: str, text: str, max_len: int = 200) -> None:
        if not text:
            logger.info("[ClaudeValidator] %s: <empty>", label)
            return
        snippet = text[:max_len].replace("\n", " ")
        snippet = snippet.encode('ascii', 'replace').decode('ascii')
        logger.info("[ClaudeValidator] %s (len=%d): %s%s",
                    label, len(text), snippet, "..." if len(text) > max_len else "")

    def _ensure_chunks(self):
        """Lazily build and cache XML chunks with normalized text."""
        if self._final_xml_chunks is not None:
            return
        logger.info("[ClaudeValidator] Creating XML chunks...")
        self._final_xml_chunks = self.chunker.chunk_xml(self.final_xml)
        for ch in self._final_xml_chunks:
            # USE THE SAME normalization for chunks as for search queries
            ch["norm"] = _normalize_for_comparison(ch["text"])
        logger.info("[ClaudeValidator] Created %d XML chunks", len(self._final_xml_chunks))

    def _ensure_page_index(self):
        """Build normalized page text cache for reliable page search."""
        if self._page_text_normalized is not None:
            return
        self._page_text_normalized = {}
        for page_num, page_text in self.gpt_processor.doc_text_by_page.items():
            # USE THE SAME normalization
            norm = _normalize_for_comparison(page_text)
            self._page_text_normalized[page_num] = norm
        logger.info("[ClaudeValidator] Built normalized page index for %d pages",
                    len(self._page_text_normalized))

    # --------------------------------------------------------- page finding (FIX #1)

    # ...existing code in ClaudeTrackChangeValidator...

    def _find_pages_for_segment(self, mapper_item: dict) -> List[int]:
        """
        Find the correct PDF page(s) for a track change segment.
        Uses previous content as anchor since it exists in the original PDF.
        """
        self._ensure_page_index()

        change_type = mapper_item.get("type", "")
        prev_content = mapper_item.get("Previous Content", "")
        tc_text = (mapper_item.get("Track changes", "")
                   if change_type == "insertion"
                   else mapper_item.get("Deleted Content", ""))

        # Build search candidates in priority order
        search_candidates = []

        # 1. Previous content (always in the original PDF)
        if prev_content:
            search_candidates.append(("previous_content", prev_content))

        # 2. For deletions, deleted text is in the original PDF
        if change_type == "deletion" and tc_text:
            search_candidates.append(("deleted_text", tc_text))

        # 3. Combined: prev_content + track_change
        if prev_content and tc_text:
            search_candidates.append(("combined", prev_content + " " + tc_text))

        # 4. For insertions, try the inserted text as a last resort
        if change_type == "insertion" and tc_text and len(tc_text) > 50:
            search_candidates.append(("insertion_text", tc_text))

        best_pages = []
        best_score = 0
        best_strategy = "none"

        for strategy_name, raw_text in search_candidates:
            norm_text = _normalize_for_comparison(raw_text)
            if not norm_text or len(norm_text.split()) < 3:
                continue

            pages = self._search_pages_by_phrases(norm_text)
            if pages:
                score = pages[0][1]
                logger.info("[PageSearch] Strategy '%s': pages %s (score: %.2f)",
                           strategy_name, [p + 1 for p, _ in pages[:3]], score)
                if score > best_score:
                    best_score = score
                    best_pages = [p for p, _ in pages[:3]]
                    best_strategy = strategy_name

        if not best_pages:
            logger.warning("[PageSearch] No pages found via phrase search, trying word overlap")
            fallback_text = _normalize_for_comparison(prev_content or tc_text)
            best_pages = self._fallback_page_search(fallback_text)
            best_strategy = "word_overlap_fallback"

        logger.info("[PageSearch] Final pages (0-based): %s via '%s' (score: %.2f)",
                   best_pages, best_strategy, best_score)
        return best_pages

    def _search_pages_by_phrases(self, search_norm: str) -> List[Tuple[int, float]]:
        """
        Search pages using sliding-window phrase matching.
        Tries longest phrases first — a 10-word match is definitive.
        """
        words = search_norm.split()
        if len(words) < 4:
            return []

        results = []

        for page_num, page_norm in self._page_text_normalized.items():
            if not page_norm:
                continue

            best_match_len = 0

            # Try from longest window down to shortest
            for window_len in range(min(15, len(words)), 3, -1):
                if best_match_len >= window_len:
                    break  # Already found a longer match

                for i in range(len(words) - window_len + 1):
                    phrase = " ".join(words[i:i + window_len])
                    if phrase in page_norm:
                        best_match_len = max(best_match_len, window_len)
                        break  # Found match at this window size

            if best_match_len >= 4:
                # Score: longer matches score exponentially higher
                score = best_match_len * best_match_len
                results.append((page_num, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _fallback_page_search(self, search_norm: str) -> List[int]:
        """Last resort: find pages with maximum meaningful word overlap."""
        if not search_norm:
            return []

        search_words = set(search_norm.split())
        common_words = {'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'for', 'is',
                       'was', 'that', 'it', 'on', 'with', 'as', 'at', 'by', 'from',
                       'be', 'this', 'not', 'are', 'but', 'have', 'has', 'had', 'may',
                       'shall', 'will', 'can', 'should', 'would', 'could', 'its', 'an'}
        meaningful = search_words - common_words

        if not meaningful:
            return []

        page_scores = []
        for page_num, page_norm in self._page_text_normalized.items():
            page_words = set(page_norm.split())
            overlap = len(meaningful & page_words)
            if overlap > 0:
                page_scores.append((page_num, overlap))

        page_scores.sort(key=lambda x: x[1], reverse=True)

        if page_scores:
            best_score = page_scores[0][1]
            threshold = best_score * 0.7
            return [p for p, s in page_scores if s >= threshold][:3]

        return []

    # --------------------------------------------------------- chunk finding (FIX #2)

    # ...existing code in ClaudeTrackChangeValidator...

    def _find_best_chunk(self, mapper_item: dict) -> Tuple[int, float, str]:
        """
        Find the best XML chunk containing the track change location.
        """
        self._ensure_chunks()

        change_type = mapper_item.get("type", "")
        prev_content = mapper_item.get("Previous Content", "")
        tc_text = (mapper_item.get("Track changes", "")
                   if change_type == "insertion"
                   else mapper_item.get("Deleted Content", ""))

        prev_norm = _normalize_for_comparison(prev_content)
        tc_norm = _normalize_for_comparison(tc_text)

        best_idx = -1
        best_score = 0.0
        best_strategy = "none"

        # Strategy 1: Search by previous content (most reliable anchor)
        if prev_norm and len(prev_norm.split()) >= 4:
            idx, score = self._search_chunks_by_phrases(prev_norm)
            if score > best_score:
                best_idx, best_score, best_strategy = idx, score, "previous_content"
                logger.info("[ChunkFind] previous_content: chunk %d, score %.2f", idx, score)

        # Strategy 2: Search by track change text
        if tc_norm and len(tc_norm.split()) >= 4:
            idx, score = self._search_chunks_by_phrases(tc_norm)
            if score > best_score:
                best_idx, best_score, best_strategy = idx, score, "track_text"
                logger.info("[ChunkFind] track_text: chunk %d, score %.2f", idx, score)

        # Strategy 3: Combined (prev + track change — simulates the continuous text)
        if prev_norm and tc_norm:
            combined = prev_norm + " " + tc_norm
            if len(combined.split()) >= 6:
                idx, score = self._search_chunks_by_phrases(combined)
                if score > best_score:
                    best_idx, best_score, best_strategy = idx, score, "combined"
                    logger.info("[ChunkFind] combined: chunk %d, score %.2f", idx, score)

        # Strategy 4: For deletion/insertion pairs where the track text is very short,
        # try to find chunks containing the FULL previous content as a substring
        if best_score < 50 and prev_norm and len(prev_norm) > 30:
            idx, score = self._search_chunks_substring(prev_norm)
            if score > best_score:
                best_idx, best_score, best_strategy = idx, score, "substring"
                logger.info("[ChunkFind] substring: chunk %d, score %.2f", idx, score)

        # Strategy 5: Word overlap fallback
        if best_idx == -1:
            logger.info("[ChunkFind] All phrase strategies failed, using word overlap")
            search_text = prev_norm or tc_norm
            idx, score = self._search_chunks_word_overlap(search_text)
            if idx != -1:
                best_idx, best_score, best_strategy = idx, float(score), "word_overlap"

        # Strategy 6: Raw XML direct search fallback
        # If chunk search yielded very low confidence, the text may fall in a chunk gap.
        # Search the full normalized XML directly and map the position to a chunk.
        if best_score < 50 and (prev_norm or tc_norm):
            search_text = prev_norm or tc_norm
            idx, score = self._search_raw_xml_fallback(search_text)
            if score > best_score:
                best_idx, best_score, best_strategy = idx, score, "raw_xml_fallback"
                logger.info("[ChunkFind] raw_xml_fallback: chunk %d, score %.2f", idx, score)

        logger.info("[ChunkFind] FINAL: chunk %d via '%s' (score %.2f)",
                   best_idx, best_strategy, best_score)
        return best_idx, best_score, best_strategy

    def _search_chunks_by_phrases(self, search_norm: str) -> Tuple[int, float]:
        """
        Find the chunk with the longest matching phrase from the search text.
        Uses sliding windows from longest to shortest.
        """
        words = search_norm.split()
        if not words:
            return -1, 0.0

        max_window = min(20, len(words))
        min_window = min(4, len(words))

        # For each chunk, find the longest matching phrase
        chunk_best = {}  # chunk_idx -> longest_match_length

        for window_len in range(max_window, min_window - 1, -1):
            for i in range(len(words) - window_len + 1):
                phrase = " ".join(words[i:i + window_len])
                if len(phrase) < 12:  # skip very short phrases
                    continue

                for j, chunk in enumerate(self._final_xml_chunks):
                    if j in chunk_best and chunk_best[j] >= window_len:
                        continue  # already found longer match for this chunk
                    if phrase in chunk.get("norm", ""):
                        chunk_best[j] = window_len

            # If we found a match at this window size, no need to go shorter
            # (we want the chunk with the LONGEST match)
            if chunk_best:
                # But continue to see if another chunk has even longer match
                # Only stop if we've checked enough
                pass

        if not chunk_best:
            return -1, 0.0

        best_idx = max(chunk_best, key=chunk_best.get)
        best_score = chunk_best[best_idx] * chunk_best[best_idx]  # Square for emphasis
        return best_idx, best_score

    def _search_chunks_substring(self, search_norm: str) -> Tuple[int, float]:
        """
        Try to find chunks where a large portion of the search text appears as a substring.
        Progressively trims from both ends.
        """
        if len(search_norm) < 20:
            return -1, 0.0

        # Try the full string, then progressively shorter from the front
        for trim_start in range(0, len(search_norm) // 2, 10):
            candidate = search_norm[trim_start:]
            if len(candidate) < 20:
                break

            for j, chunk in enumerate(self._final_xml_chunks):
                chunk_norm = chunk.get("norm", "")
                if candidate in chunk_norm:
                    return j, len(candidate)

            # Also try trimming from end
            for trim_end in range(len(candidate), max(20, len(candidate) // 2), -10):
                sub = candidate[:trim_end]
                if len(sub) < 20:
                    break
                for j, chunk in enumerate(self._final_xml_chunks):
                    if sub in chunk.get("norm", ""):
                        return j, len(sub)

        return -1, 0.0

    def _search_chunks_word_overlap(self, search_norm: str) -> Tuple[int, int]:
        """Find chunk with maximum meaningful word overlap."""
        if not search_norm:
            return -1, 0

        search_words = set(search_norm.split())
        common_words = {'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'for', 'is',
                       'was', 'that', 'it', 'on', 'with', 'as', 'at', 'by', 'from',
                       'be', 'this', 'not', 'are', 'but', 'have', 'has', 'had', 'may',
                       'shall', 'will', 'can', 'should', 'would', 'could', 'its'}
        meaningful = search_words - common_words

        if not meaningful:
            return -1, 0

        best_idx = -1
        best_overlap = 0

        for i, chunk in enumerate(self._final_xml_chunks):
            chunk_words = set(chunk.get("norm", "").split())
            overlap = len(meaningful & chunk_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = i

        if meaningful and best_overlap / len(meaningful) < 0.3:
            return -1, 0

        return best_idx, best_overlap

    def _search_raw_xml_fallback(self, search_norm: str) -> Tuple[int, float]:
        """
        Last resort: search the full normalized XML directly for the text,
        find its character position, then map that position to the nearest chunk.
        This handles cases where content falls in a gap between chunks.
        """
        if not search_norm or len(search_norm) < 15:
            return -1, 0.0

        # Build full normalized XML (cached)
        if not hasattr(self, '_full_xml_norm'):
            self._full_xml_norm = _normalize_for_comparison(self.final_xml)

        # Try sliding windows from longest to shortest
        words = search_norm.split()
        found_phrase = None
        found_pos = -1

        for window_len in range(min(15, len(words)), 3, -1):
            for i in range(len(words) - window_len + 1):
                phrase = " ".join(words[i:i + window_len])
                pos = self._full_xml_norm.find(phrase)
                if pos >= 0:
                    found_phrase = phrase
                    found_pos = pos
                    break
            if found_phrase:
                break

        if found_pos < 0:
            return -1, 0.0

        logger.info("[RawXMLFallback] Found '%s...' at norm position %d",
                   found_phrase[:50], found_pos)

        # Map norm position back to approximate raw XML position
        # Since normalization changes char counts, estimate proportionally
        ratio = len(self.final_xml) / max(1, len(self._full_xml_norm))
        approx_raw_pos = int(found_pos * ratio)

        # Find the token at this raw position
        before_text = self.final_xml[:approx_raw_pos]
        approx_token_pos = len(self.chunker._encode(before_text))

        logger.info("[RawXMLFallback] Approx raw pos: %d, token pos: %d", approx_raw_pos, approx_token_pos)

        # Find the nearest chunk to this token position
        best_chunk_idx = -1
        best_distance = float('inf')

        for j, chunk in enumerate(self._final_xml_chunks):
            # Check if token position falls within this chunk
            if chunk["start_token"] <= approx_token_pos <= chunk["end_token"]:
                best_chunk_idx = j
                best_distance = 0
                break
            # Or find the closest chunk
            dist = min(abs(approx_token_pos - chunk["start_token"]),
                      abs(approx_token_pos - chunk["end_token"]))
            if dist < best_distance:
                best_distance = dist
                best_chunk_idx = j

        if best_chunk_idx >= 0:
            # Verify the chunk actually contains some of the search text
            chunk_norm = self._final_xml_chunks[best_chunk_idx].get("norm", "")
            if found_phrase in chunk_norm:
                score = len(found_phrase.split()) ** 2  # Same scoring as phrase search
                return best_chunk_idx, score * 1.0
            else:
                # The nearest chunk doesn't have it — check neighbors
                for offset in [-1, 1, -2, 2]:
                    neighbor = best_chunk_idx + offset
                    if 0 <= neighbor < len(self._final_xml_chunks):
                        neighbor_norm = self._final_xml_chunks[neighbor].get("norm", "")
                        if found_phrase in neighbor_norm:
                            score = len(found_phrase.split()) ** 2
                            return neighbor, score * 1.0
                # Still not found in any chunk — use the nearest one anyway
                # The chunk gap fix should prevent this in practice
                logger.warning("[RawXMLFallback] Nearest chunk %d doesn't contain text, using anyway", best_chunk_idx)
                return best_chunk_idx, len(found_phrase.split()) * 5.0

        return -1, 0.0

    # --------------------------------------------------------- main validation

    def _validate_one_segment(self, item: dict) -> dict:
        """Validate a single track-change segment. Thread-safe (reads only shared caches)."""
        segment_id = item.get("Track Change Segment") or item.get("segment")
        change_type = item.get("type", "unknown")

        if change_type == "insertion":
            tc_text = item.get("Track changes", "")
        else:
            tc_text = item.get("Deleted Content", "")

        prev_ctx = item.get("Previous Content", "")

        logger.info("=" * 100)
        logger.info("[SEGMENT %s] Type: %s", segment_id, change_type)
        self._log_snippet(f"[SEGMENT {segment_id}] Track change", tc_text, 200)
        self._log_snippet(f"[SEGMENT {segment_id}] Previous content", prev_ctx, 200)

        pages_0_based = self._find_pages_for_segment(item)
        logger.info("[SEGMENT %s] Pages (1-based): %s", segment_id, [p+1 for p in pages_0_based])

        image_paths = []
        for p in pages_0_based:
            if 0 <= p < len(self.gpt_processor.doc_images):
                image_paths.append(self.gpt_processor.doc_images[p]["path"])

        best_chunk_idx, chunk_confidence, chunk_strategy = self._find_best_chunk(item)

        xml_snippet, xml_note, selected_chunks = self._build_xml_snippet(
            best_chunk_idx, chunk_confidence, chunk_strategy
        )

        debug_info = {
            "segment_id": segment_id,
            "change_type": change_type,
            "pages_found": [p+1 for p in pages_0_based],
            "chunk_idx": best_chunk_idx,
            "chunk_confidence": chunk_confidence,
            "chunk_strategy": chunk_strategy,
            "selected_chunks": selected_chunks,
            "xml_snippet_length": len(xml_snippet),
            "xml_snippet_tokens": self.chunker.count_tokens(xml_snippet),
        }

        if self.output_dir and selected_chunks and selected_chunks != ["tail"]:
            chunks_dir = os.path.join(self.output_dir, "xml_chunks")
            os.makedirs(chunks_dir, exist_ok=True)
            for idx in selected_chunks:
                if isinstance(idx, int) and 0 <= idx < len(self._final_xml_chunks):
                    chunk_file = os.path.join(chunks_dir, f"segment_{segment_id}_chunk_{idx}.xml")
                    try:
                        with open(chunk_file, 'w', encoding='utf-8') as f:
                            f.write(self._final_xml_chunks[idx]["text"])
                    except Exception as e:
                        logger.warning("Failed to save chunk: %s", e)

        prompt = self._build_prompt(
            segment_id, change_type, item,
            [p+1 for p in pages_0_based],
            xml_snippet, xml_note,
        )

        llm_start = time.time()
        llm_result = self._call_claude(prompt, image_paths)
        llm_elapsed = time.time() - llm_start

        raw_text = llm_result["text"]
        usage = llm_result.get("usage", {})
        model_used = llm_result.get("model", "unknown")

        parsed = self._parse_validation_response(raw_text)

        parsed.setdefault("segment_id", segment_id)
        parsed.setdefault("type", change_type)
        parsed.setdefault("pages", [p+1 for p in pages_0_based])
        parsed.setdefault("raw_response", raw_text)
        parsed["debug_info"] = debug_info
        parsed["llm_metrics"] = {
            "model_used": model_used,
            "duration_seconds": round(llm_elapsed, 3),
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        logger.info("[SEGMENT %s] Result: valid=%s, tagging=%s, spacing=%s",
                   segment_id, parsed.get("is_valid"),
                   parsed.get("tagging_ok"), parsed.get("spacing_ok"))
        logger.info("[SEGMENT %s] Reason: %s", segment_id,
                   str(parsed.get("reason", ""))[:200])
        logger.info("=" * 100)

        return parsed

    def validate_all(self, limit: int | None = None) -> list[dict]:
        candidates = [m for m in self.mapper if m.get("type") in ("insertion", "deletion")]
        if limit is not None:
            candidates = candidates[:limit]

        logger.info("[ClaudeValidator] Starting parallel validation for %d segments (max_workers=%d)",
                    len(candidates), self.max_workers)

        self._ensure_chunks()
        self._ensure_page_index()

        results: list[dict] = [None] * len(candidates)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(self._validate_one_segment, item): idx
                for idx, item in enumerate(candidates)
            }
            with tqdm(total=len(candidates), desc="Validating track changes with Claude", unit="change") as pbar:
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as e:
                        segment_id = (candidates[idx].get("Track Change Segment")
                                      or candidates[idx].get("segment"))
                        logger.error("[SEGMENT %s] Validation failed: %s", segment_id, e)
                        results[idx] = {
                            "segment_id": segment_id,
                            "type": candidates[idx].get("type", "unknown"),
                            "is_valid": None,
                            "error": str(e),
                        }
                    pbar.update(1)

        results = [r for r in results if r is not None]
        logger.info("[ClaudeValidator] Completed %d segments", len(results))
        return results

    def _build_xml_snippet(
        self, best_idx: int, confidence: float, strategy: str
    ) -> Tuple[str, str, list]:
        """Build XML snippet from best chunk + neighbors."""
        self._ensure_chunks()

        if best_idx == -1 or not self._final_xml_chunks:
            snippet = self.final_xml[:200000]
            note = "XML beginning (no chunk matched)"
            return snippet, note, ["beginning"]

        start = max(0, best_idx - 1)
        end = min(len(self._final_xml_chunks), best_idx + 2)
        candidate_idxs = list(range(start, end))

        parts = [self._final_xml_chunks[j]["text"] for j in candidate_idxs]
        snippet = "\n\n<!-- CHUNK_BOUNDARY -->\n\n".join(parts)
        snippet_tokens = self.chunker.count_tokens(snippet)

        # If snippet is within budget, try adding more neighbors (up to 10k tokens)
        # This helps when the matched text is near the edges of the best chunk
        max_snippet_tokens = 10000
        while snippet_tokens < max_snippet_tokens:
            expanded = False
            # Try expanding forward
            if end < len(self._final_xml_chunks):
                next_chunk = self._final_xml_chunks[end]["text"]
                next_tokens = self.chunker.count_tokens(next_chunk)
                if snippet_tokens + next_tokens <= max_snippet_tokens:
                    candidate_idxs.append(end)
                    parts.append(next_chunk)
                    end += 1
                    snippet_tokens += next_tokens
                    expanded = True
            # Try expanding backward
            if start > 0:
                prev_chunk = self._final_xml_chunks[start - 1]["text"]
                prev_tokens = self.chunker.count_tokens(prev_chunk)
                if snippet_tokens + prev_tokens <= max_snippet_tokens:
                    start -= 1
                    candidate_idxs.insert(0, start)
                    parts.insert(0, prev_chunk)
                    snippet_tokens += prev_tokens
                    expanded = True
            if not expanded:
                break

        snippet = "\n\n<!-- CHUNK_BOUNDARY -->\n\n".join(parts)
        snippet_tokens = self.chunker.count_tokens(snippet)

        if snippet_tokens > max_snippet_tokens:
            snippet = self._final_xml_chunks[best_idx]["text"]
            snippet_tokens = self.chunker.count_tokens(snippet)
            candidate_idxs = [best_idx]

        note = (
            f"XML chunks {start+1}-{end}/{len(self._final_xml_chunks)} "
            f"(best={best_idx+1}, strategy={strategy}, conf={confidence:.1f}, "
            f"tokens={snippet_tokens})"
        )
        return snippet, note, candidate_idxs

    def _build_prompt(
        self,
        segment_id: str,
        change_type: str,
        mapper_item: dict,
        pages_display: list[int],
        xml_snippet: str,
        xml_note: str,
    ) -> str:
        track_text = (
            mapper_item.get("Track changes", "")
            if change_type == "insertion"
            else mapper_item.get("Deleted Content", "")
        )
        previous_content = mapper_item.get("Previous Content", "")

        prompt = self.validation_prompt_template.format(
            pages_display=pages_display,
            segment_id=segment_id,
            change_type=change_type,
            previous_content=previous_content,
            track_text=track_text,
            xml_note=xml_note,
            xml_snippet=xml_snippet,
        )
        return prompt.strip()

    def _call_claude(self, prompt: str, image_paths: list[str]) -> dict:
        """Call LLM via fallback chain. Returns dict with 'text', 'usage', 'model'."""
        content_blocks: list[dict] = [{"type": "text", "text": prompt}]

        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                media_type = "image/png"
                if img_path.lower().endswith((".jpg", ".jpeg")):
                    media_type = "image/jpeg"
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}", "detail": "high"},
                })
            except Exception as e:
                logger.warning("[Claude] Failed to attach image: %s (%s)", img_path, e)

        messages = [{"role": "user", "content": content_blocks}]

        result = self._fallback_handler.call_with_fallback(
            messages=messages,
            temperature=0,
            max_tokens=self.max_tokens,
        )

        if result is None:
            raise RuntimeError("All models in fallback chain exhausted")

        logger.info("[Claude] Success with model: %s", result.get("model", "unknown"))
        return result

    def _parse_validation_response(self, raw_text: str) -> dict:
        text = (raw_text or "").strip()

        # 1. Direct JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        # 2. Fenced JSON (```json ... ```)
        fence_pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
        matches = list(fence_pattern.finditer(text))
        if matches:
            try:
                parsed = json.loads(matches[-1].group(1).strip())
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        # 3. Last complete JSON object in text
        brace_pattern = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
        brace_matches = list(brace_pattern.finditer(text))
        if brace_matches:
            for bm in reversed(brace_matches):
                try:
                    parsed = json.loads(bm.group(0).strip())
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue

        # 4. Truncated JSON repair: extract fields from incomplete JSON
        #    (e.g., when max_tokens was reached mid-response)
        brace_start = text.rfind("{")
        if brace_start >= 0:
            partial = text[brace_start:]
            result = {}
            for key in ["segment_id", "type", "is_valid", "tagging_ok", "spacing_ok", "reason", "suggested_fix"]:
                pat = re.compile(r'"' + key + r'"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(true|false|null))', re.DOTALL)
                m = pat.search(partial)
                if m:
                    if m.group(2):
                        val = {"true": True, "false": False, "null": None}[m.group(2)]
                    else:
                        val = m.group(1)
                    result[key] = val
            if "is_valid" in result:
                if "reason" in result:
                    result["reason"] = result["reason"] + " [response truncated]"
                else:
                    result["reason"] = "Response was truncated but key fields were recovered."
                logger.info("[Parser] Recovered truncated JSON: is_valid=%s", result.get("is_valid"))
                return result

        return {
            "is_valid": None, "tagging_ok": None, "spacing_ok": None,
            "reason": "Failed to parse Claude response as JSON.",
        }


# ============================================================================
# RUNNER
# ============================================================================

def run_outp_validation(
    mapper_path: str,
    final_xml_path: str,
    images_folder_path: str,
    pdf_path: str,
    output_dir: str,
    claude_api_key: str | None = None,
    claude_model: str = MODEL_OPUS,
    limit: int | None = None,
    validation_prompt_path: str = None,
    max_workers: int = 5,
):
    """Run validation end-to-end."""
    logger.info("[Validation] Starting")

    if claude_api_key is None:
        claude_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not claude_api_key:
            raise RuntimeError("Claude API key not provided")

    with open(mapper_path, "r", encoding="utf-8") as f:
        mapper = json.load(f)

    with open(final_xml_path, "r", encoding="utf-8") as f:
        final_xml = f.read()

    gpt_proc = GPTProcessor(
        docx_file_path="",
        output_folder=os.path.join(output_dir, "validation_images"),
        context_window_size=10,
    )
    gpt_proc.load_images_from_folder(images_folder_path, pdf_path)

    # FIX: Ensure doc_images[i] has the image for PDF page i+1 (not alphabetical order)
    # Image filenames contain page numbers like "_page_127.png" which must map to PDF page 127
    _page_to_path = {}
    for _img in gpt_proc.doc_images:
        _m = re.search(r'_page_(\d+)', _img["filename"])
        if _m:
            _page_to_path[int(_m.group(1))] = _img["path"]
    if _page_to_path:
        _fixed = 0
        for _i in range(len(gpt_proc.doc_images)):
            _page_1based = _i + 1  # doc_images[0] = page 1
            if _page_1based in _page_to_path:
                _old = gpt_proc.doc_images[_i]["filename"]
                _new_path = _page_to_path[_page_1based]
                _new_name = os.path.basename(_new_path)
                if _old != _new_name:
                    _fixed += 1
                gpt_proc.doc_images[_i]["path"] = _new_path
                gpt_proc.doc_images[_i]["filename"] = _new_name
        if _fixed:
            logger.info("[Validation] Fixed image sort order for %d pages", _fixed)

    validator = ClaudeTrackChangeValidator(
        mapper=mapper,
        final_xml=final_xml,
        gpt_processor=gpt_proc,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        max_tokens=128000,
        output_dir=output_dir,
        validation_prompt_path=validation_prompt_path,
        max_workers=max_workers,
    )

    validation_start = time.time()
    results = validator.validate_all(limit=limit)
    validation_elapsed = round(time.time() - validation_start, 3)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "track_change_validation_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    total_input_tokens = sum(r.get("llm_metrics", {}).get("input_tokens", 0) for r in results)
    total_output_tokens = sum(r.get("llm_metrics", {}).get("output_tokens", 0) for r in results)
    total_llm_time = sum(r.get("llm_metrics", {}).get("duration_seconds", 0) for r in results)
    cost_input = (total_input_tokens / 1000.0) * 0.005
    cost_output = (total_output_tokens / 1000.0) * 0.025

    llm_call_details = []
    for r in results:
        m = r.get("llm_metrics", {})
        inp = m.get("input_tokens", 0)
        out = m.get("output_tokens", 0)
        call_cost = (inp / 1000.0) * 0.005 + (out / 1000.0) * 0.025
        llm_call_details.append({
            "segment": r.get("segment_id", ""),
            "type": r.get("type", ""),
            "duration_seconds": m.get("duration_seconds", 0),
            "input_tokens": inp,
            "output_tokens": out,
            "cost_usd": round(call_cost, 6),
            "is_valid": r.get("is_valid"),
        })

    def _fmt_time(seconds):
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h: return f"{h}h {m}m {s}s"
        if m: return f"{m}m {s}s"
        return f"{round(seconds, 2)}s"

    docx_basename = os.path.splitext(os.path.basename(mapper_path))[0].replace("mapper", "").strip("_")
    if not docx_basename:
        docx_basename = "validation"

    validation_report = {
        "input_file": docx_basename,
        "validation_model": claude_model,
        "total_segments_validated": len(results),
        "valid": sum(1 for r in results if r.get("is_valid") is True),
        "invalid": sum(1 for r in results if r.get("is_valid") is False),
        "uncertain": sum(1 for r in results if r.get("is_valid") is None),
        "total_execution_time_seconds": validation_elapsed,
        "total_execution_time_formatted": _fmt_time(validation_elapsed),
        "total_llm_time_seconds": round(total_llm_time, 3),
        "total_llm_time_formatted": _fmt_time(total_llm_time),
        "llm_summary": {
            "model": claude_model,
            "total_calls": len(results),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "total_cost_usd": round(cost_input + cost_output, 4),
        },
        "llm_call_details": llm_call_details,
    }

    report_filename = f"{docx_basename}_validation_report.json"
    report_path = os.path.join(output_dir, report_filename)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(validation_report, f, indent=2, ensure_ascii=False)
    logger.info("[Validation] Report saved: %s", report_path)

    summary = {
        "total": len(results),
        "valid": sum(1 for r in results if r.get("is_valid") is True),
        "invalid": sum(1 for r in results if r.get("is_valid") is False),
        "uncertain": sum(1 for r in results if r.get("is_valid") is None),
    }

    summary_path = os.path.join(output_dir, "validation_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info("[Validation] Done: %d total, %d valid, %d invalid, %d uncertain",
               summary["total"], summary["valid"], summary["invalid"], summary["uncertain"])
    logger.info("[Validation] Total time: %s, LLM cost: $%.4f",
               _fmt_time(validation_elapsed), cost_input + cost_output)

    return results


# ====== VALIDATION-ONLY ENTRY POINT ======
# Replace the path below with your final output XML path
FINAL_XML_PATH_FOR_VALIDATION = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_8 (April 23, 2026)\CDN_HA_WD (Version 6 exe)\ajay_testing_section_tagging_1\CDN_HA_WD 14 (chapter and all children)_20260506_194623_final.pxml"


class ProcessingWorker:
    """
    Runs the processing pipeline (no GUI dependency).
    """

    def __init__(self, input_dir, output_dir, prompt_path, model_name, temperature,
                 run_validation=False, claude_api_key=None, claude_model=MODEL_OPUS,
                 validation_prompt_path=None, max_validation_workers: int = 5):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.prompt_path = Path(prompt_path)
        self.model_name = model_name
        self.temperature = temperature
        self.is_running = True
        self.run_validation = run_validation
        self.claude_api_key = claude_api_key
        self.claude_model = claude_model
        self.validation_prompt_path = Path(validation_prompt_path) if validation_prompt_path else None
        self.max_validation_workers = max_validation_workers

    def run(self):
        """
        The main logic of the worker thread.
        Finds all subfolders and processes them one by one.
        """
        try:
            # Find all subdirectories in the input folder
            # These are assumed to be the processing targets (e.g., "File_A", "File_B")
            # We skip the 'prompt' folder.
            tasks = [
                d for d in self.input_dir.iterdir()
                if d.is_dir() and d.name.lower() != 'prompt'
            ]

            if not tasks:
                print("Error: No subfolders found in the input directory.")
                return

            if not self.prompt_path.exists():
                print(f"Error: Prompt file not found at {self.prompt_path}")
                return

            if self.run_validation and self.validation_prompt_path and not self.validation_prompt_path.exists():
                print(f"Error: Validation prompt file not found at {self.validation_prompt_path}")
                return

            total_tasks = len(tasks)
            for i, task_dir in enumerate(tasks):
                if not self.is_running:
                    break

                task_name = task_dir.name
                # self.current_task_name.emit(task_name)
                print(f"--- Starting processing for: {task_name} ({i + 1}/{total_tasks}) ---")
                # self.file_progress.emit(0, f"Starting: {task_name}")

                # 1. Define file paths based on convention
                print(f"Searching for files in: {task_dir}")

                docx_files = list(task_dir.glob("*.docx"))
                xml_files = list(task_dir.glob("*.xml"))
                pxml_files = list(task_dir.glob("*.pxml"))

                docx_path = None
                xml_path = None  # This will hold either .xml or .pxml

                # Find DOCX
                if not docx_files:
                    print(f"Error: No .docx file found in '{task_name}'. Skipping.")
                    continue
                if len(docx_files) > 1:
                    print(
                        f"Warning: Multiple .docx files found in '{task_name}'. Using the first one: {docx_files[0].name}")
                docx_path = docx_files[0]

                # Find XML or PXML
                if xml_files:
                    if len(xml_files) > 1:
                        print(
                            f"Warning: Multiple .xml files found in '{task_name}'. Using the first one: {xml_files[0].name}")
                    xml_path = xml_files[0]
                elif pxml_files:
                    if len(pxml_files) > 1:
                        print(
                            f"Warning: Multiple .pxml files found in '{task_name}'. Using the first one: {pxml_files[0].name}")
                    xml_path = pxml_files[0]
                else:
                    print(f"Error: No .xml or .pxml file found in '{task_name}'. Skipping.")
                    continue

                print(f"Found DOCX: {docx_path.name}")
                print(f"Found XML/PXML: {xml_path.name}")

                # 2. Define output directories
                task_output_dir = self.output_dir / task_name
                task_temp_dir = task_output_dir / "temp"
                task_output_dir.mkdir(parents=True, exist_ok=True)
                task_temp_dir.mkdir(parents=True, exist_ok=True)

                # 3. Set up logging for this specific task
                log_file_path = task_temp_dir / "processing.log"
                self.setup_task_logging(log_file_path)

                original_dir = os.getcwd()
                try:
                    # 4. Create pipeline report
                    docx_basename = os.path.splitext(docx_path.name)[0]
                    report = PipelineReport(input_file_name=docx_basename)

                    # # 5. Initialize and run the system
                    # system = TrackChangeProcessingSystem(
                    #     docx_path=str(docx_path),
                    #     xml_file_path=str(xml_path),
                    #     prompt_file_path=str(self.prompt_path),
                    #     output_base_dir=str(task_output_dir),
                    #     model_name=self.model_name,
                    #     temperature=self.temperature,
                    #     report=report,
                    #     anthropic_api_key=self.claude_api_key or "",
                    # )

                    # # Pass the progress signal to the pipeline
                    # output_files = system.run_complete_pipeline(progress_signal=self.file_progress)

                    # # 6. Save pipeline report into temp/
                    # report_file_name = f"{docx_basename}_report.json"
                    # report.save(str(task_output_dir / "temp"), report_file_name)
                    # self.log_message.emit(f"Pipeline report saved: {report_file_name}")

                    # # Log generated output files
                    # if output_files and isinstance(output_files, dict):
                    #     self.log_message.emit("Generated files:")
                    #     for key, filename in output_files.items():
                    #         self.log_message.emit(f"  {key}: {os.path.join(str(task_output_dir), filename)}")
                    #     self.log_message.emit(f"  report: {os.path.join(str(task_output_dir), report_file_name)}")

                    # ====== VALIDATION-ONLY MODE: Replace path below with your final XML ======
                    final_xml_path_for_validation = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test_8 (April 23, 2026)\CDN_HA_WD (Version 6 exe)\ajay_testing_section_tagging_1\CDN_HA_WD 14 (chapter and all children)_20260506_194623_final.pxml"
                    validation_output_dir = task_output_dir
                    print(f"⚡ Skipping pipeline — using provided XML for validation: {final_xml_path_for_validation}")

                    # Generate required validation inputs (mapper.json + document_images)
                    task_temp_dir = validation_output_dir / "temp"
                    task_temp_dir.mkdir(parents=True, exist_ok=True)
                    images_dir = task_temp_dir / "document_images"
                    images_dir.mkdir(parents=True, exist_ok=True)

                    mapper_path = task_temp_dir / "mapper.json"
                    if not mapper_path.exists():
                        print("  [PREP] Generating mapper.json from DOCX...")
                        try:
                            docx_proc = DOCXProcessor(str(docx_path))
                            doc_tree, footnote_tree = docx_proc.extract_xml_from_docx()
                            raw_text = docx_proc.extract_full_text_with_track_changes()
                            cleaned_text = TextProcessor.remove_field_code_artifacts(raw_text)
                            cleaned_text = TextProcessor.clean_text(cleaned_text)
                            cleaned_text = cleaned_text.replace("<para></para>","").replace("</DEL_MARKER></para><para><DEL_MARKER>"," ")
                            flattened_text = TextProcessor.flatten_trace_tags(cleaned_text)
                            flattened_text = flattened_text.replace("</DEL_MARKER><DEL_MARKER>","").replace("</DEL_MARKER> <DEL_MARKER>"," ").replace("<trace> </trace>","").replace("<para></para>","").replace("<para> </para>","")

                            def resolve_nested_deletions(match):
                                inner = match.group(1)
                                return f"<trace>{re.sub(r'<DEL_MARKER>.*?</DEL_MARKER>', '<trace.deleted/>', inner, flags=re.DOTALL)}</trace>"

                            text_with_resolved = re.sub(r"<trace>(.*?)</trace>", resolve_nested_deletions, flattened_text, flags=re.DOTALL)
                            mapper_instructions = MapperGenerator.create_mapper_instructions(text_with_resolved)
                            mapper_path.write_text(json.dumps(mapper_instructions, indent=2, ensure_ascii=False), encoding="utf-8")
                            print(f"  [PREP] mapper.json created ({len(mapper_instructions)} entries)")
                        except Exception as e:
                            import traceback
                            print(f"  [PREP] Failed to create mapper.json: {e}")
                            traceback.print_exc()

                    if not any(images_dir.glob("*.png")):
                        print("  [PREP] Generating document images from DOCX...")
                        try:
                            gpt_proc = GPTProcessor(
                                docx_file_path=str(docx_path),
                                output_folder=images_dir,
                                context_window_size=10,
                            )
                            gpt_proc.convert_docx_to_images()
                            print(f"  [PREP] Document images created ({len(gpt_proc.doc_images)} pages)")
                        except Exception as e:
                            print(f"  [PREP] Failed to create images: {e}")

                    # 7. Run validation if enabled
                    if self.run_validation:
                        # self.file_progress.emit(90, f"Validating: {task_name}")
                        self._run_validation(validation_output_dir, docx_path, clean_pdf_path='', final_xml_path_override=final_xml_path_for_validation)

                    print(f"--- Finished processing for: {task_name} ---")

                except Exception as e:
                    print(f"FATAL ERROR processing {task_name}: {e}")
                    logging.getLogger(__name__).exception(f"Pipeline failed for {task_name}")
                finally:
                    os.chdir(original_dir)

                # 5. Update overall progress - calculate percentage based on completed tasks
                progress_percentage = int(((i + 1) / len(tasks)) * 100)
                # self.overall_progress.emit(progress_percentage)
                # self.file_progress.emit(100, f"Completed: {task_name}")

        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            logging.getLogger(__name__).exception("Worker thread failed")

        # self.current_task_name.emit("Idle")
        # self.file_progress.emit(0, "All tasks complete.")
        # self.overall_progress.emit(100)
        print("All tasks complete.")
        return

    def _run_validation(self, task_output_dir: Path, docx_path: Path, clean_pdf_path: str = '', final_xml_path_override: str = ''):
        """
        Run output validation using Claude, then convert results to Excel.

        Args:
            task_output_dir: The output directory for the current task.
            docx_path: Path to the original DOCX file.
            clean_pdf_path: Exact path to the clean PDF created during the pipeline.
            final_xml_path_override: Exact path to the Final_output_XML_fixed file (with timestamp).
        """
        print("=" * 60)
        print("Starting Output Validation")

        task_temp_dir = task_output_dir / "temp"

        # mapper.json and document_images live inside temp/
        mapper_path = os.path.join(str(task_temp_dir), "mapper.json")

        # Use the exact path from the pipeline if available, otherwise fall back to discovery
        final_xml_path = final_xml_path_override if final_xml_path_override and os.path.exists(final_xml_path_override) else ''
        if not final_xml_path:
            import glob
            matches = glob.glob(os.path.join(str(task_output_dir), "Final_output_XML_fixed_*.xml"))
            if matches:
                final_xml_path = max(matches, key=os.path.getmtime)
        if not final_xml_path:
            final_xml_path = os.path.join(str(task_output_dir), "Final_output_XML_fixed.xml")
        if not os.path.exists(final_xml_path):
            final_xml_path = os.path.join(str(task_output_dir), "Final_output_XML_fixed.txt")
        if not os.path.exists(final_xml_path):
            final_xml_path = os.path.join(str(task_temp_dir), "Final_output_XML.txt")
        if not os.path.exists(final_xml_path):
            final_xml_path = os.path.join(str(task_temp_dir), "Final_output_XML.xml")

        # document_images lives inside temp/
        images_folder = os.path.join(str(task_temp_dir), "document_images")

        has_validation_inputs = (
            os.path.exists(mapper_path)
            and os.path.exists(final_xml_path)
            and os.path.exists(images_folder)
        )

        if not has_validation_inputs:
            print("Skipping validation - required input files not found")
            if not os.path.exists(mapper_path):
                print(f"  Missing: {mapper_path}")
            if not os.path.exists(final_xml_path):
                print(f"  Missing: {final_xml_path}")
            if not os.path.exists(images_folder):
                print(f"  Missing: {images_folder}")
            return

        api_key = self.claude_api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("Warning: No Claude API key provided. Skipping validation.")
            return

        try:
            # Use the exact PDF path from the pipeline if available
            pdf_path = clean_pdf_path if clean_pdf_path and os.path.exists(str(clean_pdf_path)) else ''

            # Fallback: reconstruct expected path
            if not pdf_path:
                pdf_path = os.path.join(
                    str(task_temp_dir), "document_images",
                    "docx_pdf_clean.pdf",
                )

            # Fallback: search for any .pdf in document_images/
            if not os.path.exists(pdf_path):
                print(f"PDF not found at expected path: {pdf_path}")
                print("Searching for PDF in document_images directory...")
                import glob
                pdf_candidates = glob.glob(os.path.join(str(task_temp_dir), "document_images", "**", "*.pdf"), recursive=True)
                pdf_candidates += glob.glob(os.path.join(str(task_temp_dir), "document_images", "*.pdf"))
                # Deduplicate
                pdf_candidates = list(set(pdf_candidates))
                if pdf_candidates:
                    pdf_path = pdf_candidates[0]
                    print(f"Found PDF via search: {pdf_path}")
                else:
                    print("No PDF found in document_images. Listing directory contents:")
                    img_dir = os.path.join(str(task_temp_dir), "document_images")
                    if os.path.exists(img_dir):
                        for entry in os.listdir(img_dir):
                            print(f"  {entry}")
                    print("Skipping validation — clean PDF not available.")
                    return

            # Save validation JSON + summary into temp/
            validation_results = run_outp_validation(
                mapper_path=mapper_path,
                final_xml_path=final_xml_path,
                images_folder_path=images_folder,
                pdf_path=pdf_path,
                output_dir=str(task_temp_dir),
                claude_api_key=api_key,
                claude_model=self.claude_model,
                validation_prompt_path=str(self.validation_prompt_path) if self.validation_prompt_path else None,
                max_workers=self.max_validation_workers,
            )
            print(
                f"Validation complete! {len(validation_results)} segments validated."
            )

            # Convert validation JSON (in temp/) to Excel (in main output dir)
            self._convert_validation_to_excel(task_output_dir)

        except Exception as e:
            print(f"Validation failed: {e}")
            logging.getLogger(__name__).exception("Validation failed")

    def _convert_validation_to_excel(self, task_output_dir: Path):
        """
        Convert the validation results JSON to an Excel file.
        Reads JSON from temp/, writes Excel to the main output directory.
        Sheet1 = Validation Results, Sheet2 = Content Drops (if any).

        Args:
            task_output_dir: The main output directory for the current task.
        """
        print("Converting validation results to Excel")
        task_temp_dir = task_output_dir / "temp"
        json_file_path = os.path.join(str(task_temp_dir), "track_change_validation_results.json")
        content_drops_path = os.path.join(str(task_temp_dir), "content_drops.json")
        timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
        excel_file_path = os.path.join(str(task_output_dir), f"final_validation_report_{timestamp}.xlsx")

        if not os.path.exists(json_file_path):
            print(
                f"Validation JSON not found at {json_file_path}, skipping Excel conversion"
            )
            return

        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                val_data = json.load(f)
            df_validation = pd.DataFrame(val_data)

            with pd.ExcelWriter(excel_file_path, engine="openpyxl") as writer:
                df_validation.to_excel(writer, index=False, sheet_name="Validation Results")

                # Format the Validation Results sheet
                ws = writer.sheets["Validation Results"]
                self._format_validation_sheet(ws, df_validation)

                if os.path.exists(content_drops_path):
                    with open(content_drops_path, "r", encoding="utf-8") as f:
                        drops_data = json.load(f)
                    if drops_data:
                        df_drops = pd.DataFrame(drops_data)
                        df_drops.to_excel(writer, index=False, sheet_name="Content Drops")
                        print(f"Added {len(drops_data)} content drop(s) to Sheet 'Content Drops'")

            print(f"Successfully converted validation results to {excel_file_path}")
        except Exception as e:
            print(f"Excel conversion failed: {e}")

    @staticmethod
    def _format_validation_sheet(ws, df):
        """Apply formatting to the validation Excel sheet for readability."""
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter

        # Header styling
        header_font = Font(bold=True, color='FFFFFF', size=11)
        header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
        header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        thin_border = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin')
        )

        # Style headers
        for col_num in range(1, len(df.columns) + 2):  # +2 for index offset
            cell = ws.cell(row=1, column=col_num)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = thin_border

        # Data cell styling
        wrap_alignment = Alignment(vertical='top', wrap_text=True)
        for row_num in range(2, len(df) + 2):
            for col_num in range(1, len(df.columns) + 2):
                cell = ws.cell(row=row_num, column=col_num)
                cell.alignment = wrap_alignment
                cell.border = thin_border

        # Column widths based on header names
        col_widths = {
            'index': 8, 'segment_id': 12, 'type': 10,
            'correct_chunk_received': 12, 'correct_pages_received': 12,
            'Track changes': 40, 'is_valid': 10, 'tagging_ok': 10,
            'spacing_ok': 10, 'reason': 80, 'previous_content': 40,
            'suggested_fix': 50
        }
        for col_idx, col_name in enumerate(df.columns, start=2):  # start=2 because col 1 is df index
            letter = get_column_letter(col_idx)
            ws.column_dimensions[letter].width = col_widths.get(col_name, 15)

        # Freeze header row
        ws.freeze_panes = 'A2'

        # Auto-filter
        ws.auto_filter.ref = ws.dimensions

        # Row heights for data rows
        for row_num in range(2, len(df) + 2):
            ws.row_dimensions[row_num].height = 60

    def setup_task_logging(self, log_file_path):
        """
        Re-configures the root logger for the current task.
        It removes old handlers and adds a new FileHandler for the
        task's temp directory. The GUI handler is already attached.
        """
        logger = logging.getLogger()  # Get the root logger

        # Remove all existing FileHandlers to avoid logging to old files
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.removeHandler(handler)

        # Add a new FileHandler for the current task
        file_handler = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logger.addHandler(file_handler)

    def stop(self):
        self.is_running = False
        print("Stopping processing...")


class MainWindow:
    pass


def _format_validation_excel_sheet(ws, df):
    """Apply formatting to the validation Excel sheet for readability (standalone version)."""
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    for col_num in range(1, len(df.columns) + 2):
        cell = ws.cell(row=1, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    wrap_alignment = Alignment(vertical='top', wrap_text=True)
    for row_num in range(2, len(df) + 2):
        for col_num in range(1, len(df.columns) + 2):
            cell = ws.cell(row=row_num, column=col_num)
            cell.alignment = wrap_alignment
            cell.border = thin_border

    col_widths = {
        'index': 8, 'segment_id': 12, 'type': 10,
        'correct_chunk_received': 12, 'correct_pages_received': 12,
        'Track changes': 40, 'is_valid': 10, 'tagging_ok': 10,
        'spacing_ok': 10, 'reason': 80, 'previous_content': 40,
        'suggested_fix': 50
    }
    for col_idx, col_name in enumerate(df.columns, start=2):
        letter = get_column_letter(col_idx)
        ws.column_dimensions[letter].width = col_widths.get(col_name, 15)

    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for row_num in range(2, len(df) + 2):
        ws.row_dimensions[row_num].height = 60


def run_validation_for_gui(
    docx_path: str,
    final_xml_path: str,
    output_dir: str,
    prompt_path: str = None,
    validation_prompt_path: str = None,
    claude_api_key: str = None,
    claude_model: str = MODEL_OPUS,
    max_workers: int = 5,
    images_folder: str = None,
) -> str:
    """
    Run validation from the GUI pipeline.
    
    Args:
        docx_path: Path to the DOCX file (in uploads dir)
        final_xml_path: Path to the final output PXML/XML file
        output_dir: Directory to save validation results
        prompt_path: Path to prompt.txt (from validation_prompts dir)
        validation_prompt_path: Path to prompt_validation.txt
        claude_api_key: Claude API key (auto-resolved if None)
        claude_model: Claude model to use
        max_workers: Max parallel validation workers
        
    Returns:
        Path to the generated Excel validation report
    """
    from datetime import datetime
    from pathlib import Path
    
    print(f"\n{'='*60}")
    print("VALIDATION REPORT GENERATION")
    print(f"{'='*60}")
    
    output_path = Path(output_dir)
    temp_dir = output_path / "validation_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    images_dir = temp_dir / "document_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    
    # Step 1: Generate mapper.json from DOCX
    mapper_path = temp_dir / "mapper.json"
    if not mapper_path.exists():
        print("  [PREP] Generating mapper.json from DOCX...")
        try:
            docx_proc = DOCXProcessor(str(docx_path))
            doc_tree, footnote_tree = docx_proc.extract_xml_from_docx()
            raw_text = docx_proc.extract_full_text_with_track_changes()
            cleaned_text = TextProcessor.remove_field_code_artifacts(raw_text)
            cleaned_text = TextProcessor.clean_text(cleaned_text)
            cleaned_text = cleaned_text.replace("<para></para>", "").replace("</DEL_MARKER></para><para><DEL_MARKER>", " ")
            flattened_text = TextProcessor.flatten_trace_tags(cleaned_text)
            flattened_text = flattened_text.replace("</DEL_MARKER><DEL_MARKER>", "").replace("</DEL_MARKER> <DEL_MARKER>", " ").replace("<trace> </trace>", "").replace("<para></para>", "").replace("<para> </para>", "")

            def resolve_nested_deletions(match):
                inner = match.group(1)
                return f"<trace>{re.sub(r'<DEL_MARKER>.*?</DEL_MARKER>', '<trace.deleted/>', inner, flags=re.DOTALL)}</trace>"

            text_with_resolved = re.sub(r"<trace>(.*?)</trace>", resolve_nested_deletions, flattened_text, flags=re.DOTALL)
            mapper_instructions = MapperGenerator.create_mapper_instructions(text_with_resolved)
            mapper_path.write_text(json.dumps(mapper_instructions, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  [PREP] mapper.json created ({len(mapper_instructions)} entries)")
        except Exception as e:
            print(f"  [PREP] Failed to create mapper.json: {e}")
            raise
    else:
        print(f"  [PREP] mapper.json already exists, reusing")

    # Step 2: Reuse document images from uploaded ZIP folder (or existing images_dir)
    if images_folder and os.path.isdir(images_folder):
        # Copy/link images from the uploaded ZIP images folder
        import shutil
        src_images = [f for f in os.listdir(images_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff', '.bmp', '.pdf'))]
        if src_images and not any(images_dir.glob("*.png")):
            for img_file in src_images:
                src = os.path.join(images_folder, img_file)
                dst = images_dir / img_file
                if not dst.exists():
                    shutil.copy2(src, dst)
            print(f"  [PREP] Reused {len(src_images)} images from uploaded ZIP folder")
        else:
            print(f"  [PREP] Document images already exist in validation_temp, reusing")
    elif any(images_dir.glob("*.png")):
        print(f"  [PREP] Document images already exist, reusing")
    else:
        print("  [PREP] Generating document images from DOCX...")
        try:
            gpt_proc = GPTProcessor(
                docx_file_path=str(docx_path),
                output_folder=str(images_dir),
                context_window_size=10,
            )
            gpt_proc.convert_docx_to_images()
            print(f"  [PREP] Document images created ({len(gpt_proc.doc_images)} pages)")
        except Exception as e:
            print(f"  [PREP] Failed to create images: {e}")
            raise

    # Step 3: Resolve API key via TR token endpoint (same as other modules)
    if not claude_api_key:
        try:
            _client = build_anthropic_client(claude_model)
            claude_api_key = _client.api_key
        except Exception as e:
            print(f"  [WARN] Could not fetch API key from token endpoint: {e}")
            claude_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    
    # Step 4: Find PDF for validation
    import glob
    pdf_candidates = glob.glob(os.path.join(str(images_dir), "*.pdf"))
    # Also check the output_dir itself (pipeline moves PDF there to save space in images folder)
    if not pdf_candidates:
        pdf_candidates = glob.glob(os.path.join(output_dir, "*.pdf"))
    # Also check the images_folder passed in (original revision_images dir)
    if not pdf_candidates and images_folder and os.path.isdir(images_folder):
        pdf_candidates = glob.glob(os.path.join(images_folder, "*.pdf"))
    pdf_path = pdf_candidates[0] if pdf_candidates else ""
    
    if not pdf_path:
        raise RuntimeError(
            f"No PDF found for validation. Searched in:\n"
            f"  - {images_dir}\n"
            f"  - {output_dir}\n"
            f"  - {images_folder or '(no images_folder provided)'}\n"
            f"Ensure the images ZIP contains a PDF file."
        )
    
    # Step 5: Run validation
    print("  [VALIDATION] Running validation...")
    validation_results = run_outp_validation(
        mapper_path=str(mapper_path),
        final_xml_path=final_xml_path,
        images_folder_path=str(images_dir),
        pdf_path=pdf_path,
        output_dir=str(temp_dir),
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        validation_prompt_path=validation_prompt_path,
        max_workers=max_workers,
    )
    print(f"  [VALIDATION] {len(validation_results)} segments validated.")
    
    # Step 6: Convert to Excel
    json_file_path = os.path.join(str(temp_dir), "track_change_validation_results.json")
    content_drops_path = os.path.join(str(temp_dir), "content_drops.json")
    timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
    excel_file_path = os.path.join(output_dir, f"final_validation_report_{timestamp}.xlsx")
    
    if os.path.exists(json_file_path):
        with open(json_file_path, "r", encoding="utf-8") as f:
            val_data = json.load(f)
        df_validation = pd.DataFrame(val_data)
        
        with pd.ExcelWriter(excel_file_path, engine="openpyxl") as writer:
            df_validation.to_excel(writer, index=False, sheet_name="Validation Results")

            # Format the sheet
            ws = writer.sheets["Validation Results"]
            _format_validation_excel_sheet(ws, df_validation)

            if os.path.exists(content_drops_path):
                with open(content_drops_path, "r", encoding="utf-8") as f:
                    drops_data = json.load(f)
                if drops_data:
                    df_drops = pd.DataFrame(drops_data)
                    df_drops.to_excel(writer, index=False, sheet_name="Content Drops")
        
        print(f"  \u2705 Validation Excel saved to: {excel_file_path}")
    else:
        raise FileNotFoundError(f"Validation results JSON not found at {json_file_path}")
    
    return excel_file_path


def run_validation_only():
    """Run validation using the provided final XML path."""
    input_dir = r"C:\Users\C302473\Downloads\INPUT_V6\INPUT_V6"
    output_dir = r"C:\Users\C302473\Downloads\INPUT_V6\output"
    prompt_path = os.path.join(input_dir, "prompt", "prompt.txt")
    validation_prompt_path = os.path.join(input_dir, "prompt", "prompt_validation.txt")

    # Get API key from TR token endpoint
    try:
        _client = build_anthropic_client(MODEL_OPUS)
        _api_key = _client.api_key
    except Exception as e:
        print(f"  [WARN] Could not fetch API key from token endpoint: {e}")
        _api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    worker = ProcessingWorker(
        input_dir=input_dir,
        output_dir=output_dir,
        prompt_path=prompt_path,
        model_name="gpt-5",
        temperature=0.2,
        run_validation=True,
        claude_api_key=_api_key,
        claude_model=MODEL_OPUS,
        validation_prompt_path=validation_prompt_path,
    )
    worker.run()


if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    run_validation_only()
