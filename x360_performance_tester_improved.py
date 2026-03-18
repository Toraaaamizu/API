#!/usr/bin/env python3

"""
X360 API Performance Testing Script
=====================================
This script tests how fast and reliable the X360 API endpoints are.
It sends multiple requests to the API, measures how long each takes,
and saves a detailed report in an Excel file.

How to run this script:
  - Using a config file:
      python x360_performance_tester.py --config endpoints_config.json --iterations 10

  - Testing a single URL directly:
      python x360_performance_tester.py --url <url> --method <GET|POST|PUT|DELETE> --iterations 5
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS — These are external tools/libraries this script needs to work
# ─────────────────────────────────────────────────────────────────────────────

import json          # For reading and writing JSON data (a common data format)
import argparse      # For reading command-line arguments (what user types after the script name)
import sys           # For exiting the script gracefully when something goes wrong
import os            # For working with files and folders on the computer
import time          # For measuring how long things take
import statistics    # For calculating averages, medians, and other math summaries

from typing import List, Dict, Optional, Tuple  # For type hints (makes code easier to read)
from datetime import datetime                    # For generating timestamps (used in report filenames)
from collections import defaultdict             # A special dictionary that auto-creates missing keys

import pandas as pd  # For creating spreadsheets and working with tabular data
import requests      # For making HTTP requests (the actual calls to the API)
import urllib3       # Handles low-level network stuff; we use it to suppress a warning

# Suppress warnings about unverified HTTPS connections (SSL certificates not checked)
# This is intentional — useful in testing environments where certs may not be set up
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL IMPORT — Try to load a helper from another project
# If it's not available, we use a simpler version defined below
# ─────────────────────────────────────────────────────────────────────────────

# Try to import token estimation from a sibling project
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../00_Performance/scripts/generic_sse_AiAgentic_performance_tester'))

try:
    from streaming_metrics import estimate_input_tokens
except ImportError:
    # If the import fails, define a simple fallback version here
    def estimate_input_tokens(payload: dict) -> int:
        """
        Rough estimate of how many 'tokens' (word-like units) are in the request payload.
        Tokens are how AI models measure text length. This is a simplified estimate.
        """
        if not payload:
            return 0
        # Convert the payload to a string, then count words as a rough token estimate
        payload_str = json.dumps(payload)
        return len(payload_str.split())


# ─────────────────────────────────────────────────────────────────────────────
# CLASS: PerformanceMetrics
# This is like a scoreboard that tracks how well each API endpoint performed.
# It collects all the measurements from each test run and can summarize them.
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceMetrics:
    """Stores and summarizes performance data from multiple API test runs."""

    def __init__(self):
        """
        Set up empty lists to collect measurements.
        Each list will fill up as we run tests.
        """
        self.response_times = []   # How long each full request took (in milliseconds)
        self.ttfb_times = []       # Time To First Byte — how quickly the server started responding
        self.status_codes = []     # HTTP status code returned (e.g., 200 = OK, 404 = Not Found)
        self.request_sizes = []    # How many bytes were sent in each request
        self.response_sizes = []   # How many bytes came back in each response
        self.errors = []           # List of error messages (if any)
        self.success_count = 0     # How many requests succeeded
        self.error_count = 0       # How many requests failed

    def add_result(self, response_time: float, ttfb: float, status_code: int,
                   request_size: int, response_size: int, error: Optional[str] = None):
        """
        Record the results from a single API request.

        Args:
            response_time: Total time the request took (milliseconds)
            ttfb:          Time until the first byte of response was received (milliseconds)
            status_code:   HTTP status code (200 = success, 400+ = error)
            request_size:  Size of the data we sent (bytes)
            response_size: Size of the response we got back (bytes)
            error:         Error message, if the request failed (None if it worked fine)
        """
        # Record all the measurements
        self.response_times.append(response_time)
        self.ttfb_times.append(ttfb)
        self.status_codes.append(status_code)
        self.request_sizes.append(request_size)
        self.response_sizes.append(response_size)

        # Count it as an error if there's an error message OR the status code is 400+
        # (HTTP codes 400 and above mean something went wrong)
        if error or status_code >= 400:
            self.error_count += 1
            self.errors.append(error or f"HTTP {status_code}")
        else:
            self.success_count += 1

    def get_summary(self) -> Dict:
        """
        Calculate and return a summary of all the collected measurements.
        This includes averages, min/max values, and percentile stats.

        Returns:
            A dictionary (key-value pairs) with all the calculated metrics.
        """
        # If no tests were run yet, return an empty result
        if not self.response_times:
            return {}

        # Sort response times so we can calculate percentiles (P95, P99)
        # Percentiles tell us: "95% of requests finished within X milliseconds"
        sorted_times = sorted(self.response_times)
        n = len(sorted_times)

        summary = {
            # ── Request Counts ──────────────────────────────────────────────
            "Total Requests":    len(self.response_times),
            "Success Count":     self.success_count,
            "Error Count":       self.error_count,

            # Success/error rates as percentages
            "Success Rate (%)":  (self.success_count / len(self.response_times) * 100) if self.response_times else 0,
            "Error Rate (%)":    (self.error_count   / len(self.response_times) * 100) if self.response_times else 0,

            # ── Response Time Metrics (all in milliseconds) ─────────────────
            # These tell us how fast the API responded
            "Avg Response Time (ms)":    statistics.mean(self.response_times)   if self.response_times else 0,
            "Min Response Time (ms)":    min(self.response_times)               if self.response_times else 0,
            "Max Response Time (ms)":    max(self.response_times)               if self.response_times else 0,
            "Median Response Time (ms)": statistics.median(self.response_times) if self.response_times else 0,

            # P95: 95% of requests completed within this time
            "P95 Response Time (ms)":    sorted_times[int(n * 0.95)] if n > 0 else 0,
            # P99: 99% of requests completed within this time (highlights worst outliers)
            "P99 Response Time (ms)":    sorted_times[int(n * 0.99)] if n > 0 else 0,

            # ── Time to First Byte (TTFB) ───────────────────────────────────
            # TTFB = how quickly the server started sending a response back
            "Avg TTFB (ms)": statistics.mean(self.ttfb_times) if self.ttfb_times else 0,
            "Min TTFB (ms)": min(self.ttfb_times)             if self.ttfb_times else 0,
            "Max TTFB (ms)": max(self.ttfb_times)             if self.ttfb_times else 0,

            # ── Payload Size Metrics ─────────────────────────────────────────
            # How much data was transferred back and forth
            "Avg Request Size (bytes)":   statistics.mean(self.request_sizes)  if self.request_sizes  else 0,
            "Avg Response Size (bytes)":  statistics.mean(self.response_sizes) if self.response_sizes else 0,
            "Total Request Size (bytes)": sum(self.request_sizes),
            "Total Response Size (bytes)":sum(self.response_sizes),
        }

        # ── HTTP Status Code Distribution ────────────────────────────────────
        # Count how many times each status code appeared (e.g., 10x 200, 2x 500)
        status_dist = defaultdict(int)
        for code in self.status_codes:
            status_dist[code] += 1

        for code, count in sorted(status_dist.items()):
            summary[f"HTTP {code} Count"] = count

        return summary


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: measure_endpoint_performance
# Sends repeated requests to one API endpoint and records how it performs.
# ─────────────────────────────────────────────────────────────────────────────

def measure_endpoint_performance(
    method: str,
    url: str,
    payload: dict = None,
    headers: Optional[Dict] = None,
    cert: Optional[Tuple[str, str]] = None,
    timeout: float = 120.0,
    iterations: int = 1,
    print_responses: bool = False,
    response_max_length: int = 1000
) -> PerformanceMetrics:
    """
    Send multiple requests to an API endpoint and collect performance measurements.

    Args:
        method:              HTTP method to use — GET (read), POST (create), PUT (update), DELETE (remove)
        url:                 The web address of the API endpoint to test
        payload:             Data to send with the request (used with POST and PUT)
        headers:             Extra info sent with each request (e.g., authentication tokens)
        cert:                Client certificate files for secure connections (optional)
        timeout:             How many seconds to wait before giving up on a request
        iterations:          How many times to repeat the request
        print_responses:     Whether to show the API's response text in the console
        response_max_length: Maximum characters of response to display (to avoid flooding the screen)

    Returns:
        A PerformanceMetrics object containing all the recorded measurements
    """
    metrics = PerformanceMetrics()

    # Use empty defaults if nothing was provided
    headers = headers or {}
    payload = payload or {}

    # ── Calculate the size of what we're sending ─────────────────────────────
    # We count bytes in the JSON body + bytes in the headers
    request_size = 0
    if payload:
        request_size = len(json.dumps(payload).encode('utf-8'))
    if headers:
        request_size += sum(len(f"{k}: {v}".encode('utf-8')) for k, v in headers.items())

    # ── Run the test N times (iterations) ────────────────────────────────────
    for i in range(iterations):

        # Reset measurements for this iteration
        start_time    = time.perf_counter()  # High-precision timer — starts now
        ttfb          = None
        response_time = None
        status_code   = 0
        response_size = 0
        error         = None

        try:
            # ── Send the HTTP Request ─────────────────────────────────────────
            # Choose the right type of request based on the method
            if method.upper() == "GET":
                response = requests.get(
                    url, headers=headers, cert=cert, timeout=timeout, verify=False
                )
            elif method.upper() == "POST":
                response = requests.post(
                    url, json=payload, headers=headers, cert=cert, timeout=timeout, verify=False
                )
            elif method.upper() == "PUT":
                response = requests.put(
                    url, json=payload, headers=headers, cert=cert, timeout=timeout, verify=False
                )
            elif method.upper() == "DELETE":
                response = requests.delete(
                    url, headers=headers, cert=cert, timeout=timeout, verify=False
                )
            else:
                # If an unsupported method was given, record the error and skip to next iteration
                error         = f"Unsupported method: {method}"
                response_time = (time.perf_counter() - start_time) * 1000
                metrics.add_result(response_time, 0, 0, request_size, 0, error)
                continue

            # ── Measure Time To First Byte (TTFB) ────────────────────────────
            # This is measured right after the server starts responding
            ttfb = (time.perf_counter() - start_time) * 1000

            # Raise an exception if the response indicates an error (status 400+)
            response.raise_for_status()

            # Read and measure the response body
            response_text = response.text
            response_size = len(response_text.encode('utf-8'))
            status_code   = response.status_code

            # Calculate total time from when we sent the request to when we got it all back
            response_time = (time.perf_counter() - start_time) * 1000

            # ── Optionally Print the Response ─────────────────────────────────
            if print_responses:
                truncated_response = response_text
                if len(truncated_response) > response_max_length:
                    truncated_response = truncated_response[:response_max_length] + "\n...[truncated]..."

                print(f"\n  [Iteration {i+1}/{iterations}] Response (Status {status_code}):")
                try:
                    # If the response is JSON, print it in a readable, indented format
                    parsed = json.loads(truncated_response)
                    print(json.dumps(parsed, indent=2, ensure_ascii=False))
                except (json.JSONDecodeError, TypeError):
                    # Otherwise, just print the raw text
                    print(truncated_response)
                print()

        # ── Handle Specific Error Types ───────────────────────────────────────

        except requests.exceptions.Timeout:
            # The server took too long to respond
            response_time = (time.perf_counter() - start_time) * 1000
            error         = f"Timeout after {timeout}s"
            status_code   = 0

        except requests.exceptions.RequestException as e:
            # Some other network or HTTP error occurred
            response_time = (time.perf_counter() - start_time) * 1000
            error         = str(e)

            # If the error came with a response (e.g., a 404 page), capture that too
            if hasattr(e, 'response') and e.response is not None:
                status_code = e.response.status_code
                try:
                    response_text = e.response.text
                    response_size = len(response_text.encode('utf-8'))

                    # Optionally print the error response body
                    if print_responses:
                        truncated_response = response_text
                        if len(truncated_response) > response_max_length:
                            truncated_response = truncated_response[:response_max_length] + "\n...[truncated]..."

                        print(f"\n  [Iteration {i+1}/{iterations}] Error Response (Status {status_code}):")
                        try:
                            parsed = json.loads(truncated_response)
                            print(json.dumps(parsed, indent=2, ensure_ascii=False))
                        except (json.JSONDecodeError, TypeError):
                            print(truncated_response)
                        print()
                except Exception:
                    pass  # Ignore errors while trying to read the error response

        except Exception as e:
            # Catch-all for any other unexpected error
            response_time = (time.perf_counter() - start_time) * 1000
            error         = str(e)
            status_code   = 0

        # ── Save This Iteration's Results ─────────────────────────────────────
        metrics.add_result(
            response_time = response_time or 0,
            ttfb          = ttfb          or 0,
            status_code   = status_code,
            request_size  = request_size,
            response_size = response_size,
            error         = error
        )

        # Pause briefly between requests so we don't overload the server
        # (skip the pause after the very last iteration)
        if i < iterations - 1:
            time.sleep(0.1)

    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: calculate_throughput_metrics
# Figures out the overall speed of testing — how many requests were made per second
# ─────────────────────────────────────────────────────────────────────────────

def calculate_throughput_metrics(
    endpoints: List[Dict],
    results: List[Dict],
    total_time_seconds: float
) -> Dict:
    """
    Calculate how many requests were completed per second across all endpoints.

    Args:
        endpoints:           The full list of endpoints that were tested
        results:             The collected result data from all tests
        total_time_seconds:  How long all the tests took in total (seconds)

    Returns:
        A dictionary with throughput (speed) metrics
    """
    # Add up all requests and successes across every endpoint
    total_requests      = sum(r.get("Total Requests", 0) for r in results)
    successful_requests = sum(r.get("Success Count",  0) for r in results)

    throughput = {
        "Total Test Duration (s)":          total_time_seconds,
        "Total Requests":                   total_requests,
        "Successful Requests":              successful_requests,

        # RPS = Requests Per Second — higher is better
        "Requests Per Second (RPS)":        total_requests / total_time_seconds if total_time_seconds > 0 else 0,

        # How many successful transactions happened per second
        "Successful Transactions Per Second": successful_requests / total_time_seconds if total_time_seconds > 0 else 0,

        # Average number of requests per endpoint
        "Average Requests Per Endpoint":    total_requests / len(endpoints) if endpoints else 0,
    }

    return throughput


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: load_config_from_file
# Reads a JSON configuration file that contains the list of endpoints to test
# ─────────────────────────────────────────────────────────────────────────────

def load_config_from_file(config_file: str) -> List[Dict]:
    """
    Load the list of API endpoints to test from a JSON file.

    The file must contain a JSON array (list) of endpoint objects.
    If the file is missing or invalid, the script will exit with an error message.

    Args:
        config_file: Path to the JSON config file

    Returns:
        A list of endpoint configuration dictionaries
    """
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)

        # Validate that the config is a list (not just a single item)
        if not isinstance(config, list):
            raise ValueError("Config file must contain a JSON array of endpoint configurations")

        return config

    except FileNotFoundError:
        print(f"Error: Config file '{config_file}' not found.")
        sys.exit(1)

    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: run_performance_tests
# Loops through each endpoint, runs the tests, and collects all results
# ─────────────────────────────────────────────────────────────────────────────

def run_performance_tests(
    endpoints: List[Dict],
    iterations: int = 10,
    verbose: bool = False
) -> Tuple[List[Dict], Dict]:
    """
    Run performance tests against every endpoint in the provided list.

    Args:
        endpoints:   List of endpoint configurations (each with url, method, headers, etc.)
        iterations:  How many times to call each endpoint
        verbose:     If True, print detailed output to the console as tests run

    Returns:
        A tuple containing:
          - List of result summaries (one per endpoint)
          - Throughput metrics dictionary (overall speed stats)
    """
    results = []

    # Start the overall timer — we'll use this to measure total test duration
    overall_start_time = time.perf_counter()

    # ── Loop Through Each Endpoint ────────────────────────────────────────────
    for idx, endpoint in enumerate(endpoints, 1):

        # Extract settings from the endpoint config, using defaults where not specified
        url          = endpoint.get("url")
        method       = endpoint.get("method", "GET").upper()
        endpointtype = endpoint.get("endpointtype", "")    # Optional label for the endpoint
        payload      = endpoint.get("payload", {})
        headers      = endpoint.get("headers", {})
        cert_paths   = endpoint.get("cert")
        timeout      = endpoint.get("timeout", 120.0)

        # Skip this endpoint if no URL was provided
        if not url:
            print(f"Warning: Endpoint {idx} missing 'url', skipping...")
            continue

        # ── Handle SSL/TLS Client Certificate ─────────────────────────────────
        # Some secure APIs require a certificate + key file for authentication
        cert = None
        if cert_paths:
            if isinstance(cert_paths, list) and len(cert_paths) == 2:
                cert = (cert_paths[0], cert_paths[1])  # (certificate file, key file)
            elif isinstance(cert_paths, str):
                cert = (cert_paths, None)              # Certificate only (no separate key file)

        # ── Print a Header for This Endpoint ──────────────────────────────────
        print(f"\n{'='*80}")
        print(f"Testing endpoint {idx}/{len(endpoints)}: {url}")
        print(f"Method: {method} | Iterations: {iterations}")
        print(f"{'='*80}")

        # In verbose mode, print extra details before running
        if verbose:
            print(f"  Endpoint Type: {endpointtype if endpointtype else 'Not specified'}")
            if headers:
                print(f"  Headers: {json.dumps(headers, indent=2)}")
            if payload and method in ["POST", "PUT"]:
                print(f"  Payload: {json.dumps(payload, indent=2)}")

        # ── Run the Performance Test ───────────────────────────────────────────
        metrics = measure_endpoint_performance(
            method            = method,
            url               = url,
            payload           = payload,
            headers           = headers,
            cert              = cert,
            timeout           = timeout,
            iterations        = iterations,
            print_responses   = verbose,
            response_max_length = 1000
        )

        # Get the calculated summary stats
        summary = metrics.get_summary()

        # Combine the endpoint info with its performance summary into one result dict
        result = {
            "Endpoint":      url,
            "Method":        method,
            "Endpoint Type": endpointtype,
            **summary         # Merge in all the stats from get_summary()
        }
        results.append(result)

        # In verbose mode, print a quick results preview
        if verbose:
            print(f"\n  Results Summary:")
            print(f"  Success Rate:      {summary.get('Success Rate (%)', 0):.2f}%")
            print(f"  Avg Response Time: {summary.get('Avg Response Time (ms)', 0):.2f} ms")
            print(f"  P95 Response Time: {summary.get('P95 Response Time (ms)', 0):.2f} ms")
            print(f"  P99 Response Time: {summary.get('P99 Response Time (ms)', 0):.2f} ms")
            if summary.get('Error Count', 0) > 0:
                print(f"  Errors: {summary.get('Error Count', 0)}")

    # Stop the overall timer
    overall_end_time = time.perf_counter()
    total_time       = overall_end_time - overall_start_time

    # Calculate and return throughput metrics (requests per second, etc.)
    throughput_metrics = calculate_throughput_metrics(endpoints, results, total_time)

    return results, throughput_metrics


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: export_to_excel
# Saves all the test results into a nicely formatted Excel (.xlsx) file
# with three separate sheets for different views of the data
# ─────────────────────────────────────────────────────────────────────────────

def export_to_excel(results: List[Dict], throughput_metrics: Dict, output_file: str):
    """
    Write all test results to an Excel file with three sheets:
      1. Performance Metrics  — Detailed per-endpoint data
      2. Throughput Summary   — Overall speed stats
      3. Summary Statistics   — High-level aggregated numbers

    Args:
        results:            List of per-endpoint result dictionaries
        throughput_metrics: Dictionary with overall throughput stats
        output_file:        Full path where the Excel file should be saved
    """
    if not results:
        print("No results to export.")
        return

    # Create the output directory if it doesn't already exist
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        print(f"Created directory: {output_dir}")

    # ── Write to Excel ─────────────────────────────────────────────────────────
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:

        # ── Sheet 1: Detailed Results Per Endpoint ─────────────────────────────
        df_results = pd.DataFrame(results)

        # Define the preferred column order — most important info comes first
        priority_columns = [
            "Endpoint", "Method", "Endpoint Type",
            "Total Requests", "Success Count", "Error Count",
            "Success Rate (%)", "Error Rate (%)",
            "Avg Response Time (ms)", "Min Response Time (ms)", "Max Response Time (ms)",
            "Median Response Time (ms)", "P95 Response Time (ms)", "P99 Response Time (ms)",
            "Avg TTFB (ms)", "Min TTFB (ms)", "Max TTFB (ms)",
            "Avg Request Size (bytes)", "Avg Response Size (bytes)",
            "Total Request Size (bytes)", "Total Response Size (bytes)"
        ]

        # Append any remaining columns (e.g., status code breakdowns) that aren't already listed
        remaining_cols = [col for col in df_results.columns if col not in priority_columns]
        column_order   = priority_columns + remaining_cols

        # Only keep columns that actually exist in the data (avoids errors if some are missing)
        column_order = [col for col in column_order if col in df_results.columns]
        df_results   = df_results[column_order]

        df_results.to_excel(writer, sheet_name='Performance Metrics', index=False)

        # ── Sheet 2: Throughput Summary ────────────────────────────────────────
        df_throughput = pd.DataFrame([throughput_metrics])
        df_throughput.to_excel(writer, sheet_name='Throughput Summary', index=False)

        # ── Sheet 3: Summary Statistics ────────────────────────────────────────
        # A clean two-column table: Metric name | Value
        summary_stats = {
            "Metric": [
                "Total Endpoints Tested",
                "Total Requests",
                "Total Successful Requests",
                "Total Failed Requests",
                "Overall Success Rate (%)",
                "Overall Error Rate (%)",
                "Average Response Time Across All Endpoints (ms)",
                "Median Response Time Across All Endpoints (ms)",
                "P95 Response Time Across All Endpoints (ms)",
                "P99 Response Time Across All Endpoints (ms)",
                "Total Test Duration (s)",
                "Average Requests Per Second (RPS)",
            ],
            "Value": [
                len(results),
                sum(r.get("Total Requests", 0) for r in results),
                sum(r.get("Success Count",  0) for r in results),
                sum(r.get("Error Count",    0) for r in results),

                # Average success/error rate across all endpoints
                statistics.mean([r.get("Success Rate (%)", 0) for r in results]) if results else 0,
                statistics.mean([r.get("Error Rate (%)",   0) for r in results]) if results else 0,

                # Average and median response times across all endpoints
                statistics.mean(  [r.get("Avg Response Time (ms)",    0) for r in results]) if results else 0,
                statistics.median([r.get("Median Response Time (ms)", 0) for r in results]) if results else 0,

                # P95 across all endpoint P95s (95th percentile of the percentiles)
                statistics.quantiles([r.get("P95 Response Time (ms)", 0) for r in results], n=20)[18] if results else 0,

                # P99 across all endpoint P99s
                statistics.quantiles([r.get("P99 Response Time (ms)", 0) for r in results], n=100)[98] if results else 0,

                throughput_metrics.get("Total Test Duration (s)",   0),
                throughput_metrics.get("Requests Per Second (RPS)", 0),
            ]
        }

        df_summary = pd.DataFrame(summary_stats)
        df_summary.to_excel(writer, sheet_name='Summary Statistics', index=False)

        # ── Auto-Adjust Column Widths ──────────────────────────────────────────
        # Make each column wide enough to fit its longest value (up to 60 characters)
        for sheet_name in writer.sheets:
            worksheet = writer.sheets[sheet_name]
            for column in worksheet.columns:
                max_length    = 0
                column_letter = column[0].column_letter  # e.g., "A", "B", "C"

                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except Exception:
                        pass  # Ignore cells that can't be measured

                # Add 2 extra characters of padding; cap at 60 to avoid very wide columns
                adjusted_width = min(max_length + 2, 60)
                worksheet.column_dimensions[column_letter].width = adjusted_width

    print(f"\n{'='*80}")
    print(f"Results exported to: {output_file}")
    print(f"{'='*80}")


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION: main
# The entry point of the script — parses command-line arguments and kicks
# everything off in the right order
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """
    Parse command-line arguments and orchestrate the full test run:
      1. Load endpoint configuration (from file or command-line args)
      2. Run performance tests
      3. Export results to Excel
      4. Print a final summary to the console
    """

    # ── Set Up Argument Parser ─────────────────────────────────────────────────
    # This defines what flags/options the user can pass when running the script
    parser = argparse.ArgumentParser(
        description="X360 API Comprehensive Performance Testing Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using config file with 10 iterations per endpoint
  python x360_performance_tester.py --config endpoints_config.json --iterations 10

  # Single endpoint via command line
  python x360_performance_tester.py --url https://api.example.com/endpoint --method GET --iterations 5

Config file format (JSON):
  [
    {
      "url": "https://api.example.com/endpoint",
      "method": "GET",
      "endpointtype": "Account",
      "headers": {"Authorization": "Bearer token"},
      "cert": ["certs/cert.pem", "certs/key.pem"],
      "timeout": 120.0
    }
  ]
"""
    )

    # Each add_argument defines one command-line option the user can provide
    parser.add_argument("--config",       type=str,   help="Path to JSON config file containing list of endpoints")
    parser.add_argument("--url",          type=str,   help="Single endpoint URL to test")
    parser.add_argument("--method",       type=str,   choices=["GET", "POST", "PUT", "DELETE"], help="HTTP method")
    parser.add_argument("--payload",      type=str,   help="Path to JSON file containing payload (for POST/PUT requests)")
    parser.add_argument("--headers",      type=str,   help="Path to JSON file containing headers")
    parser.add_argument("--cert",         type=str,   nargs=2, metavar=("CERT_FILE", "KEY_FILE"), help="Certificate and key file paths")
    parser.add_argument("--timeout",      type=float, default=120.0, help="Request timeout in seconds (default: 120.0)")
    parser.add_argument("--iterations",   type=int,   default=10,    help="Number of iterations per endpoint (default: 10)")
    parser.add_argument("--output",       type=str,   default=None,  help="Output Excel file path (default: reports/x360_performance_<timestamp>.xlsx)")
    parser.add_argument("--reportprefix", type=str,   default=None,  help="Prefix for the output Excel filename")
    parser.add_argument("--verbose",      action="store_true",       help="Print detailed progress and results")

    args = parser.parse_args()

    # ── Build the List of Endpoints to Test ───────────────────────────────────

    endpoints = []

    if args.config:
        # Load from a JSON config file
        endpoints = load_config_from_file(args.config)

    elif args.url:
        # Build a single endpoint from the command-line flags
        endpoint = {"url": args.url}
        endpoint["method"] = args.method.upper() if args.method else "GET"

        # Load payload from file if provided
        if args.payload:
            try:
                with open(args.payload, 'r') as f:
                    endpoint["payload"] = json.load(f)
            except FileNotFoundError:
                print(f"Error: Payload file '{args.payload}' not found.")
                sys.exit(1)
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in payload file: {e}")
                sys.exit(1)
        elif endpoint["method"] in ["POST", "PUT"]:
            endpoint["payload"] = {}  # Default to empty body for POST/PUT if none given

        # Load headers from file if provided
        if args.headers:
            try:
                with open(args.headers, 'r') as f:
                    endpoint["headers"] = json.load(f)
            except FileNotFoundError:
                print(f"Error: Headers file '{args.headers}' not found.")
                sys.exit(1)
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in headers file: {e}")
                sys.exit(1)

        if args.cert:
            endpoint["cert"] = list(args.cert)

        endpoint["timeout"] = args.timeout
        endpoints = [endpoint]

    else:
        # Neither --config nor --url was given — show help and exit
        parser.print_help()
        print("\nError: Either --config or --url must be provided.")
        sys.exit(1)

    # Guard against empty endpoint list
    if not endpoints:
        print("Error: No endpoints to test.")
        sys.exit(1)

    # ── Print a Pre-Test Summary ───────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"Starting X360 API Performance Tests")
    print(f"Endpoints:               {len(endpoints)}")
    print(f"Iterations per endpoint: {args.iterations}")
    print(f"Total requests:          {len(endpoints) * args.iterations}")
    print(f"{'='*80}")

    # ── Run All the Tests ──────────────────────────────────────────────────────
    results, throughput_metrics = run_performance_tests(
        endpoints,
        iterations = args.iterations,
        verbose    = args.verbose
    )

    # ── Determine Output File Path ─────────────────────────────────────────────
    if args.output:
        output_file = args.output
    else:
        # Auto-generate a filename using the current timestamp
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix     = f"{args.reportprefix}_" if args.reportprefix else ""
        output_dir = "reports"

        # Create the reports folder if it doesn't exist
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        output_file = f"{output_dir}/{prefix}x360_performance_{timestamp}.xlsx"

    # ── Export Results to Excel ────────────────────────────────────────────────
    export_to_excel(results, throughput_metrics, output_file)

    # ── Print Final Console Summary ────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("TEST SUMMARY")
    print(f"{'='*80}")
    print(f"Total Endpoints Tested:  {len(results)}")
    print(f"Total Requests:          {sum(r.get('Total Requests', 0) for r in results)}")
    print(f"Successful Requests:     {sum(r.get('Success Count',  0) for r in results)}")
    print(f"Failed Requests:         {sum(r.get('Error Count',    0) for r in results)}")
    print(f"Overall Success Rate:    {statistics.mean([r.get('Success Rate (%)', 0) for r in results]):.2f}%")
    print(f"Average Response Time:   {statistics.mean([r.get('Avg Response Time (ms)', 0) for r in results]):.2f} ms")
    print(f"P95 Response Time:       {statistics.quantiles([r.get('P95 Response Time (ms)', 0) for r in results], n=20)[18] if results else 0:.2f} ms")
    print(f"Requests Per Second:     {throughput_metrics.get('Requests Per Second (RPS)', 0):.2f}")
    print(f"{'='*80}")


# ─────────────────────────────────────────────────────────────────────────────
# SCRIPT ENTRY POINT
# This block runs only when the script is executed directly (not imported).
# It's the standard Python way to kick off the main() function.
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
