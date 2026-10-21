#!/usr/bin/env python3
"""One-time local setup. Reads existing credentials; sends them only to GitHub Secrets."""
import argparse
import getpass
import json
from pathlib import Path
import subprocess
import sys


def gh(args, value=None):
    result = subprocess.run(["gh", *args], input=value, text=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("GitHub operation failed; verify gh login and repository/environment permissions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="esdrtf/oura-personal-integration")
    parser.add_argument("--oura-config", type=Path, default=Path.home() / ".oura-mcp/config.json")
    parser.add_argument("--oura-tokens", type=Path, default=Path.home() / ".oura-mcp/tokens.json")
    parser.add_argument("--google-authorized-user", type=Path, required=True,
                        help="OAuth JSON with client_id, client_secret, refresh_token; NOT a service-account key")
    args = parser.parse_args()
    config = json.loads(args.oura_config.expanduser().read_text()) if args.oura_config.expanduser().exists() else {}
    tokens = json.loads(args.oura_tokens.expanduser().read_text())
    google = json.loads(args.google_authorized_user.expanduser().read_text())
    if not tokens.get("refresh_token") or any(not google.get(k) for k in ("client_id", "client_secret", "refresh_token")):
        raise RuntimeError("Credential JSON missing required fields")
    secrets = {"OURA_TOKEN_JSON": json.dumps(tokens),
               "OURA_CLIENT_ID": config.get("OURA_CLIENT_ID") or getpass.getpass("Oura client ID: "),
               "OURA_CLIENT_SECRET": config.get("OURA_CLIENT_SECRET") or getpass.getpass("Oura client secret: "),
               "GOOGLE_CLIENT_ID": google["client_id"],
               "GOOGLE_CLIENT_SECRET": google["client_secret"],
               "GOOGLE_REFRESH_TOKEN": google["refresh_token"],
               "GH_SECRETS_TOKEN": getpass.getpass("Fine-grained GitHub token (this repo only, Environments read/write): ")}
    folder = input("Existing Oura Drive folder ID: ").strip()
    if not folder or any(not v for v in secrets.values()):
        raise RuntimeError("A required value is empty")
    gh(["auth", "status"])
    gh(["api", "--method", "PUT", "repos/" + args.repo + "/environments/oura-export"])
    for name, value in secrets.items():
        gh(["secret", "set", name, "--repo", args.repo, "--env", "oura-export"], value)
        print("Stored " + name)
    gh(["variable", "set", "DRIVE_FOLDER_ID", "--repo", args.repo, "--env", "oura-export", "--body", folder])
    print("Setup complete. Stop old consumers of the same Oura refresh token before dispatching the export.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError):
        print("Setup failed. Check file paths, JSON formats, GitHub login and permissions. No credential values were logged.", file=sys.stderr)
        sys.exit(1)
