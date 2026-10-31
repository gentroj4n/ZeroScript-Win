# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for workspace_agent.http_client.

The real network is never touched. Every case either exercises a
validation/refusal path or stubs urllib's opener so behaviour is deterministic.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from workspace_agent import http_client


class _FakeResp:
    def __init__(self, body=b"", status=200, headers=None, reason="OK"):
        self._buf = io.BytesIO(body)
        self.status = status
        self.reason = reason
        self.headers = headers if headers is not None else {}
        self.closed = False

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self.closed = True


class _NoHeaders(dict):
    def items(self):
        return super().items()


class SchemeTests(unittest.TestCase):
    def test_rejects_ftp(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("ftp://example.com/x")

    def test_rejects_file(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("file:///etc/passwd")

    def test_rejects_missing_host(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("http:///nothing")


class MethodTests(unittest.TestCase):
    def test_rejects_unknown_method(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("https://example.com", method="TRACE")


class LocalAddressTests(unittest.TestCase):
    def test_loopback_literal_refused(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("http://127.0.0.1:8080/x")

    def test_private_literal_refused(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("http://192.168.0.5/x")

    def test_link_local_refused(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request("http://169.254.169.254/latest/meta-data")

    def test_allow_local_permitted_when_flag_set(self):
        # With allow_local=true, the address check must not raise. We still
        # stub the opener so no real connection happens.
        fake = _FakeResp(body=b"ok", headers={"Content-Type": "text/plain"})
        opener = mock.MagicMock()
        opener.open.return_value = fake
        with mock.patch.object(http_client, "_build_opener", return_value=opener):
            result = http_client.http_request(
                "http://127.0.0.1:1/x", allow_local=True, timeout=0.5
            )
        self.assertEqual(result["body"], "ok")


class HeaderTests(unittest.TestCase):
    def test_newline_in_header_rejected(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request(
                "https://example.com", headers={"X-Test": "a\r\nb"}
            )

    def test_missing_env_rejected(self):
        with self.assertRaises(http_client.HttpClientError):
            http_client.http_request(
                "https://example.com", headers={"Authorization": "env:ZS_UNSET_FOR_TEST"}
            )

    def test_env_header_resolved(self):
        fake = _FakeResp(body=b"", headers={"Content-Type": "text/plain"})
        opener = mock.MagicMock()
        opener.open.return_value = fake
        with mock.patch.dict("os.environ", {"ZS_TEST_TOKEN": "secret-123"}), \
             mock.patch.object(http_client, "_build_opener", return_value=opener):
            # Use a non-loopback public host literal; the DNS check is bypassed
            # because _check_host accepts a literal public IP.
            http_client.http_request(
                "https://8.8.8.8/x",
                headers={"Authorization": "env:ZS_TEST_TOKEN"},
            )
        # Verify the opener was asked to send the resolved value.
        called_req = opener.open.call_args[0][0]
        self.assertEqual(called_req.get_header("Authorization"), "secret-123")


class BodyEncodingTests(unittest.TestCase):
    def _run(self, body, headers):
        fake = _FakeResp(body=body, headers=headers)
        opener = mock.MagicMock()
        opener.open.return_value = fake
        with mock.patch.object(http_client, "_build_opener", return_value=opener):
            return http_client.http_request("https://8.8.8.8/x")

    def test_utf8_body_decoded(self):
        result = self._run(b"hello", {"Content-Type": "text/plain"})
        self.assertEqual(result["body"], "hello")
        self.assertEqual(result["body_encoding"], "utf-8")

    def test_binary_body_base64(self):
        # 0xFF is not a legal UTF-8 start byte, so the response must fall back
        # to base64 for non-textual content types.
        result = self._run(b"\xff\xfe\x00\x01", {"Content-Type": "application/octet-stream"})
        self.assertEqual(result["body_encoding"], "base64")

    def test_truncation_flag(self):
        # Ask for the minimum cap (1024) and send more than that.
        fake = _FakeResp(body=b"A" * 5000, headers={"Content-Type": "text/plain"})
        opener = mock.MagicMock()
        opener.open.return_value = fake
        with mock.patch.object(http_client, "_build_opener", return_value=opener):
            result = http_client.http_request(
                "https://8.8.8.8/x", max_bytes=1024
            )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["body_bytes"], 1024)

    def test_json_content_type_preserved(self):
        payload = json.dumps({"ok": True}).encode("utf-8")
        result = self._run(payload, {"Content-Type": "application/json"})
        self.assertEqual(json.loads(result["body"]), {"ok": True})


if __name__ == "__main__":
    unittest.main()
