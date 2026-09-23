"""ViljaOps server agent.

Runs on every deployment server. Three loops on one thread:

  heartbeat  — host facts, container stats, listening ports  (default 30s)
  commands   — poll and execute approved fixes               (default 15s)
  logs       — ship new docker + nginx log lines             (default 60s)

It initiates every connection outbound, so no inbound port has to be opened on
the deployment servers.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from .collectors import (
    LogTailer,
    container_logs,
    container_stats,
    host_facts,
    list_containers,
    listening_ports,
    nginx_log_paths,
    restart_counts,
)
from .config import AGENT_VERSION, config
from .executor import execute
from .transport import ControlPlane

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("viljaops.agent")

_running = True


def _stop(signum, frame):
    global _running
    log.info("Signal %s received — shutting down after the current cycle", signum)
    _running = False


def build_heartbeat() -> dict:
    facts = host_facts()
    containers = list_containers()
    stats = container_stats()
    restarts = restart_counts()

    metrics = []
    for c in containers:
        s = stats.get(c["name"], {})
        if not s and c["state"] != "running":
            # A stopped container still deserves a sample so restart loops show up.
            s = {}
        metrics.append(
            {
                "container_name": c["name"],
                "cpu_pct": s.get("cpu_pct", 0.0),
                "mem_mb": s.get("mem_mb", 0.0),
                "mem_limit_mb": s.get("mem_limit_mb", 0.0),
                "restarts": restarts.get(c["name"], 0),
                "disk_pct": facts["disk_pct"],
                "net_rx_kb": s.get("net_rx_kb", 0.0),
                "net_tx_kb": s.get("net_tx_kb", 0.0),
            }
        )

    return {
        "agent_version": AGENT_VERSION,
        "cpu_cores": facts["cpu_cores"],
        "ram_mb": facts["ram_mb"],
        "disk_gb": facts["disk_gb"],
        "disk_pct": facts["disk_pct"],
        "load_avg": facts["load_avg"],
        "observed_ports": listening_ports(),
        "containers": containers,
        "metrics": metrics,
    }


def ship_logs(cp: ControlPlane, tailer: LogTailer) -> int:
    shipped = 0

    for c in list_containers():
        if c["state"] not in ("running", "restarting", "exited"):
            continue
        text = container_logs(c["name"], since=f"{config.log_ship_interval + 30}s")
        if not text.strip():
            continue
        # The control plane matches container name -> project/deployment.
        if cp.ship_logs("docker", text, project_slug=c["name"], container=c["name"]):
            shipped += 1

    for path, kind in nginx_log_paths():
        text = tailer.read_new(path)
        if not text.strip():
            continue
        slug = path.split("/")[-1].split(".")[0]
        if cp.ship_logs(kind, text, project_slug=slug):
            shipped += 1

    return shipped


def main() -> int:
    problems = config.validate()
    if problems:
        for p in problems:
            log.error(p)
        return 2

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cp = ControlPlane()
    tailer = LogTailer(config.state_dir)

    log.info(
        "ViljaOps agent %s starting — control plane %s%s",
        AGENT_VERSION, config.control_plane, " [DRY RUN]" if config.dry_run else "",
    )

    last_hb = last_cmd = last_logs = 0.0

    while _running:
        now = time.time()

        if now - last_hb >= config.heartbeat_interval:
            last_hb = now
            resp = cp.heartbeat(build_heartbeat())
            if resp:
                pending = resp.get("pending_commands", 0)
                anomalies = resp.get("anomalies_detected") or []
                if pending:
                    log.info("Control plane has %d pending command(s)", pending)
                    last_cmd = 0  # poll immediately
                for a in anomalies:
                    log.warning("Anomaly detected: [%s] %s", a.get("severity"), a.get("message"))
                recon = resp.get("port_reconciliation") or {}
                for u in recon.get("untracked_adopted") or []:
                    log.info("Adopted untracked port %s (%s) into the registry", u.get("port"), u.get("process"))

        if now - last_cmd >= config.command_poll_interval:
            last_cmd = now
            for cmd in cp.poll_commands():
                log.info("Executing approved command %s: %s", cmd["id"], cmd["kind"])
                result = execute(cmd)
                level = log.info if result.get("ok") else log.error
                level(
                    "Command %s %s%s",
                    cmd["kind"],
                    "succeeded" if result.get("ok") else "FAILED",
                    "" if result.get("ok") else f": {result.get('error', '')[:300]}",
                )
                cp.submit_result(
                    cmd["id"],
                    bool(result.get("ok")),
                    output=result.get("output", "") or result.get("nginx_test_output", ""),
                    error=result.get("error", ""),
                    detail=result.get("detail", {}),
                )

        if now - last_logs >= config.log_ship_interval:
            last_logs = now
            try:
                n = ship_logs(cp, tailer)
                if n:
                    log.debug("Shipped log batches: %d", n)
            except Exception:
                log.exception("Log shipping cycle failed")

        time.sleep(2)

    log.info("Agent stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
