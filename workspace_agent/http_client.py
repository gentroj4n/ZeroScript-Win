# SPDX-License-Identifier: GPL-3.0-or-later
# workspace_agent/http_client.py
# ──────────────────────────────────────────────────────────────────────────
#  Bounded, agent-safe HTTP primitive for the workspace layer.
#
#  This is the only outbound HTTP capability the workspace domain has.
#  It is intentionally restrictive: an agent processing untrusted content
#  (a fetched page, an issue body, an MCP tool result) must not be able to
#  trivially pivot into SSRF against localhost admin panels or cloud
#  metadata endpoints just by asking this tool to fetch a URL.
#
#  Boundaries:
#    - Only http and https schemes.
#    - Response bodies are capped with a streaming byte counter; a
#      Content-Length lie is ignored.
#    - Timeouts are bounded both ways and never unbounded.
#    - Redirects are re-validated hop-by-hop (urllib's own 10-hop cap applies).
#    - Loopback / private / link-local / multicast / reserved / unspecified
#      destinations are refused unless allow_local=true is explicitly set.
#    - Header values may be written as ``env:VARIABLE_NAME`` so a secret can
#      be used without ever appearing in a tool argument or being echoed back
#      in the returned result.
#
#  Known limitation: this is not a full SSRF defence against DNS rebinding
#  (a hostname that resolves to a public IP at check time and a private IP at
#  fetch time). Do not run this bridge as a service that fetches user-supplied
#  URLs on behalf of untrusted third parties.
# ──────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import base64
import ipaddress
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_BYTES = 2_000_000
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5


class HttpClientError(RuntimeError):
    """Raised for refusals and failures that should be surfaced to the caller."""


def _reject_ip(ip) -> None:
    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise HttpClientError(
            f"refusing request to non-public address {ip}; pass allow_local=true "
            "only when you intentionally target a local service"
        )


def _check_host(url: str, allow_local: bool) -> None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise HttpClientError(
            f"scheme '{scheme or '(none)'}' is not allowed; use http or https"
        )
    host = parsed.hostname
    if not host:
        raise HttpClientError("URL has no host")
    if allow_local:
        return
    # Literal IP in the URL: check directly, no DNS.
    try:
        _reject_ip(ipaddress.ip_address(host))
        return
    except ValueError:
        pass
    # Hostname: resolve every A/AAAA record and reject if any is non-public.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HttpClientError(f"could not resolve host '{host}': {exc}") from exc
    if not infos:
        raise HttpClientError(f"host '{host}' did not resolve to any address")
    for info in infos:
        addr = info[4][0]
        try:
            _reject_ip(ipaddress.ip_address(addr))
        except ValueError:
            continue


def _resolve_header_value(value: str) -> str:
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:].strip()
        if not name:
            raise HttpClientError("env: header value requires a variable name")
        if name not in os.environ:
            raise HttpClientError(
                f"environment variable '{name}' referenced by a header is not set"
            )
        return os.environ[name]
    return value


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate the destination of every redirect hop before following it."""

    def __init__(self, allow_local: bool):
        super().__init__()
        self.allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        _check_host(newurl, self.allow_local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(allow_local: bool) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SafeRedirectHandler(allow_local))


class _SizedReader:
    """Wrap a response object so .read(n) cannot exceed the byte cap."""

    def __init__(self, fp, cap: int):
        self.fp = fp
        self.cap = cap
        self.read_total = 0
        self.truncated = False

    def read(self, n: int = -1) -> bytes:
        if self.read_total >= self.cap:
            self.truncated = True
            return b""
        remaining = self.cap - self.read_total
        want = remaining if (n is None or n < 0) else min(n, remaining)
        chunk = self.fp.read(want)
        if not chunk:
            return b""
        self.read_total += len(chunk)
        if self.read_total >= self.cap:
            # We filled the buffer; a second read will set truncated.
            pass
        return chunk


def _looks_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    if not ct:
        return True
    return any(token in ct for token in (
        "text/", "json", "xml", "javascript", "x-www-form-urlencoded",
        "yaml", "csv", "html",
    ))


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    timeout: Optional[float] = None,
    max_bytes: Optional[int] = None,
    allow_local: bool = False,
) -> Dict[str, Any]:
    """Perform one bounded HTTP request.

    Returns a dict: status, reason, url (final after redirects), method,
    headers, body, body_encoding ("utf-8" or "base64"), truncated, elapsed_ms.
    Refusals and network failures raise HttpClientError.
    """
    if not isinstance(url, str) or not url.strip():
        raise HttpClientError("URL is required")
    method = (method or "GET").upper().strip()
    if method not in ALLOWED_METHODS:
        raise HttpClientError(f"method '{method}' is not allowed")

    try:
        timeout = float(timeout) if timeout is not None else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = max(0.1, min(timeout, MAX_TIMEOUT_SECONDS))

    try:
        max_bytes = int(max_bytes) if max_bytes is not None else DEFAULT_MAX_BYTES
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MAX_BYTES
    max_bytes = max(MIN_MAX_BYTES, min(max_bytes, MAX_MAX_BYTES))

    _check_host(url, allow_local)

    req_headers: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not k.strip():
            raise HttpClientError("header names must be non-empty strings")
        if "\n" in k or "\r" in k:
            raise HttpClientError("header names cannot contain newlines")
        value = _resolve_header_value(str(v))
        if "\n" in value or "\r" in value:
            raise HttpClientError("header values cannot contain newlines")
        req_headers[k] = value

    if body is None or body == "":
        data: Optional[bytes] = None
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    else:
        raise HttpClientError("body must be a string, bytes, or null")

    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    opener = _build_opener(allow_local)
    started = time.monotonic()

    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Non-2xx that urllib did not consider a redirect: still return the body.
        resp = exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None) or exc
        raise HttpClientError(f"request failed: {reason}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise HttpClientError(f"request timed out after {timeout:.1f}s") from exc
    except HttpClientError:
        raise
    except Exception as exc:
        raise HttpClientError(f"request failed: {exc}") from exc

    try:
        raw = b""
        truncated = False
        reader = _SizedReader(resp, max_bytes)
        chunks = []
        while True:
            chunk = reader.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        truncated = reader.truncated or reader.read_total >= max_bytes
    finally:
        try:
            resp.close()
        except Exception:
            pass

    status = getattr(resp, "status", None) or getattr(resp, "code", None) or 0
    reason_text = getattr(resp, "reason", "") or ""
    resp_headers: Dict[str, str] = {}
    try:
        for k, v in (resp.headers.items() if resp.headers else []):
            resp_headers[str(k)] = str(v)
    except Exception:
        pass

    content_type = resp_headers.get("Content-Type", "")
    textual = _looks_textual(content_type)
    try:
        body_out = raw.decode("utf-8")
        body_encoding = "utf-8"
    except UnicodeDecodeError:
        if textual:
            body_out = raw.decode("utf-8", errors="replace")
            body_encoding = "utf-8"
        else:
            body_out = base64.b64encode(raw).decode("ascii")
            body_encoding = "base64"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "status": int(status) if isinstance(status, int) else 0,
        "reason": str(reason_text),
        "url": url,
        "method": method,
        "headers": resp_headers,
        "body": body_out,
        "body_encoding": body_encoding,
        "body_bytes": len(raw),
        "truncated": bool(truncated),
        "elapsed_ms": elapsed_ms,
    }
