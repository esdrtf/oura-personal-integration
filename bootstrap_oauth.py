#!/usr/bin/env python3
"""One-time OAuth code exchange; all sensitive inputs/outputs stay in Secrets."""
import argparse
import hmac
import json
import os
import subprocess
import urllib.parse

from export_oura import ExportError, encode, mask_tokens, oauth, required, utc_now


def secret_command(operation, name, value=None):
    command = ["gh", "secret", operation, name, "--repo",
               required("GITHUB_REPOSITORY"), "--env", "oura-export"]
    try:
        result = subprocess.run(
            command, input=value, env=dict(os.environ, GH_TOKEN=required("GH_SECRETS_TOKEN")),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise ExportError("GitHub environment secret operation failed; no secret values were logged")


def exchange(provider):
    raw = required("OAUTH_BOOTSTRAP_JSON")
    try:
        config = json.loads(raw)
        if config["provider"] != provider:
            raise ValueError()
        redirect = urllib.parse.urlsplit(config["redirect_uri"])
        callback = urllib.parse.urlsplit(config["callback_url"])
        if (redirect.scheme, redirect.netloc, redirect.path) != (callback.scheme, callback.netloc, callback.path):
            raise ValueError()
        # This bootstrap accepts only the local redirects used by our own clients.
        if redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1", "::1") or redirect.query or redirect.fragment or callback.fragment:
            raise ValueError()
        params = urllib.parse.parse_qs(callback.query, strict_parsing=True)
        if "error" in params or len(params.get("code", [])) != 1 or len(params.get("state", [])) != 1:
            raise ValueError()
        if not config["state"] or not hmac.compare_digest(params["state"][0], config["state"]):
            raise ValueError()
        verifier = config["code_verifier"]
        if not 43 <= len(verifier) <= 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~" for c in verifier):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ExportError("Invalid bootstrap input, redirect, PKCE verifier, or OAuth state") from None
    prefix = provider.upper()
    fields = {
        "grant_type": "authorization_code", "code": params["code"][0],
        "redirect_uri": config["redirect_uri"], "code_verifier": verifier,
        "client_id": required(prefix + "_CLIENT_ID"),
        "client_secret": required(prefix + "_CLIENT_SECRET"),
    }
    # Check writer access before consuming the one-use authorization code.
    secret_command("set", "OAUTH_BOOTSTRAP_JSON", raw.encode())
    endpoint = {"oura": "https://api.ouraring.com/oauth/token",
                "google": "https://oauth2.googleapis.com/token"}[provider]
    tokens = oauth(endpoint, fields, attempts=1)
    mask_tokens(tokens)
    if not tokens.get("refresh_token"):
        raise ExportError("Provider returned no refresh token; repeat offline consent")
    if provider == "oura":
        tokens["obtained_at"] = utc_now()
        target, value = "OURA_TOKEN_JSON", encode(tokens)
    else:
        target, value = "GOOGLE_REFRESH_TOKEN", tokens["refresh_token"].encode()
    # Retry only the idempotent encrypted save, never the code exchange.
    for attempt in range(5):
        try:
            secret_command("set", target, value)
            break
        except ExportError:
            if attempt == 4:
                raise
            import time
            time.sleep(2 ** attempt)
    secret_command("delete", "OAUTH_BOOTSTRAP_JSON")
    print(f"{provider}: refresh credential saved; one-time bootstrap input deleted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("oura", "google"))
    args = parser.parse_args()
    try:
        exchange(args.provider)
    except ExportError as exc:
        print(f"OAuth bootstrap stopped: {exc}")
        raise SystemExit(1) from None
