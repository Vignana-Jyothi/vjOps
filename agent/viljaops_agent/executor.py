"""Command execution.

The agent has no general shell. It implements exactly the handlers below, and a
command whose `kind` is not in HANDLERS is refused and reported back as an
error. There is deliberately no "run arbitrary command" path — that would make
the control plane's whitelist meaningless, since the control plane is the thing
an attacker or a confused model would have to compromise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from .collectors import container_logs, run
from .config import config

log = logging.getLogger("viljaops.exec")


class ExecutionError(RuntimeError):
    pass


def execute(command: dict) -> dict:
    kind = command.get("kind", "")
    payload = command.get("payload") or {}
    handler = HANDLERS.get(kind)
    if not handler:
        return {"ok": False, "error": f"Unsupported command kind '{kind}'. This agent implements: {', '.join(sorted(HANDLERS))}"}

    if config.dry_run:
        return {"ok": True, "output": f"DRY RUN — would execute {kind} with {json.dumps(payload)[:500]}", "detail": {"dry_run": True}}

    started = time.time()
    try:
        result = handler(payload)
        result.setdefault("ok", True)
        result.setdefault("detail", {})
        result["detail"]["duration_s"] = round(time.time() - started, 2)
        return result
    except ExecutionError as exc:
        return {"ok": False, "error": str(exc), "detail": {"duration_s": round(time.time() - started, 2)}}
    except Exception as exc:
        log.exception("Handler %s crashed", kind)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "detail": {}}


def _docker(*args: str, timeout: int = 120) -> tuple[int, str, str]:
    return run([config.docker_bin, *args], timeout=timeout)


def _require_container(name: str) -> None:
    if not name:
        raise ExecutionError("No container name supplied")
    if name in config.exclude_containers:
        raise ExecutionError(f"'{name}' is a platform container and is protected from agent actions")


# --------------------------------------------------------------------------- #
def restart_container(p: dict) -> dict:
    name = p.get("container", "")
    _require_container(name)
    code, out, err = _docker("restart", name)
    if code != 0:
        raise ExecutionError(f"docker restart failed: {err.strip() or out.strip()}")

    time.sleep(5)
    c, state, _ = _docker("inspect", "-f", "{{.State.Status}}", name)
    healthy = state.strip() == "running"
    logs = container_logs(name, since="1m", tail=60)
    return {
        "ok": healthy,
        "output": f"restarted {name}; state={state.strip()}\n\n--- last 60 log lines ---\n{logs[-4000:]}",
        "error": "" if healthy else f"Container is '{state.strip()}' after restart, not running",
        "detail": {"container": name, "state": state.strip()},
    }


def recreate_container(p: dict) -> dict:
    """Recreate on a new host port, preserving image, env and volumes."""
    name = p.get("container", "")
    _require_container(name)
    new_port = int(p.get("host_port") or 0)

    code, spec_json, err = _docker("inspect", name)
    if code != 0:
        raise ExecutionError(f"Cannot inspect {name}: {err.strip()}")
    spec = json.loads(spec_json)[0]

    image = spec["Config"]["Image"]
    env = [e for e in (spec["Config"].get("Env") or []) if not e.startswith(("PATH=", "HOSTNAME="))]
    binds = spec["HostConfig"].get("Binds") or []
    restart_policy = (spec["HostConfig"].get("RestartPolicy") or {}).get("Name") or "unless-stopped"
    mem_bytes = spec["HostConfig"].get("Memory") or 0

    container_port = None
    for cport, bindings in (spec["HostConfig"].get("PortBindings") or {}).items():
        container_port = cport.split("/")[0]
        break
    if not container_port:
        exposed = list((spec["Config"].get("ExposedPorts") or {}).keys())
        container_port = exposed[0].split("/")[0] if exposed else "8000"

    backup = f"{name}-viljaops-prev-{int(time.time())}"
    _docker("rename", name, backup)
    _docker("stop", backup, timeout=60)

    args = ["run", "-d", "--name", name, f"--restart={restart_policy}"]
    if new_port:
        args += ["-p", f"127.0.0.1:{new_port}:{container_port}"]
    if mem_bytes:
        args += ["--memory", str(mem_bytes)]
    for e in env:
        args += ["-e", e]
    for b in binds:
        args += ["-v", b]
    args.append(image)

    code, out, err = _docker(*args, timeout=180)
    if code != 0:
        # Roll back to the original container rather than leaving nothing running.
        _docker("rename", backup, name)
        _docker("start", name)
        raise ExecutionError(f"Recreate failed, previous container restored: {err.strip() or out.strip()}")

    time.sleep(6)
    c, state, _ = _docker("inspect", "-f", "{{.State.Status}}", name)
    if state.strip() != "running":
        logs = container_logs(name, since="2m", tail=80)
        _docker("rm", "-f", name)
        _docker("rename", backup, name)
        _docker("start", name)
        raise ExecutionError(f"New container did not stay running (state={state.strip()}); previous container restored.\n{logs[-2000:]}")

    _docker("rm", "-f", backup)
    return {
        "ok": True,
        "output": f"recreated {name} on host port {new_port or 'unchanged'} (image {image})",
        "detail": {"container": name, "host_port": new_port, "image": image},
    }


def rollback_deployment(p: dict) -> dict:
    name = p.get("container", "")
    image = p.get("previous_image", "")
    _require_container(name)
    if not image:
        raise ExecutionError("No previous image supplied")
    code, spec_json, err = _docker("inspect", name)
    if code != 0:
        raise ExecutionError(f"Cannot inspect {name}: {err.strip()}")
    spec = json.loads(spec_json)[0]
    env = [e for e in (spec["Config"].get("Env") or []) if not e.startswith(("PATH=", "HOSTNAME="))]
    binds = spec["HostConfig"].get("Binds") or []
    port_bindings = spec["HostConfig"].get("PortBindings") or {}

    _docker("rm", "-f", name, timeout=60)
    args = ["run", "-d", "--name", name, "--restart=unless-stopped"]
    for cport, bindings in port_bindings.items():
        for b in bindings or []:
            args += ["-p", f"{b.get('HostIp') or '127.0.0.1'}:{b.get('HostPort')}:{cport.split('/')[0]}"]
    for e in env:
        args += ["-e", e]
    for b in binds:
        args += ["-v", b]
    args.append(image)
    code, out, err = _docker(*args, timeout=180)
    if code != 0:
        raise ExecutionError(f"Rollback failed: {err.strip() or out.strip()}")
    return {"ok": True, "output": f"rolled {name} back to {image}", "detail": {"image": image}}


def set_memory_limit(p: dict) -> dict:
    name = p.get("container", "")
    _require_container(name)
    mb = int(p.get("memory_mb") or 1024)
    if not 128 <= mb <= 16384:
        raise ExecutionError(f"Refusing a memory limit of {mb} MB — outside the sane range 128-16384")

    # Capture the pre-change value so the control plane can propose a
    # rollback to it if the Verification Agent later finds this made things
    # worse, without the agent needing to be asked twice.
    previous_mb = None
    code, spec_json, _err = _docker("inspect", name)
    if code == 0:
        try:
            limit_bytes = json.loads(spec_json)[0]["HostConfig"].get("Memory") or 0
            if limit_bytes:
                previous_mb = limit_bytes // (1024 * 1024)
        except (KeyError, IndexError, ValueError, json.JSONDecodeError):
            previous_mb = None

    code, out, err = _docker("update", "--memory", f"{mb}m", "--memory-swap", f"{mb}m", name)
    if code != 0:
        raise ExecutionError(f"docker update failed: {err.strip()}")
    return {
        "ok": True,
        "output": f"memory limit for {name} set to {mb} MB" + (f" (was {previous_mb} MB)" if previous_mb else ""),
        "detail": {"memory_mb": mb, "previous_memory_mb": previous_mb},
    }


def set_env_var(p: dict) -> dict:
    key, value = p.get("key", ""), p.get("value", "")
    slug = p.get("project_slug", "")
    name = p.get("container", "")
    if not key or not slug:
        raise ExecutionError("key and project_slug are required")

    env_dir = Path(config.env_dir)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / f"{slug}.env"

    lines = []
    if env_file.exists():
        lines = [l for l in env_file.read_text().splitlines() if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)

    out = f"{key} written to {env_file} (value not logged)"
    if p.get("recreate") and name:
        r = restart_container({"container": name})
        out += "\n" + r.get("output", "")
    return {"ok": True, "output": out, "detail": {"key": key, "env_file": str(env_file)}}


def run_migration(p: dict) -> dict:
    name = p.get("container", "")
    cmd = p.get("command", "")
    _require_container(name)
    if not cmd:
        raise ExecutionError("No migration command supplied")
    # Only ever exec inside the app's own container, never on the host.
    code, out, err = _docker("exec", name, "sh", "-lc", cmd, timeout=600)
    ok = code == 0
    return {
        "ok": ok,
        "output": out[-8000:],
        "error": "" if ok else err[-4000:],
        "detail": {"command": cmd, "exit_code": code},
    }


def rebuild_image(p: dict) -> dict:
    slug = p.get("project_slug", "")
    image = p.get("image") or f"{slug}:latest"
    build_dir = p.get("project_dir") or f"/srv/viljaops/{slug}"
    if not Path(build_dir).exists():
        raise ExecutionError(f"Build directory {build_dir} does not exist on this server")
    code, out, err = _docker("build", "--no-cache", "-t", image, build_dir, timeout=1800)
    ok = code == 0
    return {"ok": ok, "output": out[-8000:], "error": "" if ok else err[-6000:], "detail": {"image": image}}


def prune_docker(p: dict) -> dict:
    outputs = []
    for args in (["image", "prune", "-f"], ["builder", "prune", "-f"], ["container", "prune", "-f"]):
        code, out, err = _docker(*args, timeout=300)
        outputs.append(f"$ docker {' '.join(args)}\n{out or err}")
    total, used, free = shutil.disk_usage("/")
    return {
        "ok": True,
        "output": "\n\n".join(outputs),
        "detail": {"disk_pct_after": round(100 * used / total, 1), "free_gb": round(free / 1024**3, 1)},
    }


def prune_logs(p: dict) -> dict:
    name = p.get("container", "")
    _require_container(name)
    code, path, err = _docker("inspect", "-f", "{{.LogPath}}", name)
    if code != 0 or not path.strip():
        raise ExecutionError(f"Could not find the log file for {name}")
    lp = Path(path.strip())
    if not lp.exists():
        raise ExecutionError(f"Log file {lp} does not exist")
    size_mb = lp.stat().st_size / 1_048_576
    with lp.open("w"):
        pass
    return {"ok": True, "output": f"truncated {lp} ({size_mb:.1f} MB reclaimed)", "detail": {"reclaimed_mb": round(size_mb, 1)}}


def stop_conflicting_container(p: dict) -> dict:
    name = p.get("container", "")
    _require_container(name)
    code, out, err = _docker("stop", name, timeout=60)
    if code != 0:
        raise ExecutionError(f"docker stop failed: {err.strip()}")
    return {"ok": True, "output": f"stopped {name}", "detail": {"container": name}}


# --------------------------------------------------------------------------- #
# Nginx — the one that can take the whole server down, so it gets the most care
# --------------------------------------------------------------------------- #
def _nginx_test() -> tuple[bool, str]:
    code, out, err = run([config.nginx_bin, "-t"], timeout=30)
    return code == 0, (out + err).strip()


def apply_nginx_config(p: dict) -> dict:
    content = p.get("content", "")
    target = Path(p.get("target_path") or "")
    enabled = Path(p.get("enabled_path") or "")
    checksum = p.get("checksum", "")

    if not content or not target or not enabled:
        raise ExecutionError("content, target_path and enabled_path are all required")
    if checksum and hashlib.sha256(content.encode()).hexdigest() != checksum:
        raise ExecutionError("Config checksum mismatch — refusing to write a config that was altered in transit")
    if not str(target).startswith(config.nginx_sites_available):
        raise ExecutionError(f"Refusing to write outside {config.nginx_sites_available}")

    ok_before, before_out = _nginx_test()
    if not ok_before:
        raise ExecutionError(
            "Nginx configuration is ALREADY broken on this server before any change. "
            "Refusing to touch it — a human needs to look first.\n" + before_out
        )

    backup = None
    if target.exists():
        backup = target.with_suffix(target.suffix + p.get("backup_suffix", ".bak"))
        shutil.copy2(target, backup)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".staging")
    staging.write_text(content)

    try:
        staging.replace(target)
        if not enabled.exists():
            enabled.parent.mkdir(parents=True, exist_ok=True)
            try:
                enabled.symlink_to(target)
            except FileExistsError:
                pass

        ok, test_out = _nginx_test()
        if not ok:
            raise ExecutionError("nginx -t failed with the new config:\n" + test_out)

        code, out, err = run([config.nginx_bin, "-s", "reload"], timeout=30)
        if code != 0:
            raise ExecutionError(f"nginx reload failed: {err.strip() or out.strip()}")

        return {
            "ok": True,
            "output": f"applied {target.name} and reloaded nginx\n{test_out}",
            "nginx_test_output": test_out,
            "detail": {"target": str(target), "domain": p.get("domain"), "upstream_port": p.get("upstream_port")},
        }

    except Exception as exc:
        # Restore and prove the restore worked before reporting.
        restored = "no previous config existed; removed the new file"
        if backup and backup.exists():
            shutil.copy2(backup, target)
            restored = f"restored previous config from {backup.name}"
        else:
            target.unlink(missing_ok=True)
            enabled.unlink(missing_ok=True)
        ok_after, after_out = _nginx_test()
        if ok_after:
            run([config.nginx_bin, "-s", "reload"], timeout=30)
        raise ExecutionError(
            f"{exc}\n\nROLLBACK: {restored}. nginx -t after rollback: "
            f"{'PASS' if ok_after else 'FAIL — MANUAL INTERVENTION REQUIRED'}\n{after_out}"
        )
    finally:
        staging.unlink(missing_ok=True)


def reload_nginx(p: dict) -> dict:
    ok, test_out = _nginx_test()
    if not ok:
        raise ExecutionError("nginx -t failed; not reloading:\n" + test_out)
    code, out, err = run([config.nginx_bin, "-s", "reload"], timeout=30)
    if code != 0:
        raise ExecutionError(f"reload failed: {err.strip()}")
    return {"ok": True, "output": f"nginx reloaded\n{test_out}"}


def health_check(p: dict) -> dict:
    """Plain HTTP GET against the deployment's own mapped host port —
    affirmative evidence the app is actually serving traffic, not just that
    a shell command exited 0. Runs on the host (not inside the container),
    matching how Nginx itself reaches the app.
    """
    import urllib.error
    import urllib.request

    port = p.get("port")
    path = p.get("path") or "/health"
    if not path.startswith("/"):
        path = "/" + path
    timeout = float(p.get("timeout_s") or 5)
    if not port:
        raise ExecutionError("No port supplied for health_check")

    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed loopback host only
            code = resp.status
            body = resp.read(500).decode("utf-8", "replace")
        ok = 200 <= code < 300
        return {
            "ok": ok,
            "output": f"GET {url} -> {code}\n{body}",
            "error": "" if ok else f"GET {url} -> HTTP {code}",
            "detail": {"status_code": code, "url": url},
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "output": "", "error": f"GET {url} -> HTTP {exc.code}", "detail": {"status_code": exc.code, "url": url}}
    except Exception as exc:  # noqa: BLE001 — any network failure means "not healthy", report it, don't crash the agent
        return {"ok": False, "output": "", "error": f"GET {url} failed: {exc}", "detail": {"status_code": None, "url": url}}


HANDLERS = {
    "restart_container": restart_container,
    "recreate_container": recreate_container,
    "rollback_deployment": rollback_deployment,
    "set_memory_limit": set_memory_limit,
    "set_env_var": set_env_var,
    "run_migration": run_migration,
    "rebuild_image": rebuild_image,
    "prune_docker": prune_docker,
    "prune_logs": prune_logs,
    "stop_conflicting_container": stop_conflicting_container,
    "apply_nginx_config": apply_nginx_config,
    "reload_nginx": reload_nginx,
    "health_check": health_check,
}
