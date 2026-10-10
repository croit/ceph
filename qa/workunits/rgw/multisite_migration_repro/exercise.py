#!/usr/bin/env python3
"""Exercise suspended-version deletion, server-side copy, rename, and purge.

This uses boto3 CopyObject to model rclone's server-side copy path, not the
rclone executable. Buckets are created by this harness; --empty-after-run reuses
a verified retained fixture. Reports and remaining fixtures are retained;
no direct RADOS deletion is performed. The original cases report
observations; direct and synchronized deletion cases check their results. Exit 1
means that regression failed, and exit 2 means the exercise was incomplete.
Exit 0 does not certify the observation-only cases as bug-free.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from lab import DEFAULT_CONFIG, Lab, LabError, private_directory, private_write, redact


ZONES = ("zone1", "zone2")
CASES = ("control", "plain-delete", "null-single", "null-bulk", "null-numbered",
         "pending-head", "migration-keep", "migration-reap", "migration-direct-purge",
         "migration-synced-purge", "migration-synced-enumerate", "migration-synced-boto-delete",
         "migration-synced-single-delete", "migration-synced-empty")
SYNC_CASES = {"migration-synced-purge": "purge",
              "migration-synced-enumerate": "enumerate",
              "migration-synced-boto-delete": "boto-delete",
               "migration-synced-single-delete": "single-delete",
               "migration-synced-empty": "empty"}
SINGLE_CASE = "migration-synced-single-delete"
EMPTY_CASE = "migration-synced-empty"
PAYLOAD = b"RGW migration reproduction: real, nonempty object data.\n" * 80


class SynchronizationGateError(RuntimeError):
    """A measured consistency gate failed, distinct from transport failures."""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence

# One bounded subprocess per node, rather than one SSH connection per xattr.
# All object names are generated ASCII keys. Inspect only this bucket's marker.
INSPECT = r'''
import base64, json, pathlib, re, subprocess, sys
build, pool, marker, sample_limit = sys.argv[1:]
base = [str(pathlib.Path(build) / "bin/rados"), "-c",
        str(pathlib.Path(build) / "ceph.conf"), "-p", pool]
def run(*args):
    return subprocess.run(base + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=30)
p = run("ls")
if p.returncode:
    sys.exit(p.stderr.decode(errors="replace"))
oids = sorted((x for x in p.stdout.decode().splitlines()
               if x.startswith(marker + "_")),
              key=lambda x: (x[len(marker) + 1:].startswith("_"), x))
items = []
sizes = {}
errors = []
for oid in oids:
    stat = run("stat", oid)
    if stat.returncode:
        errors.append({"oid": oid, "error": stat.stderr.decode(errors="replace")})
        continue
    text = stat.stdout.decode()
    match = re.search(r", size (\d+)", text)
    size = int(match.group(1)) if match else None
    sizes[oid] = size
    if len(items) >= int(sample_limit):
        continue
    names = run("listxattr", oid)
    attrs = {}
    if names.returncode == 0:
        for name in names.stdout.decode().splitlines():
            name = name.strip()
            if name.startswith("user.rgw.olh.") or name == "user.rgw.manifest":
                value = run("getxattr", oid, name)
                attrs[name] = {"base64": base64.b64encode(value.stdout).decode(),
                               "returncode": value.returncode}
    items.append({"oid": oid, "size": size, "stat": text.strip(), "attrs": attrs})
print(json.dumps({"pool": pool, "marker": marker, "object_count": len(oids),
                  "zero_size_count": sum(size == 0 for size in sizes.values()),
                  "objects": oids, "sizes": sizes, "samples": items,
                  "errors": errors}))
'''


def version_rows(response, strict=False):
    rows = []
    for field, marker in (("Versions", False), ("DeleteMarkers", True)):
        for item in response.get(field, []):
            if strict and (not isinstance(item.get("Key"), str) or not item["Key"]
                           or not isinstance(item.get("VersionId"), str) or not item["VersionId"]
                           or type(item.get("IsLatest")) is not bool
                           or (not marker and (type(item.get("Size")) is not int or item["Size"] < 0))):
                raise RuntimeError("Incomplete key/version identity in version-list response")
            rows.append({"key": item["Key"],
                         "version_id": item.get("VersionId") or "null",
                         "delete_marker": marker,
                         "is_latest": bool(item.get("IsLatest")),
                         "size": item.get("Size", 0)})
    return rows


def canonical(rows):
    return sorted((r["key"], r["version_id"], r["delete_marker"],
                   r["is_latest"], r["size"]) for r in rows)


def counts(rows):
    return {"versions": sum(not r["delete_marker"] for r in rows),
            "null_data": sum(not r["delete_marker"] and r["version_id"] == "null"
                             for r in rows),
            "markers": sum(r["delete_marker"] for r in rows),
            "null_markers": sum(r["delete_marker"] and r["version_id"] == "null"
                                for r in rows)}


def expected_after_plain_delete(rows, key):
    """Suspended DELETE replaces null data/marker, preserving numbered history."""
    result = []
    for row in rows:
        if row["key"] == key and row["version_id"] == "null":
            continue
        entry = dict(row)
        if entry["key"] == key:
            entry["is_latest"] = False
        result.append(entry)
    result.append({"key": key, "version_id": "null", "delete_marker": True,
                   "is_latest": True, "size": 0})
    return result


def numbered_cohort(rows, objects):
    """Require every numbered data version from the known fixture writes."""
    size = len(PAYLOAD)
    expected = {"seed": [size]}
    for i in range(objects):
        expected["marker{:04d}".format(i)] = [size]
        expected["history{:04d}".format(i)] = [size, size + len(b"newer")]
    numbered = [r for r in rows if not r["delete_marker"] and r["version_id"] != "null"]
    if len(numbered) != 3 * objects + 1:
        raise RuntimeError("Numbered OLD fixture has {} versions; expected {}".format(
            len(numbered), 3 * objects + 1))
    sids = [r["version_id"] for r in numbered]
    if (any(not isinstance(sid, str) or not sid for sid in sids)
            or len(set(sids)) != len(sids)):
        raise RuntimeError("Numbered OLD fixture has duplicate or missing version IDs")
    observed = {}
    for row in numbered:
        observed.setdefault(row["key"], []).append(row["size"])
    if {key: sorted(sizes) for key, sizes in observed.items()} != expected:
        raise RuntimeError("Numbered OLD fixture differs from the seed/marker/history writes")
    return numbered


def fixture_current_payloads(objects):
    expected = {"seed": PAYLOAD, "final": PAYLOAD + b"final"}
    expected.update({"data{:04d}".format(i): PAYLOAD for i in range(objects)})
    expected.update({"history{:04d}".format(i): PAYLOAD + b"null-overwrite"
                     for i in range(objects)})
    return expected


def checked_version_rows(rows):
    """Explicit deletion requires complete, unambiguous S3 identities."""
    if not isinstance(rows, list):
        raise RuntimeError("Missing version-list observation")
    identities, sids = set(), set()
    for row in rows:
        if (not isinstance(row, dict)
                or not {"key", "version_id", "delete_marker", "is_latest", "size"} <= row.keys()
                or not isinstance(row["key"], str) or not row["key"]
                or not isinstance(row["version_id"], str) or not row["version_id"]
                or type(row["delete_marker"]) is not bool or type(row["is_latest"]) is not bool
                or type(row["size"]) is not int or row["size"] < 0
                or (row["delete_marker"] and row["size"] != 0)):
            raise RuntimeError("Incomplete or invalid explicit-version deletion identity")
        identity = (row["key"], row["version_id"])
        if identity in identities or (row["version_id"] != "null" and row["version_id"] in sids):
            raise RuntimeError("Duplicate or ambiguous version identity")
        identities.add(identity)
        if row["version_id"] != "null":
            sids.add(row["version_id"])
    for key in {r["key"] for r in rows}:
        if sum(r["is_latest"] for r in rows if r["key"] == key) != 1:
            raise RuntimeError("Version history lacks one authoritative latest entry for " + key)
    return rows


def retained_empty_cohort(rows, objects):
    checked_version_rows(rows)
    numbered = numbered_cohort(rows, objects)
    expected_counts = {"versions": 3 * objects + 1, "null_data": 0,
                       "markers": 4 * objects + 2, "null_markers": 3 * objects + 2}
    if counts(rows) != expected_counts or any(r["is_latest"] for r in numbered):
        raise RuntimeError("OLD is not the complete retained post-plain-delete fixture")
    markers = {r["key"]: r for r in rows if r["delete_marker"]}
    null_keys = set(fixture_current_payloads(objects)) | {
        "nullmarker{:04d}".format(i) for i in range(objects)}
    numbered_keys = {"marker{:04d}".format(i) for i in range(objects)}
    if (set(markers) != null_keys | numbered_keys
            or len(markers) != expected_counts["markers"]
            or any(not r["is_latest"] for r in markers.values())
            or any((markers[key]["version_id"] == "null") != (key in null_keys)
                   for key in markers)):
        raise RuntimeError("OLD delete-marker cohorts differ from the known fixture")
    return numbered


def ordered_version_deletes(rows):
    checked_version_rows(rows)
    if any(r["is_latest"] and not r["delete_marker"] for r in rows):
        raise RuntimeError("Explicit emptying does not support current data or head promotion")
    return sorted((dict(r) for r in rows), key=lambda r: (
        0 if not r["delete_marker"] else (2 if r["is_latest"] else 1),
        r["key"], r["version_id"]))


def expected_after_version_delete(rows, target):
    checked_version_rows(rows)
    if not isinstance(target, dict) or type(target.get("is_latest")) is not bool:
        raise RuntimeError("Incomplete explicit deletion target")
    checked_version_rows([dict(target, is_latest=True)])
    matches = [r for r in rows if (r["key"], r["version_id"]) ==
               (target["key"], target["version_id"])]
    if len(matches) != 1 or canonical(matches) != canonical([target]):
        raise RuntimeError("Explicit deletion target is missing or changed")
    remaining = [dict(r) for r in rows if (r["key"], r["version_id"]) !=
                 (target["key"], target["version_id"])]
    if target["is_latest"] and any(r["key"] == target["key"] for r in remaining):
        raise RuntimeError("Refusing an explicit deletion that would promote another version")
    checked_version_rows(remaining)
    return remaining


def checked_resume_run(run):
    if not isinstance(run, str) or re.fullmatch(r"rgwlab-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", run) is None:
        raise ValueError("--empty-after-run requires a generated rgwlab-YYYYMMDD-HHMMSS-4hex run")
    return run


def resume_report_path(artifact_dir, run):
    checked_resume_run(run)
    try:
        expected = Path(artifact_dir).resolve() / "runs" / run / "report.json"
        if expected.resolve() != expected:
            raise ValueError("Resume report must remain at its exact owned artifact_dir/runs path")
    except (OSError, RuntimeError) as error:
        raise ValueError("Cannot resolve the owned resume report path") from error
    return expected


def resume_empty_spec(prior, run, endpoints, objects):
    checked_resume_run(run)
    if (not isinstance(prior, dict) or prior.get("run") != run
            or prior.get("completed") is not True or prior.get("endpoints") != endpoints):
        raise ValueError("Resume requires a completed matching-endpoint report for this run")
    cases = prior.get("cases", {})
    if not isinstance(cases, dict) or not isinstance(cases.get(SINGLE_CASE), dict):
        raise ValueError("Resume report lacks the single-delete case")
    case = cases[SINGLE_CASE]
    expected = fixture_current_payloads(objects)
    retired, replacement = run + "-" + SINGLE_CASE + "-tmp", run + "-" + SINGLE_CASE
    if (case.get("passed") is not True or case.get("all_keys_processed") is not True
            or case.get("old_bucket_after_rename") != retired
            or case.get("replacement_bucket_after_rename") != replacement
            or case.get("planned_keys") != sorted(expected)
            or case.get("requests_sent") != len(expected)
            or case.get("numbered_versions_expected") != 3 * objects + 1
            or case.get("operation") != "S3 DeleteObject without VersionId"
            or case.get("bulk") is not False or case.get("bucket_purge_performed") is not False):
        raise ValueError("Resume requires the exact successful single-delete fixture and object count")
    old_ids, new_ids = case.get("old_ids", {}), case.get("new_ids", {})
    if (not isinstance(old_ids, dict) or not isinstance(new_ids, dict)
            or set(old_ids) != set(ZONES) or set(new_ids) != set(ZONES)
            or any(not isinstance(ids.get(z), str) or not ids[z] for ids in (old_ids, new_ids) for z in ZONES)
            or len(set(old_ids.values())) != 1 or len(set(new_ids.values())) != 1
            or any(old_ids[z] == new_ids[z] for z in ZONES)):
        raise ValueError("Resume requires distinct, consistent saved OLD/NEW identities")
    deletes = case.get("deletes", [])
    if (not isinstance(deletes, list) or any(not isinstance(d, dict) for d in deletes)
            or len(deletes) != len(expected) or [d.get("key") for d in deletes] != sorted(expected)
            or any(d.get("passed") is not True or d.get("zone") != "zone1"
                   or d.get("explicit_version_id") is not False
                   or not isinstance(d.get("response"), dict)
                   or d.get("response", {}).get("http_status") != 204
                   or d.get("response", {}).get("delete_marker") is not True
                   or d.get("response", {}).get("version_id") != "null" for d in deletes)):
        raise ValueError("Resume requires all successful ordinary DELETE receipts")
    last = deletes[-1]
    if any(not isinstance(last.get(field), dict) for field in ("old_state", "new_state", "physical")):
        raise ValueError("Resume report lacks final per-zone observations")
    histories, markers = {}, {}
    for zone in ZONES:
        old = last.get("old_state", {}).get(zone, {})
        new = last.get("new_state", {}).get(zone, {})
        if (not isinstance(old, dict) or not isinstance(new, dict)
                or old.get("exists") is not True or old.get("admin_returncode") != 0
                or old.get("id") != old_ids[zone] or old.get("current_keys") != []
                or new.get("exists") is not True or new.get("admin_returncode") != 0
                or new.get("id") != new_ids[zone] or new.get("current_keys") != sorted(expected)):
            raise ValueError("Saved final metadata does not describe the retained OLD and protected NEW")
        histories[zone] = old.get("rows")
        retained_empty_cohort(histories[zone], objects)
        if old.get("counts") != counts(histories[zone]):
            raise ValueError("Saved OLD counts disagree with its complete version history")
        physical = last["physical"].get(zone, {})
        markers[zone] = physical.get("marker") if isinstance(physical, dict) else None
        if not isinstance(markers[zone], str) or not markers[zone]:
            raise ValueError("Resume requires the saved OLD physical marker")
    if canonical(histories["zone1"]) != canonical(histories["zone2"]):
        raise ValueError("Saved OLD histories differ across zones")
    return {"retired": retired, "replacement": replacement, "old_ids": old_ids,
            "new_ids": new_ids, "old_markers": markers, "rows": histories["zone1"],
            "expected": expected, "single_deletion": case}


def marker_only(rows):
    data_keys = {r["key"] for r in rows if not r["delete_marker"]}
    return [r for r in rows if r["delete_marker"] and r["key"] not in data_keys]


def observed_rows(snapshot, zone):
    observation = snapshot[zone]
    if "rows" not in observation:
        raise RuntimeError("Version-list observation failed on {}: {}".format(
            zone, observation.get("error", "unknown error")))
    return observation["rows"]


def direct_purge_checks(returncode, old_state, physical, replacement_state,
                        replacement_reads, expected_keys, expected_ids):
    """Do not confuse missing metadata or failed observations with cleanup."""
    checks = {"purge_command_succeeded": returncode == 0}
    for zone in ZONES:
        old = old_state[zone]
        raw = physical[zone]
        new = replacement_state[zone]
        reads = replacement_reads[zone]
        checks[zone + "_old_bucket_removed"] = (
            old.get("exists") is False and old.get("admin_returncode") is not None
            and old["admin_returncode"] != 0)
        checks[zone + "_old_raw_objects_removed"] = (
            raw.get("object_count") == 0 and not raw.get("errors")
            and "error" not in raw and raw.get("returncode", 0) == 0)
        checks[zone + "_replacement_identity_preserved"] = (
            new.get("exists") is True and new.get("admin_returncode") == 0
            and new.get("id") == expected_ids[zone])
        checks[zone + "_replacement_listing_preserved"] = (
            new.get("current_keys") == sorted(expected_keys))
        checks[zone + "_replacement_payloads_preserved"] = (
            len(reads) == len(expected_keys) and bool(expected_keys)
            and sorted(r["key"] for r in reads) == sorted(expected_keys)
            and all(r.get("matches") is True for r in reads))
    return checks


def select_cases(selected, backlog, empty_after_run=None):
    if empty_after_run is not None:
        checked_resume_run(empty_after_run)
        if backlog or (selected is not None and tuple(selected) != (EMPTY_CASE,)):
            raise ValueError("--empty-after-run permits only migration-synced-empty without backlog")
        return (EMPTY_CASE,)
    checkpointed = {"migration-direct-purge", *SYNC_CASES}
    cases = tuple(selected) if selected else tuple(
        name for name in CASES if not backlog or name not in checkpointed)
    if backlog and any(name in checkpointed for name in cases):
        raise ValueError("checkpointed deletion cases cannot use --rename-with-backlog")
    return cases


def stream_caught_up(status):
    text = status.get("stdout", "")
    # Recognize the per-bucket completed-incremental status, not a global
    # "incremental sync: 0/N shards" counter or an unrecognized summary.
    incremental = re.findall(r"(?m)^\s*incremental sync on ([1-9][0-9]*) shards\s*$", text)
    return (status.get("returncode") == 0 and len(incremental) == 1
            and re.search(r"(?m)^\s*bucket is caught up with source\s*$", text) is not None
            and re.search(r"(?i)\bfull sync\b|\bbucket is behind\b", text) is None)


class Exercise:
    def __init__(self, lab, objects, timeout, backlog):
        self.lab = lab
        self.objects = objects
        self.timeout = timeout
        self.backlog = backlog
        self.prefix = "rgwlab-" + datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.output = private_directory(Path(lab.config["artifact_dir"]) / "runs" / self.prefix)
        self.report = {"run": self.prefix, "started_at": self.now(),
                       "endpoints": lab.config["endpoints"],
                       "versions": lab.config.get("versions"), "cases": {},
                       "method": "boto3 CopyObject; fixtures retained; no raw RADOS deletes",
                       "rename_with_backlog": backlog, "completed": False}
        self.identities = {}
        self.pools = {}
        user = lab.config["user"]
        config = Config(signature_version="s3v4", connect_timeout=5,
                        read_timeout=30, retries={"mode": "standard", "max_attempts": 2},
                        s3={"addressing_style": "path"},
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required")
        self.s3 = {zone: boto3.client("s3", endpoint_url=lab.config["endpoints"][zone],
                                     region_name="us-east-1", config=config,
                                     aws_access_key_id=user["access_key"],
                                     aws_secret_access_key=user["secret_key"])
                   for zone in ZONES}
        for zone in ZONES:
            info = json.loads(lab.admin(zone, "zone", "get").stdout)
            placements = info["placement_pools"]
            if isinstance(placements, list):
                placements = {entry["key"]: entry["val"] for entry in placements}
            placement = placements["default-placement"]
            self.pools[zone] = placement["storage_classes"]["STANDARD"]["data_pool"]

    @staticmethod
    def now():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def save(self):
        private_write(self.output / "report.json", json.dumps(self.report, indent=2) + "\n")

    def artifact(self, name, value):
        private_write(self.output / (name + ".json"),
                      json.dumps(value, indent=2, default=str) + "\n")

    def owned(self, bucket):
        if (not bucket.startswith(self.prefix + "-")
                and bucket not in getattr(self, "resumed_buckets", ())):
            raise RuntimeError("Refusing to mutate a bucket not created by this run")

    def wait(self, description, probe):
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            try:
                result = probe()
                if result:
                    return result
            except ClientError as error:
                last = error.response["Error"]["Code"]
            time.sleep(1)
        raise RuntimeError("Timed out waiting for {} (last S3 error: {})".format(
            description, last))

    def get(self, zone, bucket, key, version=None):
        args = {"Bucket": bucket, "Key": key}
        if version is not None:
            args["VersionId"] = version
        response = self.s3[zone].get_object(**args)
        with response["Body"] as body:
            return body.read()

    def rows(self, zone, bucket, prefix=None, strict=False):
        kwargs = {"Bucket": bucket}
        if prefix is not None:
            kwargs["Prefix"] = prefix
        result = []
        for page in self.s3[zone].get_paginator("list_object_versions").paginate(**kwargs):
            result.extend(version_rows(page, strict=strict))
        return sorted(result, key=lambda r: (r["key"], r["version_id"], r["delete_marker"]))

    def sync_status(self, zone, bucket):
        source = "zone1" if zone == "zone2" else "zone2"
        result = self.lab.admin(zone, "bucket", "sync", "status", "--bucket", bucket,
                                "--source-zone", source, check=False, timeout=30)
        return {"returncode": result.returncode, "stdout": result.stdout,
                "stderr": self.lab.redact(result.stderr)}

    def drain(self, bucket, zone="zone2", required=True):
        deadline = time.monotonic() + self.timeout
        stable = 0
        status = {}
        while time.monotonic() < deadline:
            status = self.sync_status(zone, bucket)
            if status["returncode"] == 0 and "bucket is caught up with source" in status["stdout"]:
                stable += 1
                if stable == 2:
                    return status
            else:
                stable = 0
            time.sleep(1)
        if required:
            raise RuntimeError("Replication did not reach its marker checkpoint for " + bucket)
        return status

    def matching(self, bucket, diagnostic_label=None):
        try:
            self.wait("version-list equality for " + bucket,
                      lambda: canonical(self.rows("zone1", bucket)) == canonical(
                          self.rows("zone2", bucket)))
        except RuntimeError:
            if diagnostic_label:
                self.artifact(diagnostic_label, self.snapshot(bucket))
            raise
        self.drain(bucket)

    def prepare(self, tag, versioned=False):
        bucket = self.prefix + "-" + tag
        self.s3["zone1"].create_bucket(Bucket=bucket)
        self.wait("secondary bucket metadata", lambda: self.s3["zone2"].head_bucket(Bucket=bucket))
        if versioned:
            self.set_versioning(bucket, "Enabled")
        self.s3["zone1"].put_object(Bucket=bucket, Key="seed", Body=PAYLOAD)
        self.wait("initial seed replication", lambda: self.get("zone2", bucket, "seed") == PAYLOAD)
        status = self.drain(bucket)
        if "incremental sync" not in status["stdout"]:
            raise RuntimeError("Initial full sync has not finished: " + bucket)
        for zone in ZONES:
            self.identities[(zone, bucket)] = json.loads(self.lab.admin(
                zone, "bucket", "stats", "--bucket", bucket).stdout)
        return bucket

    def set_versioning(self, bucket, status):
        self.owned(bucket)
        self.s3["zone1"].put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": status})
        for zone in ZONES:
            self.wait(zone + " versioning=" + status, lambda z=zone:
                      self.s3[z].get_bucket_versioning(Bucket=bucket).get("Status") == status)

    def write_nulls(self, bucket, after_suspend=None):
        self.set_versioning(bucket, "Suspended")
        if after_suspend is not None:
            after_suspend()
        for i in range(self.objects):
            self.s3["zone1"].put_object(Bucket=bucket, Key="data{:04d}".format(i), Body=PAYLOAD)
        self.matching(bucket)

    def snapshot(self, bucket):
        result = {}
        for zone in ZONES:
            try:
                rows = self.rows(zone, bucket)
                result[zone] = {"rows": rows, "counts": counts(rows),
                                "sync": self.sync_status(zone, bucket)}
            except ClientError as error:
                result[zone] = {"error": error.response["Error"]}
        return result

    def inspect(self, bucket, label):
        result = {}
        for zone in ZONES:
            identity = self.identities.get((zone, bucket))
            if identity is None:
                result[zone] = {"error": "no saved pre-removal bucket identity"}
                continue
            response = self.lab.nodes[zone].run(
                ["python3", "-c", INSPECT, self.lab.nodes[zone].build_dir,
                 self.pools[zone], identity["marker"], "12"], timeout=120, check=False)
            if response.returncode:
                result[zone] = {"returncode": response.returncode,
                                "error": self.lab.redact(response.stderr)}
            else:
                result[zone] = json.loads(response.stdout)
        self.artifact(label, result)
        return result

    def delete_rows(self, zone, bucket, rows, bulk=False):
        self.owned(bucket)
        errors = []
        if bulk:
            for start in range(0, len(rows), 1000):
                objects = [{"Key": r["key"], "VersionId": r["version_id"]}
                           for r in rows[start:start + 1000]]
                response = self.s3[zone].delete_objects(Bucket=bucket,
                                                       Delete={"Objects": objects, "Quiet": False})
                errors.extend(response.get("Errors", []))
        else:
            for row in rows:
                try:
                    self.s3[zone].delete_object(Bucket=bucket, Key=row["key"],
                                                VersionId=row["version_id"])
                except ClientError as error:
                    errors.append({"key": row["key"], "version_id": row["version_id"],
                                   "error": error.response["Error"]})
        return errors

    def checkpoint_write(self, bucket):
        self.s3["zone1"].put_object(Bucket=bucket, Key="checkpoint", Body=PAYLOAD)
        self.wait("post-delete checkpoint object", lambda:
                  self.get("zone2", bucket, "checkpoint") == PAYLOAD)
        self.drain(bucket)

    def deletion_case(self, name):
        bucket = self.prepare(name, versioned=name != "control")
        if name == "control":
            for i in range(self.objects):
                self.s3["zone1"].put_object(Bucket=bucket, Key="data{:04d}".format(i), Body=PAYLOAD)
            self.matching(bucket)
        else:
            self.write_nulls(bucket)
        before = self.snapshot(bucket)
        self.artifact(name + "-before", before)
        errors = []
        if name in ("control", "plain-delete"):
            for i in range(self.objects):
                response = self.s3["zone1"].delete_object(Bucket=bucket, Key="data{:04d}".format(i))
                errors.extend(response.get("Errors", []))
        else:
            selected = [r for r in self.rows("zone1", bucket, "data") if not r["delete_marker"]]
            errors = self.delete_rows("zone1", bucket, selected, bulk=name == "null-bulk")
        self.checkpoint_write(bucket)
        after = self.snapshot(bucket)
        self.artifact(name + "-after", after)
        selected = {zone: [r for r in observed_rows(after, zone) if r["key"].startswith("data")]
                    for zone in ZONES}
        primary, secondary = counts(selected["zone1"]), counts(selected["zone2"])
        parity = canonical(selected["zone1"]) == canonical(selected["zone2"])
        finding = {"bucket": bucket, "primary": primary, "secondary": secondary,
                   "version_lists_match": parity, "api_errors": errors,
                   "null_version_entries_retained": primary["null_data"] == 0 and secondary["null_data"] > 0,
                   "checkpoint": after["zone2"].get("sync")}
        retained = []
        for row in selected["zone2"]:
            if row["delete_marker"] or row["version_id"] != "null":
                continue
            try:
                data = self.get("zone2", bucket, row["key"], "null")
                retained.append({"key": row["key"], "size": len(data),
                                 "payload_matches": data == PAYLOAD})
            except ClientError as error:
                retained.append({"key": row["key"], "error": error.response["Error"]})
        finding["retained_null_payloads"] = retained
        finding["readable_retained_nulls"] = sum(r.get("payload_matches") is True for r in retained)
        finding["null_data_retention_reproduced"] = (
            finding["null_version_entries_retained"] and finding["readable_retained_nulls"] > 0)
        self.report["cases"][name] = finding
        self.inspect(bucket, name + "-heads")
        print("{}: primary null data={}, secondary null data={}, parity={}".format(
            name, primary["null_data"], secondary["null_data"], parity), flush=True)

    def pending_head(self):
        bucket = self.prepare("pending-head", versioned=True)
        version_ids = {}
        for i in range(self.objects):
            key = "data{:04d}".format(i)
            first = self.s3["zone1"].put_object(Bucket=bucket, Key=key, Body=PAYLOAD)["VersionId"]
            marker = self.s3["zone1"].delete_object(Bucket=bucket, Key=key)["VersionId"]
            second = self.s3["zone1"].put_object(Bucket=bucket, Key=key, Body=PAYLOAD + b"second")["VersionId"]
            version_ids[key] = (first, marker, second)
        self.matching(bucket)
        # Delete a non-current marker, then the remaining data versions before
        # its pending tag can expire. Do not GET the removed keys afterward.
        for key, (first, marker, second) in version_ids.items():
            for version in (marker, first, second):
                self.s3["zone1"].delete_object(Bucket=bucket, Key=key, VersionId=version)
        self.drain(bucket, required=False)
        after = self.snapshot(bucket)
        self.artifact("pending-head-after", after)
        physical = self.inspect(bucket, "pending-head-physical")
        self.report["cases"]["pending-head"] = {
            "bucket": bucket, "listed_data_keys": {z: sum(r["key"].startswith("data")
                for r in observed_rows(after, z)) for z in ZONES},
            "zero_size_objects": {z: physical[z].get("zero_size_count") for z in ZONES},
            "deleted_key_heads": {z: (sum(physical[z]["sizes"].get(
                self.identities[(z, bucket)]["marker"] + "_data{:04d}".format(i)) == 0
                for i in range(self.objects)) if "sizes" in physical[z] else None) for z in ZONES},
            "note": "Inspect samples for OLH pending attributes; seed is deliberately retained."}
        print("pending-head: zero-size objects=" + str(
            self.report["cases"]["pending-head"]["zero_size_objects"]), flush=True)

    def null_numbered(self):
        bucket = self.prepare("null-numbered")
        for i in range(self.objects):
            self.s3["zone1"].put_object(Bucket=bucket, Key="data{:04d}".format(i), Body=PAYLOAD)
        self.matching(bucket)
        self.set_versioning(bucket, "Enabled")
        numbered = []
        for i in range(self.objects):
            key = "data{:04d}".format(i)
            version = self.s3["zone1"].put_object(
                Bucket=bucket, Key=key, Body=PAYLOAD + b"numbered")["VersionId"]
            numbered.append({"key": key, "version_id": version})
        self.matching(bucket)
        self.artifact("null-numbered-before", self.snapshot(bucket))
        errors = self.delete_rows("zone1", bucket, [
            {"key": row["key"], "version_id": "null"} for row in numbered])
        self.checkpoint_write(bucket)
        after_null = self.snapshot(bucket)
        self.artifact("null-numbered-after-null", after_null)
        preserved = self.validate_current(bucket, {
            row["key"]: PAYLOAD + b"numbered" for row in numbered})
        self.artifact("null-numbered-numbered-payloads", preserved)
        errors.extend(self.delete_rows("zone1", bucket, numbered))
        self.drain(bucket)
        after_all = self.snapshot(bucket)
        self.artifact("null-numbered-after-all", after_all)
        selected = {z: [r for r in observed_rows(after_null, z)
                        if r["key"].startswith("data")] for z in ZONES}
        self.report["cases"]["null-numbered"] = {
            "bucket": bucket, "api_errors": errors,
            "after_null_counts": {z: counts(selected[z]) for z in ZONES},
            "lists_match_after_null": canonical(selected["zone1"]) == canonical(selected["zone2"]),
            "numbered_payloads_preserved": all(r.get("matches") is True
                for values in preserved.values() for r in values),
            "listed_data_keys_after_all": {z: sum(r["key"].startswith("data")
                for r in observed_rows(after_all, z)) for z in ZONES}}
        print("null-numbered: after-null counts=" + str(
            self.report["cases"]["null-numbered"]["after_null_counts"]), flush=True)

    def current_rows(self, zone, bucket):
        rows = []
        for page in self.s3[zone].get_paginator("list_objects_v2").paginate(Bucket=bucket):
            rows.extend(page.get("Contents", []))
        return rows

    def copy_current(self, source, destination):
        self.owned(source)
        self.owned(destination)
        keys = [r["Key"] for r in self.current_rows("zone1", source)]
        for key in keys:
            self.s3["zone1"].copy_object(Bucket=destination, Key=key,
                                        CopySource={"Bucket": source, "Key": key},
                                        MetadataDirective="COPY")
        return keys

    def validate_current(self, bucket, expected):
        result = {}
        for zone in ZONES:
            records = []
            for key, payload in expected.items():
                try:
                    data = self.get(zone, bucket, key)
                    records.append({"key": key, "matches": data == payload,
                                    "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                except ClientError as error:
                    records.append({"key": key, "error": error.response["Error"]})
            result[zone] = records
        return result

    def bucket_state(self, zone, bucket, strict=False):
        result = self.lab.admin(zone, "bucket", "stats", "--bucket", bucket,
                                check=False, timeout=15)
        state = {"admin_returncode": result.returncode,
                 "admin_stderr": self.lab.redact(result.stderr)}
        if result.returncode == 0:
            state["id"] = json.loads(result.stdout)["id"]
        try:
            rows = self.rows(zone, bucket, strict=True) if strict else self.rows(zone, bucket)
            state.update(exists=True, counts=counts(rows), rows=rows)
            state["current_keys"] = sorted(r["Key"] for r in self.current_rows(zone, bucket))
        except ClientError as error:
            code = error.response["Error"]["Code"]
            state["exists"] = False if code == "NoSuchBucket" else None
            state["s3_error"] = error.response["Error"]
        return state

    def renamed_checkpoint(self, name, retired, replacement, expected,
                           expected_old_rows, label, start_sync):
        """Check OLD and, when present, protected NEW in both directions."""
        self.owned(retired)
        roles = [("old", retired)]
        if replacement is not None:
            self.owned(replacement)
            roles.append(("new", replacement))
            if retired == replacement:
                raise RuntimeError("OLD and NEW names must be distinct")
        for zone in ZONES:
            old_id = self.identities[(zone, retired)].get("id")
            if not old_id:
                raise RuntimeError("OLD bucket identity is missing on " + zone)
            if replacement is not None:
                new_id = self.identities[(zone, replacement)].get("id")
                if not new_id or old_id == new_id:
                    raise RuntimeError("OLD and NEW bucket IDs must be distinct on " + zone)
        sync_commands = []
        if start_sync:
            for role, bucket in roles:
                for zone in ("zone2", "zone1"):
                    source = "zone1" if zone == "zone2" else "zone2"
                    command = self.lab.admin(
                        zone, "bucket", "sync", "run", "--bucket", bucket,
                        "--source-zone", source, timeout=max(120, self.timeout), check=False)
                    sync_commands.append({
                        "zone": zone, "bucket": bucket, "source_zone": source,
                        "returncode": command.returncode,
                        "stdout": self.lab.redact(command.stdout),
                        "stderr": self.lab.redact(command.stderr)})
                    self.artifact(name + "-" + label + "-commands", sync_commands)
                    if command.returncode != 0:
                        raise RuntimeError("Could not start synchronization of the renamed buckets")
                    # Do not overlap opposing full-sync/replay operations on
                    # the same version history. Wait for this direction before
                    # starting the reverse direction; the final gate still
                    # checks both buckets and directions twice together.
                    self.wait(zone + " renamed stream checkpoint for " + bucket,
                              lambda z=zone, b=bucket: stream_caught_up(self.sync_status(z, b)))
            self.artifact(name + "-" + label + "-commands", sync_commands)
            if any(c["returncode"] != 0 for c in sync_commands):
                raise RuntimeError("Could not start synchronization of the renamed buckets")

        deadline = time.monotonic() + self.timeout
        stable = 0
        observations = []
        while True:
            statuses = {role: {z: self.sync_status(z, bucket) for z in ZONES}
                        for role, bucket in roles}
            old_state = {z: self.bucket_state(z, retired) for z in ZONES}
            new_state = ({z: self.bucket_state(z, replacement) for z in ZONES}
                         if replacement is not None else {})
            reads = self.validate_current(replacement, expected) if replacement is not None else {}
            checks = {}
            for role, bucket in roles:
                for zone in ZONES:
                    state = old_state[zone] if role == "old" else new_state[zone]
                    checks[role + "_" + zone + "_sync_caught_up"] = stream_caught_up(
                        statuses[role][zone])
                    checks[role + "_" + zone + "_identity_matches"] = (
                        state.get("exists") is True and state.get("admin_returncode") == 0
                        and state.get("id") == self.identities[(zone, bucket)]["id"])
            for zone in ZONES:
                checks["old_" + zone + "_versions_match"] = (
                    "rows" in old_state[zone] and canonical(old_state[zone]["rows"])
                    == canonical(expected_old_rows))
                if replacement is not None:
                    checks["new_" + zone + "_listing_matches"] = (
                        new_state[zone].get("current_keys") == sorted(expected))
                    checks["new_" + zone + "_payloads_match"] = (
                        len(reads[zone]) == len(expected)
                        and all(r.get("matches") is True for r in reads[zone]))
            stable = stable + 1 if all(checks.values()) else 0
            observation = {"observed_at": self.now(), "checks": checks, "sync": statuses,
                           "consecutive_successes": stable,
                           "old_counts": {z: old_state[z].get("counts") for z in ZONES}}
            observations.append(observation)
            if stable >= 2 or time.monotonic() >= deadline:
                break
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        evidence = {"passed": stable >= 2, "sync_commands": sync_commands,
                    "last_observation": observation, "observations": observations,
                    "expected_old_rows": expected_old_rows, "old_state": old_state,
                    "new_state": new_state, "replacement_reads": reads}
        self.artifact(name + "-" + label, evidence)
        if not evidence["passed"]:
            failed = [k for k, v in checks.items() if not v]
            self.report["cases"][name] = {
                "old_bucket_after_rename": retired, "replacement_bucket_after_rename": replacement,
                "blocked_at": label, "failed_checks": failed, "passed": None,
                "bucket_deletion_performed": False,
                "enumerated_deletion_performed": label == "post-delete-empty-sync",
                "note": "Checkpoint not satisfied; do not infer a purge result."}
            self.save()
            raise SynchronizationGateError("Synchronization consistency gate failed: "
                                           + ", ".join(failed), evidence)
        return evidence

    def synchronized_delete(self, name, retired, replacement, expected, old_rows):
        synchronization = self.renamed_checkpoint(
            name, retired, replacement, expected, old_rows,
            "post-rename-sync" if replacement is not None else "pre-delete-sync",
            start_sync=replacement is not None)
        mode = SYNC_CASES[name]
        if mode in ("single-delete", "empty"):
            self.single_deletes(name, retired, replacement, expected, old_rows, synchronization)
            if mode == "empty":
                plain = self.report["cases"][name]
                if plain.get("passed") is not True:
                    plain["emptying_performed"] = False
                    return
                self.empty_versions(name, retired, replacement, expected,
                                    plain["deletes"][-1]["old_state"]["zone1"]["rows"], plain)
            return
        if mode == "boto-delete":
            self.boto_delete_populated(name, retired, replacement, expected, synchronization)
            return
        cleanup = None
        if mode == "enumerate":
            rows = self.rows("zone1", retired)
            if canonical(rows) != canonical(old_rows):
                raise RuntimeError("OLD versions changed after synchronization checkpoint")
            cleanup = {"zone": "zone1", "bulk": False, "enumerated_rows": rows,
                       "errors": self.delete_rows("zone1", retired, rows)}
            self.artifact(name + "-enumerated-deletions", cleanup)
            if cleanup["errors"]:
                raise RuntimeError("Version deletion failed; refusing to purge a nonempty bucket")
            # Do not force a new full sync against the now-empty source. Wait
            # for the already checkpointed streams to replicate these deletes.
            cleanup["empty_checkpoint"] = self.renamed_checkpoint(
                name, retired, replacement, expected, [], "post-delete-empty-sync", start_sync=False)
        self.direct_purge(name, retired, replacement, expected, synchronization, cleanup)

    def numbered_payloads(self, bucket, numbered):
        result = {}
        for zone in ZONES:
            records = []
            for row in numbered:
                payload = PAYLOAD
                if row["key"].startswith("history") and row["size"] == len(PAYLOAD) + len(b"newer"):
                    payload += b"newer"
                elif row["size"] != len(PAYLOAD):
                    raise RuntimeError("Unknown numbered payload in the single-delete fixture")
                try:
                    body = self.get(zone, bucket, row["key"], row["version_id"])
                    records.append({"key": row["key"], "version_id": row["version_id"],
                                    "matches": body == payload, "size": len(body),
                                    "sha256": hashlib.sha256(body).hexdigest()})
                except ClientError as error:
                    records.append({"key": row["key"], "version_id": row["version_id"],
                                    "error": error.response["Error"]})
            result[zone] = records
        return result

    def single_delete_observation(self, retired, replacement, expected, expected_rows,
                                  numbered, step, label, had_null_data):
        current_keys = sorted(r["key"] for r in expected_rows
                              if r["is_latest"] and not r["delete_marker"])
        remaining = {key: expected[key] for key in current_keys}
        step["remaining_old_payload_oracle"] = {
            key: {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for key, payload in remaining.items()}
        step["old_state"] = {z: self.bucket_state(z, retired) for z in ZONES}
        step["counts"] = {z: step["old_state"][z].get("counts") for z in ZONES}
        step["new_state"] = ({z: self.bucket_state(z, replacement) for z in ZONES}
                             if replacement is not None else {})
        step["physical"] = self.inspect(retired, label + "-physical")
        step["numbered_payloads"] = self.numbered_payloads(retired, numbered)
        step["remaining_old_payloads"] = self.validate_current(retired, remaining)
        step["new_payloads"] = self.validate_current(replacement, expected) if replacement is not None else {}
        missing = {}
        for zone in ZONES:
            try:
                body = self.get(zone, retired, step["key"])
                missing[zone] = {"unexpected_payload_size": len(body),
                                 "sha256": hashlib.sha256(body).hexdigest()}
            except ClientError as error:
                missing[zone] = {"error": error.response["Error"],
                                 "http_status": error.response["ResponseMetadata"]["HTTPStatusCode"]}
        step["current_reads"] = missing
        checks = {
            "single_delete_response": (step["response"]["http_status"] == 204
                                        and step["response"]["delete_marker"] is True
                                        and step["response"]["version_id"] == "null"),
            "post_delete_checkpoint": step["checkpoint_passed"]}
        for zone in ZONES:
            old = step["old_state"][zone]
            checks[zone + "_old_versions_match"] = (
                "rows" in old and canonical(old["rows"]) == canonical(expected_rows))
            checks[zone + "_old_identity_preserved"] = (
                old.get("id") == self.identities[(zone, retired)]["id"])
            checks[zone + "_old_current_listing_matches"] = (
                old.get("current_keys") == current_keys)
            reads = step["remaining_old_payloads"][zone]
            checks[zone + "_old_current_payloads_preserved"] = (
                len(reads) == len(remaining)
                and sorted(r["key"] for r in reads) == current_keys
                and all(r.get("matches") is True for r in reads))
            checks[zone + "_current_read_missing"] = (
                missing[zone].get("error", {}).get("Code") == "NoSuchKey"
                and missing[zone].get("http_status") == 404)
            checks[zone + "_numbered_history_preserved"] = (
                len(step["numbered_payloads"][zone]) == len(numbered)
                and all(r.get("matches") is True for r in step["numbered_payloads"][zone]))
            if replacement is not None:
                new = step["new_state"][zone]
                checks[zone + "_new_preserved"] = (
                    new.get("id") == self.identities[(zone, replacement)]["id"]
                    and new.get("current_keys") == sorted(expected)
                    and len(step["new_payloads"][zone]) == len(expected)
                    and all(r.get("matches") is True for r in step["new_payloads"][zone]))
            if had_null_data:
                raw = step["physical"][zone]
                oid = self.identities[(zone, retired)]["marker"] + "_" + step["key"]
                checks[zone + "_null_payload_removed"] = (
                    "objects" in raw and not raw.get("errors") and "error" not in raw
                    and (oid not in raw["objects"] or raw.get("sizes", {}).get(oid) == 0))
        step.update(checks=checks, passed=all(checks.values()))

    def record_single_delete(self, name, finding, step, label):
        if not finding["deletes"] or finding["deletes"][-1] is not step:
            finding["deletes"].append(step)
        finding["requests_sent"] = len(finding["deletes"])
        finding["all_keys_processed"] = finding["requests_sent"] == len(finding["planned_keys"])
        finding["passed"] = finding["all_keys_processed"] and all(
            d.get("passed") is True for d in finding["deletes"])
        finding["stopped_at"] = step["key"] if not step["passed"] else None
        finding["counts"] = step["counts"]
        finding["failed_checks"] = [k for k, v in step["checks"].items() if not v]
        self.report["cases"][name] = finding
        self.save()
        self.artifact(label, step)

    def single_deletes(self, name, retired, replacement, expected, old_rows, synchronization):
        """Plain DeleteObject, one key at a time; never bulk-delete or purge."""
        self.owned(retired)
        numbered = numbered_cohort(old_rows, self.objects)
        baseline = self.numbered_payloads(retired, numbered)
        self.artifact(name + "-numbered-before-deletes", baseline)
        if not all(r.get("matches") is True for records in baseline.values() for r in records):
            raise RuntimeError("Numbered OLD payloads differ before single-object deletion")
        finding = {
            "old_bucket_after_rename": retired, "replacement_bucket_after_rename": replacement,
            "old_ids": {z: self.identities[(z, retired)]["id"] for z in ZONES},
            "new_ids": {z: self.identities[(z, replacement)]["id"] for z in ZONES
                        if replacement is not None},
            "post_rename_sync": synchronization, "delete_zone": "zone1",
            "operation": "S3 DeleteObject without VersionId", "bulk": False,
            "bucket_purge_performed": False, "planned_keys": sorted(expected),
            "numbered_versions_expected": len(numbered), "deletes": [], "passed": False,
            "note": "Plain suspended DELETE leaves null markers and numbered history; no bucket purge."}
        self.report["cases"][name] = finding
        expected_rows = old_rows
        checkpoint = synchronization
        for index, key in enumerate(sorted(expected)):
            had_null_data = any(r["key"] == key and r["version_id"] == "null"
                                and not r["delete_marker"] for r in expected_rows)
            label = name + "-single-delete-{:02d}".format(index)
            step = {"key": key, "zone": "zone1", "explicit_version_id": False,
                    "passed": False, "counts": {z: None for z in ZONES},
                    "counts_before_request": {z: checkpoint.get("old_state", {}).get(
                        z, {}).get("counts") for z in ZONES}}
            try:
                response = self.s3["zone1"].delete_object(Bucket=retired, Key=key)
                step["response"] = {"http_status": response["ResponseMetadata"]["HTTPStatusCode"],
                                     "delete_marker": response.get("DeleteMarker"),
                                     "version_id": response.get("VersionId")}
            except ClientError as error:
                step.update(error=error.response["Error"], checkpoint_passed=False,
                            checkpoint_skipped=True,
                            note="DeleteObject returned an S3 error; no post-delete checkpoint was attempted.")
                step["response"] = {
                    "http_status": error.response["ResponseMetadata"]["HTTPStatusCode"],
                    "delete_marker": None, "version_id": None}
            else:
                expected_rows = expected_after_plain_delete(expected_rows, key)
                try:
                    checkpoint = self.renamed_checkpoint(
                        name, retired, replacement, expected, expected_rows,
                        "single-delete-{:02d}-sync".format(index), start_sync=False)
                    step["checkpoint_passed"] = checkpoint["passed"]
                except SynchronizationGateError as error:
                    step.update(checkpoint_passed=False, checkpoint_error=self.lab.redact(str(error)),
                                checkpoint_failed_checks=self.report["cases"][name].get("failed_checks", []),
                                note="Post-delete consistency failed; follow-up diagnostics cannot make this step pass.")
                    checkpoint = error.evidence
                if checkpoint is not None:
                    step["checkpoint_counts"] = checkpoint["last_observation"]["old_counts"]
                    step["counts"] = step["checkpoint_counts"]
            step["checks"] = {
                "single_delete_response": (step["response"]["http_status"] == 204
                                            and step["response"]["delete_marker"] is True
                                            and step["response"]["version_id"] == "null"),
                "post_delete_checkpoint": step["checkpoint_passed"]}
            if not all(step["checks"].values()):
                # Persist the issued request and known failure before optional
                # diagnostics; the gate may have replaced this case's report.
                self.record_single_delete(name, finding, step, label)
                try:
                    self.single_delete_observation(
                        retired, replacement, expected, expected_rows, numbered,
                        step, label, had_null_data)
                except (BotoCoreError, ClientError, LabError, subprocess.SubprocessError,
                        RuntimeError, KeyError, ValueError, OSError) as error:
                    step["diagnostic_error"] = self.lab.redact(str(error))
                step["passed"] = False
            else:
                # With no measured failure, observation transport errors still
                # propagate to run() as an incomplete exercise (exit 2).
                self.single_delete_observation(
                    retired, replacement, expected, expected_rows, numbered,
                    step, label, had_null_data)
            self.record_single_delete(name, finding, step, label)
            print("{}: single DELETE key={}, counts={}, consistent={}".format(
                name, key, step["counts"], step["passed"]), flush=True)
            if not step["passed"]:
                break

    def resume_empty(self, run):
        """Authorize only the two verified bucket names from a completed test."""
        path = resume_report_path(self.lab.config["artifact_dir"], run)
        if self.prefix == run or self.output.resolve() == path.parent:
            raise ValueError("Resume requires a different fresh output run")
        self.report["resumed_from_run"] = run
        self.report["resume_report"] = str(path)
        try:
            prior = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError("Could not read the owned resume report: " + self.lab.redact(str(error)))
        spec = resume_empty_spec(prior, run, self.lab.config["endpoints"], self.objects)
        retired, replacement = spec["retired"], spec["replacement"]
        identities = {}
        for zone in ZONES:
            for role, bucket in (("old", retired), ("new", replacement)):
                result = self.lab.admin(zone, "bucket", "stats", "--bucket", bucket,
                                        check=False, timeout=15)
                if result.returncode != 0:
                    raise RuntimeError("Resume bucket metadata is unavailable on " + zone)
                identity = json.loads(result.stdout)
                if (not isinstance(identity, dict) or identity.get("id") != spec[role + "_ids"][zone]
                        or not isinstance(identity.get("marker"), str) or not identity["marker"]
                        or (role == "old" and identity["marker"] != spec["old_markers"][zone])):
                    raise RuntimeError("Resume bucket identity/marker changed on " + zone)
                identities[(zone, bucket)] = identity
            old = self.bucket_state(zone, retired, strict=True)
            new = self.bucket_state(zone, replacement, strict=True)
            if (old.get("exists") is not True or old.get("admin_returncode") != 0
                    or old.get("id") != spec["old_ids"][zone] or old.get("current_keys") != []
                    or "rows" not in old or canonical(old["rows"]) != canonical(spec["rows"])
                    or new.get("exists") is not True or new.get("admin_returncode") != 0
                    or new.get("id") != spec["new_ids"][zone]
                    or new.get("current_keys") != sorted(spec["expected"])
                    or self.s3[zone].get_bucket_versioning(Bucket=retired).get("Status") != "Suspended"):
                raise RuntimeError("Resume fixture changed or is not suspended on " + zone)
        # No authorization extension occurs until both OLD/NEW identities and
        # both complete OLD histories have been verified on the owned lab.
        self.identities.update(identities)
        self.resumed_buckets = set(getattr(self, "resumed_buckets", ())) | {retired, replacement}
        self.report["resume_verified_ids"] = {"old": spec["old_ids"], "new": spec["new_ids"]}
        self.empty_versions(EMPTY_CASE, retired, replacement, spec["expected"],
                            spec["rows"], spec["single_deletion"])

    def record_empty_delete(self, name, finding, step, label):
        if not finding["version_deletes"] or finding["version_deletes"][-1] is not step:
            finding["version_deletes"].append(step)
        finding["requests_sent"] = len(finding["version_deletes"])
        finding["all_versions_processed"] = finding["requests_sent"] == len(finding["planned_version_deletes"])
        finding["passed"] = False  # Final logical AND physical verification is still required.
        finding["stopped_at"] = step["target"] if step.get("passed") is False else None
        finding["logical_counts"] = step["counts"]
        finding["failed_checks"] = [k for k, v in step.get("checks", {}).items() if not v]
        self.report["cases"][name] = finding
        self.save()
        self.artifact(label, step)

    def empty_delete_observation(self, retired, replacement, expected, remaining, step):
        step["old_state"] = {z: self.bucket_state(z, retired, strict=True) for z in ZONES}
        step["counts"] = {z: step["old_state"][z].get("counts") for z in ZONES}
        step["new_state"] = ({z: self.bucket_state(z, replacement, strict=True) for z in ZONES}
                             if replacement is not None else {})
        numbered = [r for r in remaining if not r["delete_marker"]]
        step["numbered_payloads"] = self.numbered_payloads(retired, numbered)
        step["new_payloads"] = self.validate_current(replacement, expected) if replacement is not None else {}
        checks = dict(step["checks"])
        target = step["target"]
        for zone in ZONES:
            old = step["old_state"][zone]
            checks[zone + "_target_removed"] = ("rows" in old and not any(
                (r["key"], r["version_id"]) == (target["key"], target["version_id"])
                for r in old["rows"]))
            checks[zone + "_old_versions_match"] = (
                "rows" in old and canonical(old["rows"]) == canonical(remaining))
            checks[zone + "_old_identity_preserved"] = (
                old.get("exists") is True and old.get("admin_returncode") == 0
                and old.get("id") == self.identities[(zone, retired)]["id"])
            checks[zone + "_old_current_list_empty"] = old.get("current_keys") == []
            checks[zone + "_remaining_numbered_preserved"] = (
                len(step["numbered_payloads"][zone]) == len(numbered)
                and all(r.get("matches") is True for r in step["numbered_payloads"][zone]))
            if replacement is not None:
                new = step["new_state"][zone]
                checks[zone + "_new_preserved"] = (
                    new.get("exists") is True and new.get("admin_returncode") == 0
                    and new.get("id") == self.identities[(zone, replacement)]["id"]
                    and new.get("current_keys") == sorted(expected)
                    and len(step["new_payloads"][zone]) == len(expected)
                    and all(r.get("matches") is True for r in step["new_payloads"][zone]))
        step.update(checks=checks, passed=all(checks.values()))

    def empty_final(self, name, retired, replacement, expected):
        deadline = time.monotonic() + self.timeout
        stable, observations = 0, []
        roles = [("old", retired)]
        if replacement is not None:
            roles.append(("new", replacement))
        while True:
            statuses = {role: {z: self.sync_status(z, bucket) for z in ZONES}
                        for role, bucket in roles}
            old = {z: self.bucket_state(z, retired, strict=True) for z in ZONES}
            new = ({z: self.bucket_state(z, replacement, strict=True) for z in ZONES}
                   if replacement is not None else {})
            physical = self.inspect(retired, name + "-final-empty-physical")
            reads = self.validate_current(replacement, expected) if replacement is not None else {}
            versioning, checks = {}, {}
            for zone in ZONES:
                try:
                    versioning[zone] = self.s3[zone].get_bucket_versioning(Bucket=retired)
                except ClientError as error:
                    versioning[zone] = {"error": error.response["Error"]}
                checks[zone + "_old_bucket_preserved"] = (
                    old[zone].get("exists") is True and old[zone].get("admin_returncode") == 0
                    and old[zone].get("id") == self.identities[(zone, retired)]["id"]
                    and versioning[zone].get("Status") == "Suspended")
                checks[zone + "_old_logically_empty"] = (
                    old[zone].get("rows") == [] and old[zone].get("current_keys") == []
                    and old[zone].get("counts") == counts([]))
                raw = physical[zone]
                checks[zone + "_old_raw_objects_removed"] = (
                    raw.get("object_count") == 0 and raw.get("objects") == []
                    and not raw.get("errors") and "error" not in raw
                    and raw.get("returncode", 0) == 0
                    and raw.get("marker") == self.identities[(zone, retired)]["marker"])
                if replacement is not None:
                    checks[zone + "_new_preserved"] = (
                        new[zone].get("exists") is True and new[zone].get("admin_returncode") == 0
                        and new[zone].get("id") == self.identities[(zone, replacement)]["id"]
                        and new[zone].get("current_keys") == sorted(expected)
                        and len(reads[zone]) == len(expected)
                        and all(r.get("matches") is True for r in reads[zone]))
                for role, bucket in roles:
                    checks[role + "_" + zone + "_sync_caught_up"] = stream_caught_up(statuses[role][zone])
            stable = stable + 1 if all(checks.values()) else 0
            observations.append({"observed_at": self.now(), "checks": checks, "sync": statuses,
                                 "logical_counts": {z: old[z].get("counts") for z in ZONES},
                                 "raw_object_counts": {z: physical[z].get("object_count") for z in ZONES},
                                 "consecutive_successes": stable})
            if stable >= 2 or time.monotonic() >= deadline:
                break
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        checks["empty_stable_for_two_observations"] = stable >= 2
        result = {"passed": all(checks.values()), "checks": checks, "observations": observations,
                  "old_state": old, "old_versioning": versioning, "new_state": new,
                  "physical": physical, "new_payloads": reads,
                  "logical_counts": {z: old[z].get("counts") for z in ZONES},
                  "raw_object_counts": {z: physical[z].get("object_count") for z in ZONES}}
        self.artifact(name + "-final-empty", result)
        return result

    def empty_versions(self, name, retired, replacement, expected, old_rows, plain):
        """Empty the retained history only through Zone1 explicit-version DELETEs."""
        self.owned(retired)
        if replacement is not None:
            self.owned(replacement)
        finding = {"old_bucket_after_rename": retired, "replacement_bucket_after_rename": replacement,
                   "old_ids": {z: self.identities[(z, retired)]["id"] for z in ZONES},
                   "new_ids": {z: self.identities[(z, replacement)]["id"] for z in ZONES
                               if replacement is not None},
                   "single_deletion": plain, "operation": "S3 DeleteObject with VersionId",
                   "delete_zone": "zone1", "bulk": False, "bucket_purge_performed": False,
                   "zone2_object_cleanup_performed": False, "raw_cleanup_performed": False,
                   "version_deletes": [], "requests_sent": 0, "passed": None,
                   "note": ("Empty both zones naturally; retain the suspended OLD bucket and protected NEW."
                            if replacement is not None else
                            "Empty both zones naturally; no copy or rename; retain the suspended bucket.")}
        self.report["cases"][name] = finding
        if expected != fixture_current_payloads(self.objects):
            raise RuntimeError("NEW payload oracle differs from the known emptying fixture")
        rows = self.rows("zone1", retired, strict=True)  # One frozen enumeration for the deletion plan.
        if canonical(checked_version_rows(rows)) != canonical(checked_version_rows(old_rows)):
            raise RuntimeError("OLD history changed since the successful ordinary DELETEs")
        numbered = retained_empty_cohort(rows, self.objects)
        targets = ordered_version_deletes(rows)
        finding["planned_version_deletes"] = targets
        for zone in ZONES:
            if self.s3[zone].get_bucket_versioning(Bucket=retired).get("Status") != "Suspended":
                raise RuntimeError("Explicit emptying requires suspended OLD on " + zone)
        baseline = self.numbered_payloads(retired, numbered)
        self.artifact(name + "-numbered-before-emptying", baseline)
        if not all(r.get("matches") is True for records in baseline.values() for r in records):
            raise RuntimeError("Numbered OLD payloads differ before explicit emptying")
        try:
            checkpoint = self.renamed_checkpoint(
                name, retired, replacement, expected, rows, "pre-empty-sync", start_sync=False)
        except SynchronizationGateError as error:
            finding.update(blocked_at="pre-empty-sync", failed_checks=self.report["cases"][name]["failed_checks"])
            self.report["cases"][name] = finding
            self.save()
            raise
        if (checkpoint.get("passed") is not True
                or any(checkpoint["old_state"][z].get("current_keys") != [] for z in ZONES)):
            raise RuntimeError("OLD is not a checkpointed, current-list-empty retained fixture")
        finding.update(pre_empty_checkpoint=checkpoint, passed=False)
        remaining = rows
        for index, target in enumerate(targets):
            after = expected_after_version_delete(remaining, target)  # Reject promotion BEFORE the request.
            label = name + "-version-delete-{:02d}".format(index)
            step = {"target": target, "zone": "zone1", "explicit_version_id": True,
                    "request": {"Bucket": retired, "Key": target["key"], "VersionId": target["version_id"]},
                    "passed": None, "counts": {z: None for z in ZONES}, "checks": {}}
            try:
                response = self.s3["zone1"].delete_object(**step["request"])
                step["response"] = {"http_status": response["ResponseMetadata"]["HTTPStatusCode"],
                                     "delete_marker": response.get("DeleteMarker"),
                                     "version_id": response.get("VersionId")}
            except ClientError as error:
                step.update(error=error.response["Error"], checkpoint_passed=False, checkpoint_skipped=True)
                step["response"] = {"http_status": error.response["ResponseMetadata"]["HTTPStatusCode"],
                                     "delete_marker": None, "version_id": None}
            step["checks"]["explicit_version_delete_response"] = (
                step["response"]["http_status"] == 204
                and step["response"]["version_id"] in (None, target["version_id"])
                and step["response"]["delete_marker"] in (None, target["delete_marker"]))
            # Keep the confirmed receipt even if a later required observation
            # has a transport error and the run is incomplete.
            self.record_empty_delete(name, finding, step, label)
            if "error" not in step:
                remaining = after
                try:
                    checkpoint = self.renamed_checkpoint(
                        name, retired, replacement, expected, remaining,
                        "version-delete-{:02d}-sync".format(index), start_sync=False)
                    step["checkpoint_passed"] = checkpoint["passed"]
                except SynchronizationGateError as error:
                    step.update(checkpoint_passed=False, checkpoint_error=self.lab.redact(str(error)),
                                checkpoint_failed_checks=self.report["cases"][name].get("failed_checks", []))
                    checkpoint = error.evidence
                if checkpoint is not None:
                    step["counts"] = checkpoint["last_observation"]["old_counts"]
            step["checks"]["post_delete_checkpoint"] = step["checkpoint_passed"]
            if not all(step["checks"].values()):
                step["passed"] = False
                self.record_empty_delete(name, finding, step, label)
                try:
                    self.empty_delete_observation(retired, replacement, expected, remaining, step)
                except (BotoCoreError, ClientError, LabError, subprocess.SubprocessError,
                        RuntimeError, KeyError, ValueError, OSError) as error:
                    step["diagnostic_error"] = self.lab.redact(str(error))
                step["passed"] = False
            else:
                self.empty_delete_observation(retired, replacement, expected, remaining, step)
            self.record_empty_delete(name, finding, step, label)
            print("{}: explicit DELETE target={}/{}, counts={}, consistent={}".format(
                name, target["key"], target["version_id"], step["counts"], step["passed"]), flush=True)
            if not step["passed"]:
                return
        final = self.empty_final(name, retired, replacement, expected)
        finding.update(final_empty=final, logical_counts=final["logical_counts"],
                       raw_object_counts=final["raw_object_counts"], passed=final["passed"],
                       failed_checks=[k for k, v in final["checks"].items() if not v])
        self.report["cases"][name] = finding
        self.save()
        print("{}: explicit DELETEs={}, OLD logical={}, OLD raw={}, empty={}".format(
            name, finding["requests_sent"], finding["logical_counts"],
            finding["raw_object_counts"], finding["passed"]), flush=True)

    def boto_delete_populated(self, name, retired, replacement, expected, synchronization):
        """S3 DeleteBucket has no purge option: expect rejection, not cleanup."""
        self.owned(retired)
        before = {z: self.bucket_state(z, retired) for z in ZONES}
        before_raw = self.inspect(retired, name + "-old-before-api-delete-physical")
        operation = {"api": "s3.delete_bucket", "zone": "zone1", "bucket": retired,
                     "succeeded": False}
        try:
            response = self.s3["zone1"].delete_bucket(Bucket=retired)
            operation.update(succeeded=True, http_status=response["ResponseMetadata"]["HTTPStatusCode"])
        except ClientError as error:
            operation.update(error=error.response["Error"],
                             http_status=error.response["ResponseMetadata"]["HTTPStatusCode"])
        self.artifact(name + "-boto-delete", operation)
        after = {z: self.bucket_state(z, retired) for z in ZONES}
        after_raw = self.inspect(retired, name + "-old-after-api-delete-physical")
        new = {z: self.bucket_state(z, replacement) for z in ZONES}
        reads = self.validate_current(replacement, expected)
        checks = {"nonempty_delete_rejected": (
            not operation["succeeded"] and operation.get("error", {}).get("Code") == "BucketNotEmpty"
            and operation.get("http_status") == 409)}
        for zone in ZONES:
            checks[zone + "_old_identity_preserved"] = (
                after[zone].get("exists") is True and after[zone].get("admin_returncode") == 0
                and after[zone].get("id") == self.identities[(zone, retired)]["id"])
            checks[zone + "_old_versions_preserved"] = (
                "rows" in after[zone] and canonical(after[zone]["rows"])
                == canonical(before[zone]["rows"]))
            checks[zone + "_old_raw_objects_preserved"] = (
                "objects" in after_raw[zone] and not after_raw[zone].get("errors")
                and "error" not in after_raw[zone]
                and after_raw[zone]["objects"] == before_raw[zone]["objects"]
                and after_raw[zone].get("sizes") == before_raw[zone].get("sizes"))
            checks[zone + "_replacement_preserved"] = (
                new[zone].get("id") == self.identities[(zone, replacement)]["id"]
                and new[zone].get("current_keys") == sorted(expected)
                and len(reads[zone]) == len(expected)
                and all(r.get("matches") is True for r in reads[zone]))
        self.artifact(name + "-old-after-api-delete-metadata", after)
        self.artifact(name + "-new-after-api-delete", reads)
        self.report["cases"][name] = {
            "old_bucket_after_rename": retired, "replacement_bucket_after_rename": replacement,
            "post_rename_sync": synchronization, "operation": operation,
            "bucket_deletion_succeeded": operation["succeeded"],
            "old_remaining_raw_objects": {z: after_raw[z].get("object_count") for z in ZONES},
            "checks": checks, "failed_checks": [k for k, v in checks.items() if not v],
            "passed": all(checks.values()),
            "note": "Passing means expected BucketNotEmpty rejection; OLD was not deleted."}
        print("{}: boto3 error={}, OLD raw objects={}, rejection test passed={}".format(
            name, operation.get("error", {}).get("Code"),
            self.report["cases"][name]["old_remaining_raw_objects"], all(checks.values())), flush=True)

    def direct_purge(self, name, retired, replacement, expected, synchronization=None, cleanup=None):
        """Purge only on Zone1; an optional cleanup enumerates only on Zone1."""
        self.owned(retired)
        before = self.snapshot(retired)
        before_state = {z: self.bucket_state(z, retired) for z in ZONES}
        before_raw = self.inspect(retired, name + "-old-before-purge-physical")
        new_ids = {z: self.identities[(z, replacement)]["id"] for z in ZONES}
        for zone in ZONES:
            old_id = self.identities[(zone, retired)]["id"]
            if before_state[zone].get("id") != old_id or old_id == new_ids[zone]:
                raise RuntimeError("Unexpected OLD/NEW identity before direct purge on " + zone)
            if self.s3[zone].get_bucket_versioning(
                    Bucket=retired).get("Status") != "Suspended":
                raise RuntimeError("Direct-purge fixture is not suspended on " + zone)
            c = counts(observed_rows(before, zone))
            if cleanup is not None and (c["versions"] != 0 or c["markers"] != 0):
                raise RuntimeError("OLD is not empty after enumeration on " + zone)
            if cleanup is None and not (c["null_data"] > 0 and c["versions"] > c["null_data"]
                                        and c["null_markers"] > 0 and c["markers"] > c["null_markers"]):
                raise RuntimeError("Direct-purge fixture lacks mixed versions/markers on " + zone)
            if ("object_count" not in before_raw[zone] or before_raw[zone].get("errors")
                    or "error" in before_raw[zone]
                    or (cleanup is None and before_raw[zone]["object_count"] <= 0)):
                raise RuntimeError("Could not verify populated OLD storage on " + zone)
        if synchronization is not None and synchronization.get("passed") is not True:
            raise RuntimeError("Refusing purge without a successful post-rename checkpoint")
        if canonical(observed_rows(before, "zone1")) != canonical(
                observed_rows(before, "zone2")):
            raise RuntimeError("OLD version lists differ before direct purge")
        before_reads = self.validate_current(replacement, expected)
        new_before_state = {z: self.bucket_state(z, replacement) for z in ZONES}
        for zone in ZONES:
            new = new_before_state[zone]
            if not (new.get("exists") is True and new.get("admin_returncode") == 0
                    and new.get("id") == new_ids[zone]
                    and new.get("current_keys") == sorted(expected)):
                raise RuntimeError("Replacement identity/listing differs before purge on " + zone)
        if not all(r.get("matches") is True for values in before_reads.values() for r in values):
            raise RuntimeError("Replacement payloads differ before direct purge")
        self.artifact(name + "-old-before-purge", before)
        self.artifact(name + "-old-before-purge-metadata", before_state)
        self.artifact(name + "-new-before-old-purge", before_reads)
        self.artifact(name + "-new-before-old-purge-metadata", new_before_state)

        removal = self.lab.admin("zone1", "bucket", "rm", "--bucket", retired,
                                 "--purge-objects", timeout=max(120, self.timeout), check=False)
        self.artifact(name + "-purge", {
            "zone": "zone1", "bucket": retired, "purge_objects": True,
            "returncode": removal.returncode,
            "stdout": self.lab.redact(removal.stdout), "stderr": self.lab.redact(removal.stderr)})

        started = time.monotonic()
        deadline = started + self.timeout
        stable = 0
        observations = []
        while True:
            old_state = {z: self.bucket_state(z, retired) for z in ZONES}
            physical = self.inspect(retired, name + "-old-after-purge-physical")
            new_state = {z: self.bucket_state(z, replacement) for z in ZONES}
            reads = self.validate_current(replacement, expected)
            checks = direct_purge_checks(removal.returncode, old_state, physical,
                                         new_state, reads, expected, new_ids)
            elapsed = round(time.monotonic() - started, 3)
            observations.append({
                "elapsed_seconds": elapsed, "checks": checks,
                "old_bucket_exists": {z: old_state[z].get("exists") for z in ZONES},
                "old_raw_objects": {z: physical[z].get("object_count") for z in ZONES}})
            stable = stable + 1 if all(checks.values()) else 0
            if stable >= 2 or removal.returncode != 0 or time.monotonic() >= deadline:
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))

        checks["cleanup_stable_for_two_observations"] = stable >= 2
        self.artifact(name + "-purge-observations", observations)
        self.artifact(name + "-old-after-purge-metadata", old_state)
        self.artifact(name + "-new-after-old-purge", reads)
        self.artifact(name + "-new-after-old-purge-metadata", new_state)
        finding = {
            "old_bucket_after_rename": retired, "replacement_bucket_after_rename": replacement,
            "old_ids": {z: self.identities[(z, retired)]["id"] for z in ZONES},
            "new_ids": new_ids, "old_versioning": "Suspended",
            "before_counts": {z: before[z]["counts"] for z in ZONES},
            "purge_zone": "zone1", "purge_returncode": removal.returncode,
            "per_zone_object_cleanup_performed": False,
            "enumerated_object_cleanup_performed": cleanup is not None,
            "manual_sync_performed": synchronization is not None,
            "post_rename_sync": synchronization, "enumerated_cleanup": cleanup,
            "polling_budget_seconds": self.timeout, "observed_seconds": elapsed,
            "consecutive_successful_observations": stable,
            "old_bucket_exists_after_purge": {z: old_state[z].get("exists") for z in ZONES},
            "old_remaining_raw_objects": {z: physical[z].get("object_count") for z in ZONES},
            "old_remaining_zero_size_objects": {z: physical[z].get("zero_size_count") for z in ZONES},
            "new_before_old_purge": before_reads, "new_after_old_purge": reads,
            "checks": checks, "failed_checks": [k for k, v in checks.items() if not v],
            "passed": all(checks.values())}
        self.report["cases"][name] = finding
        print("{}: purge exit={}, OLD raw objects={}, OLD exists={}, passed={}".format(
            name, removal.returncode, finding["old_remaining_raw_objects"],
            finding["old_bucket_exists_after_purge"], finding["passed"]), flush=True)

    def no_rename_setup_checkpoint(self, name, bucket, label):
        """Record the existing fixture's stages without changing its deletes."""
        rows = self.rows("zone1", bucket, strict=True)
        checkpoint = self.renamed_checkpoint(
            name, bucket, None, {}, rows, label, start_sync=False)
        checkpoint["versioning"] = {
            z: self.s3[z].get_bucket_versioning(Bucket=bucket).get("Status") for z in ZONES}
        self.report.setdefault("setup_checkpoints", {})[label] = checkpoint
        self.artifact(name + "-" + label, checkpoint)
        self.save()
        print("{}: {}, versioning={}, counts={}, consistent=True".format(
            name, label, checkpoint["versioning"],
            checkpoint["last_observation"]["old_counts"]), flush=True)

    def migrate(self, name):
        synchronized = name in SYNC_CASES
        direct = name == "migration-direct-purge" or synchronized
        no_copy_rename = getattr(self, "no_copy_rename", False)
        if no_copy_rename and name != EMPTY_CASE:
            raise ValueError("No-copy/no-rename is supported only for migration-synced-empty")
        if direct and self.backlog:
            raise RuntimeError("Direct purge requires checkpointed rename, not --rename-with-backlog")
        old = self.prepare(name, versioned=True)
        if no_copy_rename:
            self.no_rename_setup_checkpoint(name, old, "create-versioned-bucket-with-seed")
        new = None if no_copy_rename else self.prepare(name + "-new")
        for i in range(self.objects):
            key = "history{:04d}".format(i)
            self.s3["zone1"].put_object(Bucket=old, Key=key, Body=PAYLOAD)
            self.s3["zone1"].put_object(Bucket=old, Key=key, Body=PAYLOAD + b"newer")
            key = "marker{:04d}".format(i)
            version = self.s3["zone1"].put_object(Bucket=old, Key=key, Body=PAYLOAD)["VersionId"]
            self.s3["zone1"].delete_object(Bucket=old, Key=key)
            if not direct:
                self.s3["zone1"].delete_object(Bucket=old, Key=key, VersionId=version)
        self.matching(old, name + "-enabled-parity-failure" if direct else None)
        if no_copy_rename:
            self.no_rename_setup_checkpoint(name, old, "create-numbered-versions")
        self.write_nulls(old, after_suspend=(
            lambda: self.no_rename_setup_checkpoint(name, old, "suspend-versioning"))
            if no_copy_rename else None)
        # Existing numbered history plus a new co-located null version is the
        # source state relevant to copying OLH attributes into a plain bucket.
        for i in range(self.objects):
            self.s3["zone1"].put_object(Bucket=old, Key="history{:04d}".format(i),
                                       Body=PAYLOAD + b"null-overwrite")
            if direct:
                # Keep this cohort free of numbered history: null markers over
                # numbered history have a separate replication mismatch. The
                # history/marker cohorts still exercise populated versioned keys.
                key = "nullmarker{:04d}".format(i)
                self.s3["zone1"].put_object(Bucket=old, Key=key, Body=PAYLOAD)
                self.s3["zone1"].delete_object(Bucket=old, Key=key)
        self.matching(old, name + "-suspended-parity-failure" if direct else None)
        if no_copy_rename:
            self.s3["zone1"].put_object(Bucket=old, Key="final", Body=PAYLOAD + b"final")
            self.no_rename_setup_checkpoint(name, old, "create-null-versions")
            self.synchronized_delete(name, old, None, fixture_current_payloads(self.objects),
                                     self.rows("zone1", old, strict=True))
            self.report["cases"][name].update(
                bucket=old, copy_performed=False, rename_performed=False,
                manual_sync_performed=False)
            return
        reap = []
        reap_errors = []
        confirmed_reaped = 0
        if name == "migration-reap":
            reap = marker_only(self.rows("zone1", old))
            reap_errors = self.delete_rows("zone1", old, reap, bulk=True)
            remaining = {(r["key"], r["version_id"]) for r in self.rows("zone1", old)
                         if r["delete_marker"]}
            confirmed_reaped = sum((r["key"], r["version_id"]) not in remaining for r in reap)
            self.drain(old, required=False)
        copied = self.copy_current(old, new)
        if direct:
            # Use the writes, not the source listing, as the data oracle.
            expected = {"seed": PAYLOAD}
            expected.update({"data{:04d}".format(i): PAYLOAD for i in range(self.objects)})
            expected.update({"history{:04d}".format(i): PAYLOAD + b"null-overwrite"
                             for i in range(self.objects)})
            if sorted(copied) != sorted(expected):
                raise RuntimeError("OLD current listing differs from direct-purge fixture writes")
        else:
            expected = {key: self.get("zone1", old, key) for key in copied}
        self.drain(new, required=direct)
        copy_heads = self.inspect(new, name + "-copied-new-heads")
        before_rename_copy = self.validate_current(new, expected)
        self.artifact(name + "-new-before-rename", before_rename_copy)
        before = self.snapshot(old)
        self.artifact(name + "-old-before-rename", before)
        saved = {z: self.identities[(z, old)] for z in ZONES}
        saved_new = {z: self.identities[(z, new)] for z in ZONES}
        temporary = old + "-tmp"
        secondary_stopped = False
        try:
            if self.backlog:
                # Synthetic timing control; not a claim the customer stopped
                # Zone2. Only this lab's PID-validated RGW is paused.
                self.lab._pid("zone2", action="stop")
                secondary_stopped = True
            self.s3["zone1"].put_object(Bucket=old, Key="final", Body=PAYLOAD + b"final")
            expected["final"] = PAYLOAD + b"final"
            self.copy_current(old, new)  # final pass; no more workload writes
            if not self.backlog:
                self.drain(old)
                self.drain(new, required=direct)
            final_old_rows = self.rows("zone1", old) if synchronized else None
            self.lab.admin("zone1", "bucket", "link", "--bucket", old,
                           "--bucket-new-name", temporary, "--uid", self.lab.config["user"]["uid"])
            self.lab.admin("zone1", "bucket", "link", "--bucket", new,
                           "--bucket-new-name", old, "--uid", self.lab.config["user"]["uid"])
        finally:
            if secondary_stopped:
                self.lab.restart_gateway("zone2", 120)
        for zone in ZONES:
            def names_match(z=zone):
                a = self.lab.admin(z, "bucket", "stats", "--bucket", temporary, check=False, timeout=15)
                b = self.lab.admin(z, "bucket", "stats", "--bucket", old, check=False, timeout=15)
                return (a.returncode == b.returncode == 0
                        and json.loads(a.stdout)["id"] == saved[z]["id"]
                        and json.loads(b.stdout)["id"] == saved_new[z]["id"])
            self.wait(zone + " renamed bucket identities", names_match)
            self.identities[(zone, temporary)] = saved[zone]
            self.identities[(zone, old)] = saved_new[zone]
        if direct:
            if synchronized:
                self.synchronized_delete(name, temporary, old, expected, final_old_rows)
                return
            # Unlike the original migration cases, go straight to one Zone1
            # purge: no delete_rows(), Zone2 purge, or manual bucket sync.
            self.direct_purge(name, temporary, old, expected)
            return
        after = self.snapshot(temporary)
        self.artifact(name + "-old-after-rename", after)
        manual = self.lab.admin("zone2", "bucket", "sync", "run", "--bucket", temporary,
                                "--source-zone", "zone1", timeout=max(120, self.timeout), check=False)
        self.artifact(name + "-manual-sync", {"returncode": manual.returncode,
                      "stdout": self.lab.redact(manual.stdout), "stderr": self.lab.redact(manual.stderr)})
        after_manual = self.snapshot(temporary)
        self.artifact(name + "-old-after-manual-sync", after_manual)
        matched_before = canonical(observed_rows(after, "zone1")) == canonical(
            observed_rows(after, "zone2"))
        matched_after = canonical(observed_rows(after_manual, "zone1")) == canonical(
            observed_rows(after_manual, "zone2"))
        copy_results = self.validate_current(old, expected)
        self.artifact(name + "-new-before-old-purge", copy_results)
        cleanup = {}
        for zone in ZONES:
            cleanup[zone] = self.delete_rows(zone, temporary, self.rows(zone, temporary))
            self.drain(temporary, "zone1" if zone == "zone2" else "zone2", required=False)
        self.artifact(name + "-old-before-purge", self.snapshot(temporary))
        removal = self.lab.admin("zone1", "bucket", "rm", "--bucket", temporary,
                                 "--purge-objects", timeout=max(120, self.timeout), check=False)
        self.artifact(name + "-purge", {"returncode": removal.returncode,
                      "stdout": self.lab.redact(removal.stdout), "stderr": self.lab.redact(removal.stderr)})
        time.sleep(3)
        physical = self.inspect(temporary, name + "-old-after-purge-physical")
        after_purge = self.validate_current(old, expected)
        self.artifact(name + "-new-after-old-purge", after_purge)
        self.report["cases"][name] = {
            "old_bucket_after_rename": temporary, "replacement_bucket_after_rename": old,
            "old_ids": {z: saved[z]["id"] for z in ZONES},
            "new_ids": {z: saved_new[z]["id"] for z in ZONES},
            "copied_current_objects": len(copied), "marker_reap_attempted": len(reap),
            "reaped_marker_only_versions": confirmed_reaped, "marker_reap_errors": reap_errors,
            "new_copy_olh_samples": {z: sum(any(a.startswith("user.rgw.olh.")
                for a in sample.get("attrs", {})) for sample in copy_heads[z].get("samples", []))
                for z in ZONES},
            "new_before_old_purge": copy_results, "new_after_old_purge": after_purge,
            "new_before_rename": before_rename_copy,
            "manual_sync_returncode": manual.returncode,
            "old_lists_match_before_manual": matched_before,
            "old_lists_match_after_manual": matched_after,
            "old_sync_before_manual": after["zone2"].get("sync"),
            "old_sync_after_manual": after_manual["zone2"].get("sync"),
            "cleanup_errors": cleanup, "purge_returncode": removal.returncode,
            "old_remaining_raw_objects": {z: physical[z].get("object_count") for z in ZONES},
            "old_remaining_zero_size_objects": {z: physical[z].get("zero_size_count") for z in ZONES}}
        print("{}: manual sync exit={}, purge exit={}, remaining raw heads={}".format(
            name, manual.returncode, removal.returncode,
            self.report["cases"][name]["old_remaining_raw_objects"]), flush=True)

    def run(self, selected):
        resume = getattr(self, "empty_after_run", None)
        if resume is not None:
            if tuple(selected) != (EMPTY_CASE,):
                raise ValueError("A retained-run continuation can execute only migration-synced-empty")
            path = resume_report_path(self.lab.config["artifact_dir"], resume)
            if self.prefix == resume or self.output.resolve() == path.parent:
                # Outside the report-writing finally block: never overwrite the source report.
                raise ValueError("Resume requires a different fresh output run")
        try:
            for name in selected:
                print("Running " + name, flush=True)
                if resume is not None:
                    self.resume_empty(resume)
                elif name == "pending-head":
                    self.pending_head()
                elif name == "null-numbered":
                    self.null_numbered()
                elif name.startswith("migration-"):
                    self.migrate(name)
                else:
                    self.deletion_case(name)
                self.save()
            self.report["completed"] = True
            self.report["regression_failures"] = [
                name for name, result in self.report["cases"].items()
                if result.get("passed") is False]
            return 1 if self.report["regression_failures"] else 0
        except (BotoCoreError, ClientError, LabError, subprocess.SubprocessError,
                RuntimeError, KeyError, ValueError) as error:
            self.report["completed"] = False
            self.report["infrastructure_error"] = self.lab.redact(str(error))
            print("Exercise incomplete: " + self.lab.redact(str(error)), file=sys.stderr)
            return 2
        finally:
            self.report["finished_at"] = self.now()
            self.save()
            print("Report: " + str(self.output / "report.json"), flush=True)
            try:
                self.lab.collect_logs(self.output / "logs")
            except (LabError, subprocess.SubprocessError, OSError) as error:
                self.artifact("log-collection-error", {"error": self.lab.redact(str(error))})


def self_test():
    import contextlib
    import io
    from unittest import mock

    sample = {"Versions": [{"Key": "a", "VersionId": "null", "Size": 7, "IsLatest": True}],
              "DeleteMarkers": [{"Key": "b", "VersionId": "null", "IsLatest": True}]}
    rows = version_rows(sample)
    assert counts(rows) == {"versions": 1, "null_data": 1, "markers": 1, "null_markers": 1}
    assert marker_only(rows) == [rows[1]]
    assert canonical(rows) == canonical(list(reversed(rows)))
    assert not marker_only(rows + [{"key": "b", "version_id": "v1", "delete_marker": False,
                                   "is_latest": False, "size": 1}])
    compile(INSPECT, "<RADOS inspection helper>", "exec")
    assert '"key": "default-placement"' in redact('{"key": "default-placement"}')
    assert "key=data0000" in redact("sync key=data0000")
    assert "secret-value" not in redact("secret key: secret-value")
    try:
        observed_rows({"zone1": {"error": "NoSuchBucket"}}, "zone1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Failed observations must not become empty version lists")

    old = {z: {"exists": False, "admin_returncode": 2} for z in ZONES}
    raw = {z: {"object_count": 0, "errors": []} for z in ZONES}
    new = {z: {"exists": True, "admin_returncode": 0, "id": "new-id",
               "current_keys": ["a"]} for z in ZONES}
    reads = {z: [{"key": "a", "matches": True}] for z in ZONES}
    ids = {z: "new-id" for z in ZONES}

    def checks():
        return direct_purge_checks(0, old, raw, new, reads, ["a"], ids)

    assert all(checks().values())
    raw["zone2"]["object_count"] = 3
    assert not checks()["zone2_old_raw_objects_removed"]
    raw["zone2"] = {"error": "inventory failed"}
    assert not checks()["zone2_old_raw_objects_removed"]
    raw["zone2"] = {"object_count": 0, "errors": [{"error": "stat failed"}]}
    assert not checks()["zone2_old_raw_objects_removed"]
    raw["zone2"] = {"object_count": 0, "errors": []}
    old["zone2"]["exists"] = None
    assert not checks()["zone2_old_bucket_removed"]
    old["zone2"]["exists"] = False
    old["zone2"]["admin_returncode"] = None
    assert not checks()["zone2_old_bucket_removed"]
    old["zone2"]["admin_returncode"] = 2
    new["zone2"]["id"] = "wrong-id"
    assert not checks()["zone2_replacement_identity_preserved"]
    new["zone2"]["id"] = "new-id"
    new["zone2"]["current_keys"] = ["a", "unexpected"]
    assert not checks()["zone2_replacement_listing_preserved"]
    new["zone2"]["current_keys"] = ["a"]
    reads["zone2"][0]["matches"] = False
    assert not checks()["zone2_replacement_payloads_preserved"]
    assert not direct_purge_checks(1, old, raw, new, reads, ["a"], ids)["purge_command_succeeded"]
    assert select_cases(None, False) == CASES
    assert select_cases(None, True) == CASES[:8]
    assert select_cases(["migration-keep"], True) == ("migration-keep",)
    try:
        select_cases(["migration-direct-purge"], True)
    except ValueError:
        pass
    else:
        raise AssertionError("Incompatible selections must fail before mutating the lab")
    caught = "incremental sync on 11 shards\nbucket is caught up with source\n"
    assert stream_caught_up({"returncode": 0, "stdout": caught})
    assert not stream_caught_up({"returncode": 0,
                                "stdout": "incremental sync on 11 shards\nbucket is behind on 8 shards\n"})
    assert not stream_caught_up({"returncode": 1, "stdout": caught})
    assert not stream_caught_up({"returncode": 0,
                                "stdout": "full sync; bucket is caught up with source"})
    assert not stream_caught_up({"returncode": 0,
                                "stdout": "full sync: 8/8 shards\nincremental sync: 0/8 shards\n"
                                          "bucket is caught up with source\n"})
    assert not stream_caught_up({"returncode": 0, "stdout": "full sync on 1 shard\n" + caught})
    assert not stream_caught_up({"returncode": 0, "stdout": "unknown status\n"})
    fake = object.__new__(Exercise)
    fake.prefix = "self-test"
    fake.identities = {(z, b): {"id": "aliased-id"} for z in ZONES
                       for b in ("self-test-old", "self-test-new")}
    try:
        fake.renamed_checkpoint("self-test", "self-test-old", "self-test-new", {}, [],
                                "checkpoint", start_sync=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Aliased identities must fail before synchronization or deletion")
    for name in SYNC_CASES:
        assert select_cases([name], False) == (name,)
        try:
            select_cases([name], True)
        except ValueError:
            pass
        else:
            raise AssertionError("Synchronized deletion must not run with a synthetic backlog")
    initial = [{"key": "a", "version_id": "v1", "delete_marker": False,
                "is_latest": False, "size": 7},
               {"key": "a", "version_id": "null", "delete_marker": False,
                "is_latest": True, "size": 9},
               {"key": "b", "version_id": "v2", "delete_marker": False,
                "is_latest": True, "size": 11}]
    deleted = expected_after_plain_delete(initial, "a")
    assert counts(deleted) == {"versions": 2, "null_data": 0, "markers": 1, "null_markers": 1}
    assert initial[1]["is_latest"] and not initial[1]["delete_marker"]
    assert deleted[1] == initial[2]
    assert canonical(expected_after_plain_delete(deleted, "a")) == canonical(deleted)
    deleted = expected_after_plain_delete(deleted, "b")
    assert all(not r["is_latest"] for r in deleted if not r["delete_marker"])

    numbered_sample = [
        {"key": "seed", "version_id": "seed-v", "delete_marker": False,
         "is_latest": True, "size": 4480},
        {"key": "marker0000", "version_id": "marker-v", "delete_marker": False,
         "is_latest": False, "size": 4480},
        {"key": "history0000", "version_id": "history-old", "delete_marker": False,
         "is_latest": False, "size": 4480},
        {"key": "history0000", "version_id": "history-new", "delete_marker": False,
         "is_latest": False, "size": 4485}]
    fixture = numbered_sample + [
        {"key": "data0000", "version_id": "null", "delete_marker": False,
         "is_latest": True, "size": len(PAYLOAD)},
        {"key": "history0000", "version_id": "null", "delete_marker": False,
         "is_latest": True, "size": len(PAYLOAD + b"null-overwrite")},
        {"key": "final", "version_id": "null", "delete_marker": False,
         "is_latest": True, "size": len(PAYLOAD + b"final")},
        {"key": "marker0000", "version_id": "marker-dm", "delete_marker": True,
         "is_latest": True, "size": 0},
        {"key": "nullmarker0000", "version_id": "null", "delete_marker": True,
         "is_latest": True, "size": 0}]
    assert numbered_cohort(fixture, 1) == numbered_sample
    three_cohorts = [dict(numbered_sample[0])]
    for i in range(3):
        for row in numbered_sample[1:]:
            entry = dict(row)
            entry["key"] = row["key"][:-4] + "{:04d}".format(i)
            entry["version_id"] += "-{}".format(i)
            three_cohorts.append(entry)
    assert len(numbered_cohort(three_cohorts, 3)) == 10
    invalid_cohorts = [[], numbered_sample[:-1],
                       numbered_sample + [dict(numbered_sample[0])]]
    for index, field, value in ((3, "version_id", "history-old"),
                                (0, "version_id", "marker-v"),
                                (2, "version_id", ""),
                                (1, "key", "unknown"),
                                (3, "size", 4480),
                                (0, "size", 4485)):
        invalid = [dict(row) for row in numbered_sample]
        invalid[index][field] = value
        invalid_cohorts.append(invalid)
    for invalid in invalid_cohorts:
        try:
            numbered_cohort(invalid, 1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Incomplete, duplicated, or unknown numbered cohort was accepted")

    # Every external operation is replaced by an in-memory fixture. Exercise
    # run() too, so measured failures and unavailable observations retain their
    # distinct exit codes without using Lab.load(), SDK clients, or clusters.
    def offline_single_delete(checkpoint_failure=False, observation_error=False,
                              corrupt_remaining=False, request_error=False, missing_numbered=False):
        fake = object.__new__(Exercise)
        fake.prefix = "self-test"
        fake.objects = 1
        fake.output = Path("self-test-output")
        fake.report = {"cases": {}, "completed": False}
        retired, replacement = "self-test-old", "self-test-new"
        name = "migration-synced-single-delete"
        expected = {"seed": PAYLOAD, "data0000": PAYLOAD,
                    "history0000": PAYLOAD + b"null-overwrite", "final": PAYLOAD + b"final"}
        rows = [dict(row) for row in fixture
                if not missing_numbered or row["version_id"] != "history-old"]
        state = {"requests": [], "deleted": {z: set() for z in ZONES},
                 "rows": {z: list(rows) for z in ZONES}, "saved": [],
                 "persisted_before_diagnostics": False}
        fake.identities = {(z, b): {"id": role, "marker": role + "-marker"}
                           for z in ZONES for b, role in ((retired, "old"), (replacement, "new"))}

        class OfflineLab:
            def redact(self, text):
                return str(text)

            def collect_logs(self, output):
                pass

        class OfflineS3:
            def delete_object(self, Bucket, Key):
                assert Bucket == retired
                state["requests"].append(Key)
                if request_error:
                    raise ClientError({"Error": {"Code": "InternalError", "Message": "offline request failed"},
                                       "ResponseMetadata": {"HTTPStatusCode": 500}}, "DeleteObject")
                state["deleted"]["zone1"].add(Key)
                return {"ResponseMetadata": {"HTTPStatusCode": 204},
                        "DeleteMarker": True, "VersionId": "null"}

        fake.lab = OfflineLab()
        fake.s3 = {"zone1": OfflineS3()}
        fake.save = lambda: state["saved"].append(json.loads(json.dumps(fake.report)))
        fake.artifact = lambda label, value: None
        numbered_bodies = {("seed", "seed-v"): PAYLOAD, ("marker0000", "marker-v"): PAYLOAD,
                           ("history0000", "history-old"): PAYLOAD,
                           ("history0000", "history-new"): PAYLOAD + b"newer"}

        def get(zone, bucket, key, version=None):
            if bucket == replacement:
                return expected[key]
            assert bucket == retired
            if version is not None:
                return numbered_bodies[(key, version)]
            if key in state["deleted"][zone]:
                raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "offline key missing"},
                                   "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
            if corrupt_remaining and state["requests"] and zone == "zone2" and key == "final":
                return b"x" * len(expected[key])
            return expected[key]

        def bucket_state(zone, bucket):
            observed = state["rows"][zone] if bucket == retired else [
                {"key": key, "version_id": "null", "delete_marker": False,
                 "is_latest": True, "size": len(payload)} for key, payload in expected.items()]
            return {"exists": True, "admin_returncode": 0,
                    "id": fake.identities[(zone, bucket)]["id"],
                    "rows": observed, "counts": counts(observed),
                    "current_keys": sorted(r["key"] for r in observed
                                           if r["is_latest"] and not r["delete_marker"])}

        def checkpoint(case, old, new, payloads, expected_rows, label, start_sync):
            assert (old, new) == (retired, replacement) and not start_sync
            state["rows"]["zone1"] = expected_rows
            if not checkpoint_failure:
                state["rows"]["zone2"] = expected_rows
                state["deleted"]["zone2"] = set(state["deleted"]["zone1"])
            old_state = {z: bucket_state(z, retired) for z in ZONES}
            evidence = {"passed": not checkpoint_failure, "old_state": old_state,
                        "last_observation": {"old_counts": {z: old_state[z]["counts"] for z in ZONES}}}
            if checkpoint_failure:
                fake.report["cases"][case] = {"passed": None, "failed_checks": ["old_zone2_versions_match"]}
                fake.save()
                raise SynchronizationGateError("offline measured mismatch", evidence)
            return evidence

        def inspect(bucket, label):
            assert bucket == retired
            if observation_error:
                if checkpoint_failure:
                    saved = state["saved"][-1]["cases"][name]
                    state["persisted_before_diagnostics"] = (
                        saved["passed"] is False and saved["requests_sent"] == 1
                        and saved["stopped_at"] == state["requests"][-1]
                        and saved["deletes"][-1]["checkpoint_passed"] is False)
                raise BotoCoreError()
            physical = {}
            for zone in ZONES:
                sizes = {"old-marker_" + key: (0 if key in state["deleted"][zone] else len(payload))
                         for key, payload in expected.items()}
                physical[zone] = {"objects": sorted(sizes), "sizes": sizes, "errors": []}
            return physical

        fake.get = get
        fake.bucket_state = bucket_state
        fake.renamed_checkpoint = checkpoint
        fake.inspect = inspect
        synchronization = {"passed": True, "old_state": {z: bucket_state(z, retired) for z in ZONES}}
        fake.migrate = lambda case: fake.single_deletes(
            case, retired, replacement, expected, rows, synchronization)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = fake.run([name])
        return result, fake.report, state

    name = "migration-synced-single-delete"
    result, report, state = offline_single_delete()
    finding = report["cases"][name]
    assert result == 0 and report["completed"] and finding["passed"]
    assert state["requests"] == ["data0000", "final", "history0000", "seed"]
    assert finding["requests_sent"] == 4 and finding["stopped_at"] is None
    assert [len(d["remaining_old_payload_oracle"]) for d in finding["deletes"]] == [3, 2, 1, 0]
    assert all(d["checks"][z + "_old_current_payloads_preserved"]
               for d in finding["deletes"] for z in ZONES)

    result, report, state = offline_single_delete(corrupt_remaining=True)
    finding = report["cases"][name]
    assert result == 1 and report["completed"] and not finding["passed"]
    assert state["requests"] == ["data0000"] and finding["stopped_at"] == "data0000"
    assert not finding["deletes"][0]["checks"]["zone2_old_current_payloads_preserved"]
    assert finding["deletes"][0]["remaining_old_payload_oracle"]["final"]["sha256"] == hashlib.sha256(
        PAYLOAD + b"final").hexdigest()

    result, report, state = offline_single_delete(checkpoint_failure=True, observation_error=True)
    finding = report["cases"][name]
    assert result == 1 and report["completed"] and report["regression_failures"] == [name]
    assert "infrastructure_error" not in report and state["persisted_before_diagnostics"]
    assert finding["requests_sent"] == 1 and finding["stopped_at"] == "data0000"
    assert not finding["deletes"][0]["checkpoint_passed"]
    assert "diagnostic_error" in finding["deletes"][0]
    assert finding["counts"] == finding["deletes"][0]["checkpoint_counts"]

    result, report, state = offline_single_delete(observation_error=True)
    assert result == 2 and not report["completed"] and "infrastructure_error" in report
    assert state["requests"] == ["data0000"]

    result, report, state = offline_single_delete(missing_numbered=True)
    assert result == 2 and not report["completed"] and not state["requests"]

    result, report, state = offline_single_delete(request_error=True)
    finding = report["cases"][name]
    assert result == 1 and report["completed"] and finding["requests_sent"] == 1
    assert finding["stopped_at"] == "data0000" and finding["note"]
    assert finding["deletes"][0]["error"]["Code"] == "InternalError"
    assert finding["counts"] == {z: counts(fixture) for z in ZONES}

    # Build a real successful offline single-delete report, then continue that
    # snapshot with a separate output run and explicit-version SDK stubs.
    _, prior, _ = offline_single_delete()
    retained_run = "rgwlab-20261009-132521-57c7"
    endpoints = {"zone1": "offline-zone1", "zone2": "offline-zone2"}
    prior.update(run=retained_run, endpoints=endpoints)
    retained_case = prior["cases"][SINGLE_CASE]
    retired = retained_run + "-" + SINGLE_CASE + "-tmp"
    replacement = retained_run + "-" + SINGLE_CASE
    retained_case.update(old_bucket_after_rename=retired, replacement_bucket_after_rename=replacement)
    for zone in ZONES:
        retained_case["deletes"][-1]["physical"][zone]["marker"] = "old-marker"
    spec = resume_empty_spec(prior, retained_run, endpoints, 1)
    retained = spec["rows"]
    assert counts(retained) == {"versions": 4, "null_data": 0, "markers": 6, "null_markers": 5}
    plan = ordered_version_deletes(retained)
    assert len(plan) == 10 and all(not r["delete_marker"] for r in plan[:4])
    remaining = retained
    for target in plan:
        flags = {(r["key"], r["version_id"]): r["is_latest"] for r in remaining}
        after = expected_after_version_delete(remaining, target)
        assert len(after) == len(remaining) - 1
        assert all(r["is_latest"] == flags[(r["key"], r["version_id"])] for r in after)
        remaining = after
    assert remaining == [] and canonical(retained) == canonical(spec["rows"])
    full_retained = [dict(row, is_latest=False) for row in three_cohorts]
    for key in set(fixture_current_payloads(3)) | {"nullmarker{:04d}".format(i) for i in range(3)}:
        full_retained.append({"key": key, "version_id": "null", "delete_marker": True,
                              "is_latest": True, "size": 0})
    for i in range(3):
        full_retained.append({"key": "marker{:04d}".format(i), "version_id": "marker-dm-{}".format(i),
                              "delete_marker": True, "is_latest": True, "size": 0})
    assert len(retained_empty_cohort(full_retained, 3)) == 10
    full_plan = ordered_version_deletes(full_retained)
    assert len(full_plan) == 24 and all(not r["delete_marker"] for r in full_plan[:10])
    remaining = full_retained
    for target in full_plan:
        remaining = expected_after_version_delete(remaining, target)
    assert remaining == []

    def rejected(call):
        try:
            call()
        except (RuntimeError, ValueError):
            return
        raise AssertionError("Unsafe resume or explicit-version plan was accepted")

    history_marker = next(r for r in retained if r["key"] == "history0000" and r["is_latest"])
    rejected(lambda: expected_after_version_delete(retained, history_marker))
    rejected(lambda: expected_after_version_delete(retained, dict(plan[0], version_id="missing")))
    rejected(lambda: ordered_version_deletes(retained + [dict(retained[0])]))
    partial = [dict(r) for r in retained]
    del partial[0]["version_id"]
    rejected(lambda: ordered_version_deletes(partial))
    rejected(lambda: retained_empty_cohort(retained[:-1], 1))
    rejected(lambda: version_rows({"Versions": [{"Key": "partial", "Size": 1, "IsLatest": True}]}, strict=True))
    artificial = [{"key": "k", "version_id": "data", "delete_marker": False,
                   "is_latest": False, "size": 1},
                  {"key": "k", "version_id": "older-dm", "delete_marker": True,
                   "is_latest": False, "size": 0},
                  {"key": "k", "version_id": "null", "delete_marker": True,
                   "is_latest": True, "size": 0}]
    assert [r["version_id"] for r in ordered_version_deletes(artificial)] == ["data", "older-dm", "null"]
    assert select_cases(None, False, retained_run) == (EMPTY_CASE,)
    assert select_cases([EMPTY_CASE], False, retained_run) == (EMPTY_CASE,)
    rejected(lambda: select_cases([SINGLE_CASE], False, retained_run))
    rejected(lambda: select_cases([EMPTY_CASE], True, retained_run))
    for invalid_run in ("../../report", "production-bucket", retained_run + "/report.json", "rgwlab-1"):
        rejected(lambda value=invalid_run: resume_report_path("/offline-artifacts", value))
    expected_path = Path("/offline-artifacts/runs") / retained_run / "report.json"
    assert resume_report_path("/offline-artifacts", retained_run) == expected_path
    with mock.patch.object(Path, "resolve", autospec=True, side_effect=lambda path:
                           Path("/outside/report.json") if path.name == "report.json" else path):
        rejected(lambda: resume_report_path("/offline-artifacts", retained_run))
    collision = object.__new__(Exercise)
    collision.prefix, collision.empty_after_run = retained_run, retained_run
    collision.output = expected_path.parent

    class OfflineConfig:
        config = {"artifact_dir": "/offline-artifacts"}

    def reject_source_write():
        raise AssertionError("Source report must not be written")

    collision.lab = OfflineConfig()
    collision.save = reject_source_write
    rejected(lambda: collision.run([EMPTY_CASE]))

    for field, value in (("completed", False), ("endpoints", {"zone1": "wrong"})):
        bad = json.loads(json.dumps(prior))
        bad[field] = value
        rejected(lambda value=bad: resume_empty_spec(value, retained_run, endpoints, 1))
    for field, value in (("passed", False), ("all_keys_processed", False),
                         ("old_bucket_after_rename", "production-bucket"),
                         ("new_ids", {z: "old" for z in ZONES})):
        bad = json.loads(json.dumps(prior))
        bad["cases"][SINGLE_CASE][field] = value
        rejected(lambda value=bad: resume_empty_spec(value, retained_run, endpoints, 1))
    rejected(lambda: resume_empty_spec(prior, retained_run, endpoints, 3))

    def offline_empty(source=None, bad_actual_id=False, checkpoint_failure=False,
                      diagnostic_failure=False, transport_failure=False, raw_leak=False):
        fake = object.__new__(Exercise)
        fake.prefix = "rgwlab-20261009-140000-abcd"
        fake.output = Path("/offline-artifacts/runs") / fake.prefix
        fake.objects, fake.timeout = 1, 1
        fake.empty_after_run = retained_run
        fake.report = {"run": fake.prefix, "cases": {}, "completed": False}
        fake.identities = {}
        state = {"requests": [], "admin": [], "saved": [], "enumerations": 0,
                 "rows": {z: [dict(r) for r in retained] for z in ZONES},
                 "persisted_before_diagnostics": False}
        expected = fixture_current_payloads(1)

        def actual_id(zone, bucket):
            return "wrong-id" if bad_actual_id and zone == "zone2" and bucket == replacement else (
                "old" if bucket == retired else "new")

        class OfflineLab:
            config = {"artifact_dir": "/offline-artifacts", "endpoints": endpoints}

            def redact(self, text):
                return str(text)

            def collect_logs(self, output):
                pass

            def admin(self, zone, *args, **kwargs):
                assert args[:2] == ("bucket", "stats")
                bucket = args[args.index("--bucket") + 1]
                assert bucket in (retired, replacement)
                state["admin"].append((zone, bucket))
                identity = {"id": actual_id(zone, bucket),
                            "marker": "old-marker" if bucket == retired else "new-marker"}
                return subprocess.CompletedProcess([], 0, json.dumps(identity), "")

        class OfflineS3:
            def __init__(self, zone):
                self.zone = zone

            def get_bucket_versioning(self, Bucket):
                assert Bucket == retired
                return {"Status": "Suspended"}

            def delete_object(self, Bucket, Key, VersionId):
                assert self.zone == "zone1" and Bucket == retired and VersionId
                assert len(state["admin"]) == 4 and set(fake.resumed_buckets) == {retired, replacement}
                state["requests"].append((Key, VersionId))
                if transport_failure:
                    raise BotoCoreError()
                matches = [r for r in state["rows"]["zone1"]
                           if (r["key"], r["version_id"]) == (Key, VersionId)]
                assert len(matches) == 1
                target = matches[0]
                state["rows"]["zone1"] = [r for r in state["rows"]["zone1"]
                                          if (r["key"], r["version_id"]) != (Key, VersionId)]
                return {"ResponseMetadata": {"HTTPStatusCode": 204},
                        "VersionId": VersionId, "DeleteMarker": target["delete_marker"]}

        fake.lab = OfflineLab()
        fake.s3 = {z: OfflineS3(z) for z in ZONES}
        fake.save = lambda: state["saved"].append(json.loads(json.dumps(fake.report)))
        fake.artifact = lambda label, value: None

        def rows(zone, bucket, prefix=None, strict=False):
            assert bucket == retired and strict
            state["enumerations"] += 1
            return [dict(r) for r in state["rows"][zone]]

        def bucket_state(zone, bucket, strict=False):
            if diagnostic_failure and state["requests"]:
                saved = state["saved"][-1]["cases"][EMPTY_CASE]
                state["persisted_before_diagnostics"] = (
                    saved["passed"] is False and saved["requests_sent"] == 1
                    and saved["version_deletes"][-1]["passed"] is False)
                raise BotoCoreError()
            observed = state["rows"][zone] if bucket == retired else [
                {"key": k, "version_id": "null", "delete_marker": False,
                 "is_latest": True, "size": len(v)} for k, v in expected.items()]
            if strict:
                checked_version_rows(observed)
            return {"exists": True, "admin_returncode": 0, "id": actual_id(zone, bucket),
                    "rows": observed, "counts": counts(observed),
                    "current_keys": sorted(r["key"] for r in observed
                                           if r["is_latest"] and not r["delete_marker"])}

        def get(zone, bucket, key, version=None):
            if bucket == replacement:
                return expected[key]
            matches = [r for r in state["rows"][zone]
                       if (r["key"], r["version_id"]) == (key, version) and not r["delete_marker"]]
            assert len(matches) == 1
            return PAYLOAD + (b"newer" if key.startswith("history") and matches[0]["size"] == 4485 else b"")

        def checkpoint(case, old, new, payloads, expected_rows, label, start_sync):
            assert not start_sync and (old, new) == (retired, replacement)
            failure = checkpoint_failure and bool(state["requests"])
            if not failure:
                state["rows"]["zone2"] = [dict(r) for r in state["rows"]["zone1"]]
                assert canonical(state["rows"]["zone1"]) == canonical(expected_rows)
            old_state = {z: {"counts": counts(state["rows"][z]), "current_keys": []} for z in ZONES}
            evidence = {"passed": not failure, "old_state": old_state,
                        "last_observation": {"old_counts": {z: old_state[z]["counts"] for z in ZONES}}}
            if failure:
                fake.report["cases"][case] = {"passed": None, "failed_checks": ["old_zone2_versions_match"]}
                fake.save()
                raise SynchronizationGateError("offline peer retained target", evidence)
            return evidence

        def inspect(bucket, label):
            assert bucket == retired
            return {z: {"marker": "old-marker", "object_count": int(raw_leak and z == "zone2"),
                        "objects": ["old-marker_retained"] if raw_leak and z == "zone2" else [],
                        "errors": []} for z in ZONES}

        fake.rows, fake.bucket_state, fake.get = rows, bucket_state, get
        fake.renamed_checkpoint, fake.inspect = checkpoint, inspect
        fake.sync_status = lambda zone, bucket: {"returncode": 0, "stdout": caught}
        ticks = iter(i / 10 for i in range(100))
        with mock.patch.object(Path, "read_text", return_value=json.dumps(prior if source is None else source)), \
                mock.patch.object(time, "sleep", return_value=None), \
                mock.patch.object(time, "monotonic", side_effect=lambda: next(ticks)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = fake.run([EMPTY_CASE])
        return result, fake, state

    result, fake, state = offline_empty()
    finding = fake.report["cases"][EMPTY_CASE]
    assert result == 0 and finding["passed"] and fake.report["resumed_from_run"] == retained_run
    assert fake.report["run"] != retained_run and finding["requests_sent"] == 10
    assert state["enumerations"] == 1 and state["requests"] == [(r["key"], r["version_id"]) for r in plan]
    assert sum(sid == "null" for key, sid in state["requests"]) == 5
    assert finding["logical_counts"] == {z: counts([]) for z in ZONES}
    assert finding["raw_object_counts"] == {z: 0 for z in ZONES}
    rejected(lambda: fake.owned(retained_run + "-unrelated"))
    result, fake, state = offline_empty(checkpoint_failure=True, diagnostic_failure=True)
    assert result == 1 and fake.report["completed"] and state["persisted_before_diagnostics"]
    assert len(state["requests"]) == 1 and "infrastructure_error" not in fake.report
    assert "diagnostic_error" in fake.report["cases"][EMPTY_CASE]["version_deletes"][0]
    result, fake, state = offline_empty(transport_failure=True)
    assert result == 2 and not fake.report["completed"] and len(state["requests"]) == 1
    result, fake, state = offline_empty(raw_leak=True)
    assert result == 1 and fake.report["completed"] and len(state["requests"]) == 10
    assert fake.report["cases"][EMPTY_CASE]["logical_counts"] == {z: counts([]) for z in ZONES}
    assert fake.report["cases"][EMPTY_CASE]["raw_object_counts"]["zone2"] == 1
    result, fake, state = offline_empty(bad_actual_id=True)
    assert result == 2 and not state["requests"] and not getattr(fake, "resumed_buckets", ())
    bad = json.loads(json.dumps(prior))
    bad["cases"][SINGLE_CASE]["passed"] = False
    result, fake, state = offline_empty(source=bad)
    assert result == 2 and not state["admin"] and not state["requests"]
    assert not getattr(fake, "resumed_buckets", ())

    for success in (False, True):
        fake = object.__new__(Exercise)
        fake.report = {"cases": {}}
        events = []
        fake.renamed_checkpoint = lambda *args, **kwargs: events.append("initial-sync") or {"passed": True}

        def plain_stage(case, *args):
            events.append("plain")
            fake.report["cases"][case] = {"passed": success,
                                         "deletes": [{"old_state": {"zone1": {"rows": retained}}}]}

        fake.single_deletes = plain_stage
        fake.empty_versions = lambda *args: events.append("explicit")
        fake.synchronized_delete(EMPTY_CASE, "old", "new", fixture_current_payloads(1), fixture)
        assert events == (["initial-sync", "plain", "explicit"] if success else ["initial-sync", "plain"])
    print("self-test: passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--objects", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--case", action="append", choices=CASES,
                        help="repeat to select cases; default: all")
    parser.add_argument("--rename-with-backlog", action="store_true",
                        help="briefly pause Zone2 RGW during final copy/rename; synthetic timing control")
    parser.add_argument("--no-copy-rename", action="store_true",
                        help="reuse migration-synced-empty's fixture and deletes, omitting copy and rename")
    parser.add_argument("--empty-after-run", metavar="RUN",
                        help="empty a verified successful retained single-delete fixture in a new report")
    parser.add_argument("--self-test", action="store_true", help="test helpers without accessing clusters")
    args = parser.parse_args()
    if args.no_copy_rename and (args.case != [EMPTY_CASE] or args.rename_with_backlog
                               or args.empty_after_run is not None or args.self_test):
        parser.error("--no-copy-rename requires only --case migration-synced-empty")
    if args.empty_after_run is not None:
        try:
            select_cases(args.case, args.rename_with_backlog, args.empty_after_run)
        except ValueError as error:
            parser.error(str(error))
        if args.self_test:
            parser.error("--empty-after-run cannot be combined with --self-test")
    if args.self_test:
        self_test()
        return 0
    if not 1 <= args.objects <= 1000 or args.timeout < 5:
        parser.error("--objects must be 1..1000 and --timeout at least 5 seconds")
    try:
        selected = select_cases(args.case, args.rename_with_backlog, args.empty_after_run)
    except ValueError as error:
        parser.error(str(error))
    try:
        lab = Lab.load(args.config)
        lab.validate(args.timeout)
        exercise = Exercise(lab, args.objects, args.timeout, args.rename_with_backlog)
        if args.no_copy_rename:
            exercise.no_copy_rename = True
            exercise.report.update(no_copy_rename=True,
                                   method="Existing migration emptying sequence; no copy or rename")
        if args.empty_after_run is not None:
            exercise.empty_after_run = args.empty_after_run
        return exercise.run(selected)
    except (BotoCoreError, ClientError, LabError, subprocess.SubprocessError, ValueError) as error:
        print("Lab unavailable: " + redact(str(error)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
