"""EventBridge Scheduler-backed windows for `channel.py schedule
add/remove/list` (aws-media and ecs-express only -- local-docker has no
AWS presence to schedule against; see channel.py's cmd_schedule, which
refuses it before calling anything here).

A "window" is an on-air period: an optional start (omitted = starts
immediately -- the caller is expected to also run `channel.py start`,
see cmd_schedule) and an optional end (omitted = runs until a manual
`channel.py stop`). Each edge that has an actual future timestamp becomes
a one-time EventBridge Scheduler schedule targeting the shared Lambda
from scheduler_stack.py/_scheduler_lambda.py; an omitted edge creates
nothing there.

Windows are tracked in a small local JSON sidecar file next to the
channel's TOML config (<name>.schedule.json) -- NOT in the TOML config
itself (channel.py never writes that; see igor's channel_store.py) and
NOT solely reconstructed from EventBridge, since an immediate-start/
manual-end window can have zero EventBridge schedules of its own --
EventBridge alone can't tell "currently live with no scheduled edges"
apart from "no window at all".
"""

import datetime
import json
import os
import sys
import uuid

_SCHEDULER_STACK_NAME = "ItsALiveSharedStack-scheduler"
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
_MAX_DT = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


def _group_name(channel_name):
    return f"its-a-live-{channel_name}"


def _state_path(config_path):
    base, _ext = os.path.splitext(os.path.abspath(config_path))
    return f"{base}.schedule.json"


def _load(config_path):
    path = _state_path(config_path)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("windows", [])


def _save(config_path, windows):
    path = _state_path(config_path)
    with open(path, "w") as f:
        json.dump({"windows": windows}, f, indent=2)
        f.write("\n")


def _parse(iso):
    try:
        return datetime.datetime.strptime(iso, _TIME_FMT).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        sys.exit(f"Schedule timestamps must be ISO8601 UTC like 2026-01-01T00:00:00Z, got {iso!r}")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _effective_bounds(window):
    """(start, end) datetimes for overlap-checking and status: a null
    `start` becomes the window's recorded creation time (its actual,
    already-past, effective start); a null `end` becomes _MAX_DT
    (open-ended, runs until a manual stop)."""
    start = _parse(window["start"]) if window["start"] else _parse(window["created_at"])
    end = _parse(window["end"]) if window["end"] else _MAX_DT
    return start, end


def _describe_window(window):
    start = window["start"] or f"immediately (from {window['created_at']})"
    end = window["end"] or "manual stop"
    return f"{start} - {end}"


def _validate_no_overlap(existing, new_start, new_end):
    for window in existing:
        ex_start, ex_end = _effective_bounds(window)
        if new_start < ex_end and ex_start < new_end:
            sys.exit(
                f"Window {window['id']} already covers {_describe_window(window)} -- "
                f"overlapping windows are not allowed. Remove or narrow it first."
            )


def _window_status(window, now):
    start, end = _effective_bounds(window)
    if now < start:
        return "upcoming"
    if end != _MAX_DT and now >= end:
        return "past"
    return "active"


def _scheduler_function(session):
    """Looks up the shared scheduler stack's Lambda ARN + invocation role
    (see scheduler_stack.py) -- required before creating any schedule."""
    cf = session.client("cloudformation")
    try:
        resp = cf.describe_stacks(StackName=_SCHEDULER_STACK_NAME)
    except cf.exceptions.ClientError:
        sys.exit(
            f"{_SCHEDULER_STACK_NAME} is not deployed in this region -- "
            f"run `cdk deploy {_SCHEDULER_STACK_NAME}` first (see its-a-live/AGENTS.md)."
        )
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    function_arn = outputs.get("SchedulerFunctionArn")
    role_arn = outputs.get("SchedulerInvocationRoleArn")
    if not function_arn or not role_arn:
        sys.exit(f"{_SCHEDULER_STACK_NAME} is missing expected outputs -- redeploy it.")
    return function_arn, role_arn


def _ensure_group(scheduler, group_name):
    try:
        scheduler.create_schedule_group(Name=group_name)
    except scheduler.exceptions.ConflictException:
        pass


def _create_edge_schedule(scheduler, group_name, function_arn, role_arn,
                           schedule_name, when, channel_name, backend, region, action):
    scheduler.create_schedule(
        Name=schedule_name,
        GroupName=group_name,
        ScheduleExpression=f"at({when.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": function_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({
                "channel_name": channel_name,
                "backend": backend,
                "region": region,
                "action": action,
            }),
        },
    )


def add_window(cfg, session, channel_name, config_path, start_iso, end_iso):
    """Validates non-overlap, creates whichever EventBridge Scheduler
    one-time schedules the given edges need (none for an omitted edge),
    and records the window locally. Returns the saved window dict.

    Does NOT itself start the channel for an immediate (start_iso=None)
    window -- see channel.py's cmd_schedule, which runs the existing
    `start` command right after this when that's the case, so "what
    start means" still has exactly one implementation."""
    backend = cfg["deploy"]["backend"]
    region = cfg.get("aws", {}).get("region")
    if not region:
        sys.exit("[aws].region must be set in the config to schedule start/stop.")

    now = _now()
    new_start = _parse(start_iso) if start_iso else now
    new_end = _parse(end_iso) if end_iso else _MAX_DT
    if start_iso and new_start <= now:
        sys.exit(f"--start {start_iso} must be in the future.")
    if end_iso and new_end <= now:
        sys.exit(f"--end {end_iso} must be in the future.")
    if start_iso and end_iso and new_end <= new_start:
        sys.exit("--end must be after --start.")

    existing = _load(config_path)
    _validate_no_overlap(existing, new_start, new_end)

    function_arn, role_arn = _scheduler_function(session)
    scheduler = session.client("scheduler")
    group_name = _group_name(channel_name)
    _ensure_group(scheduler, group_name)

    window_id = uuid.uuid4().hex[:12]
    if start_iso:
        _create_edge_schedule(
            scheduler, group_name, function_arn, role_arn,
            f"{window_id}-start", new_start, channel_name, backend, region, "start",
        )
    if end_iso:
        _create_edge_schedule(
            scheduler, group_name, function_arn, role_arn,
            f"{window_id}-stop", new_end, channel_name, backend, region, "stop",
        )

    window = {
        "id": window_id,
        "start": start_iso,
        "end": end_iso,
        "created_at": now.strftime(_TIME_FMT),
    }
    existing.append(window)
    existing.sort(key=lambda w: _effective_bounds(w)[0])
    _save(config_path, existing)
    return window


def remove_window(session, channel_name, config_path, window_id):
    existing = _load(config_path)
    window = next((w for w in existing if w["id"] == window_id), None)
    if window is None:
        sys.exit(f"No scheduled window with id {window_id!r} for this channel.")

    scheduler = session.client("scheduler")
    group_name = _group_name(channel_name)
    for suffix in ("start", "stop"):
        try:
            scheduler.delete_schedule(Name=f"{window_id}-{suffix}", GroupName=group_name)
        except scheduler.exceptions.ResourceNotFoundException:
            # Never created (that edge was omitted) or already fired and
            # auto-deleted (ActionAfterCompletion="DELETE") -- both fine.
            pass

    remaining = [w for w in existing if w["id"] != window_id]
    _save(config_path, remaining)
    return window


def list_windows(config_path):
    now = _now()
    return [{**w, "status": _window_status(w, now)} for w in _load(config_path)]


def delete_all(session, channel_name, config_path):
    """Best-effort cleanup for `channel.py terminate`: deletes every
    schedule in this channel's group, the group itself, and the local
    state file. Safe to call even if nothing was ever scheduled (group
    doesn't exist) or the scheduler stack was never deployed."""
    group_name = _group_name(channel_name)
    try:
        scheduler = session.client("scheduler")
        paginator = scheduler.get_paginator("list_schedules")
        for page in paginator.paginate(GroupName=group_name):
            for entry in page.get("Schedules", []):
                try:
                    scheduler.delete_schedule(Name=entry["Name"], GroupName=group_name)
                except scheduler.exceptions.ResourceNotFoundException:
                    pass
        scheduler.delete_schedule_group(Name=group_name)
    except Exception:
        pass

    path = _state_path(config_path)
    if os.path.exists(path):
        os.remove(path)
