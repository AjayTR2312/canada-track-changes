"""Turn a pipeline failure into something a reviewer can act on.

A raw exception text ("Connection error.", "KeyError: 'idx'") does not tell a user whether to run the
chapter again or to stop and work on it by hand. classify_error() returns a small, JSON-safe dict:

    {
      "category": "llm_busy" | "llm_auth" | "llm_request" | "llm_config" | "stopped" | "input" | "storage" | "content" | "unknown",
      "title":    short plain-language headline,
      "hint":     what to do next, in one or two sentences,
      "action":   "rerun" | "rerun_then_report" | "fix_input" | "manual" | "report",
      "retryable": bool,        # True when running the chapter again is a sensible first step
      "detail":   the original message (trimmed) for people who want the specifics,
    }

It looks at the exception's class names (so no dependency on the anthropic package here), HTTP status code
and message text, and follows __cause__ / __context__ because the useful reason is often chained.
"""
from __future__ import annotations

import re

_MAX_DETAIL = 400

_BUSY_NAMES = {"RateLimitError", "OverloadedError", "InternalServerError", "APITimeoutError", "APIConnectionError",
               "ServiceUnavailableError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout", "TimeoutError"}
_AUTH_NAMES = {"AuthenticationError", "PermissionDeniedError"}
_REQUEST_NAMES = {"BadRequestError", "UnprocessableEntityError"}
_INPUT_NAMES = {"BadZipFile", "FileNotFoundError", "UnicodeDecodeError", "XMLSyntaxError", "ParseError", "PackageNotFoundError",
                "InvalidFileException"}
_STORAGE_NAMES = {"PermissionError", "OSError", "IOError", "ClientError", "S3UploadFailedError", "NoCredentialsError", "EndpointConnectionError"}
_CONTENT_NAMES = {"KeyError", "IndexError", "AttributeError", "TypeError", "ValueError", "AssertionError", "ZeroDivisionError",
                  "NameError", "UnboundLocalError", "RecursionError"}

_BUSY_TEXT = re.compile(r"overloaded|rate.?limit|too many requests|\b(429|500|502|503|504|529)\b|timed? ?out|timeout|"
                        r"connection (error|reset|aborted|refused)|temporar|try again|service unavailable|bad gateway", re.I)
_AUTH_TEXT = re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|invalid.*(api.?key|token|credential)|authentication|"
                        r"access denied|credential|ciam", re.I)
_REQUEST_TEXT = re.compile(r"prompt is too long|context (length|window)|max_tokens|too many tokens|invalid_request|"
                           r"\b400\b.*(thinking|temperature|messages)|exceeds? the (maximum|limit)", re.I)
_STORAGE_TEXT = re.compile(r"\bs3\b|bucket|accessdenied|nosuchbucket|no space left|disk|permission denied|"
                           r"drive|upload failed|download failed", re.I)
_S3_TEXT = re.compile(r"\bs3\b|bucket", re.I)
_TLS_TEXT = re.compile(r"ssl:|sslerror|ssleof|certificate verify|handshake|unexpected_eof_while_reading", re.I)
_INPUT_TEXT = re.compile(r"not a zip|bad zip|no such file|file not found|cannot find|could not (read|parse|open)|"
                         r"corrupt|invalid (docx|xml|pxml)|xml syntax|mismatched tag|no (docx|pxml|xml) (file )?(found|provided)|"
                         r"empty (file|document)", re.I)


def _chain(exc: BaseException):
    seen = []
    while exc is not None and exc not in seen and len(seen) < 6:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _names(exc: BaseException) -> set:
    return {c.__name__ for c in type(exc).__mro__}


def _status(exc: BaseException):
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _result(category: str, title: str, hint: str, action: str, retryable: bool, detail: str) -> dict:
    detail = re.sub(r"\s+", " ", (detail or "").strip())
    if len(detail) > _MAX_DETAIL:
        detail = detail[:_MAX_DETAIL - 1] + "…"
    return {"category": category, "title": title, "hint": hint, "action": action, "retryable": retryable, "detail": detail}


def llm_outage_info(kind: str, failures, last_error: str = "", secondary: str = "") -> dict:
    """The AI service (LLM) itself was failing. `kind` is "busy" (down / overloaded / unreachable) or "auth"
    (credentials refused). `secondary` is whatever other error the pipeline raised as a result."""
    detail = (last_error or "")
    if secondary:
        detail = f"{detail} | then the pipeline failed with: {secondary}" if detail else secondary
    n = f"{failures} AI calls in a row" if failures else "Repeated AI calls"
    if kind == "auth":
        return _result("llm_auth", "AI service (LLM) problem: it rejected our sign-in",
                       f"{n} were refused, so the run stopped instead of wasting more calls. This is not a problem with your "
                       "files. Run it again once; if it repeats, tell the tool owner that the AI credentials need attention.",
                       "rerun_then_report", True, detail)
    return _result("llm_busy", "AI service (LLM) outage or overload",
                   f"{n} failed, so the run stopped instead of wasting more calls. This is not a problem with your files. "
                   "Run it again in a few minutes.", "rerun", True, detail)


def classify_error(err) -> dict:
    """Classify an exception (preferred) or an error message string."""
    # Stopped on purpose by the circuit breaker (config.LLMUnavailable) or reported by a validation run
    # (ValidationInterrupted, which carries the same facts across the worker-process boundary).
    if isinstance(err, BaseException) and type(err).__name__ in ("LLMUnavailable", "ValidationInterrupted"):
        kind = getattr(err, "kind", "busy")
        if kind == "stop":
            return _result("stopped", "Stopped by user", "You stopped this run. Nothing is wrong; run it again when ready.",
                           "rerun", True, "")
        return llm_outage_info(kind, getattr(err, "failures", 0), getattr(err, "last_error", ""))

    if isinstance(err, BaseException):
        chain = _chain(err)
        text = " | ".join(f"{type(c).__name__}: {c}" for c in chain)
        names = set().union(*(_names(c) for c in chain))
        statuses = [s for s in (_status(c) for c in chain) if s]
        detail = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    else:
        text = detail = str(err or "")
        names, statuses = set(), []
        m = re.search(r"\b([45]\d\d)\b", text)
        if m:
            statuses = [int(m.group(1))]

    # 1) The AI service
    if (404 in statuses or "NotFoundError" in names) and re.search(r"model", text, re.I):
        return _result("llm_config", "The configured AI model is not available",
                       "Running again will not help. Tell the tool owner: a model name in the configuration is wrong or "
                       "no longer offered.", "report", False, detail)
    if names & _AUTH_NAMES or any(s in (401, 403) for s in statuses):
        return _result("llm_auth", "The AI service rejected the sign-in",
                       "Try the chapter once more (the access token may have expired). If it fails again, tell the tool owner: "
                       "the AI credentials need attention.", "rerun_then_report", True, detail)
    if names & _REQUEST_NAMES or 400 in statuses or _REQUEST_TEXT.search(text):
        return _result("llm_request", "The AI could not accept a request from this chapter",
                       "Running it again usually gives the same result. A very large paragraph or table is the common cause: "
                       "process this chapter manually or split the content, and report it so the prompt can be improved.",
                       "manual", False, detail)
    if names & {"SSLError", "SSLEOFError", "SSLCertVerificationError", "CertificateError"} or _TLS_TEXT.search(text):
        return _result("llm_busy", "Could not connect securely to the AI service (network / VPN problem)",
                       "The secure connection to the AI platform failed before any work started. This is almost always the "
                       "network, VPN or a proxy, not your files. Check you are connected to the company network or VPN, then "
                       "run it again.", "rerun", True, detail)
    if names & _BUSY_NAMES or any(s in (408, 409, 429, 500, 502, 503, 504, 529) for s in statuses):
        return _result("llm_busy", "AI service (LLM) outage or overload",
                       "The AI service did not answer. This is not a problem with your files and is usually temporary: run "
                       "it again in a few minutes. Work already finished for other chapters is kept.", "rerun", True, detail)
    if _AUTH_TEXT.search(text) and not (names & _STORAGE_NAMES) and not _S3_TEXT.search(text):
        return _result("llm_auth", "A sign-in or permission problem",
                       "Try again once. If it repeats, tell the tool owner.", "rerun_then_report", True, detail)

    # 2) Files and storage
    if names & _INPUT_NAMES or _INPUT_TEXT.search(text):
        return _result("input", "A problem with the input files",
                       "Check that the Word file, the XML and the images zip belong to the same chapter and open correctly, "
                       "then upload them again.", "fix_input", False, detail)
    if names & _STORAGE_NAMES or _STORAGE_TEXT.search(text):
        return _result("storage", "Could not read or write a file (S3 storage or disk)",
                       "Often temporary: run the chapter again. If it keeps failing, tell the tool owner (the storage bucket "
                       "or its permissions may need attention).", "rerun_then_report", True, detail)
    if _BUSY_TEXT.search(text):
        return _result("llm_busy", "A temporary connection problem (likely the AI service)",
                       "Run the chapter again in a few minutes.", "rerun", True, detail)

    # 3) Something in the document content tripped the pipeline's own logic
    if names & _CONTENT_NAMES:
        return _result("content", "The pipeline hit content it could not handle",
                       "Running it again will most likely fail the same way. Process this chapter manually and report it "
                       "with the log so the logic can be fixed.", "manual", False, detail)

    return _result("unknown", "The chapter failed for an unexpected reason",
                   "Try running it once more. If it fails again, work on it manually and report it with the log.",
                   "rerun_then_report", True, detail)
