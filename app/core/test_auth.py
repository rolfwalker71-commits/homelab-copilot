"""Auth path helpers for HTML login redirects."""

from __future__ import annotations

import unittest

from starlette.datastructures import Headers

from app.core.auth import wants_html


class WantsHtmlTests(unittest.TestCase):
    def test_mobile_companion(self) -> None:
        headers = Headers({"accept": "text/html"})
        self.assertTrue(wants_html(headers, "/mobile"))
        self.assertTrue(wants_html(headers, "/mobile/hosts"))
        self.assertTrue(wants_html(headers, "/mobile/mehr"))

    def test_json_api_not_html(self) -> None:
        headers = Headers({"accept": "application/json"})
        self.assertFalse(wants_html(headers, "/api/topology"))
