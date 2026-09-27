#!/usr/bin/env python3
"""Zero-provider-cost diagnostics for the Paper Lab preparation pipeline."""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.request
import zipfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


LONDON = ZoneInfo("Europe/London")
SCHEMA_VERSION = "paper-lab-pipeline-status-1"
CYCLE_CONFIG = {
    "morning": {
        "fresh_after": time(9, 30),
        "schedules": {"42 9 * * *", "57 9 * * *"},
    },
    "evening": {
        "fresh_after": time(16, 0),
        "schedules": {"12 16 * * *", "27 16 * * *"},
    },
}


def utc_now():
    return datetime.now(timezone.utc)


def parse_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value):
    return parse_timestamp(value).isoformat()


def cycle_floor(now, cycle):
    if cycle not in CYCLE_CONFIG:
        raise ValueError(f"Unknown scheduled cycle: {cycle}")
    local_now = parse_timestamp(now).astimezone(LONDON)
    candidate = datetime.combine(
        local_now.date(), CYCLE_CONFIG[cycle]["fresh_after"], tzinfo=LONDON
    )
    if local_now < candidate:
        candidate -= timedelta(days=1)
    return candidate


def expected_window_end(floor):
    floor_local = floor.astimezone(LONDON)
    return datetime.combine(
        floor_local.date() + timedelta(days=1), time(10, 0), tzinfo=LONDON
    )


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)


def deep_merge(base, updates):
    merged = dict(base or {})
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def update_status(path, stage, **details):
    status = load_json(path, {})
    now = iso_utc(utc_now())
    status.setdefault("schema_version", SCHEMA_VERSION)
    status.setdefault("started_at_utc", now)
    status.setdefault("provider_contacted", False)
    status.setdefault("paid_call_attempted", False)
    status.setdefault("paid_resources_consumed", False)
    status.setdefault("paid_resource_consumption_status", "not_attempted")
    status.setdefault("history", [])
    status["current_stage"] = stage
    status["updated_at_utc"] = now
    status = deep_merge(status, details)
    status["history"].append({"at_utc": now, "stage": stage})
    atomic_write_json(path, status)
    return status


def board_freshness(board_path, cycle, now=None):
    now = parse_timestamp(now or utc_now())
    floor = cycle_floor(now, cycle)
    expected_end = expected_window_end(floor).astimezone(timezone.utc)
    board = load_json(board_path, {})
    result = {
        "fresh": False,
        "cycle": cycle,
        "cycle_floor_utc": floor.astimezone(timezone.utc).isoformat(),
        "expected_window_end_utc": expected_end.isoformat(),
        "reason": "board_missing_or_invalid",
    }
    try:
        observed = parse_timestamp(board["observed_at_utc"])
        window_end = parse_timestamp(board["collection_window_end_utc"])
    except (KeyError, TypeError, ValueError):
        return result

    result.update(
        {
            "board_observed_at_utc": observed.isoformat(),
            "board_window_end_utc": window_end.isoformat(),
            "board_event_count": board.get("event_count"),
        }
    )
    if observed < floor.astimezone(timezone.utc):
        result["reason"] = "board_precedes_cycle"
    elif observed > now + timedelta(minutes=5):
        result["reason"] = "board_timestamp_in_future"
    elif window_end != expected_end:
        result["reason"] = "board_window_end_mismatch"
    else:
        result["fresh"] = True
        result["reason"] = "fresh_board_for_cycle"
    return result


def append_github_output(path, values):
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            handle.write(f"{key}={rendered}\n")


def gate(args):
    status = update_status(
        args.status,
        "freshness_gate_started",
        run={
            "id": args.run_id,
            "event": args.event,
            "schedule": args.schedule,
            "sha": args.sha,
        },
        cycle=args.cycle,
    )
    if args.cycle == "manual":
        freshness = {"fresh": False, "reason": "explicit_live_collection"}
        required = True
    else:
        freshness = board_freshness(args.board, args.cycle, args.now)
        required = not freshness["fresh"]
    status = update_status(
        args.status,
        "freshness_gate_completed",
        cycle=args.cycle,
        freshness=freshness,
        collect_required=required,
    )
    append_github_output(
        args.github_output,
        {
            "cycle": args.cycle,
            "collect_required": required,
            "gate_reason": freshness["reason"],
        },
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def mark(args):
    details = json.loads(args.details) if args.details else {}
    status = update_status(args.status, args.stage, **details)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def failure_classification(stage):
    stage = str(stage or "")
    if "provider" in stage or "collection" in stage or "api" in stage:
        return "collector_started_provider_api_failed"
    if "normalization" in stage:
        return "collection_succeeded_normalization_failed"
    if any(label in stage for label in ("board", "manifest", "evidence")):
        return "normalization_succeeded_board_manifest_failed"
    return "workflow_started_collector_failed_unknown_stage"


def finalize(args):
    status = load_json(args.status, {})
    required = str(args.collect_required).lower() == "true"
    if args.gate != "success":
        classification = "workflow_started_freshness_gate_failed"
    elif not required:
        classification = "fully_successful_fresh_board"
    elif args.collector != "success":
        failed_at = status.get("failed_at_stage") or status.get("current_stage")
        classification = failure_classification(failed_at)
    elif args.archive != "success" or args.commit != "success":
        classification = "collection_and_board_succeeded_commit_publish_failed"
    else:
        classification = "fully_successful_fresh_board"
    status = update_status(
        args.status,
        "diagnostic_finalized",
        classification=classification,
        step_outcomes={
            "gate": args.gate,
            "collector": args.collector,
            "archive": args.archive,
            "commit": args.commit,
        },
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def github_json(url, token=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "blood.twin-paper-lab-health/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download_diagnostic(repo, run_id, token):
    listing = github_json(
        f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        token,
    )
    candidates = [
        artifact
        for artifact in listing.get("artifacts", [])
        if artifact.get("name", "").startswith("paper-lab-diagnostic-")
        and not artifact.get("expired")
    ]
    if not candidates:
        return {}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "blood.twin-paper-lab-health/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(candidates[-1]["archive_download_url"], headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    for name in archive.namelist():
        if name.endswith(".json"):
            return json.loads(archive.read(name).decode("utf-8"))
    return {}


def inspect_github(args):
    now = parse_timestamp(args.now or utc_now())
    freshness = board_freshness(args.board, args.cycle, now)
    report = {
        "schema_version": SCHEMA_VERSION,
        "checked_at_utc": now.isoformat(),
        "cycle": args.cycle,
        "freshness": freshness,
        "provider_contacted_by_diagnostic": False,
        "paid_resources_consumed_by_diagnostic": False,
    }

    if freshness["fresh"]:
        report["classification"] = "fully_successful_fresh_board"
    else:
        token = args.token or os.environ.get("GITHUB_TOKEN")
        runs = github_json(
            f"https://api.github.com/repos/{args.repo}/actions/workflows/"
            f"paper-lab.yml/runs?event=schedule&per_page=100",
            token,
        ).get("workflow_runs", [])
        floor = cycle_floor(now, args.cycle).astimezone(timezone.utc)
        schedules = CYCLE_CONFIG[args.cycle]["schedules"]
        candidates = []
        for run in runs:
            created = parse_timestamp(run.get("created_at"))
            title = str(run.get("display_title") or "")
            if created >= floor and any(schedule in title for schedule in schedules):
                candidates.append(run)

        if not candidates:
            report["classification"] = "scheduled_run_never_fired"
        else:
            run = sorted(candidates, key=lambda item: item["created_at"])[-1]
            report["run"] = {
                key: run.get(key)
                for key in (
                    "id", "run_number", "status", "conclusion", "created_at",
                    "run_started_at", "updated_at", "html_url", "head_sha",
                    "display_title",
                )
            }
            if run.get("status") != "completed":
                report["classification"] = "workflow_started_not_completed"
            else:
                jobs = github_json(run["jobs_url"] + "?per_page=100", token).get("jobs", [])
                test_job = next((job for job in jobs if job.get("name") == "test"), {})
                collect_job = next((job for job in jobs if job.get("name") == "collect"), {})
                if test_job.get("conclusion") == "failure":
                    report["classification"] = "workflow_started_tests_failed"
                elif collect_job.get("conclusion") == "failure":
                    diagnostic = download_diagnostic(args.repo, run["id"], token) if token else {}
                    report["run_diagnostic"] = diagnostic
                    report["classification"] = diagnostic.get(
                        "classification",
                        failure_classification(
                            diagnostic.get("failed_at_stage") or diagnostic.get("current_stage")
                        ),
                    )
                elif run.get("conclusion") == "success":
                    report["classification"] = "run_succeeded_but_fresh_board_not_published"
                else:
                    report["classification"] = "workflow_started_failed_before_classification"

    atomic_write_json(args.output, report)
    if args.summary:
        summary = (
            "## Paper Lab pipeline health\n\n"
            f"- Classification: `{report['classification']}`\n"
            f"- Cycle: `{args.cycle}`\n"
            f"- Board fresh: `{freshness['fresh']}`\n"
            f"- Diagnostic provider calls: `false`\n"
            f"- Diagnostic paid-resource use: `false`\n"
        )
        Path(args.summary).write_text(summary, encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    healthy = report["classification"] == "fully_successful_fresh_board"
    return 0 if healthy or not args.fail_unhealthy else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--cycle", choices=("morning", "evening", "manual"), required=True)
    gate_parser.add_argument("--board", required=True)
    gate_parser.add_argument("--status", required=True)
    gate_parser.add_argument("--github-output")
    gate_parser.add_argument("--now")
    gate_parser.add_argument("--run-id")
    gate_parser.add_argument("--event")
    gate_parser.add_argument("--schedule")
    gate_parser.add_argument("--sha")
    gate_parser.set_defaults(handler=gate)

    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("--status", required=True)
    mark_parser.add_argument("--stage", required=True)
    mark_parser.add_argument("--details")
    mark_parser.set_defaults(handler=mark)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--status", required=True)
    finalize_parser.add_argument("--gate", required=True)
    finalize_parser.add_argument("--collect-required", required=True)
    finalize_parser.add_argument("--collector", required=True)
    finalize_parser.add_argument("--archive", required=True)
    finalize_parser.add_argument("--commit", required=True)
    finalize_parser.set_defaults(handler=finalize)

    inspect_parser = subparsers.add_parser("inspect-github")
    inspect_parser.add_argument("--repo", required=True)
    inspect_parser.add_argument("--cycle", choices=("morning", "evening"), required=True)
    inspect_parser.add_argument("--board", required=True)
    inspect_parser.add_argument("--output", required=True)
    inspect_parser.add_argument("--summary")
    inspect_parser.add_argument("--token")
    inspect_parser.add_argument("--now")
    inspect_parser.add_argument("--fail-unhealthy", action="store_true")
    inspect_parser.set_defaults(handler=inspect_github)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
