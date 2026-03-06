#!/usr/bin/env python3
"""
rj_streaming_metrics.py
-----------------------
Utilities for measuring Server-Sent Events (SSE) streaming performance.

Provides:
  - StreamingMetrics       : dataclass holding all timing and token-count results
  - estimate_input_tokens  : lightweight token estimator from a request payload
  - measure_sse_stream     : POST a JSON payload and record SSE timing metrics

Token key auto-detection
------------------------
Different servers name the text field in their SSE JSON differently.
This module tries the following keys in order until one yields a
non-empty, non-whitespace value:

    "data", "token", "text", "content", "delta", "message",
    "output", "response", "chunk", "answer"

You can override this by passing token_keys=["your_key"] to
measure_sse_stream.  Use --verbose when running the tester to print
each raw SSE line so you can see exactly what key your server uses.

Typical usage
-------------
    from rj_streaming_metrics import measure_sse_stream

    metrics = measure_sse_stream(
        url="https://api.example.com/stream",
        payload={"prompt": "Hello", "model": "my-model"},
        headers={"Authorization": "Bearer <token>"},
        verbose=True,
    )
    print("TTFT: %.1f ms" % metrics.time_to_first_token_ms)
"""

import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

# Suppress InsecureRequestWarning for self-signed cert environments.
# Set verify=True (or pass a CA bundle) in production.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class StreamingMetrics:
    """
    Container for streaming response performance metrics.

    All time fields are milliseconds relative to the moment the request
    is sent (t=0). Fields that could not be measured remain at -1.

    Attributes
    ----------
    stream_start_time_ms:
        Time until HTTP response headers arrived (stream is ready).
    time_to_first_token_ms:
        Time until the first non-whitespace token arrived. -1 if none.
    time_to_last_token_ms:
        Time of the most recently received token. -1 if none.
    total_response_time_ms:
        Wall-clock time from request start until stream ended.
    input_tokens:
        Estimated input tokens (~1 per 4 chars of compact JSON payload).
    output_tokens:
        Count of non-whitespace token values observed in the stream.
    token_key:
        The JSON field name that contained token text, or None if no
        tokens were found.
    error:
        Human-readable error message, or None on success.
    """

    stream_start_time_ms: float
    time_to_first_token_ms: float
    time_to_last_token_ms: float
    total_response_time_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    token_key: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SSE headers required by the spec and commonly expected by proxies.
_SSE_HEADERS: Dict[str, str] = {
    "Accept": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# Sentinel values that signal end-of-stream.
_DONE_SENTINELS = {"[done]", "done"}

# Ordered list of JSON field names to probe for token text.
# The first key that yields a non-empty, non-whitespace string is used
# for the rest of the stream.
_DEFAULT_TOKEN_KEYS: List[str] = [
    "data",       # original / legacy key used in earlier versions
    "token",      # common in custom LLM APIs
    "text",       # used by some Hugging Face / FastAPI servers
    "content",    # OpenAI chat format (choices[0].delta.content)
    "delta",      # some servers flatten the delta object
    "message",    # occasional variant
    "output",     # used by some inference servers
    "response",   # occasional variant
    "chunk",      # used by some streaming APIs
    "answer",     # domain-specific APIs
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def estimate_input_tokens(payload: dict) -> int:
    """
    Estimate the number of input tokens from a request payload.

    Uses a coarse approximation of ~1 token per 4 characters of compact
    JSON.  This is a reasonable lower-bound for English text; real
    tokenisation depends on the model vocabulary.

    Parameters
    ----------
    payload : dict
        The request payload that will be sent to the API.

    Returns
    -------
    int
        Estimated token count (minimum 1 if payload is non-empty).
    """
    if not payload:
        return 0
    payload_str = json.dumps(payload, separators=(",", ":"))
    return max(1, len(payload_str) // 4)


def _extract_token(data: dict, token_keys: List[str], discovered_key: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Extract a token string from a parsed SSE JSON object.

    If discovered_key is already set (we have seen a token before) we use
    that key directly.  Otherwise we probe token_keys in order and return
    the first non-empty hit along with the key name so the caller can
    cache it.

    Parameters
    ----------
    data : dict
        Parsed JSON object from the SSE data line.
    token_keys : list of str
        Keys to probe, in priority order.
    discovered_key : str or None
        The key that worked on a previous event, or None.

    Returns
    -------
    (token_text, key_used)
        token_text is "" if nothing was found.
        key_used is the key that produced the text, or None.
    """
    # OpenAI-style nested path: choices[0].delta.content
    # Try this first as a special case.
    if discovered_key is None:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta", {})
            text = delta.get("content", "")
            if text and not text.isspace():
                return text, "choices[0].delta.content"

    if discovered_key == "choices[0].delta.content":
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            text = choices[0].get("delta", {}).get("content", "")
            return text, discovered_key

    # Standard flat key lookup.
    keys_to_try = [discovered_key] if discovered_key else token_keys
    for key in keys_to_try:
        val = data.get(key, "")
        if isinstance(val, str) and val and not val.isspace():
            return val, key

    return "", None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def measure_sse_stream(
    url: str,
    payload: dict,
    headers: Optional[Dict[str, str]] = None,
    cert: Optional[Tuple[str, str]] = None,
    timeout: float = 120.0,
    verbose: bool = False,
    token_keys: Optional[List[str]] = None,
) -> StreamingMetrics:
    """
    POST payload to url and measure SSE streaming performance.

    Opens a streaming HTTP connection, consumes every SSE event, and
    records the key latency milestones defined in StreamingMetrics.

    Parameters
    ----------
    url : str
        Fully-qualified URL of the SSE endpoint.
    payload : dict
        JSON-serialisable dict sent as the POST body.
    headers : dict, optional
        Extra HTTP request headers (e.g. Authorization).  SSE-specific
        headers are added automatically without overwriting yours.
    cert : (cert_file, key_file), optional
        Mutual-TLS certificate tuple forwarded to requests.
    timeout : float
        Socket connect + read timeout in seconds (default 120).
    verbose : bool
        When True, print each raw SSE line AND each extracted token.
        Use this to discover what key your server uses.
    token_keys : list of str, optional
        Override the default key probe list.  Supply a single-element
        list (e.g. ["token"]) once you know your server's key.

    Returns
    -------
    StreamingMetrics
        Populated metrics.  On error, the error field is set and timing
        fields that could not be measured remain -1.

    Notes
    -----
    verify=False is used so self-signed certs common in staging do not
    block tests.  Switch to verify=True or a CA-bundle path in production.
    """
    effective_token_keys = token_keys if token_keys is not None else _DEFAULT_TOKEN_KEYS

    metrics = StreamingMetrics(
        stream_start_time_ms=-1,
        time_to_first_token_ms=-1,
        time_to_last_token_ms=-1,
        total_response_time_ms=-1,
        input_tokens=estimate_input_tokens(payload),
        output_tokens=0,
    )

    merged_headers = {**_SSE_HEADERS, **(headers or {})}
    start_time = time.perf_counter()
    first_token_received = False
    discovered_key: Optional[str] = None  # cached once the first token is found

    try:
        with requests.post(
            url,
            json=payload,
            headers=merged_headers,
            cert=cert,
            stream=True,
            timeout=timeout,
            verify=False,  # switch to True / CA-bundle in production
        ) as response:
            metrics.stream_start_time_ms = (time.perf_counter() - start_time) * 1000
            response.raise_for_status()

            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue

                elapsed_ms = (time.perf_counter() - start_time) * 1000

                if verbose:
                    print("[SSE RAW] %s" % raw_line)

                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if data_str.lower() in _DONE_SENTINELS:
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    # Non-JSON lines (comments, keep-alives) are ignored.
                    continue

                if not isinstance(data, dict):
                    continue

                token, used_key = _extract_token(data, effective_token_keys, discovered_key)

                if token:
                    # Cache the winning key for all subsequent events.
                    if discovered_key is None:
                        discovered_key = used_key
                        metrics.token_key = used_key
                        print("[SSE] Token key detected: '%s'" % used_key)

                    metrics.output_tokens += 1
                    metrics.time_to_last_token_ms = elapsed_ms

                    if not first_token_received:
                        metrics.time_to_first_token_ms = elapsed_ms
                        first_token_received = True

                    if verbose:
                        print(token, end="", flush=True)

    except Exception as exc:  # noqa: BLE001
        metrics.error = str(exc)

    metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
    return metrics
