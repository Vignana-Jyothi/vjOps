"""Host-side collection: container stats, listening ports, logs, disk."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import config

log = logging.getLogger("viljaops.collect")


def run(cmd: list[str], *, timeout: int = 30, check: bool = False) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            log.debug("Command failed %s: %s", " ".join(cmd), p.stderr[:200])
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


# --------------------------------------------------------------------------- #
def host_facts() -> dict:
    cpu = os.cpu_count() or 0
    ram_mb = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    ram_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    total, used, free = shutil.disk_usage("/")
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    return {
        "cpu_cores": cpu,
        "ram_mb": ram_mb,
        "disk_gb": total // (1024**3),
        "disk_pct": round(100 * used / total, 1) if total else 0.0,
        "load_avg": round(load, 2),
    }


# --------------------------------------------------------------------------- #
def listening_ports() -> list[dict]:
    """What is actually bound right now — the ground truth the registry reconciles against."""
    out: list[dict] = []
    code, stdout, _ = run(["ss", "-tlnp"])
    if code != 0:
        code, stdout, _ = run(["netstat", "-tlnp"])
    if code != 0:
        return out

    seen = set()
    for line in stdout.splitlines()[1:]:
        m = re.search(r"[:\.](\d{2,5})\s", line)
        if not m:
            continue
        port = int(m.group(1))
        if port in seen:
            continue
        seen.add(port)
        proc = ""
        pm = re.search(r'users:\(\("([^"]+)"|(\S+)/(\S+)\s*$', line)
        if pm:
            proc = pm.group(1) or pm.group(3) or ""
        out.append({"port": port, "process": proc})

    port_to_container = {}
    for c in list_containers():
        for p in c.get("host_ports", []):
            port_to_container[p] = c["name"]
    for entry in out:
        if entry["port"] in port_to_container:
            entry["container"] = port_to_container[entry["port"]]
    return out


# --------------------------------------------------------------------------- #
_PORT_RE = re.compile(r"(?:(\d+\.\d+\.\d+\.\d+|\[::\]):)?(\d+)->(\d+)/tcp")


def list_containers() -> list[dict]:
    code, stdout, _ = run([config.docker_bin, "ps", "-a", "--format", "{{json .}}"])
    if code != 0:
        return []
    containers = []
    for line in stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = raw.get("Names", "").split(",")[0]
        if name in config.exclude_containers:
            continue
        host_ports = [int(m.group(2)) for m in _PORT_RE.finditer(raw.get("Ports", ""))]
        containers.append(
            {
                "name": name,
                "id": raw.get("ID", ""),
                "image": raw.get("Image", ""),
                "state": raw.get("State", ""),
                "status": raw.get("Status", ""),
                "host_ports": host_ports,
                "restarting": "Restarting" in raw.get("Status", ""),
            }
        )
    return containers


def container_stats() -> dict[str, dict]:
    code, stdout, _ = run(
        [config.docker_bin, "stats", "--no-stream", "--format", "{{json .}}"],
        timeout=45,
    )
    if code != 0:
        return {}
    stats: dict[str, dict] = {}
    for line in stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = raw.get("Name", "")
        if not name or name in config.exclude_containers:
            continue
        stats[name] = {
            "cpu_pct": _pct(raw.get("CPUPerc", "0%")),
            "mem_mb": _mem(raw.get("MemUsage", "0B / 0B").split("/")[0]),
            "mem_limit_mb": _mem(raw.get("MemUsage", "0B / 0B").split("/")[-1]),
            "net_rx_kb": _mem(raw.get("NetIO", "0B / 0B").split("/")[0]) * 1024,
            "net_tx_kb": _mem(raw.get("NetIO", "0B / 0B").split("/")[-1]) * 1024,
        }
    return stats


def restart_counts() -> dict[str, int]:
    code, stdout, _ = run(
        [config.docker_bin, "ps", "-a", "--format", "{{.Names}}", "--filter", "status=running", "--filter", "status=restarting"]
    )
    if code != 0:
        return {}
    counts: dict[str, int] = {}
    for name in [n for n in stdout.split() if n and n not in config.exclude_containers]:
        c, out, _ = run([config.docker_bin, "inspect", "-f", "{{.RestartCount}}", name])
        if c == 0 and out.strip().isdigit():
            counts[name] = int(out.strip())
    return counts


def _pct(value: str) -> float:
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _mem(value: str) -> float:
    """Docker memory strings -> MB."""
    v = value.strip()
    m = re.match(r"([\d\.]+)\s*([KMGT]?i?B)", v, re.I)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2).upper().replace("I", "")
    factor = {"B": 1 / 1_048_576, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1_048_576.0}
    return round(num * factor.get(unit, 1.0), 2)


# --------------------------------------------------------------------------- #
def container_logs(name: str, *, since: str = "5m", tail: int | None = None) -> str:
    code, stdout, stderr = run(
        [config.docker_bin, "logs", "--timestamps", "--since", since, "--tail", str(tail or config.log_tail_lines), name],
        timeout=45,
    )
    if code != 0:
        return ""
    # Docker interleaves stdout/stderr here; the parser handles both.
    return (stdout or "") + ("\n" + stderr if stderr and "Error" not in stderr[:40] else "")


class LogTailer:
    """Byte-offset tailer that survives restarts and handles rotation."""

    def __init__(self, state_dir: str):
        self.state_path = Path(state_dir) / "log_offsets.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.offsets = json.loads(self.state_path.read_text())
        except Exception:
            self.offsets = {}

    def read_new(self, path: str, *, max_bytes: int = 512_000) -> str:
        p = Path(path)
        if not p.exists():
            return ""
        try:
            size = p.stat().st_size
        except OSError:
            return ""
        offset = self.offsets.get(path, 0)
        if size < offset:
            offset = 0  # rotated
        if size == offset:
            return ""
        if size - offset > max_bytes:
            offset = size - max_bytes  # skip ahead rather than flood
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(offset)
                data = fh.read()
                self.offsets[path] = fh.tell()
        except OSError:
            return ""
        self._save()
        return data

    def _save(self):
        try:
            self.state_path.write_text(json.dumps(self.offsets))
        except OSError:
            pass


def nginx_log_paths() -> list[tuple[str, str]]:
    """[(path, kind)] for ViljaOps-managed per-project logs plus the global error log."""
    out: list[tuple[str, str]] = []
    d = Path(config.nginx_log_dir)
    if not d.exists():
        return out
    for p in sorted(d.glob("*.error.log")):
        out.append((str(p), "nginx_error"))
    for p in sorted(d.glob("*.access.log")):
        out.append((str(p), "nginx_access"))
    global_err = d / "error.log"
    if global_err.exists():
        out.append((str(global_err), "nginx_error"))
    return out[:40]
