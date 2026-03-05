#!/usr/bin/env python3
"""
generic_sse_AiAgentic_performance_tester.py
-------------------------------------------
Generic SSE / Non-Streaming API Performance Testing Script.

Accepts a list of API endpoint configurations (URL, HTTP method, payload,
headers, certificates) and measures streaming performance metrics, exporting
results to an Excel workbook.

Usage
-----
    # Config-driven (recommended)
    python generic_sse_AiAgentic_performance_tester.py --config config.json

    # Single endpoint via CLI
    python generic_sse_AiAgentic_performance_tester.py \\
        --url https://api.example.com/stream \\
        --method POST \\
        --payload payload.json

Config file formats
-------------------
Legacy (array):
    [
        { "url": "...", "method": "POST", "payload": {...}, ... }
    ]

With variables and response-chaining (recommended):
    {
        "variables": { "AiOrchestrationUrl": "https://..." },
        "endpoints": [
            { "url": "{{AiOrchestrationUrl}}/field_tagging/tag", ... },
            {
                "url": "{{AiOrchestrationUrl}}/job_handler/get_job?job_uuid={{job_uuid}}",
                ...
            }
        ]
    }

Endpoint config keys
--------------------
url         : (required) Full endpoint URL.  Supports {{var}} / ${var} templates.
method      : "GET" or "POST" (default "POST").
responsetype: "Streaming" (default) or "NonStreaming".
endpointtype: Free-text label included verbatim in the output report.
payload     : JSON object sent as the POST body.  Supports templates.
headers     : HTTP headers dict.  Supports templates.
cert        : [cert_file, key_file] for mutual-TLS, or a single cert_file string.
timeout     : Per-request timeout in seconds (default 120.0).
extract     : {"varName": "$.json.path"} - extract values from a NonStreaming
              response into variables available to subsequent endpoints.
set         : {"varName": "value"} - set variables before this endpoint runs.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests
import urllib3

# Suppress InsecureRequestWarning for environments that use self-signed certs.
# Remove in production and set verify=True on all requests.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Dependency: streaming_metrics module
# ---------------------------------------------------------------------------
try:
    from streaming_metrics import StreamingMetrics, estimate_input_tokens, measure_sse_stream
except ImportError:
    # Allow running from any working directory by adding the script's own
    # directory to sys.path as a fallback.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _script_dir)
    from streaming_metrics import StreamingMetrics, estimate_input_tokens, measure_sse_stream


# ---------------------------------------------------------------------------
# Optional debug logging
# ---------------------------------------------------------------------------

#: Absolute path for the append-only NDJSON debug log.  Set to ``None`` or
#: an empty string to disable debug logging entirely.
DEBUG_LOG_PATH: Optional[str] = os.environ.get("PERF_DEBUG_LOG")


def _debug_log(
    session_id: str,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
) -> None:
    """
    Append a structured NDJSON entry to the debug log file.

    Silently swallows all I/O errors so that logging never interrupts
    the main test flow.  Logging is a no-op when ``DEBUG_LOG_PATH`` is
    falsy.

    Parameters
    ----------
    session_id, run_id, hypothesis_id:
        Identifiers used to correlate entries across a test session.
    location:
        Human-readable source location, e.g. ``"module.py:line"``.
    message:
        Short description of the event being logged.
    data:
        Arbitrary dict of contextual values.
    """
    if not DEBUG_LOG_PATH:
        return
    try:
        entry = {
            "sessionId": session_id,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(DEBUG_LOG_PATH, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

#: Matches ``{{identifier}}`` placeholders.
_DOUBLE_BRACE_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
#: Matches ``${identifier}`` placeholders.
_DOLLAR_BRACE_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _render_template_str(s: str, variables: Dict[str, Any]) -> str:
    """
    Substitute ``{{var}}`` and ``${var}`` placeholders in *s*.

    Resolution order:
    1. *variables* dict (caller-supplied runtime values).
    2. Process environment variables (``os.environ``).
    3. If neither source has the key, the placeholder is left unchanged.

    Parameters
    ----------
    s:
        Template string, e.g. ``"https://{{host}}/api"``.
    variables:
        Mapping of variable names to their current values.

    Returns
    -------
    str
        The string with all resolvable placeholders replaced.
    """
    if not isinstance(s, str) or not s:
        return s

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        if key in os.environ:
            return str(os.environ[key])
        return m.group(0)  # leave unknown placeholders intact

    s = _DOUBLE_BRACE_RE.sub(_repl, s)
    s = _DOLLAR_BRACE_RE.sub(_repl, s)
    return s


def render_templates(obj: Any, variables: Dict[str, Any]) -> Any:
    """
    Recursively render ``{{var}}`` / ``${var}`` templates in *obj*.

    Traverses nested dicts and lists, applying
    :func:`_render_template_str` to every string value encountered.
    Non-string leaves are returned unchanged.

    Parameters
    ----------
    obj:
        A string, list, dict, or any other value.
    variables:
        Variable bindings used during substitution.

    Returns
    -------
    Any
        A new object of the same structure with templates resolved.
    """
    if isinstance(obj, str):
        return _render_template_str(obj, variables)
    if isinstance(obj, list):
        return [render_templates(v, variables) for v in obj]
    if isinstance(obj, dict):
        return {k: render_templates(v, variables) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# JSON-path helpers
# ---------------------------------------------------------------------------

def _parse_json_path(path: str) -> List[Union[str, int]]:
    """
    Parse a simple JSON-path expression into a list of access tokens.

    Supported forms:
      - ``"job_uuid"``
      - ``"data.job_uuid"``
      - ``"$.data.job_uuid"``
      - ``"items[0].id"``

    Parameters
    ----------
    path:
        The JSON-path string to parse.

    Returns
    -------
    list of str | int
        Ordered access tokens.  Numeric bracket indices are returned as
        ``int``; all other segments are ``str``.
    """
    if not path:
        return []

    p = str(path).strip().lstrip("$").lstrip(".")
    tokens: List[Union[str, int]] = []

    for part in p.split("."):
        if not part:
            continue
        # Extract the bare key name before any bracket indices.
        m = re.match(r"^([^\[]+)", part)
        if m:
            tokens.append(m.group(1))
        # Extract all bracket indices, e.g. "[0][1]" -> [0, 1].
        for idx in re.findall(r"\[(\d+)\]", part):
            tokens.append(int(idx))

    return tokens


def get_value_by_path(data: Any, path: str) -> Any:
    """
    Retrieve a nested value from *data* using a simple JSON-path expression.

    Parameters
    ----------
    data:
        Parsed JSON object (dict, list, or scalar).
    path:
        JSON-path string as accepted by :func:`_parse_json_path`.

    Returns
    -------
    Any
        The value found at the given path.

    Raises
    ------
    KeyError
        If any segment of the path does not exist in *data*.
    """
    cur = data
    for tok in _parse_json_path(path):
        if isinstance(tok, int):
            if not isinstance(cur, list) or tok >= len(cur):
                raise KeyError(f"Index {tok} not found for path '{path}'")
            cur = cur[tok]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                raise KeyError(f"Key '{tok}' not found for path '{path}'")
            cur = cur[tok]
    return cur


# ---------------------------------------------------------------------------
# SSE GET measurement
# ---------------------------------------------------------------------------

def measure_sse_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    cert: Optional[Tuple[str, str]] = None,
    timeout: float = 120.0,
    verbose: bool = False,
) -> StreamingMetrics:
    """
    Measure SSE streaming performance for a plain GET request.

    Functionally identical to :func:`streaming_metrics.measure_sse_stream`
    but issues a GET rather than a POST.  Useful for endpoints that initiate
    a stream by polling a URL (e.g. job-status endpoints).

    Parameters
    ----------
    url:
        Fully-qualified SSE endpoint URL.
    headers:
        Optional HTTP headers.  SSE headers are added automatically.
    cert:
        Optional ``(cert_file, key_file)`` tuple for mutual-TLS.
    timeout:
        Socket timeout in seconds (default 120 s).
    verbose:
        Print tokens to stdout as they arrive.

    Returns
    -------
    StreamingMetrics
        Populated metrics object (``input_tokens`` is always 0 for GET).
    """
    _SSE_DEFAULTS = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    merged_headers = {**_SSE_DEFAULTS, **(headers or {})}

    metrics = StreamingMetrics(
        stream_start_time_ms=-1,
        time_to_first_token_ms=-1,
        time_to_last_token_ms=-1,
        total_response_time_ms=-1,
        input_tokens=0,  # GET requests carry no body -> no input tokens
        output_tokens=0,
    )

    start_time = time.perf_counter()
    first_token_received = False
    _DONE_SENTINELS = {"[done]", "done"}

    try:
        with requests.get(
            url,
            headers=merged_headers,
            cert=cert,
            stream=True,
            timeout=timeout,
            verify=False,  # <- switch to True in production
        ) as response:
            metrics.stream_start_time_ms = (time.perf_counter() - start_time) * 1000
            response.raise_for_status()

            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue

                elapsed_ms = (time.perf_counter() - start_time) * 1000
                line = raw_line.strip()

                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if data_str.lower() in _DONE_SENTINELS:
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                token = data.get("data", "")
                if token and not token.isspace():
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


# ---------------------------------------------------------------------------
# Non-streaming measurement
# ---------------------------------------------------------------------------

def measure_nonstreaming_request(
    method: str,
    url: str,
    payload: dict,
    headers: Optional[Dict[str, str]] = None,
    cert: Optional[Tuple[str, str]] = None,
    timeout: float = 120.0,
    verbose_response: bool = False,
    response_max_length: int = 5_000,
    is_multipart: bool = False,
    on_response: Optional[Callable[[str, Optional[requests.Response]], None]] = None,
) -> StreamingMetrics:
    """
    Measure performance for a conventional (non-streaming) HTTP endpoint.

    For non-streaming responses only ``stream_start_time_ms`` (TTFB) and
    ``total_response_time_ms`` carry meaningful values; token metrics are
    not applicable and remain at their sentinel ``-1``.

    Parameters
    ----------
    method:
        ``"GET"`` or ``"POST"``.
    url:
        Target endpoint URL.
    payload:
        Request body for POST requests.  For multipart uploads the values
        keyed ``"file"`` / ``"files"`` must be local file paths (str or
        list of str); all other keys become form fields.
    headers:
        Optional HTTP headers.  When *is_multipart* is ``True``, any
        ``Content-Type`` header is stripped so ``requests`` can set the
        correct ``multipart/form-data; boundary=...`` value automatically.
    cert:
        Optional ``(cert_file, key_file)`` for mutual-TLS.
    timeout:
        Socket timeout in seconds (default 120 s).
    verbose_response:
        Print the (possibly truncated) response body to stdout.
    response_max_length:
        Maximum number of characters printed when *verbose_response* is
        ``True``.
    is_multipart:
        Set ``True`` to send a ``multipart/form-data`` upload instead of
        a JSON body.
    on_response:
        Optional callback ``(response_text, response_obj) -> None`` invoked
        with the raw response body text *before* ``raise_for_status()``.
        Useful for extracting data from error responses.

    Returns
    -------
    StreamingMetrics
        Populated with TTFB and total time.  On error the ``error`` field
        describes what went wrong.
    """
    input_tokens = estimate_input_tokens(payload) if method == "POST" and not is_multipart else 0

    metrics = StreamingMetrics(
        stream_start_time_ms=-1,
        time_to_first_token_ms=-1,
        time_to_last_token_ms=-1,
        total_response_time_ms=-1,
        input_tokens=input_tokens,
        output_tokens=0,
    )

    start_time = time.perf_counter()
    response_text = ""
    response: Optional[requests.Response] = None

    try:
        if method == "GET":
            response = requests.get(
                url, headers=headers, cert=cert, timeout=timeout, verify=False
            )

        elif method == "POST":
            if is_multipart:
                response = _post_multipart(url, payload, headers, cert, timeout, metrics, start_time)
                if response is None:
                    # _post_multipart already filled metrics.error / total time
                    return metrics
            else:
                _debug_log(
                    "debug-session", "run1", "A",
                    f"{__file__}:measure_nonstreaming_request",
                    "Before POST request",
                    {"url": url, "payload": payload, "headers": headers},
                )
                response = requests.post(
                    url, json=payload, headers=headers, cert=cert, timeout=timeout, verify=False
                )
                _debug_log(
                    "debug-session", "run1", "A",
                    f"{__file__}:measure_nonstreaming_request",
                    "After POST request",
                    {"status_code": response.status_code, "response_url": response.url},
                )
        else:
            metrics.error = f"Unsupported method: {method}"
            metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
            return metrics

        # Record TTFB (headers received).
        metrics.stream_start_time_ms = (time.perf_counter() - start_time) * 1000

        response_text = getattr(response, "text", "") or ""
        if on_response:
            try:
                on_response(response_text, response)
            except Exception:  # noqa: BLE001
                pass

        response.raise_for_status()
        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000

        if verbose_response:
            _print_response_body(response_text, response_max_length, label="VERBOSE-RESPONSE")

    except Exception as exc:  # noqa: BLE001
        # Build a richer error message for common HTTP errors.
        error_msg = str(exc)
        if response is not None and getattr(response, "status_code", None) == 404:
            error_msg = (
                f"{exc}. The endpoint may not exist on the server. "
                "Please verify the endpoint path is correct."
            )
        metrics.error = error_msg
        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000

        if verbose_response and response_text:
            _print_response_body(response_text, response_max_length, label="Partial Response Body")

        _debug_log(
            "debug-session", "run1", "A",
            f"{__file__}:measure_nonstreaming_request",
            "Exception caught",
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "status_code": getattr(response, "status_code", None),
                "response_text": response_text[:500],
            },
        )

    return metrics


def _post_multipart(
    url: str,
    payload: dict,
    headers: Optional[Dict[str, str]],
    cert: Optional[Tuple[str, str]],
    timeout: float,
    metrics: StreamingMetrics,
    start_time: float,
) -> Optional[requests.Response]:
    """
    Issue a multipart/form-data POST and return the response.

    File fields are identified by the key names ``"file"`` and ``"files"``.
    All other payload keys become regular form fields.

    On failure, populates *metrics.error* and *metrics.total_response_time_ms*
    and returns ``None`` so the caller can short-circuit.

    Parameters
    ----------
    url, payload, headers, cert, timeout:
        Forwarded from :func:`measure_nonstreaming_request`.
    metrics:
        The :class:`StreamingMetrics` instance to update on error.
    start_time:
        ``time.perf_counter()`` value captured at the start of the request.

    Returns
    -------
    requests.Response or None
    """
    # Strip Content-Type so requests can set the correct multipart boundary.
    upload_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "content-type"}
    files_list: List[Tuple[str, Any]] = []
    opened_files: List[Any] = []
    form_data: Dict[str, Any] = {}

    try:
        for key, value in payload.items():
            is_file_field = str(key).strip().lower() in ("file", "files")
            if is_file_field:
                file_paths = value if isinstance(value, list) else [value]
                for path in file_paths:
                    if not isinstance(path, str):
                        metrics.error = f"Invalid file path type for '{key}': {type(path).__name__}"
                        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
                        return None
                    try:
                        fh = open(path, "rb")
                        opened_files.append(fh)
                        files_list.append((key, fh))
                    except FileNotFoundError:
                        metrics.error = f"File not found: {path}"
                        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
                        return None
            else:
                form_data[key] = value

        return requests.post(
            url,
            files=files_list or None,
            data=form_data or None,
            headers=upload_headers,
            cert=cert,
            timeout=timeout,
            verify=False,
        )
    finally:
        # Always close file handles, even if an exception is raised.
        for fh in opened_files:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


def _print_response_body(text: str, max_length: int, label: str = "Response Body") -> None:
    """
    Pretty-print a (possibly truncated) response body to stdout.

    Parameters
    ----------
    text:
        Raw response body string.
    max_length:
        Maximum number of characters to print.  A truncation notice is
        appended when the body exceeds this limit.
    label:
        Short label shown in the printed header.
    """
    truncated = text if not max_length or len(text) <= max_length else text[:max_length] + "\n...[truncated]..."
    print(f"\n[{label}]")
    try:
        print(json.dumps(json.loads(truncated), indent=4, ensure_ascii=False))
    except (json.JSONDecodeError, TypeError):
        print(truncated)
    print()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config_from_file(config_file: str) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    Load endpoint configurations from a JSON file.

    Supports two formats:

    **Legacy (array)** - backward-compatible, no variable support:

    .. code-block:: json

        [
            { "url": "...", "method": "POST", ... }
        ]

    **Object with variables + endpoints** - recommended for chaining:

    .. code-block:: json

        {
            "variables": { "AiOrchestrationUrl": "https://..." },
            "endpoints": [
                { "url": "{{AiOrchestrationUrl}}/tag", ... },
                { "url": "{{AiOrchestrationUrl}}/job?id={{job_uuid}}", ... }
            ]
        }

    Parameters
    ----------
    config_file:
        Path to the JSON configuration file.

    Returns
    -------
    (endpoints, variables)
        *endpoints* is the list of endpoint dicts.
        *variables* is the initial variable mapping (empty dict for legacy
        format).

    Raises
    ------
    SystemExit
        On missing file, invalid JSON, or schema violations.
    """
    try:
        with open(config_file, "r") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        print(f"Error: Config file '{config_file}' not found.")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in config file: {exc}")
        sys.exit(1)

    if isinstance(config, list):
        return config, {}

    if isinstance(config, dict):
        endpoints = config.get("endpoints")
        if not isinstance(endpoints, list):
            print("Error: Config object must contain an 'endpoints' array.")
            sys.exit(1)

        variables = config.get("variables") or {}
        if not isinstance(variables, dict):
            print("Error: Config 'variables' must be an object/dictionary.")
            sys.exit(1)

        return endpoints, variables

    print("Error: Config file must contain a JSON array OR an object with {variables, endpoints}.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

#: Column order used for the Excel output.
_COLUMN_ORDER = [
    "Endpoint",
    "Endpoint Type",
    "Response Type",
    "Method",
    "Stream Start (ms)",
    "Time to First Token (ms)",
    "Time to Last Token (ms)",
    "Total Response Time (ms)",
    "Input Tokens",
    "Output Tokens",
    "Error",
]


def _build_result(
    url: str,
    endpointtype: str,
    responsetype: str,
    method: str,
    metrics: StreamingMetrics,
    is_nonstreaming: bool,
) -> Dict[str, Any]:
    """
    Assemble a single result dict from a completed :class:`StreamingMetrics`.

    Non-streaming endpoints use ``"-"`` for token fields that don't apply.

    Parameters
    ----------
    url, endpointtype, responsetype, method:
        Endpoint metadata.
    metrics:
        Completed metrics object.
    is_nonstreaming:
        Whether the endpoint was non-streaming.

    Returns
    -------
    dict
        A row ready for inclusion in the results list / DataFrame.
    """
    def _ms(val: float) -> Optional[float]:
        return val if val >= 0 else None

    base = {
        "Endpoint": url,
        "Endpoint Type": endpointtype,
        "Response Type": responsetype,
        "Method": method,
        "Stream Start (ms)": _ms(metrics.stream_start_time_ms),
        "Total Response Time (ms)": _ms(metrics.total_response_time_ms),
        "Input Tokens": metrics.input_tokens,
        "Error": metrics.error or "",
    }

    if is_nonstreaming:
        base.update({
            "Time to First Token (ms)": "-",
            "Time to Last Token (ms)": "-",
            "Output Tokens": "-",
        })
    else:
        base.update({
            "Time to First Token (ms)": _ms(metrics.time_to_first_token_ms),
            "Time to Last Token (ms)": _ms(metrics.time_to_last_token_ms),
            "Output Tokens": metrics.output_tokens,
        })

    return base


def _error_result(
    url: str,
    endpointtype: str,
    responsetype: str,
    method: str,
    error: str,
    is_nonstreaming: bool,
) -> Dict[str, Any]:
    """
    Build a result dict for an endpoint that raised an unexpected exception.

    Parameters
    ----------
    url, endpointtype, responsetype, method:
        Endpoint metadata.
    error:
        String representation of the exception.
    is_nonstreaming:
        Whether the endpoint was non-streaming.

    Returns
    -------
    dict
    """
    sentinel = "-" if is_nonstreaming else None
    return {
        "Endpoint": url,
        "Endpoint Type": endpointtype,
        "Response Type": responsetype,
        "Method": method,
        "Stream Start (ms)": None,
        "Time to First Token (ms)": sentinel,
        "Time to Last Token (ms)": sentinel,
        "Total Response Time (ms)": None,
        "Input Tokens": 0,
        "Output Tokens": sentinel,
        "Error": error,
    }


# ---------------------------------------------------------------------------
# Core test runner
# ---------------------------------------------------------------------------

def run_performance_tests(
    endpoints: List[Dict],
    verbose: bool = False,
    verbose_response: bool = False,
    response_max_length: int = 5_000,
    variables: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """
    Execute performance tests against each endpoint in *endpoints*.

    Processes endpoints sequentially.  Supports variable chaining: a
    NonStreaming endpoint can ``"extract"`` values from its response JSON and
    make them available (via template substitution) to all subsequent
    endpoints.

    Parameters
    ----------
    endpoints:
        List of endpoint configuration dicts.  See module docstring for the
        full list of supported keys.
    verbose:
        Print SSE tokens to stdout as they arrive.
    verbose_response:
        Print NonStreaming response bodies to stdout.
    response_max_length:
        Maximum characters printed for verbose response bodies.
    variables:
        Initial variable mapping (merged with any ``"set"`` directives).

    Returns
    -------
    list of dict
        One entry per endpoint containing all ``_COLUMN_ORDER`` keys.
    """
    results: List[Dict] = []
    variables = dict(variables or {})

    for idx, endpoint in enumerate(endpoints, 1):
        # Apply any ``"set"`` directives before processing this endpoint.
        set_vars = endpoint.get("set", {})
        if isinstance(set_vars, dict) and set_vars:
            variables.update(render_templates(set_vars, variables))

        url = render_templates(endpoint.get("url"), variables)
        method = endpoint.get("method", "POST").upper()
        endpointtype = endpoint.get("endpointtype", "")

        # Normalise responsetype to "Streaming" or "NonStreaming".
        responsetype_raw = str(endpoint.get("responsetype", "Streaming")).strip()
        responsetype_key = responsetype_raw.lower().replace("-", "").replace(" ", "")
        is_nonstreaming = responsetype_key in ("nonstreaming", "nonstream")
        responsetype = "NonStreaming" if is_nonstreaming else "Streaming"

        payload = render_templates(endpoint.get("payload", {}), variables)
        headers = render_templates(endpoint.get("headers", {}), variables)
        cert_cfg = endpoint.get("cert")
        timeout = float(endpoint.get("timeout", 120.0))
        extract_map = endpoint.get("extract", {})

        if not url:
            print(f"Warning: Endpoint {idx} missing 'url', skipping...")
            continue

        # Build cert tuple from list, path string, or None.
        cert: Optional[Tuple[str, str]] = None
        if isinstance(cert_cfg, (list, tuple)) and len(cert_cfg) == 2:
            cert = (cert_cfg[0], cert_cfg[1])
        elif isinstance(cert_cfg, str):
            cert = (cert_cfg, None)

        # ---- Print header --------------------------------------------------
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"Testing endpoint {idx}/{len(endpoints)}: {url}")
        print(f"Method: {method}")
        print(sep)

        if verbose:
            print("\n[VERBOSE] Request Details:")
            print(f"  URL    : {url}")
            print(f"  Method : {method}")
            if headers:
                print(f"  Headers: {json.dumps(headers, indent=4)}")
            if payload and method == "POST":
                print(f"  Payload: {json.dumps(payload, indent=4)}")
            if cert:
                print(f"  Cert   : {cert[0] or 'None'}")
                print(f"  Key    : {cert[1] or 'None'}")
            print(f"  Timeout: {timeout}s")
            print(f"  Endpoint Type : {endpointtype or 'Not specified'}")
            print(f"  Response Type : {responsetype}")
            print()

        _debug_log(
            "debug-session", "run1", "A",
            f"{__file__}:run_performance_tests",
            "Endpoint config loaded",
            {"endpoint_idx": idx, "url": url, "raw_endpoint": endpoint, "variables": variables},
        )

        # ---- Closure to capture response text for extraction ---------------
        last_response_text: str = ""
        last_response_json: Optional[Any] = None

        def _capture_response(resp_text: str, _resp: Optional[requests.Response]) -> None:
            nonlocal last_response_text, last_response_json
            last_response_text = resp_text or ""
            try:
                last_response_json = json.loads(last_response_text) if last_response_text else None
            except Exception:  # noqa: BLE001
                last_response_json = None

        # ---- Dispatch to the appropriate measurement function --------------
        try:
            is_multipart = (headers or {}).get("Content-Type", "").lower() == "multipart/form-data"

            if is_nonstreaming:
                metrics = measure_nonstreaming_request(
                    method=method,
                    url=url,
                    payload=payload,
                    headers=headers,
                    cert=cert,
                    timeout=timeout,
                    verbose_response=verbose_response,
                    response_max_length=response_max_length,
                    is_multipart=is_multipart,
                    on_response=_capture_response,
                )
            elif method == "GET":
                metrics = measure_sse_get(
                    url=url, headers=headers, cert=cert, timeout=timeout, verbose=verbose
                )
            elif method == "POST":
                metrics = measure_sse_stream(
                    url=url, payload=payload, headers=headers, cert=cert, timeout=timeout, verbose=verbose
                )
            else:
                print(f"Error: Unsupported method '{method}'. Only GET and POST are supported.")
                metrics = StreamingMetrics(
                    stream_start_time_ms=-1,
                    time_to_first_token_ms=-1,
                    time_to_last_token_ms=-1,
                    total_response_time_ms=-1,
                    input_tokens=0,
                    output_tokens=0,
                    error=f"Unsupported method: {method}",
                )

            result = _build_result(url, endpointtype, responsetype, method, metrics, is_nonstreaming)
            results.append(result)

            # ---- Variable extraction (NonStreaming only) -------------------
            if isinstance(extract_map, dict) and extract_map:
                if not is_nonstreaming:
                    print(f"  [WARN] Endpoint {idx} has 'extract' rules but is Streaming. Skipping.")
                elif last_response_json is None:
                    print(f"  [WARN] Endpoint {idx} response is not valid JSON. Skipping extraction.")
                else:
                    for var_name, json_path in extract_map.items():
                        try:
                            value = get_value_by_path(last_response_json, str(json_path))
                            variables[var_name] = value
                            if verbose:
                                print(f"  [CHAIN] Extracted {var_name} = {value!r} from '{json_path}'")
                        except Exception as ex:  # noqa: BLE001
                            print(f"  [WARN] Failed to extract '{var_name}' from '{json_path}': {ex}")

        except Exception as exc:  # noqa: BLE001
            print(f"Error testing endpoint {idx}: {exc}")
            results.append(_error_result(url, endpointtype, responsetype, method, str(exc), is_nonstreaming))
            metrics = StreamingMetrics(-1, -1, -1, -1)  # for summary below

        # ---- Print per-endpoint summary ------------------------------------
        print(f"\nResults for endpoint {idx}:")
        print(f"  Stream Start  : {metrics.stream_start_time_ms:8.1f} ms" if metrics.stream_start_time_ms >= 0 else "  Stream Start  : N/A")
        print(f"  First Token   : {metrics.time_to_first_token_ms:8.1f} ms" if metrics.time_to_first_token_ms >= 0 else "  First Token   : N/A")
        print(f"  Last Token    : {metrics.time_to_last_token_ms:8.1f} ms" if metrics.time_to_last_token_ms >= 0 else "  Last Token    : N/A")
        print(f"  Total Response: {metrics.total_response_time_ms:8.1f} ms" if metrics.total_response_time_ms >= 0 else "  Total Response: N/A")
        print(f"  Input Tokens  : {metrics.input_tokens}")
        print(f"  Output Tokens : {metrics.output_tokens}")
        if metrics.error:
            print(f"  Error         : {metrics.error}")

    return results


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def export_to_excel(results: List[Dict], output_file: str) -> None:
    """
    Export test results to a formatted Excel workbook.

    Creates any missing parent directories automatically.  Columns are
    ordered per ``_COLUMN_ORDER`` and widths are auto-adjusted to content.

    Parameters
    ----------
    results:
        List of result dicts as produced by :func:`run_performance_tests`.
    output_file:
        Destination ``.xlsx`` file path.
    """
    if not results:
        print("No results to export.")
        return

    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        print(f"Created directory: {output_dir}")

    df = pd.DataFrame(results)[_COLUMN_ORDER]

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Performance Metrics", index=False)
        ws = writer.sheets["Performance Metrics"]

        # Auto-size columns (capped at 50 characters to avoid overly wide cells).
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=0)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"Results exported to: {output_file}")
    print(sep)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Parse command-line arguments and execute the performance test suite.

    Supports two modes:

    1. **Config file** (``--config``): full feature set including multiple
       endpoints, variables, chaining, and certificate configuration.
    2. **Single endpoint** (``--url``): quick ad-hoc test of a single URL.

    Exits with code 1 on argument errors.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Generic SSE Performance Testing Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Config file
  python generic_sse_AiAgentic_performance_tester.py --config endpoints.json

  # Single endpoint
  python generic_sse_AiAgentic_performance_tester.py \\
      --url https://api.example.com/stream --method POST --payload payload.json
""",
    )
    parser.add_argument("--config", help="Path to JSON config file containing endpoint list")
    parser.add_argument("--url", help="Single endpoint URL to test")
    parser.add_argument("--method", choices=["GET", "POST"], help="HTTP method")
    parser.add_argument("--payload", help="Path to JSON file containing request payload (POST)")
    parser.add_argument("--headers", help="Path to JSON file containing request headers")
    parser.add_argument("--cert", nargs=2, metavar=("CERT_FILE", "KEY_FILE"), help="Mutual-TLS cert and key files")
    parser.add_argument("--timeout", type=float, default=120.0, help="Request timeout in seconds (default: 120)")
    parser.add_argument("--output", default=None, help="Output Excel file path")
    parser.add_argument("--reportprefix", default=None, help="Prefix for auto-generated Excel filename")
    parser.add_argument("--verbose", action="store_true", help="Print SSE tokens as they arrive")
    parser.add_argument("--verbose-response", action="store_true", help="Print NonStreaming response bodies")

    args = parser.parse_args()

    endpoints: List[Dict] = []
    variables: Dict[str, Any] = {}

    if args.config:
        endpoints, variables = load_config_from_file(args.config)

    elif args.url:
        endpoint: Dict[str, Any] = {
            "url": args.url,
            "method": (args.method or "POST").upper(),
            "timeout": args.timeout,
        }

        if args.payload:
            try:
                with open(args.payload, "r") as fh:
                    endpoint["payload"] = json.load(fh)
            except FileNotFoundError:
                print(f"Error: Payload file '{args.payload}' not found.")
                sys.exit(1)
            except json.JSONDecodeError as exc:
                print(f"Error: Invalid JSON in payload file: {exc}")
                sys.exit(1)
        elif endpoint["method"] == "POST":
            endpoint["payload"] = {}

        if args.headers:
            try:
                with open(args.headers, "r") as fh:
                    endpoint["headers"] = json.load(fh)
            except FileNotFoundError:
                print(f"Error: Headers file '{args.headers}' not found.")
                sys.exit(1)
            except json.JSONDecodeError as exc:
                print(f"Error: Invalid JSON in headers file: {exc}")
                sys.exit(1)

        if args.cert:
            endpoint["cert"] = list(args.cert)

        endpoints = [endpoint]

    else:
        parser.print_help()
        print("\nError: Either --config or --url must be provided.")
        sys.exit(1)

    if not endpoints:
        print("Error: No endpoints to test.")
        sys.exit(1)

    print(f"\nStarting performance tests for {len(endpoints)} endpoint(s)...")
    results = run_performance_tests(
        endpoints,
        verbose=args.verbose,
        verbose_response=args.verbose_response,
        variables=variables,
    )

    if args.output:
        output_file = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{args.reportprefix}_" if args.reportprefix else ""
        output_file = f"reports/{prefix}sse_performance_{timestamp}.xlsx"

    export_to_excel(results, output_file)


if __name__ == "__main__":
    main()
