"""The whitelist of things this system is ever allowed to do.

Nothing outside ACTION_CATALOG can be executed. The LLM proposes an
`action_type`; if it isn't in this dict the proposal is downgraded to a manual
instruction that a human has to carry out. That is the containment boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Risk


@dataclass(frozen=True)
class ActionSpec:
    key: str
    title: str
    description: str
    risk: Risk
    # Executed by the agent on the target server? If False it is advisory only
    # (a code change the student must make, an issue to open, etc.)
    executable: bool = True
    params: tuple[str, ...] = ()
    # Even with AUTO_REMEDIATION=true, these never run unattended.
    never_auto: bool = False
    reversible: bool = True
    verify: str = ""
    # Does `verify` resolve from the command's own exit code (True), or does
    # it require watching telemetry for a while afterward (False)? Keeps this
    # decision next to the criterion it describes instead of in a second,
    # easy-to-forget-to-update lookup table in the verification engine.
    immediate_verify: bool = False
    # Anomaly kinds (see observability/anomaly.py) whose reappearance on this
    # deployment after the fix means the `verify` criterion was violated.
    # Empty for immediate actions and for actions with no matching anomaly
    # detector yet.
    watch_kinds: tuple[str, ...] = ()
    # How long to watch before deciding, in minutes. Only meaningful when
    # immediate_verify is False.
    grace_minutes: int = 3
    # Blast radius: does this action only ever touch the one project's own
    # container/config ("project"), or could it reach infrastructure shared
    # across projects ("shared")? Nothing in the current catalog is scope
    # "shared" yet — every handler only ever touches the caller's own
    # container or nginx entry — but the field exists so the day a
    # shared-Redis/shared-Postgres action gets added, `approve_fix` can
    # require an explicit extra confirmation for it rather than that being
    # a new thing someone has to remember to add.
    scope: str = "project"


ACTION_CATALOG: dict[str, ActionSpec] = {
    a.key: a
    for a in [
        # ---------------- container lifecycle ----------------
        ActionSpec(
            "restart_container",
            "Restart the container",
            "docker restart <container>. Clears transient crashes and re-runs the entrypoint.",
            Risk.moderate,
            params=("container",),
            verify="container is running and healthy for 60s",
            watch_kinds=("restart_loop",),
            grace_minutes=3,
        ),
        ActionSpec(
            "recreate_container",
            "Recreate the container from the current image",
            "docker rm -f then docker run with the recorded run spec.",
            Risk.moderate,
            params=("container",),
            verify="container is running and port responds",
            watch_kinds=("restart_loop",),
            grace_minutes=3,
        ),
        ActionSpec(
            "rebuild_image",
            "Rebuild the image without cache",
            "docker build --no-cache. Fixes stale layers and half-installed dependencies.",
            Risk.moderate,
            params=("project_dir", "image"),
            verify="build exits 0",
            immediate_verify=True,
        ),
        ActionSpec(
            "rollback_deployment",
            "Roll back to the last known-good image",
            "Re-point the container at the previous image tag that ran healthy.",
            Risk.moderate,
            params=("container", "previous_image"),
            verify="previous image running and healthy",
            watch_kinds=("restart_loop",),
            grace_minutes=3,
        ),
        ActionSpec(
            "set_memory_limit",
            "Raise the container memory limit",
            "docker update --memory. Used when the kernel OOM-killed the process.",
            Risk.moderate,
            params=("container", "memory_mb"),
            verify="no OOM kill for 15 min",
            watch_kinds=("memory_ceiling", "memory_leak_trend", "restart_loop"),
            grace_minutes=15,
        ),
        # ---------------- ports & proxy ----------------
        ActionSpec(
            "reassign_port",
            "Assign a free host port and re-point Nginx",
            "Allocates an unused port from this server's range, recreates the "
            "container on it, regenerates the Nginx upstream, validates and reloads.",
            Risk.moderate,
            params=("deployment_id",),
            verify="nginx -t passes and the domain returns non-502",
            watch_kinds=("error_rate_spike",),
            grace_minutes=3,
        ),
        ActionSpec(
            "apply_nginx_config",
            "Write and reload the Nginx site config",
            "Writes the rendered vhost, runs nginx -t, reloads only if valid, "
            "restores the previous file if not.",
            Risk.moderate,
            params=("config_id",),
            verify="nginx -t passes",
            immediate_verify=True,
        ),
        ActionSpec(
            "reload_nginx",
            "Reload Nginx",
            "nginx -t && nginx -s reload. No downtime.",
            Risk.safe,
            params=(),
            verify="nginx -t passes",
            immediate_verify=True,
        ),
        ActionSpec(
            "stop_conflicting_container",
            "Stop the container squatting on the port",
            "Stops another container that holds the port we need.",
            Risk.dangerous,
            params=("container",),
            never_auto=True,
            verify="port is free",
            reversible=False,
            watch_kinds=("error_rate_spike",),
            grace_minutes=3,
        ),
        # ---------------- environment ----------------
        ActionSpec(
            "set_env_var",
            "Set a missing environment variable",
            "Writes the key to the deployment's env file and recreates the container. "
            "Values are stored write-only and masked in all logs and UI.",
            Risk.moderate,
            params=("deployment_id", "key", "value"),
            never_auto=True,
            verify="container starts and the variable is present",
            watch_kinds=("restart_loop",),
            grace_minutes=3,
        ),
        # ---------------- housekeeping ----------------
        ActionSpec(
            "prune_docker",
            "Prune dangling images and build cache",
            "docker image prune -f && docker builder prune -f. Never touches "
            "named volumes or running containers.",
            Risk.safe,
            params=(),
            verify="disk usage drops below 85%",
            watch_kinds=("disk_pressure",),
            grace_minutes=3,
        ),
        ActionSpec(
            "prune_logs",
            "Truncate oversized container log files",
            "Truncates json-file logs over the size threshold and applies a log rotation cap.",
            Risk.safe,
            params=("container",),
            verify="disk usage drops",
            watch_kinds=("disk_pressure",),
            grace_minutes=3,
        ),
        # ---------------- advisory / code changes ----------------
        ActionSpec(
            "code_change",
            "Change required in the repository",
            "A patch the team must apply and push. Never applied automatically.",
            Risk.safe,
            executable=False,
            params=("file", "explanation"),
        ),
        ActionSpec(
            "add_dependency",
            "Add a missing dependency",
            "Adds the package to requirements.txt / package.json and rebuilds.",
            Risk.safe,
            executable=False,
            params=("package", "manifest"),
        ),
        ActionSpec(
            "add_healthcheck",
            "Add a health endpoint and a Docker HEALTHCHECK",
            "Without one, nothing can tell a hung app from a healthy one.",
            Risk.safe,
            executable=False,
            params=("path",),
        ),
        ActionSpec(
            "rotate_secret",
            "Rotate a leaked credential",
            "The secret is in git history and must be considered compromised.",
            Risk.dangerous,
            executable=False,
            never_auto=True,
            reversible=False,
            params=("secret_kind", "location"),
        ),
        ActionSpec(
            "run_migration",
            "Run database migrations",
            "Schema changes can destroy data. Always requires a named human approver.",
            Risk.dangerous,
            params=("container", "command"),
            never_auto=True,
            reversible=False,
            verify="migration exits 0 and app starts",
            immediate_verify=True,
        ),
        ActionSpec(
            "notify_team",
            "Notify the student team",
            "Posts the diagnosis to the team with the exact next step.",
            Risk.safe,
            executable=False,
            params=("message",),
        ),
        ActionSpec(
            "escalate_to_devops",
            "Escalate to a DevOps engineer",
            "Used when confidence is low or the fix is outside the whitelist.",
            Risk.safe,
            executable=False,
            params=("reason",),
        ),
    ]
}


def is_allowed(action_type: str) -> bool:
    return action_type in ACTION_CATALOG


def spec_for(action_type: str) -> ActionSpec:
    return ACTION_CATALOG.get(action_type) or ACTION_CATALOG["escalate_to_devops"]


def can_auto_execute(action_type: str, auto_enabled: bool) -> bool:
    if not auto_enabled:
        return False
    spec = ACTION_CATALOG.get(action_type)
    if not spec or not spec.executable or spec.never_auto:
        return False
    return spec.risk == Risk.safe


def allowed_action_summary() -> list[dict]:
    """Compact list handed to the LLM so it can only propose real actions."""
    return [
        {
            "action_type": s.key,
            "what_it_does": s.description,
            "risk": s.risk.value,
            "params": list(s.params),
            "executable": s.executable,
        }
        for s in ACTION_CATALOG.values()
    ]
