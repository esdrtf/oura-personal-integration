import csv
import io
import json
import unittest
from datetime import date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import export_oura as e


class MemoryDrive:
    def __init__(self, files=None):
        self.root = "root"
        self.files = dict(files or {})
        self.writes = []

    def find(self, parent, name):
        path = name if parent == self.root else parent + "/" + name
        if path in self.files or any(p.startswith(path + "/") for p in self.files):
            return {"id": path, "name": name}

    def children(self, parent):
        return [{"id": p, "name": p[len(parent)+1:]} for p in self.files
                if p.startswith(parent + "/") and "/" not in p[len(parent)+1:]]

    def get(self, fid):
        return self.files[fid]

    def read(self, path):
        return self.files.get(path)

    def put(self, path, data, mime="application/json", immutable=False):
        if immutable and path in self.files and self.files[path] != data:
            raise AssertionError("immutable overwrite")
        if self.files.get(path) != data:
            self.writes.append(path)
            self.files[path] = data
        return {"drive_id": path, "path": path, "sha256": e.sha(data), "bytes": len(data)}


def fake_fetch(collection, start, end, tz, token, archive):
    records = []
    if collection in {"daily_sleep", "daily_readiness", "daily_activity"}:
        records = [{"id": collection + "-oct", "day": "2026-10-02", "score": 80}]
    if collection == "sleep":
        records = [{"id": "sleep-oct", "day": "2026-10-02", "type": "long_sleep", "total_sleep_duration": 25200}]
    raw = e.encode({"data": records, "next_token": None})
    return records, [{"raw": archive(collection, raw)}]


class ExportTests(unittest.TestCase):
    def legacy(self):
        return MemoryDrive({
            "manifest.json": e.encode({"schema_version": 1, "endpoints": {"daily_sleep": {"last_success_at": "2026-10-01T00:01:02Z"}}}),
            "history/2026-09.json": e.encode({"endpoints": {"daily_sleep": [{"id": "sep", "day": "2026-09-30", "score": 65}]}}),
            "profile.json": e.encode({"endpoints": {"personal_info": {"id": "person"}, "ring_configuration": [{"id": "ring"}]}}),
            "changes/old.json": b'{"old":true}',
        })

    def export(self, drive, fetcher=fake_fetch, now="2026-10-21T16:00:00Z"):
        return e.run_export(drive, "fake", start=date(2026, 10, 1), end=date(2026, 10, 22), fetcher=fetcher, now=now)

    def test_migration_and_repeated_backfill_are_idempotent(self):
        drive = self.legacy()
        legacy = {p: b for p, b in drive.files.items() if p != "manifest.json"}
        first = self.export(drive)
        before = set(drive.files)
        second = self.export(drive, now="2026-10-21T17:00:00Z")
        self.assertEqual(first["collections"]["daily_sleep"]["record_count"], 2)
        for name in e.COLLECTIONS:
            self.assertEqual(first["collections"][name]["file"], second["collections"][name]["file"])
        self.assertFalse(any(p.startswith("normalized/") for p in set(drive.files) - before))
        for p, b in legacy.items():
            self.assertEqual(drive.files[p], b)
            self.assertNotIn(p, drive.writes)
        self.assertEqual(second["collections"]["personal_info"]["record_count"], 1)
        self.assertEqual(second["collections"]["ring_configuration"]["record_count"], 1)
        csv_rows = list(csv.DictReader(io.StringIO(drive.files["derived/daily_summary.csv"].decode())))
        self.assertEqual(len({r["day"] for r in csv_rows}), len(csv_rows))
        self.assertEqual(csv_rows[-1]["day"], "2026-10-21")
        self.assertEqual(csv_rows[-1]["is_complete_day"], "false")
        self.assertEqual(next(r for r in csv_rows if r["day"] == "2026-10-03")["steps"], "")

    def test_failed_collection_retains_file_cursor_and_last_success(self):
        drive = self.legacy()
        first = self.export(drive)
        def fail(name, *args):
            if name == "daily_sleep":
                raise e.HTTPFailure(403)
            return fake_fetch(name, *args)
        second = self.export(drive, fail, now="2026-10-22T16:00:00Z")
        old, new = first["collections"]["daily_sleep"], second["collections"]["daily_sleep"]
        self.assertEqual(second["status"], "partial_failure")
        for key in ("file", "record_count", "last_success_at", "covered_through"):
            self.assertEqual(old[key], new[key])
        self.assertEqual(new["status"], "error")
        self.assertTrue(new["not_exported"])

    def test_same_id_updates_fields_without_dropping_other_ids(self):
        records = e.merge_records("sleep", [{"id": "a", "x": 1}, {"id": "b", "x": 2}], [{"id": "a", "x": 3, "array": [1, None, 3]}])
        self.assertEqual(records, [{"id": "a", "x": 3, "array": [1, None, 3]}, {"id": "b", "x": 2}])

    def test_idless_samples_deduplicate_equivalent_timestamps_and_preserve_sources(self):
        a = {"timestamp": "2026-10-01T00:00:00Z", "source": "sleep", "bpm": 60}
        b = {"timestamp": "2026-09-30T20:00:00-04:00", "source": "sleep", "bpm": 61}
        c = dict(b, source="awake")
        self.assertEqual(len(e.merge_records("heartrate", [a], [b, c])), 2)
        self.assertNotIn("id", b)

    def test_unknown_idless_records_not_collapsed(self):
        self.assertEqual(len(e.merge_records("tag", [{"x": 1}], [{"x": 2}, {"x": 1}])), 2)

    def test_oura_day_and_dst_boundaries(self):
        tz = ZoneInfo("America/New_York")
        self.assertEqual(e.record_day({"day": "2026-10-02", "timestamp": "2026-10-02T01:00:00Z"}, tz), "2026-10-02")
        self.assertEqual(e.record_day({"timestamp": "2026-10-02T01:00:00Z"}, tz), "2026-10-01")
        p = e.query_params("heartrate", date(2026, 11, 1), date(2026, 11, 2), tz)
        self.assertTrue(p["start_datetime"].endswith("-04:00"))
        self.assertTrue(p["end_datetime"].endswith("-05:00"))

    def test_pagination_archives_exact_bytes_and_all_pages(self):
        bodies = [b'{ "data": [{"id":"a","day":"2026-10-01","extra":[null,2]}], "next_token":"p2" }', b'{"data":[{"id":"b","day":"2026-10-02"}],"next_token":null}']
        archived = []
        def archive(name, body):
            archived.append(body)
            return {"sha256": e.sha(body)}
        with patch.object(e, "request", side_effect=[(b, {}) for b in bodies]) as mock:
            rows, evidence = e.fetch_collection("sleep", date(2026, 10, 1), date(2026, 10, 3), ZoneInfo("America/New_York"), "fake", archive)
        self.assertEqual(archived, bodies)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(evidence), 2)
        self.assertIn("next_token=p2", mock.call_args_list[1].args[1])
        self.assertNotIn("fields=", mock.call_args_list[0].args[1])

    def test_repeated_cursor_is_error(self):
        body = e.encode({"data": [], "next_token": "loop"})
        with patch.object(e, "request", return_value=(body, {})):
            with self.assertRaises(e.ExportError):
                e.fetch_collection("sleep", date(2026, 10, 1), date(2026, 10, 2), ZoneInfo("UTC"), "fake", lambda *a: {})

    def test_missing_pagination_marker_is_not_success(self):
        with patch.object(e, "request", return_value=(b'{"data":[]}', {})):
            with self.assertRaisesRegex(e.ExportError, "pagination envelope"):
                e.fetch_collection("sleep", date(2026, 10, 1), date(2026, 10, 2), ZoneInfo("UTC"), "fake", lambda *a: {})

    def test_missing_legacy_file_and_record_count_fail_closed(self):
        drive = self.legacy()
        prev = {"schema_version": 1, "files": {"history/missing.json": {"sha256": "missing"}}}
        with self.assertRaisesRegex(e.ExportError, "Legacy file"):
            e.load_existing(drive, prev)
        prev = {"schema_version": 1, "endpoints": {"daily_sleep": {"count": 2}}}
        with self.assertRaisesRegex(e.ExportError, "record count decreased"):
            e.load_existing(drive, prev)

    def test_drive_create_retry_uses_same_generated_id(self):
        drive = object.__new__(e.Drive)
        drive.root, drive.cache = "root", {"root": []}
        with patch.object(drive, "new_id", return_value="generated-id") as new_id, \
             patch.object(drive, "call", side_effect=e.HTTPFailure(409)) as create, \
             patch.object(drive, "get", return_value=b"payload"):
            ref = drive.put("file.json", b"payload", immutable=True)
            self.assertEqual(ref["drive_id"], "generated-id")
            self.assertIn(b'"id":"generated-id"', create.call_args.kwargs["body"])
            drive.put("file.json", b"payload", immutable=True)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(new_id.call_count, 1)

    def test_incremental_watermark_and_backfill_do_not_skip_gaps(self):
        prev = {"collections": {"sleep": {"covered_through": "2026-10-15"}}}
        self.assertEqual(e.start_for("sleep", prev, None, date(2026, 10, 1), 14), date(2026, 10, 1))
        drive = self.legacy()
        first = self.export(drive)
        second = e.run_export(drive, "fake", start=date(2026, 9, 1), end=date(2026, 9, 3), fetcher=fake_fetch, now="2026-10-22T16:00:00Z")
        self.assertEqual(first["collections"]["sleep"]["covered_through"], second["collections"]["sleep"]["covered_through"])

    def test_summary_main_sleep_and_workout_union(self):
        records = {c: [] for c in e.COLLECTIONS}
        records["sleep"] = [{"id": "main", "day": "2026-10-02", "type": "long_sleep", "total_sleep_duration": 25000},
                            {"id": "nap", "day": "2026-10-02", "type": "sleep", "total_sleep_duration": 2000}]
        records["workout"] = [{"id": "w1", "day": "2026-10-02", "start_datetime": "2026-10-02T12:00:00Z", "end_datetime": "2026-10-02T13:00:00Z"},
                              {"id": "w2", "day": "2026-10-02", "start_datetime": "2026-10-02T12:30:00Z", "end_datetime": "2026-10-02T13:30:00Z"}]
        b = e.daily_summary(records, {c: {"status": "ok"} for c in e.COLLECTIONS}, ZoneInfo("UTC"), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 3))
        row = list(csv.DictReader(io.StringIO(b.decode())))[0]
        self.assertEqual(row["sleep_seconds"], "25000")
        self.assertEqual(row["workout_seconds"], "5400")
        self.assertEqual(row["workout_count"], "2")

    def test_concurrent_manifest_change_aborts_publication(self):
        drive = self.legacy()
        def racing_fetch(name, *args):
            if name == "workout":
                drive.files["manifest.json"] = b'{"external":true}'
            return fake_fetch(name, *args)
        with self.assertRaisesRegex(e.ExportError, "Manifest changed"):
            self.export(drive, racing_fetch)
        self.assertEqual(drive.files["manifest.json"], b'{"external":true}')

    def test_checksum_mismatch_is_not_replaced(self):
        drive = self.legacy()
        manifest = self.export(drive)
        ref = manifest["collections"]["sleep"]["file"]["drive_id"]
        drive.files[ref] = b'{}\n'
        old = drive.files["manifest.json"]
        with self.assertRaisesRegex(e.ExportError, "checksum"):
            self.export(drive)
        self.assertEqual(drive.files["manifest.json"], old)

    @patch.dict(e.os.environ, {"OURA_TOKEN_JSON": '{"refresh_token":"old"}', "OURA_CLIENT_ID": "id", "OURA_CLIENT_SECRET": "secret"})
    def test_rotation_persists_before_and_immediately_after_oauth(self):
        events = []
        def persist(tokens):
            events.append(tokens["refresh_token"])
        def oauth(*a, **kw):
            events.append("oauth")
            self.assertEqual(kw, {})  # OAuth default is one attempt, not generic retries.
            return {"access_token": "access", "refresh_token": "new"}
        with patch.object(e, "persist_oura_tokens", side_effect=persist), patch.object(e, "oauth", side_effect=oauth):
            self.assertEqual(e.oura_access_token(), "access")
        self.assertEqual(events, ["old", "oauth", "new"])

    @patch.dict(e.os.environ, {"OURA_TOKEN_JSON": '{"refresh_token":"old"}'})
    def test_failed_secret_preflight_does_not_consume_refresh(self):
        with patch.object(e, "persist_oura_tokens", side_effect=e.ExportError("no permission")), patch.object(e, "oauth") as refresh:
            with self.assertRaises(e.ExportError):
                e.oura_access_token()
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
