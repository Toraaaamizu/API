#!/usr/bin/env python3
"""
streaming_metrics.py
--------------------
Utilities for measuring Server-Sent Events (SSE) streaming performance.

Provides:
  - StreamingMetrics  : dataclass holding all timing and token-count results
  - estimate_input_tokens : lightweight token estimator from a request payload
  - measure_sse_stream    : POST a JSON payload and record SSE timing metrics

Typical usage
-------------
    from streaming_metrics import measure_sse_stream

    metrics = measure_sse_stream(
        url="https://api.example.com/stream",
        payload={"prompt": "Hello", "model": "my-model"},
        headers={"Authorization": "Bearer <token>"},
        verbose=True,
    )
    print(f"TTFT: {metrics.time_to_first_token_ms:.1f} ms")
"""

import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import requests
import urllib3

# Suppress the InsecureRequestWarning that fires when verify=False.
# Remove this suppression (and flip verify=True) before deploying to production.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass
class StreamingMetrics:
    """
    Container for streaming response performance metrics.

    All time fields are in milliseconds relative to the moment
    requests.post() is called (i.e. ``t=0`` is just before the network call).

    Attributes
    ----------
    stream_start_time_ms:
        Elapsed time until the HTTP response headers are received and the
        response body (stream) is ready to be consumed.
    time_to_first_token_ms:
        Elapsed time until the first non-whitespace token arrives.
        ``-1`` if no token was ever received.
    time_to_last_token_ms:
        Elapsed time of the most-recently received non-whitespace token.
        ``-1`` if no token was ever received.
    total_response_time_ms:
        Elapsed time from request start until the stream terminates
        (either via a ``[DONE]`` sentinel or a network-level EOF).
    input_tokens:
        Estimated number of input tokens derived from the serialised payload
        (~1 token per 4 characters).  Purely an approximation.
    output_tokens:
        Count of non-whitespace ``data`` tokens observed during streaming.
    error:
        Human-readable error message if the request or stream failed,
        ``None`` otherwise.
    """

    stream_start_time_ms: float
    time_to_first_token_ms: float
    time_to_last_token_ms: float
    total_response_time_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# SSE headers required by the spec and commonly expected by proxies.
_SSE_HEADERS: Dict[str, str] = {
    "Accept": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# Sentinels that signal the end of an SSE stream.
_DONE_SENTINELS = {"[done]", "done"}


def estimate_input_tokens(payload: dict) -> int:
    """
    Estimate the number of input tokens from a request payload.

    Uses a coarse but fast approximation: ~1 token per 4 characters of the
    compact JSON representation.  This is a reasonable lower-bound estimate
    for English text; real tokenisation depends on the model vocabulary.

    Parameters
    ----------
    payload:
        The request payload dictionary that will be sent to the API.

    Returns
    -------
    int
        Estimated token count (minimum 1 if the payload is non-empty).
    """
    if not payload:
        return 0
    # Compact JSON minimises noise from formatting whitespace.
    payload_str = json.dumps(payload, separators=(",", ":"))
    return max(1, len(payload_str) // 4)


def _make_metrics(input_tokens: int = 0) -> StreamingMetrics:
    """Return a StreamingMetrics instance pre-filled with sentinel values."""
    return StreamingMetrics(
        stream_start_time_ms=-1,
        time_to_first_token_ms=-1,
        time_to_last_token_ms=-1,
        total_response_time_ms=-1,
        input_tokens=input_tokens,
        output_tokens=0,
    )


def _process_sse_line(
    line: str,
    elapsed_ms: float,
    metrics: StreamingMetrics,
    first_token_received: bool,
    verbose: bool,
) -> Tuple[bool, bool]:
    """
    Parse a single SSE data line and update *metrics* in-place.

    Parameters
    ----------
    line:
        Raw (stripped) SSE line, e.g. ``data: {"data": "Hello"}``
    elapsed_ms:
        Milliseconds elapsed since the request started.
    metrics:
        The StreamingMetrics object to update.
    first_token_received:
        Whether a non-whitespace token has already been observed.
    verbose:
        If ``True``, print each token to stdout as it arrives.

    Returns
    -------
    (done, first_token_received)
        ``done`` is ``True`` if the stream sentinel was encountered.
        ``first_token_received`` reflects updated state.
    """
    if not line.startswith("data:"):
        return False, first_token_received

    data_str = line[5:].strip()

    # Stream termination sentinels
    if data_str.lower() in _DONE_SENTINELS:
        return True, first_token_received

    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        # Non-JSON SSE lines (comments, keep-alives) are silently ignored.
        return False, first_token_received

    token = data.get("data", "")
    if token and not token.isspace():
        metrics.output_tokens += 1
        metrics.time_to_last_token_ms = elapsed_ms

        if not first_token_received:
            metrics.time_to_first_token_ms = elapsed_ms
            first_token_received = True

        if verbose:
            print(token, end="", flush=True)

    return False, first_token_received


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
) -> StreamingMetrics:
    """
    POST *payload* to *url* and measure SSE streaming performance.

    Opens a streaming HTTP connection, consumes every SSE event, and records
    the key latency milestones defined in :class:`StreamingMetrics`.

    Parameters
    ----------
    url:
        Fully-qualified URL of the SSE endpoint, e.g.
        ``https://api.example.com/v1/chat/stream``.
    payload:
        JSON-serialisable dict sent as the POST body (``Content-Type:
        application/json``).  Also used by :func:`estimate_input_tokens`.
    headers:
        Optional extra HTTP request headers (e.g. ``Authorization``).
        SSE-specific headers (``Accept``, ``Cache-Control``, ``Connection``)
        are added automatically and will not overwrite values you supply.
    cert:
        Optional ``(cert_file, key_file)`` tuple for mutual-TLS
        authentication, forwarded directly to ``requests``.
    timeout:
        Socket connect + read timeout in seconds.  Raise this for slow
        models or large outputs.  Defaults to 120 s.
    verbose:
        When ``True``, print each token to stdout as it is received.

    Returns
    -------
    StreamingMetrics
        Populated metrics object.  On network or HTTP errors the
        ``error`` field is set and timing fields that could not be measured
        remain ``-1``.

    Notes
    -----
    * ``verify=False`` is used so that self-signed certificates common in
      internal / staging environments don't block tests.  **Switch to
      ``verify=True`` (or a CA bundle path) before using in production.**
    * Output tokens are counted as the number of non-whitespace ``"data"``
      values seen in the SSE stream, not by a proper tokeniser.
    """
    metrics = _make_metrics(input_tokens=estimate_input_tokens(payload))

    # Merge caller-supplied headers with required SSE defaults.
    merged_headers = {**_SSE_HEADERS, **(headers or {})}

    start_time = time.perf_counter()
    first_token_received = False

    try:
        with requests.post(
            url,
            json=payload,
            headers=merged_headers,
            cert=cert,
            stream=True,
            timeout=timeout,
            verify=False,  # ← switch to True / CA-bundle in production
        ) as response:
            # Record when the HTTP response headers arrived and the body stream
            # is ready (equivalent to "time to first byte" at the HTTP layer).
            metrics.stream_start_time_ms = (time.perf_counter() - start_time) * 1000

            response.raise_for_status()

            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue  # Skip SSE keep-alive blank lines

                elapsed_ms = (time.perf_counter() - start_time) * 1000
                done, first_token_received = _process_sse_line(
                    raw_line.strip(),
                    elapsed_ms,
                    metrics,
                    first_token_received,
                    verbose,
                )
                if done:
                    break

    except Exception as exc:  # noqa: BLE001
        metrics.error = str(exc)

    # Always record total wall-clock time, regardless of success/failure.
    metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
    return metrics
