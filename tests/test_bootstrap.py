import json
import os
import unittest
from unittest.mock import patch

import bootstrap_oauth as b


class BootstrapTests(unittest.TestCase):
    def config(self, **changes):
        value = dict(provider="oura", redirect_uri="http://localhost:3000/callback",
                     callback_url="http://localhost:3000/callback?code=test-code&state=test-state",
                     state="test-state", code_verifier="a" * 43)
        value.update(changes)
        return json.dumps(value)

    def run_exchange(self, config, provider="oura"):
        env = {"OAUTH_BOOTSTRAP_JSON": config, "OURA_CLIENT_ID": "test-client",
               "OURA_CLIENT_SECRET": "test-secret"}
        with patch.dict(os.environ, env), patch.object(b, "secret_command") as save, patch.object(b, "oauth") as oauth:
            oauth.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
            b.exchange(provider)
            return save.call_args_list, oauth.call_args_list

    def test_exchange_then_persist_then_cleanup(self):
        events = []
        with patch.dict(os.environ, {"OAUTH_BOOTSTRAP_JSON": self.config(), "OURA_CLIENT_ID": "id", "OURA_CLIENT_SECRET": "secret"}), patch.object(b, "secret_command", side_effect=lambda op, name, value=None: events.append((op, name))), patch.object(b, "oauth", side_effect=lambda *a, **kw: events.append(("exchange", kw["attempts"])) or {"access_token": "access", "refresh_token": "refresh"}):
            b.exchange("oura")
        self.assertEqual(events, [("set", "OAUTH_BOOTSTRAP_JSON"), ("exchange", 1), ("set", "OURA_TOKEN_JSON"), ("delete", "OAUTH_BOOTSTRAP_JSON")])

    def test_mismatch_and_duplicate_code_rejected_before_exchange(self):
        cases = [dict(state="wrong"), dict(provider="google"),
                 dict(callback_url="https://example.com/callback?code=x&state=test-state"),
                 dict(callback_url="http://localhost:3000/callback?code=x&code=y&state=test-state")]
        for changes in cases:
            with self.subTest(changes=changes), patch.object(b, "oauth") as oauth:
                with self.assertRaises(b.ExportError):
                    self.run_exchange(self.config(**changes))
                oauth.assert_not_called()

    def test_failed_preflight_does_not_consume_code(self):
        with patch.dict(os.environ, {"OAUTH_BOOTSTRAP_JSON": self.config(), "OURA_CLIENT_ID": "id", "OURA_CLIENT_SECRET": "secret"}), patch.object(b, "secret_command", side_effect=b.ExportError("write failed")), patch.object(b, "oauth") as oauth:
            with self.assertRaises(b.ExportError):
                b.exchange("oura")
            oauth.assert_not_called()
