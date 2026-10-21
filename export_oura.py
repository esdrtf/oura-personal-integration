#!/usr/bin/env python3
"""Oura v2 -> Drive. Python 3.11+ stdlib; GitHub CLI persists rotating secrets."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

LOG = logging.getLogger("oura-export")
SCHEMA = 2
COLLECTIONS = (
    "daily_activity", "daily_cardiovascular_age", "daily_readiness",
    "daily_resilience", "daily_sleep", "daily_spo2", "daily_stress",
    "enhanced_tag", "heartrate", "personal_info", "rest_mode_period",
    "ring_battery_level", "ring_configuration", "session", "sleep",
    "sleep_time", "tag", "vO2_max", "workout",
)
TIMESERIES = {"heartrate", "ring_battery_level"}
UNDATED = {"personal_info", "ring_configuration"}
API = "https://api.ouraring.com/v2/usercollection/"
NOT_EXPORTED = [
    "Metrics not exposed by the official Oura v2 API (including app-only insights).",
    "Records deleted upstream are not deleted from this append-preserving archive.",
]


class ExportError(Exception):
    """Safe to print: never contains response bodies, URLs, tokens or health data."""


class HTTPFailure(ExportError):
    def __init__(self, status, oauth_error=None):
        self.status = status
        super().__init__(f"HTTP {status}" + (f" ({oauth_error})" if oauth_error else ""))


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ExportError(f"Missing secret/configuration: {name}")
    return value


def retry_delay(attempt, headers):
    value = headers.get("Retry-After", "")
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            delay = 2 ** attempt + random.random()
    return max(0, min(60, delay))


def request(method, url, *, headers=None, body=None, attempts=5, oauth_errors=False):
    """Retries only safe/idempotent operations. OAuth rotation calls use attempts=1."""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            status, response_headers = exc.code, exc.headers
            oauth_error = None
            if oauth_errors:
                # Never expose descriptions or provider bodies: they may echo secrets.
                try:
                    code = json.loads(exc.read(16384)).get("error")
                    if isinstance(code, str) and code in {
                        "invalid_request", "invalid_client", "invalid_grant",
                        "unauthorized_client", "unsupported_grant_type", "invalid_scope",
                        "access_denied", "temporarily_unavailable", "server_error",
                    }:
                        oauth_error = code
                except (ValueError, AttributeError, OSError):
                    pass
            exc.close()
            if status not in (408, 429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise HTTPFailure(status, oauth_error) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            response_headers = {}
            if attempt + 1 == attempts:
                raise ExportError("Network request failed; response outcome may be unknown") from None
        LOG.warning("Transient HTTP/network failure; retry %d/%d", attempt + 1, attempts)
        time.sleep(retry_delay(attempt, response_headers))
    raise AssertionError("unreachable")


def oauth(url, fields, *, attempts=1):
    data, _ = request("POST", url, body=urllib.parse.urlencode(fields).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      attempts=attempts, oauth_errors=True)
    try:
        result = json.loads(data)
        if not result.get("access_token"):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ExportError("Invalid OAuth response") from None


def mask_tokens(tokens):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for key in ("access_token", "refresh_token"):
            if tokens.get(key):
                # Workflow command escaping, including token values not already in Secrets.
                value = str(tokens[key]).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print(f"::add-mask::{value}", flush=True)


def persist_oura_tokens(tokens):
    """stdin only; no token in command arguments, filesystem, stdout or stderr."""
    env = dict(os.environ, GH_TOKEN=required("GH_SECRETS_TOKEN"))
    command = ["gh", "secret", "set", "OURA_TOKEN_JSON", "--repo",
               required("GITHUB_REPOSITORY"), "--env", "oura-export"]
    for attempt in range(5):
        try:
            result = subprocess.run(command, input=encode(tokens), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=60, check=False)
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < 4:
            time.sleep(2 ** attempt)
    raise ExportError("Cannot persist Oura tokens in GitHub environment secret; check GH_SECRETS_TOKEN permissions/expiry")


def oura_access_token():
    try:
        tokens = json.loads(required("OURA_TOKEN_JSON"))
        if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            raise ValueError()
    except (ValueError, TypeError):
        raise ExportError("OURA_TOKEN_JSON must contain a refresh_token") from None
    mask_tokens(tokens)
    # Validate write access BEFORE consuming the single-use refresh token.
    persist_oura_tokens(tokens)
    try:
        fresh = oauth("https://api.ouraring.com/oauth/token", {
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": required("OURA_CLIENT_ID"),
            "client_secret": required("OURA_CLIENT_SECRET"),
        })
    except ExportError:
        raise ExportError("Oura refresh failed or its outcome is unknown; do not retry the old token blindly. Reauthorize if invalidated") from None
    mask_tokens(fresh)
    if not fresh.get("refresh_token"):
        raise ExportError("Oura did not return a replacement refresh token; reauthorization required")
    fresh["obtained_at"] = utc_now()
    # Nothing, including data export, happens between receiving and persisting rotation.
    persist_oura_tokens(fresh)
    return fresh["access_token"]


class Drive:
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.refresh()
        metadata = self.json("GET", "files/" + root, params={"fields": "id,mimeType,trashed"})
        if metadata.get("trashed") or metadata["mimeType"] != "application/vnd.google-apps.folder":
            raise ExportError("Drive root is not an active folder")

    def refresh(self):
        token = oauth("https://oauth2.googleapis.com/token", {
            "grant_type": "refresh_token", "client_id": required("GOOGLE_CLIENT_ID"),
            "client_secret": required("GOOGLE_CLIENT_SECRET"),
            "refresh_token": required("GOOGLE_REFRESH_TOKEN"),
        }, attempts=5)
        self.token = token["access_token"]

    def call(self, method, path, *, params=None, body=None, content_type=None, upload=False):
        base = "https://www.googleapis.com/" + ("upload/drive/v3/" if upload else "drive/v3/")
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for auth_attempt in range(2):
            headers = {"Authorization": "Bearer " + self.token}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                return request(method, url, headers=headers, body=body)[0]
            except HTTPFailure as exc:
                if exc.status != 401 or auth_attempt:
                    raise
                self.refresh()
        raise AssertionError("unreachable")

    def json(self, method, path, *, params=None, value=None):
        return json.loads(self.call(method, path, params=params,
                                   body=encode(value) if value is not None else None,
                                   content_type="application/json"))

    def children(self, parent):
        if parent not in self.cache:
            files, cursor = [], None
            while True:
                params = {"q": "'" + parent.replace("'", "\\'") + "' in parents and trashed = false",
                          "pageSize": 1000, "fields": "nextPageToken,files(id,name,mimeType,md5Checksum)"}
                if cursor:
                    params["pageToken"] = cursor
                result = self.json("GET", "files", params=params)
                files.extend(result.get("files", []))
                cursor = result.get("nextPageToken")
                if not cursor:
                    break
            self.cache[parent] = files
        return self.cache[parent]

    def find(self, parent, name):
        matches = [f for f in self.children(parent) if f["name"] == name]
        if len(matches) > 1:
            raise ExportError("Duplicate Drive names detected; refusing ambiguous write")
        return matches[0] if matches else None

    def new_id(self):
        return self.json("GET", "files/generateIds", params={"count": 1})["ids"][0]

    def folder(self, parent, name):
        existing = self.find(parent, name)
        if existing:
            if existing["mimeType"] != "application/vnd.google-apps.folder":
                raise ExportError("Drive folder name conflicts with file")
            return existing["id"]
        fid = self.new_id()
        value = {"id": fid, "name": name, "parents": [parent],
                 "mimeType": "application/vnd.google-apps.folder"}
        try:
            self.json("POST", "files", value=value)
        except HTTPFailure as exc:
            if exc.status != 409:
                raise
            self.json("GET", "files/" + fid)
        self.children(parent).append(value)
        return fid

    def get(self, fid):
        return self.call("GET", "files/" + fid, params={"alt": "media"})

    def read(self, path):
        parts = path.split("/")
        parent = self.root
        for part in parts:
            entry = self.find(parent, part)
            if not entry:
                return None
            parent = entry["id"]
        return self.get(parent)

    def put(self, path, data, mime="application/json", *, immutable=False):
        parent = self.root
        parts = path.split("/")
        for part in parts[:-1]:
            parent = self.folder(parent, part)
        name = parts[-1]
        old = self.find(parent, name)
        digest = hashlib.md5(data).hexdigest()
        if old and old.get("md5Checksum") == digest:
            return {"drive_id": old["id"], "path": path, "sha256": sha(data), "bytes": len(data)}
        if old and immutable:
            if self.get(old["id"]) != data:
                raise ExportError("Immutable archive collision; refusing overwrite")
        elif old:
            self.call("PATCH", "files/" + old["id"], upload=True,
                      params={"uploadType": "media"}, body=data, content_type=mime)
        else:
            # Generated ID makes retries safe even when the first POST response is lost.
            fid = self.new_id()
            meta = {"id": fid, "name": name, "parents": [parent], "mimeType": mime}
            boundary = "oura_" + sha(data)[:32]
            payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                       + encode(meta) + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
                       + data + f"\r\n--{boundary}--\r\n".encode())
            try:
                self.call("POST", "files", upload=True, params={"uploadType": "multipart"},
                          body=payload, content_type="multipart/related; boundary=" + boundary)
            except HTTPFailure as exc:
                if exc.status != 409:
                    raise
                if self.get(fid) != data:
                    raise ExportError("Drive upload conflict") from None
            old = meta
            self.children(parent).append(old)
        old["md5Checksum"] = digest
        return {"drive_id": old["id"], "path": path, "sha256": sha(data), "bytes": len(data)}


def record_day(record, tz):
    # Oura day is authoritative (especially for overnight sleep).
    for key in ("day", "start_day"):
        if record.get(key):
            return date.fromisoformat(record[key]).isoformat()
    for key in ("timestamp", "start_time", "bedtime_end"):
        if record.get(key):
            stamp = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ExportError("Timestamp lacks timezone")
            return stamp.astimezone(tz).date().isoformat()
    return None


def record_key(collection, record):
    if record.get("id") is not None:
        return "id:" + str(record["id"])
    if collection in TIMESERIES and record.get("timestamp"):
        stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ExportError("Time-series timestamp lacks timezone")
        identity = [stamp.astimezone(timezone.utc).isoformat()]
        if collection == "heartrate":
            identity.append(record.get("source"))
        return "sample:" + sha(encode(identity))
    # Preserve unknown id-less records without pretending a synthetic key is an Oura ID.
    return "content:" + sha(encode(record))


def merge_records(collection, old, incoming):
    indexed = {}
    for record in [*old, *incoming]:
        if not isinstance(record, dict):
            raise ExportError("Collection contains a non-object record")
        indexed[record_key(collection, record)] = record
    return [indexed[key] for key in sorted(indexed)]


def jsonl(records):
    return b"".join(encode(record) + b"\n" for record in records)


def read_jsonl(data):
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def load_existing(drive, previous):
    records = {name: [] for name in COLLECTIONS}
    if previous.get("schema_version") == SCHEMA:
        for name, state in previous.get("collections", {}).items():
            ref = state.get("file")
            if name not in records or not ref:
                raise ExportError("Unknown collection or missing snapshot in existing manifest")
            data = drive.get(ref["drive_id"])
            if sha(data) != ref["sha256"]:
                raise ExportError("Normalized snapshot checksum mismatch")
            records[name] = merge_records(name, [], read_jsonl(data))
        return records
    if previous and previous.get("schema_version") != 1:
        raise ExportError("Unsupported existing manifest schema; refusing migration")
    # A restricted Drive scope can hide children. Verify all manifest references,
    # rather than interpreting a partial listing as an empty history.
    for path, ref in previous.get("files", {}).items():
        data = drive.read(path)
        if data is None or sha(data) != ref.get("sha256"):
            raise ExportError("Legacy file missing or checksum mismatch; refusing incomplete migration")
    # Legacy files are read, never overwritten/moved/deleted.
    history = drive.find(drive.root, "history")
    if history:
        for entry in sorted(drive.children(history["id"]), key=lambda x: x["name"]):
            if not re.fullmatch(r"\d{4}-\d{2}\.json", entry["name"]):
                continue
            payload = json.loads(drive.get(entry["id"]))
            if not isinstance(payload.get("endpoints"), dict):
                raise ExportError("Unrecognized legacy history; refusing incomplete migration")
            for name, rows in payload["endpoints"].items():
                if name not in records or not isinstance(rows, list):
                    raise ExportError("Unknown legacy collection; migration needs review")
                records[name] = merge_records(name, records[name], rows)
    profile = drive.read("profile.json")
    if profile:
        payload = json.loads(profile)
        for name in UNDATED:
            rows = payload.get(name)
            if rows is None:
                rows = payload.get("endpoints", {}).get(name)
            if rows is not None:
                records[name] = merge_records(name, [], rows if isinstance(rows, list) else [rows])
    for name, state in previous.get("endpoints", {}).items():
        if name not in records or len(records[name]) < state.get("count", 0):
            raise ExportError("Legacy record count decreased during migration; review before publishing")
    return records


def windows(start, end, days=7):
    while start < end:
        stop = min(start + timedelta(days=days), end)
        yield start, stop
        start = stop


def query_params(collection, start, end, tz):
    if collection in UNDATED:
        return {}
    if collection in TIMESERIES:
        def midnight(day):
            return datetime.combine(day, datetime.min.time(), tzinfo=tz).isoformat()
        return {"start_datetime": midnight(start), "end_datetime": midnight(end)}
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def fetch_collection(collection, start, end, tz, token, archive):
    records, evidence = [], []
    ranges = [(start, end)] if collection in UNDATED else windows(start, end)
    for left, right in ranges:
        params = query_params(collection, left, right, tz)
        seen, page = set(), 0
        while True:
            url = API + collection + ("?" + urllib.parse.urlencode(params) if params else "")
            body, _ = request("GET", url, headers={"Authorization": "Bearer " + token})
            # Persist complete raw bytes before parsing, filtering, or normalizing.
            ref = archive(collection, body)
            evidence.append({"query": dict(params), "page": page, "raw": ref})
            data = json.loads(body)
            if not isinstance(data, dict) or (collection != "personal_info" and "next_token" not in data):
                raise ExportError("Missing pagination envelope; collection completeness unknown")
            rows = [data] if collection == "personal_info" else data.get("data")
            if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                raise ExportError("Invalid Oura collection response")
            records.extend(rows)
            cursor = None if collection == "personal_info" else data.get("next_token")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ExportError("Repeated/invalid Oura pagination cursor")
            seen.add(cursor)
            params["next_token"] = cursor
            page += 1
            if page >= 10000:
                raise ExportError("Pagination safety limit reached; collection incomplete")
    return merge_records(collection, [], records), evidence


DAILY_FIELDS = {
    "daily_sleep": {"sleep_score": "score"},
    "daily_readiness": {"readiness_score": "score", "temperature_deviation_c": "temperature_deviation"},
    "daily_activity": {"activity_score": "score", "steps": "steps", "active_calories_kcal": "active_calories", "total_calories_kcal": "total_calories"},
    "daily_stress": {"stress_high_seconds": "stress_high", "recovery_high_seconds": "recovery_high"},
    "daily_cardiovascular_age": {"vascular_age_years": "vascular_age"},
    "daily_resilience": {"resilience_level": "level"},
}
SLEEP_FIELDS = {
    "sleep_seconds": "total_sleep_duration", "deep_sleep_seconds": "deep_sleep_duration",
    "rem_sleep_seconds": "rem_sleep_duration", "light_sleep_seconds": "light_sleep_duration",
    "awake_seconds": "awake_time", "time_in_bed_seconds": "time_in_bed",
    "sleep_efficiency_percent": "efficiency", "sleep_hrv_ms": "average_hrv",
    "sleep_avg_heart_rate_bpm": "average_heart_rate", "sleep_lowest_heart_rate_bpm": "lowest_heart_rate",
    "breaths_per_minute": "average_breath", "bedtime_start": "bedtime_start", "bedtime_end": "bedtime_end",
}


def daily_summary(records, states, tz, start, end, today):
    grouped = {c: defaultdict(list) for c in COLLECTIONS}
    days = set()
    for collection, rows in records.items():
        if collection in UNDATED:
            continue
        for record in rows:
            day = record_day(record, tz)
            if day:
                grouped[collection][day].append(record)
                days.add(day)
    # Include days without measurements as explicit missing rows, never zeros.
    first = min([start, *[date.fromisoformat(d) for d in days]])
    last = max([end - timedelta(days=1), *[date.fromisoformat(d) for d in days]])
    columns = ["day", "timezone", "is_complete_day", "source", "missing_collections", "failed_collections", "quality_notes",
               *[k for fields in DAILY_FIELDS.values() for k in fields],
               *SLEEP_FIELDS, "main_sleep_id", "sleep_record_count", "sleep_record_ids",
               "spo2_percent", "workout_count", "workout_seconds", "workout_record_ids"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    failures = ";".join(c for c, state in states.items() if state["status"] == "error")
    for index in range((last - first).days + 1):
        day = (first + timedelta(days=index)).isoformat()
        row = {"day": day, "timezone": str(tz), "is_complete_day": str(day < today.isoformat()).lower(),
               "source": "Oura Cloud API v2", "failed_collections": failures}
        missing, notes = [], []
        for collection, fields in DAILY_FIELDS.items():
            candidates = grouped[collection][day]
            if not candidates:
                missing.append(collection)
                continue
            if len(candidates) > 1:
                notes.append(collection + ":multiple_records_selected_latest_timestamp_then_id")
            selected = max(candidates, key=lambda r: (r.get("timestamp") or "", str(r.get("id", ""))))
            row.update({dest: selected.get(src) for dest, src in fields.items()})
        sleeps = grouped["sleep"][day]
        if sleeps:
            # Never add overlapping alternative sleep algorithms or naps to main sleep.
            main = max(sleeps, key=lambda r: (r.get("type") == "long_sleep", r.get("total_sleep_duration") or 0,
                                              r.get("bedtime_end") or "", str(r.get("id", ""))))
            row.update({dest: main.get(src) for dest, src in SLEEP_FIELDS.items()})
            row.update(main_sleep_id=main.get("id"), sleep_record_count=len(sleeps),
                       sleep_record_ids=";".join(sorted(str(r.get("id", "")) for r in sleeps)))
            if len(sleeps) > 1:
                notes.append("sleep_metrics_use_one_main_sleep;other_records_retained_in_normalized")
        else:
            missing.append("sleep")
        spo2 = grouped["daily_spo2"][day]
        if len(spo2) == 1:
            row["spo2_percent"] = (spo2[0].get("spo2_percentage") or {}).get("average")
        else:
            missing.append("daily_spo2")
            if len(spo2) > 1:
                notes.append("daily_spo2:multiple_records_not_aggregated")
        workouts = grouped["workout"][day]
        if workouts:
            row["workout_count"] = len(workouts)
            row["workout_record_ids"] = ";".join(sorted(str(r.get("id", "")) for r in workouts))
            durations = [(datetime.fromisoformat(r["start_datetime"].replace("Z", "+00:00")),
                          datetime.fromisoformat(r["end_datetime"].replace("Z", "+00:00")))
                         for r in workouts if r.get("start_datetime") and r.get("end_datetime")]
            durations.sort()
            # Overlapping workout durations form a union, not a double-counted sum.
            union = []
            for left, right in durations:
                if right < left:
                    raise ExportError("Workout duration is negative")
                if union and left <= union[-1][1]:
                    union[-1] = (union[-1][0], max(right, union[-1][1]))
                else:
                    union.append((left, right))
            if len(durations) == len(workouts):
                row["workout_seconds"] = int(sum((b - a).total_seconds() for a, b in union))
            if len(union) < len(workouts):
                notes.append("workout_count_is_record_count;duration_uses_union")
        else:
            missing.append("workout")
        row["missing_collections"] = ";".join(missing)
        row["quality_notes"] = ";".join(notes)
        writer.writerow(row)
    return output.getvalue().encode()


def start_for(collection, previous, requested, bootstrap, overlap):
    if requested:
        return requested
    state = previous.get("collections", {}).get(collection, {})
    # Query watermark, not latest measurement day (empty collections also advance).
    watermark = state.get("covered_through")
    if watermark:
        return date.fromisoformat(watermark) - timedelta(days=overlap)
    legacy = previous.get("endpoints", {}).get(collection, {}).get("last_success_at")
    if legacy:
        return date.fromisoformat(legacy[:10]) - timedelta(days=overlap)
    return bootstrap


def run_export(drive, token, *, start=None, end=None, timezone_name="America/New_York", overlap=14,
               bootstrap=date(2026, 10, 1), fetcher=fetch_collection, now=None):
    tz = ZoneInfo(timezone_name)
    generated = now or utc_now()
    today = datetime.fromisoformat(generated.replace("Z", "+00:00")).astimezone(tz).date()
    end = end or today + timedelta(days=1)
    old_bytes = drive.read("manifest.json")
    previous = json.loads(old_bytes) if old_bytes else {}
    records = load_existing(drive, previous)
    if old_bytes:
        drive.put("raw/manifests/" + sha(old_bytes) + ".json", old_bytes, immutable=True)
    states, queries, aliases = {}, {}, []
    for collection in COLLECTIONS:
        state = dict(previous.get("collections", {}).get(collection, {}))
        if not state:
            state["last_success_at"] = previous.get("endpoints", {}).get(collection, {}).get("last_success_at")
        left = start_for(collection, previous, start, bootstrap, overlap)
        if left >= end:
            raise ExportError("Start date must precede exclusive end date")
        state.update(last_attempt_at=generated, last_query={"start": left.isoformat(), "end_exclusive": end.isoformat()})
        try:
            def archive(name, body):
                return drive.put(f"raw/{name}/{sha(body)}.json", body, immutable=True)
            incoming, evidence = fetcher(collection, left, end, tz, token, archive)
            merged = merge_records(collection, records[collection], incoming)
            blob = jsonl(merged)
            ref = drive.put(f"normalized/{collection}/{sha(blob)}.jsonl", blob,
                            "application/x-ndjson", immutable=True)
            records[collection] = merged
            aliases.append((f"normalized/{collection}.jsonl", blob, "application/x-ndjson"))
            state.update(status="ok" if incoming else "empty", last_success_at=generated, file=ref,
                         last_fetch_count=len(incoming), pagination_complete=True)
            # Explicit historical backfills must not skip an unqueried gap or move cursor back.
            prior_end = state.get("covered_through")
            if not prior_end or left <= date.fromisoformat(prior_end):
                state["covered_through"] = max(prior_end or "", end.isoformat())
            state.pop("error", None)
            queries[collection] = evidence
            LOG.info("%s: %s", collection, state["status"])
        except (ExportError, ValueError, TypeError, KeyError) as exc:
            state.update(status="error", pagination_complete=False,
                         error=str(exc) if isinstance(exc, ExportError) else "Invalid data/schema")
            # The previous normalized file and successful cursor stay authoritative.
            LOG.error("%s: %s", collection, state["error"])
        rows = records[collection]
        if not state.get("file"):
            blob = jsonl(rows)
            state["file"] = drive.put(f"normalized/{collection}/{sha(blob)}.jsonl", blob,
                                      "application/x-ndjson", immutable=True)
        days = [record_day(r, tz) for r in rows] if collection not in UNDATED else []
        state.update(record_count=len(rows), last_record_day=max((d for d in days if d), default=None),
                     not_exported=(["Latest requested window failed; retained previous successful records"]
                                   if state["status"] == "error" else []),
                     coverage_notes=[
                         "All original record fields and Oura IDs retained; no upstream deletions propagated.",
                         "Empty API result is not evidence of normal health or a zero measurement.",
                         ("API has no record ID; dedup uses UTC timestamp" + (" + source." if collection == "heartrate" else "."))
                         if collection in TIMESERIES else "Dedup by collection + Oura record ID; unknown id-less records use a content hash.",
                         "Record day differs from export time; recent days can be incomplete.",
                     ])
        states[collection] = state
    run_bytes = encode({"generated_at": generated, "collections": queries})
    run_ref = drive.put("raw/runs/" + sha(run_bytes) + ".json", run_bytes, immutable=True)
    summary = daily_summary(records, states, tz, start or bootstrap, end, today)
    summary_ref = drive.put("derived/snapshots/" + sha(summary) + ".csv", summary, "text/csv", immutable=True)
    failed = [name for name, state in states.items() if state["status"] == "error"]
    manifest = {"schema_version": SCHEMA, "generated_at": generated,
                "source": "Oura Cloud API v2", "timezone": timezone_name,
                "status": "partial_failure" if failed else "ok", "collections": states,
                "derived": {"daily_summary": summary_ref}, "raw_run": run_ref,
                "coverage_notes": ["Legacy history/, changes/ and profile.json preserved without modification.",
                                   "Raw archives retain complete HTTP response bodies; legacy responses cannot be reconstructed.",
                                   "Manifest references immutable snapshots; stable filenames are convenience copies."],
                "not_exported": NOT_EXPORTED + ["Failed collection: " + c for c in failed],
                "legacy_files": previous.get("legacy_files", previous.get("files", {}))}
    # All versioned outputs must exist before switching the manifest pointer.
    # Abort if a concurrent old exporter changed the root manifest meanwhile.
    if drive.read("manifest.json") != old_bytes:
        raise ExportError("Manifest changed during export; stop other writers and rerun")
    manifest_bytes = encode(manifest)
    drive.put("raw/manifests/" + sha(manifest_bytes) + ".json", manifest_bytes, immutable=True)
    drive.put("manifest.json", manifest_bytes)
    if drive.read("manifest.json") != manifest_bytes:
        raise ExportError("Manifest readback mismatch")
    for path, blob, mime in aliases:
        drive.put(path, blob, mime)
    drive.put("derived/daily_summary.csv", summary, "text/csv")
    if drive.read("derived/daily_summary.csv") != summary:
        raise ExportError("Daily summary readback mismatch")
    # Verify each published immutable collection, including count and ID uniqueness.
    for collection, state in states.items():
        data = drive.get(state["file"]["drive_id"])
        rows = read_jsonl(data)
        if sha(data) != state["file"]["sha256"] or len(rows) != state["record_count"]:
            raise ExportError("Published collection checksum/count mismatch")
        if len({record_key(collection, r) for r in rows}) != len(rows):
            raise ExportError("Published collection has duplicate record IDs")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, help="Inclusive backfill day; default incremental with overlap")
    parser.add_argument("--end", type=date.fromisoformat, help="Exclusive end; default tomorrow in configured timezone")
    parser.add_argument("--timezone", default=os.environ.get("OURA_TIMEZONE", "America/New_York"))
    parser.add_argument("--overlap-days", type=int, default=14)
    parser.add_argument("--verify-from", type=date.fromisoformat, help="Require core daily data on/after this day")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.overlap_days < 1 or (args.start and args.end and args.start >= args.end):
            raise ExportError("Invalid date range/overlap")
        for key in ("OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "OURA_TOKEN_JSON", "GH_SECRETS_TOKEN",
                    "GITHUB_REPOSITORY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "DRIVE_FOLDER_ID"):
            required(key)
        # Fail on Drive authorization before consuming Oura's one-use token.
        drive = Drive(required("DRIVE_FOLDER_ID"))
        token = oura_access_token()
        manifest = run_export(drive, token, start=args.start, end=args.end,
                              timezone_name=args.timezone, overlap=args.overlap_days)
        if manifest["status"] != "ok":
            raise ExportError("Export completed with collection failures; see manifest and sanitized logs")
        if args.verify_from:
            for name in ("daily_sleep", "daily_readiness", "daily_activity"):
                latest = manifest["collections"][name]["last_record_day"]
                if not latest or latest < args.verify_from.isoformat():
                    raise ExportError("Backfill verification failed: core daily data has not reached requested period")
        LOG.info("Export and Drive readback verification succeeded")
        return 0
    except ExportError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception:
        LOG.error("Unexpected failure; no response body or credentials logged. Run unit tests and inspect configuration")
        return 1


if __name__ == "__main__":
    sys.exit(main())
