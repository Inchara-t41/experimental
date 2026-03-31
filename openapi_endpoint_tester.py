#!/usr/bin/env python3
"""Test an OpenAPI endpoint with Python and verify it with curl."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _load_openapi_document(spec_source: str) -> dict[str, Any]:
    if spec_source.startswith(("http://", "https://")):
        with urllib.request.urlopen(spec_source) as response:
            raw = response.read().decode("utf-8")
    else:
        raw = Path(spec_source).read_text(encoding="utf-8")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError(
                "Spec is not valid JSON. Install PyYAML to read YAML specs."
            ) from exc
        return yaml.safe_load(raw)


def _resolve_base_url(spec: dict[str, Any], base_url: str | None) -> str:
    if base_url:
        return base_url.rstrip("/")

    servers = spec.get("servers") or []
    if not servers:
        raise ValueError("No base URL provided and the OpenAPI spec has no servers entry.")

    url = servers[0].get("url")
    if not url:
        raise ValueError("The first OpenAPI server entry does not include a url.")
    return str(url).rstrip("/")


def _validate_operation(spec: dict[str, Any], path: str, method: str) -> None:
    paths = spec.get("paths") or {}
    path_item = paths.get(path)
    if not path_item:
        raise ValueError(f"Path {path!r} was not found in the OpenAPI spec.")

    if method.lower() not in path_item:
        raise ValueError(f"Method {method.upper()} is not defined for path {path!r}.")


def _parse_key_value_pairs(pairs: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE format, got: {item}")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


def _build_url(base_url: str, path: str, query_params: dict[str, str]) -> str:
    url = f"{base_url}{path}"
    if query_params:
        url = f"{url}?{urllib.parse.urlencode(query_params)}"
    return url


def _python_request(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers)

    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": response.status,
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "body": body,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "status": None,
            "body": str(exc),
        }


def _build_curl_command(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
    timeout: int,
) -> list[str]:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(timeout),
        "--request",
        method.upper(),
        url,
        "--write-out",
        "\nHTTP_STATUS:%{http_code}",
    ]

    for key, value in headers.items():
        command.extend(["--header", f"{key}: {value}"])

    if json_body is not None:
        if "Content-Type" not in headers:
            command.extend(["--header", "Content-Type: application/json"])
        command.extend(["--data", json.dumps(json_body)])

    return command


def _curl_request(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = completed.stdout
    marker = "\nHTTP_STATUS:"

    if marker in output:
        body, status_text = output.rsplit(marker, 1)
        try:
            status = int(status_text.strip())
        except ValueError:
            status = None
    else:
        body = output
        status = None

    return {
        "ok": completed.returncode == 0 and status is not None and status < 400,
        "status": status,
        "body": body,
        "stderr": completed.stderr.strip(),
        "returncode": completed.returncode,
    }


def _print_result(label: str, result: dict[str, Any]) -> None:
    print(f"\n{label}")
    print(f"status: {result.get('status')}")
    print(f"ok: {result.get('ok')}")
    body = (result.get("body") or "").strip()
    if body:
        preview = body[:1000]
        print("body:")
        print(preview)
        if len(body) > len(preview):
            print("... truncated ...")
    stderr = result.get("stderr")
    if stderr:
        print("stderr:")
        print(stderr)


def test_openapi_endpoint(
    spec_source: str,
    path: str,
    method: str = "GET",
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    query_params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    spec = _load_openapi_document(spec_source)
    _validate_operation(spec, path, method)
    resolved_base_url = _resolve_base_url(spec, base_url)
    url = _build_url(resolved_base_url, path, query_params or {})
    request_headers = headers or {}

    python_result = _python_request(method, url, request_headers, json_body, timeout)
    curl_command = _build_curl_command(method, url, request_headers, json_body, timeout)
    curl_result = _curl_request(curl_command)

    return {
        "url": url,
        "curl_command": " ".join(shlex.quote(part) for part in curl_command),
        "python_result": python_result,
        "curl_result": curl_result,
        "both_working": python_result.get("ok") and curl_result.get("ok"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test an OpenAPI endpoint in Python and verify it with curl."
    )
    parser.add_argument("--spec", required=True, help="OpenAPI JSON/YAML file path or URL.")
    parser.add_argument("--path", required=True, help="Path from the OpenAPI spec, for example /users.")
    parser.add_argument("--method", default="GET", help="HTTP method, for example GET or POST.")
    parser.add_argument("--base-url", help="Override the server URL from the OpenAPI spec.")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Header in KEY=VALUE format. Repeat for multiple headers.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query parameter in KEY=VALUE format. Repeat for multiple values.",
    )
    parser.add_argument(
        "--json-body",
        help="Inline JSON body string, for example '{\"name\":\"alice\"}'.",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds.")
    args = parser.parse_args()

    try:
        headers = _parse_key_value_pairs(args.header)
        query_params = _parse_key_value_pairs(args.query)
        json_body = json.loads(args.json_body) if args.json_body else None
        result = test_openapi_endpoint(
            spec_source=args.spec,
            path=args.path,
            method=args.method,
            base_url=args.base_url,
            headers=headers,
            query_params=query_params,
            json_body=json_body,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"resolved url: {result['url']}")
    print(f"curl command: {result['curl_command']}")
    _print_result("python request", result["python_result"])
    _print_result("curl request", result["curl_result"])
    print(f"\nboth working: {result['both_working']}")
    return 0 if result["both_working"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
