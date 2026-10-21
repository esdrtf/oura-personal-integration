import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import export_oura as e


class OAuthErrorTests(unittest.TestCase):
    def test_only_standard_error_codes_are_exposed_without_retry(self):
        for code, expected in [("invalid_client", "HTTP 400 (invalid_client)"),
                               ("secret-value", "HTTP 400"),
                               ({"secret": "value"}, "HTTP 400")]:
            body = json.dumps({"error": code, "error_description": "secret-value"}).encode()
            error = urllib.error.HTTPError("https://example.com/token", 400,
                                           "secret-value", {}, io.BytesIO(body))
            with self.subTest(code=code), patch.object(e.urllib.request, "urlopen", side_effect=error) as send:
                with self.assertRaises(e.HTTPFailure) as caught:
                    e.oauth("https://example.com/token", {"client_secret": "secret-value"})
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(send.call_count, 1)
