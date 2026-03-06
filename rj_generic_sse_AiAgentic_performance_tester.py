#!/usr/bin/env python3
"""
rj_generic_sse_AiAgentic_performance_tester.py
-----------------------------------------------
Simple SSE / HTTP Performance Tester

What this script does
---------------------
- Send HTTP requests to one or more endpoints (GET or POST).
- Measure how long the server takes to start the stream, send the first
  token, and finish the response. For non-streaming endpoints it measures
  time-to-first-byte and total response time.
- Save a simple report with these timings to an Excel (.xlsx) file.

Why this is useful
------------------
- Helps developers and QAs verify response latency and streaming behavior.
- Useful when testing SSE (Server-Sent Events) or regular HTTP APIs.

Quick start examples
--------------------
1) Test multiple endpoints defined in a JSON config file (recommended):
   python rj_generic_sse_AiAgentic_performance_tester.py --config endpoints.json

2) Test a single endpoint from the command line:
   python rj_generic_sse_AiAgentic_performance_tester.py \
       --url https://api.example.com/stream --method POST --payload payload.json

Config file formats (two options)
----------------------------------
1) Simple legacy format (an array of endpoint objects):
   [ { "url": "...", "method": "POST", "payload": {...} } ]

2) Recommended format with variables and chaining:
   {
       "variables": { "BaseUrl": "https://api.example.com" },
       "endpoints": [
           { "url": "{{BaseUrl}}/start_stream", "method": "POST", ... },
           {
               "url": "{{BaseUrl}}/status?job={{job_id}}",
               "responsetype": "NonStreaming",
               "extract": {"job_id": "data.job_uuid"}
           }
       ]
   }

Key endpoint config keys
------------------------
- url          : Full endpoint URL. Supports placeholders like {{var}} or ${var}.
- method       : GET or POST (default POST).
- responsetype : "Streaming" (default) or "NonStreaming".
- payload      : JSON object for POST requests. Can include templates.
- headers      : HTTP headers dictionary. Content-Type drives dispatch:
                   "application/json"    -> JSON POST body
                   "multipart/form-data" -> multipart file upload
- cert         : TLS cert info for mutual TLS (optional).
- timeout      : Request timeout in seconds (default 120).
- extract      : For NonStreaming responses, extract JSON values into
                 variables for later endpoints.
- set          : Set variables before running this endpoint (used for templating).
"""

import io
import json
import mimetypes
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
# Remove in production and set DEFAULT_VERIFY = True.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

# Set to True in production environments.
DEFAULT_VERIFY: bool = False

# Shared session for connection reuse across requests.
_SESSION = requests.Session()

# Default headers required by the SSE spec and commonly expected by proxies.
_SSE_DEFAULTS: Dict[str, str] = {
    "Accept": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Dependency: rj_streaming_metrics module
# ---------------------------------------------------------------------------

try:
    from rj_streaming_metrics import StreamingMetrics, estimate_input_tokens, measure_sse_stream
except ImportError:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _script_dir)
    from rj_streaming_metrics import StreamingMetrics, estimate_input_tokens, measure_sse_stream

# ---------------------------------------------------------------------------
# Optional debug logging
# ---------------------------------------------------------------------------

# Set the PERF_DEBUG_LOG environment variable to an absolute file path to
# enable append-only NDJSON debug logging. Leave unset to disable.
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

    Silently swallows all I/O errors so logging never interrupts the main
    test flow. Is a no-op when DEBUG_LOG_PATH is falsy.
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

_DOUBLE_BRACE_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_DOLLAR_BRACE_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _render_template_str(s: str, variables: Dict[str, Any]) -> str:
    """
    Substitute {{var}} and ${var} placeholders in s.

    Resolution order:
    1. variables dict (caller-supplied runtime values).
    2. Process environment variables (os.environ).
    3. Unknown placeholders are left unchanged.
    """
    if not isinstance(s, str) or not s:
        return s

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        if key in os.environ:
            return str(os.environ[key])
        return m.group(0)

    s = _DOUBLE_BRACE_RE.sub(_repl, s)
    s = _DOLLAR_BRACE_RE.sub(_repl, s)
    return s


def render_templates(obj: Any, variables: Dict[str, Any]) -> Any:
    """
    Recursively render {{var}} / ${var} templates in obj.

    Traverses nested dicts and lists, applying _render_template_str to
    every string value. Non-string leaves are returned unchanged.
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
      "job_uuid"
      "data.job_uuid"
      "$.data.job_uuid"
      "items[0].id"
    """
    if not path:
        return []

    p = str(path).strip().lstrip("$").lstrip(".")
    tokens: List[Union[str, int]] = []

    for part in p.split("."):
        if not part:
            continue
        m = re.match(r"^([^\[]+)", part)
        if m:
            tokens.append(m.group(1))
        for idx in re.findall(r"\[(\d+)\]", part):
            tokens.append(int(idx))

    return tokens


def get_value_by_path(data: Any, path: str) -> Any:
    """
    Retrieve a nested value from data using a simple JSON-path expression.

    Raises KeyError if any segment of the path does not exist.
    """
    cur = data
    for tok in _parse_json_path(path):
        if isinstance(tok, int):
            if not isinstance(cur, list) or tok >= len(cur):
                raise KeyError("Index %d not found for path '%s'" % (tok, path))
            cur = cur[tok]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                raise KeyError("Key '%s' not found for path '%s'" % (tok, path))
            cur = cur[tok]
    return cur


# ---------------------------------------------------------------------------
# Content-type helpers
# ---------------------------------------------------------------------------

def _get_content_type(headers: Optional[Dict[str, str]]) -> str:
    """
    Return the Content-Type header value (lowercased), or "" if absent.

    Handles case-insensitive header key lookup.
    """
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "content-type":
            return str(v or "").lower()
    return ""


def _is_json_request(headers: Optional[Dict[str, str]]) -> bool:
    """Return True when the caller explicitly requests application/json."""
    return "application/json" in _get_content_type(headers)


def _is_multipart_request(headers: Optional[Dict[str, str]]) -> bool:
    """Return True when the caller explicitly requests multipart/form-data."""
    return "multipart/form-data" in _get_content_type(headers)


# ---------------------------------------------------------------------------
# SSE GET measurement
# ---------------------------------------------------------------------------

def measure_sse_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    cert: Optional[Tuple[str, str]] = None,
    timeout: float = 120.0,
    verbose: bool = False,
    token_keys: Optional[List[str]] = None,
) -> StreamingMetrics:
    """
    Measure SSE streaming performance for a plain GET request.

    SSE-specific headers are added automatically and will not overwrite
    values already present in headers.

    token_keys controls which JSON field names are probed for token text.
    When None, the default probe list from rj_streaming_metrics is used.
    Use verbose=True to print raw SSE lines and discover your server key.

    Returns a StreamingMetrics object. input_tokens is always 0 for GET
    requests since they carry no body.
    """
    from rj_streaming_metrics import _DEFAULT_TOKEN_KEYS, _extract_token

    effective_token_keys = token_keys if token_keys is not None else _DEFAULT_TOKEN_KEYS
    merged_headers = {**_SSE_DEFAULTS, **(headers or {})}

    metrics = StreamingMetrics(
        stream_start_time_ms=-1,
        time_to_first_token_ms=-1,
        time_to_last_token_ms=-1,
        total_response_time_ms=-1,
        input_tokens=0,
        output_tokens=0,
    )

    start_time = time.perf_counter()
    first_token_received = False
    discovered_key: Optional[str] = None
    done_sentinels = {"[done]", "done"}

    try:
        with _SESSION.get(
            url,
            headers=merged_headers,
            cert=cert,
            stream=True,
            timeout=timeout,
            verify=DEFAULT_VERIFY,
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
                if data_str.lower() in done_sentinels:
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if not isinstance(data, dict):
                    continue

                token, used_key = _extract_token(data, effective_token_keys, discovered_key)
                if token:
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

    Dispatch logic
    --------------
    The function inspects the Content-Type header to decide how to send
    the request body:

      Content-Type: application/json    -> requests sends json=payload
      Content-Type: multipart/form-data -> _post_multipart() handles files
      (anything else / GET)             -> requests sends json=payload

    The is_multipart parameter is accepted for backward compatibility but
    the Content-Type header takes precedence.

    Only stream_start_time_ms (TTFB) and total_response_time_ms carry
    meaningful values for non-streaming responses; token fields remain -1.
    """
    # Determine actual content type from headers (takes precedence over
    # the is_multipart flag kept for backward compatibility).
    if _is_multipart_request(headers):
        effective_multipart = True
    elif _is_json_request(headers):
        effective_multipart = False
    else:
        # Fall back to the caller's flag if no Content-Type was set.
        effective_multipart = is_multipart

    input_tokens = (
        estimate_input_tokens(payload)
        if method == "POST" and not effective_multipart
        else 0
    )

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
            response = _SESSION.get(
                url, headers=headers, cert=cert, timeout=timeout, verify=DEFAULT_VERIFY
            )

        elif method == "POST":
            if effective_multipart:
                # _post_multipart returns None on error (metrics already populated).
                response = _post_multipart(url, payload, headers, cert, timeout, metrics, start_time)
                if response is None:
                    return metrics
            else:
                # JSON body (application/json or unspecified).
                _debug_log(
                    "debug-session", "run1", "A",
                    "%s:measure_nonstreaming_request" % __file__,
                    "Before JSON POST",
                    {"url": url, "payload_keys": list((payload or {}).keys())},
                )
                response = _SESSION.post(
                    url, json=payload, headers=headers, cert=cert,
                    timeout=timeout, verify=DEFAULT_VERIFY,
                )
                _debug_log(
                    "debug-session", "run1", "A",
                    "%s:measure_nonstreaming_request" % __file__,
                    "After JSON POST",
                    {"status_code": response.status_code},
                )
        else:
            metrics.error = "Unsupported method: %s" % method
            metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000
            return metrics

        # Record TTFB.
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
        error_msg = str(exc)
        if response is not None and getattr(response, "status_code", None) == 404:
            error_msg = (
                "%s. The endpoint may not exist on the server. "
                "Please verify the endpoint path is correct." % exc
            )
        metrics.error = error_msg
        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000

        if verbose_response and response_text:
            _print_response_body(response_text, response_max_length, label="Partial Response Body")

        _debug_log(
            "debug-session", "run1", "A",
            "%s:measure_nonstreaming_request" % __file__,
            "Exception caught",
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "status_code": getattr(response, "status_code", None),
                "response_text": response_text[:500],
            },
        )

    return metrics


# ---------------------------------------------------------------------------
# Multipart POST helper
# ---------------------------------------------------------------------------

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

    Payload field handling
    ----------------------
    "file" / "files" keys
        Each value must be either:
          - A dict with "path" (local file path) and optional "filename".
          - A dict with "content" (str or bytes) and optional "filename"
            and "content_type".
        A list of such dicts is accepted for multi-file uploads.

    "file_metadata_str" key
        Must be a dict. Serialised to compact JSON and sent as a form field.

    All other keys
        Sent as plain string form fields.

    Returns None and populates metrics.error on failure so the caller can
    return early without raising.
    """
    # Strip Content-Type so requests can set the correct multipart boundary.
    # Keep Authorization and all other headers intact.
    upload_headers = {
        k: v for k, v in (headers or {}).items()
        if k.lower() != "content-type"
    }
    upload_headers.setdefault("Accept", "application/json")

    files_list: List[Tuple[str, Any]] = []
    opened_files: List[Any] = []
    form_data: Dict[str, Any] = {}

    def _fail(msg: str) -> None:
        metrics.error = msg
        metrics.total_response_time_ms = (time.perf_counter() - start_time) * 1000

    try:
        for key, value in payload.items():
            is_file_field = key.lower() in ("file", "files")

            if is_file_field:
                file_items = value if isinstance(value, list) else [value]
                for item in file_items:
                    if not isinstance(item, dict):
                        _fail("File field '%s' must be a dict with 'path' or 'content'." % key)
                        return None

                    if "path" in item:
                        # File sourced from disk.
                        path = item["path"]
                        filename = item.get("filename") or os.path.basename(path)
                        try:
                            fh = open(path, "rb")
                            opened_files.append(fh)
                        except FileNotFoundError:
                            _fail("File not found: %s" % path)
                            return None
                        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                        files_list.append((key, (filename, fh, mime)))

                    elif "content" in item:
                        # In-memory file content.
                        content = item["content"]
                        content_bytes = (
                            content.encode("utf-8") if isinstance(content, str) else bytes(content)
                        )
                        fh = io.BytesIO(content_bytes)
                        opened_files.append(fh)
                        filename = item.get("filename", "upload.bin")
                        mime = item.get("content_type", "application/octet-stream")
                        files_list.append((key, (filename, fh, mime)))

                    else:
                        _fail(
                            "File field '%s' dict must contain 'path' or 'content'. Got: %s"
                            % (key, list(item.keys()))
                        )
                        return None

            elif key == "file_metadata_str":
                # Special case: dict value serialised to JSON string form field.
                if not isinstance(value, dict):
                    _fail("'file_metadata_str' must be a dict, got %s." % type(value).__name__)
                    return None
                form_data[key] = json.dumps(value, separators=(",", ":"))

            else:
                # Regular string form field.
                form_data[key] = value

        response = requests.post(
            url,
            files=files_list or None,
            data=form_data or None,
            headers=upload_headers,
            cert=cert,
            timeout=timeout,
            verify=DEFAULT_VERIFY,
        )
        return response

    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))
        return None

    finally:
        for fh in opened_files:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Response printing
# ---------------------------------------------------------------------------

def _print_response_body(text: str, max_length: int, label: str = "Response Body") -> None:
    """Pretty-print a (possibly truncated) response body to stdout."""
    truncated = (
        text if not max_length or len(text) <= max_length
        else text[:max_length] + "\n...[truncated]..."
    )
    print("\n[%s]" % label)
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
      Legacy (array): [ { "url": "...", ... } ]
      Object: { "variables": {...}, "endpoints": [...] }

    Returns (endpoints, variables). Exits with code 1 on errors.
    """
    try:
        with open(config_file, "r") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        print("Error: Config file '%s' not found." % config_file)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print("Error: Invalid JSON in config file: %s" % exc)
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
    "Token Key",
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
    """Assemble a result row dict from a completed StreamingMetrics object."""

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
            "Token Key": "-",
        })
    else:
        base.update({
            "Time to First Token (ms)": _ms(metrics.time_to_first_token_ms),
            "Time to Last Token (ms)": _ms(metrics.time_to_last_token_ms),
            "Output Tokens": metrics.output_tokens,
            "Token Key": getattr(metrics, "token_key", None) or "",
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
    """Build a result row for an endpoint that raised an unexpected exception."""
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
        "Token Key": sentinel,
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
    Execute performance tests against each endpoint sequentially.

    Supports variable chaining: a NonStreaming endpoint can "extract"
    values from its response JSON and make them available to all
    subsequent endpoints via template substitution.

    Returns a list of result dicts, one per endpoint.
    """
    results: List[Dict] = []
    variables = dict(variables or {})

    for idx, endpoint in enumerate(endpoints, 1):
        # Apply "set" directives before processing this endpoint.
        set_vars = endpoint.get("set", {})
        if isinstance(set_vars, dict) and set_vars:
            variables.update(render_templates(set_vars, variables))

        url = render_templates(endpoint.get("url"), variables)
        method = endpoint.get("method", "POST").upper()
        endpointtype = endpoint.get("endpointtype", "")

        responsetype_raw = str(endpoint.get("responsetype", "Streaming")).strip()
        responsetype_key = responsetype_raw.lower().replace("-", "").replace(" ", "")
        is_nonstreaming = responsetype_key in ("nonstreaming", "nonstream")
        responsetype = "NonStreaming" if is_nonstreaming else "Streaming"

        payload = render_templates(endpoint.get("payload", {}), variables)
        headers = render_templates(endpoint.get("headers", {}), variables)
        cert_cfg = endpoint.get("cert")
        timeout = float(endpoint.get("timeout", 120.0))
        extract_map = endpoint.get("extract", {})
        # Optional: override token key probe list for this endpoint.
        # e.g. "token_keys": ["token"] once you know your server's key.
        token_keys_cfg = endpoint.get("token_keys", None)

        if not url:
            print("Warning: Endpoint %d missing 'url', skipping..." % idx)
            continue

        cert: Optional[Tuple[str, str]] = None
        if isinstance(cert_cfg, (list, tuple)) and len(cert_cfg) == 2:
            cert = (cert_cfg[0], cert_cfg[1])
        elif isinstance(cert_cfg, str):
            cert = (cert_cfg, None)

        sep = "=" * 60
        print("\n%s" % sep)
        print("Testing endpoint %d/%d: %s" % (idx, len(endpoints), url))
        print("Method: %s" % method)
        print(sep)

        if verbose:
            print("\n[VERBOSE] Request Details:")
            print("  URL    : %s" % url)
            print("  Method : %s" % method)
            if headers:
                print("  Headers: %s" % json.dumps(headers, indent=4))
            if payload and method == "POST":
                print("  Payload: %s" % json.dumps(payload, indent=4))
            if cert:
                print("  Cert   : %s" % (cert[0] or "None"))
                print("  Key    : %s" % (cert[1] or "None"))
            print("  Timeout         : %ss" % timeout)
            print("  Endpoint Type   : %s" % (endpointtype or "Not specified"))
            print("  Response Type   : %s" % responsetype)
            print("  Content-Type    : %s" % (_get_content_type(headers) or "(not set)"))
            print()

        _debug_log(
            "debug-session", "run1", "A",
            "%s:run_performance_tests" % __file__,
            "Endpoint config loaded",
            {"endpoint_idx": idx, "url": url, "variables": variables},
        )

        last_response_text: str = ""
        last_response_json: Optional[Any] = None

        def _capture_response(
            resp_text: str, _resp: Optional[requests.Response]
        ) -> None:
            nonlocal last_response_text, last_response_json
            last_response_text = resp_text or ""
            try:
                last_response_json = json.loads(last_response_text) if last_response_text else None
            except Exception:  # noqa: BLE001
                last_response_json = None

        try:
            if is_nonstreaming:
                # Determine multipart vs JSON from Content-Type header.
                effective_multipart = _is_multipart_request(headers)
                metrics = measure_nonstreaming_request(
                    method=method,
                    url=url,
                    payload=payload,
                    headers=headers,
                    cert=cert,
                    timeout=timeout,
                    verbose_response=verbose_response,
                    response_max_length=response_max_length,
                    is_multipart=effective_multipart,
                    on_response=_capture_response,
                )

            elif method == "GET":
                metrics = measure_sse_get(
                    url=url, headers=headers, cert=cert, timeout=timeout,
                    verbose=verbose, token_keys=token_keys_cfg,
                )

            elif method == "POST":
                metrics = measure_sse_stream(
                    url=url, payload=payload, headers=headers,
                    cert=cert, timeout=timeout, verbose=verbose,
                    token_keys=token_keys_cfg,
                )

            else:
                print("Error: Unsupported method '%s'. Only GET and POST are supported." % method)
                metrics = StreamingMetrics(
                    stream_start_time_ms=-1,
                    time_to_first_token_ms=-1,
                    time_to_last_token_ms=-1,
                    total_response_time_ms=-1,
                    input_tokens=0,
                    output_tokens=0,
                    error="Unsupported method: %s" % method,
                )

            results.append(_build_result(url, endpointtype, responsetype, method, metrics, is_nonstreaming))

            # Variable extraction (NonStreaming only).
            if isinstance(extract_map, dict) and extract_map:
                if not is_nonstreaming:
                    print("  [WARN] Endpoint %d has 'extract' rules but is Streaming. Skipping." % idx)
                elif last_response_json is None:
                    print("  [WARN] Endpoint %d response is not valid JSON. Skipping extraction." % idx)
                else:
                    for var_name, json_path in extract_map.items():
                        try:
                            value = get_value_by_path(last_response_json, str(json_path))
                            variables[var_name] = value
                            if verbose:
                                print("  [CHAIN] Extracted %s = %r from '%s'" % (var_name, value, json_path))
                        except Exception as ex:  # noqa: BLE001
                            print("  [WARN] Failed to extract '%s' from '%s': %s" % (var_name, json_path, ex))

        except Exception as exc:  # noqa: BLE001
            print("Error testing endpoint %d: %s" % (idx, exc))
            results.append(_error_result(url, endpointtype, responsetype, method, str(exc), is_nonstreaming))
            metrics = StreamingMetrics(-1, -1, -1, -1)

        # Per-endpoint summary.
        print("\nResults for endpoint %d:" % idx)
        print(("  Stream Start    : %8.1f ms" % metrics.stream_start_time_ms)
              if metrics.stream_start_time_ms >= 0 else "  Stream Start    : N/A")
        print(("  First Token     : %8.1f ms" % metrics.time_to_first_token_ms)
              if metrics.time_to_first_token_ms >= 0 else "  First Token     : N/A")
        print(("  Last Token      : %8.1f ms" % metrics.time_to_last_token_ms)
              if metrics.time_to_last_token_ms >= 0 else "  Last Token      : N/A")
        print(("  Total Response  : %8.1f ms" % metrics.total_response_time_ms)
              if metrics.total_response_time_ms >= 0 else "  Total Response  : N/A")
        print("  Input Tokens    : %s" % metrics.input_tokens)
        print("  Output Tokens   : %s" % metrics.output_tokens)
        if metrics.error:
            print("  Error           : %s" % metrics.error)

    return results


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def export_to_excel(results: List[Dict], output_file: str) -> None:
    """
    Export test results to a formatted Excel workbook.

    Creates any missing parent directories automatically. Columns follow
    _COLUMN_ORDER and widths are auto-adjusted to content (capped at 50).
    """
    if not results:
        print("No results to export.")
        return

    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        print("Created directory: %s" % output_dir)

    df = pd.DataFrame(results)[_COLUMN_ORDER]

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Performance Metrics", index=False)
        ws = writer.sheets["Performance Metrics"]
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=0)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    sep = "=" * 60
    print("\n%s" % sep)
    print("Results exported to: %s" % output_file)
    print(sep)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Parse CLI arguments and run the performance test suite.

    Modes:
      --config  Full feature set: multiple endpoints, variables, chaining.
      --url     Quick ad-hoc test of a single endpoint.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Generic SSE Performance Testing Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python rj_generic_sse_AiAgentic_performance_tester.py --config endpoints.json
  python rj_generic_sse_AiAgentic_performance_tester.py --url https://api.example.com/stream --method POST --payload payload.json
""",
    )
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--url", help="Single endpoint URL to test")
    parser.add_argument("--method", choices=["GET", "POST"], help="HTTP method")
    parser.add_argument("--payload", help="Path to JSON payload file (POST)")
    parser.add_argument("--headers", help="Path to JSON headers file")
    parser.add_argument("--cert", nargs=2, metavar=("CERT_FILE", "KEY_FILE"), help="mTLS cert and key")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds (default 120)")
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
                print("Error: Payload file '%s' not found." % args.payload)
                sys.exit(1)
            except json.JSONDecodeError as exc:
                print("Error: Invalid JSON in payload file: %s" % exc)
                sys.exit(1)
        elif endpoint["method"] == "POST":
            endpoint["payload"] = {}

        if args.headers:
            try:
                with open(args.headers, "r") as fh:
                    endpoint["headers"] = json.load(fh)
            except FileNotFoundError:
                print("Error: Headers file '%s' not found." % args.headers)
                sys.exit(1)
            except json.JSONDecodeError as exc:
                print("Error: Invalid JSON in headers file: %s" % exc)
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

    print("\nStarting performance tests for %d endpoint(s)..." % len(endpoints))
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
        prefix = "%s_" % args.reportprefix if args.reportprefix else ""
        output_file = "reports/%ssse_performance_%s.xlsx" % (prefix, timestamp)

    export_to_excel(results, output_file)


if __name__ == "__main__":
    main()
