# DevOps runbook

For the people who own the servers.

---

## Rollout order

Turn this on gradually. Each stage is useful on its own, and each one earns trust for the next.

1. **Read-only.** Enroll agents with `VILJAOPS_DRY_RUN=true`. Logs, metrics and diagnoses flow;
   nothing executes. Run readiness analyses on existing projects. Watch whether the diagnoses
   match what you'd have concluded yourself.
2. **Port registry.** Reconcile against what's actually listening (`POST /api/agents/heartbeat`
   does this automatically) and let new deployments draw from it. This alone removes the manual
   port hunt.
3. **Nginx generation.** Use `POST /api/infra/nginx/preview` and read the output before applying
   anything. Once the rendered configs look right, let the platform apply them.
4. **Approved fixes.** Turn off dry run. Fixes still require a human click.
5. **Auto-remediation** (`AUTO_REMEDIATION=true`) — only if you want it. It permits `safe`,
   reversible actions only (prune, reload). Dangerous actions never run unattended regardless.

---

## The safety model, precisely

**The agent has no general shell.** It implements twelve handlers in
`agent/viljaops_agent/executor.py`. A command whose `kind` isn't one of them is refused and
reported back as an error. There is deliberately no "run arbitrary command" path, because that
would make the control plane's whitelist meaningless.

**The control plane decides; the agent obeys.** All parameters are computed server-side. The agent
receives exact instructions, never intent.

**Three categories:**

| Risk | Meaning | Examples |
|---|---|---|
| `safe` | read-only or trivially reversible | `prune_docker`, `reload_nginx`, `prune_logs` |
| `moderate` | restarts, config reloads | `restart_container`, `reassign_port`, `apply_nginx_config` |
| `dangerous` | data loss possible, `never_auto` | `run_migration`, `rotate_secret`, `stop_conflicting_container` |

Risk is assigned by the catalog, never by the model. If the model proposes an action outside the
whitelist, the proposal is rewritten to `escalate_to_devops` with the original reasoning attached.

**Platform containers are protected.** Anything in `VILJAOPS_EXCLUDE` is refused by the agent.

---

## Verification Agent — apply is not the same as fixed

Every `ActionSpec` in `rca/fixes.py` declares a `verify` criterion (e.g. `"no OOM kill for 15 min"`).
Historically nothing checked it: a fix was marked `succeeded` the moment the agent's shell command
returned exit 0, which only proves the command ran. `app/remediation/verify.py` closes that gap.

**How it resolves each fix:**

- **Immediate actions** (`rebuild_image`, `apply_nginx_config`, `reload_nginx`, `run_migration`) — the
  command's own exit code *is* the verify criterion. Resolves to `passed`/`failed` instantly, no wait.
- **Observed actions** (`restart_container`, `set_memory_limit`, `reassign_port`, ...) — go to
  `pending` and are watched for a grace window (3 min default, 15 min for `set_memory_limit`, per
  `settings.verify_grace_minutes*`). A Celery beat task (`viljaops.verify_fixes`, every minute)
  resolves anything whose window has elapsed:
  - `passed` — no matching anomaly, no fresh deployment failure, no restart-count regression.
    `Incident.auto_verified` is set. This is *not* the same as a human confirming the diagnosis for
    the training set (`was_ai_correct`) — it only means the fix operationally held.
  - `failed` — a matching `Anomaly` (see `WATCH_KINDS` in `verify.py`) or a new failed deployment
    appeared since the fix ran. The incident reopens to `diagnosed`, and an `escalate_to_devops` fix
    is proposed automatically, with the evidence attached — never a silent retry loop.
  - `unknown` — the grace window elapsed with zero telemetry from the agent at all. One extension is
    granted before giving up; this is surfaced to a human rather than assumed fine.
- A DevOps engineer can force a decision early with `POST /api/incidents/fixes/{id}/verify` (the
  "Check now" button in the incident view), instead of waiting for the beat schedule.

**Agent trace.** `app/tracing.py` writes one line per pipeline stage (Repository Agent on readiness
analysis, Incident Investigator on diagnosis, Remediation Agent on approval, Verification Agent on
the outcome above) to `agent_traces`. `GET /api/incidents/{id}/trace` returns it in order — it's a
narration of the existing code paths, not a separate decision-making layer, so there's nothing to
keep in sync by hand.

**Level 2 — affirmative health checks.** Absence of a bad anomaly is not the same as confirmed
health. `start_verification` dispatches one `health_check` agent command (plain HTTP GET against
`Project.health_path`, default `/health`, on the deployment's mapped port) right after a fix
executes. A non-2xx or unreachable result is decisive evidence and fails verification immediately,
without waiting out the grace window. This is a single snapshot taken shortly after the fix, not
continuous uptime monitoring — a supplementary signal, not a replacement for real monitoring.

**Rollback proposals.** `ActionSpec.reversible` existed since the first version of the action
catalog but was only ever shown in the UI. It's acted on now: if a reversible action's own agent
handler captured the pre-change value (see `PREVIOUS_VALUE_FIELD` in `verify.py` — currently just
`set_memory_limit`, which now inspects the container's existing limit before changing it), a failed
verification automatically proposes a same-action fix restoring that value. It still requires human
approval like anything else — this is not an automatic rollback.

**Blast radius.** `ActionSpec.scope` is `"project"` or `"shared"`. Nothing in the current catalog is
`"shared"` yet — every handler only ever touches the calling project's own container or nginx entry
— but `approve_fix` already enforces it: a `"shared"` action is rejected unless the approval request
includes `params.confirm_shared: true`. This exists so the day a shared-Redis/shared-Postgres action
gets added, the extra confirmation is already there rather than being a thing to remember.

---

## Nginx apply protocol

A bad config takes down every site on the box, so:

1. Render from template → static lint in the control plane (braces, `proxy_pass`, semicolons)
2. **`nginx -t` before touching anything** — if the config is *already* broken, the agent refuses
   to proceed and asks for a human. It will not be blamed for a pre-existing break.
3. Back up the current file, write to `.staging`, atomic rename into place
4. `nginx -t` → fail means restore the backup, `nginx -t` again, reload, and report
   `ROLLBACK: restored … nginx -t after rollback: PASS`
5. Pass means `nginx -s reload`

A checksum travels with the config; the agent refuses to write one that was altered in transit.

**If a rollback itself fails**, the result says `MANUAL INTERVENTION REQUIRED` and names the file.
Restore from the `.viljaops-bak-*` backup beside it and run `nginx -t` by hand.

---

## Port registry

- Allocation is one transaction against a `UNIQUE(server_id, port)` constraint. Two deployments
  racing cannot get the same port — the loser retries the next candidate.
- Reserved and never allocated: 22, 25, 80, 443, 3306, 5432, 6379, 8080, 9090, 9100, 11434, 27017.
- A redeploy **reuses its project's existing port**, so the Nginx config doesn't churn.
- A failed deployment releases its port immediately.
- Each heartbeat reconciles the registry against `ss -tlnp`. Ports held outside the platform are
  adopted as `reserved` with a note, so they're never handed out.
- `possible_stale_allocations` in the reconcile output lists ports the registry thinks are in use
  but nothing is listening on — review these periodically and release them.

Widen a range in the dashboard when utilisation passes ~80%; `POST /api/deployments/start` returns
**507** with a specific message when a server is full.

---

## Common operations

```bash
make logs                                    # control plane + worker
docker compose exec control-plane python -m app.rag.seed_kb --force   # re-seed KB
make export-dataset                          # anonymized train/eval JSONL

journalctl -u viljaops-agent -f              # on a deployment server
systemctl restart viljaops-agent
```

**Rotate an agent token:** dashboard → Infrastructure → the server → rotate. Update
`/etc/viljaops/agent.env` and restart the agent. The old token stops working immediately.

**A server shows offline but the box is up:** the agent daemon is down. Note that a running CI job
does *not* mark a server online — only the agent's own heartbeat/poll/result calls do, precisely so
a dead agent can't look healthy while approved fixes queue forever.

**Inference is down:** `/api/system/status` reports `degraded_mode`, the dashboard shows a banner,
and the signature engine keeps diagnosing. Nothing else is affected. Fix it when convenient.

---

## Keeping the diagnoses honest

Confirm root causes as you resolve incidents. It takes thirty seconds and does three things:
closes the incident, adds a record to the retrieval index so the next instance of that class is
diagnosed better, and creates a labelled dataset example.

`GET /api/incidents/meta/accuracy` reports how often the AI's first diagnosis was right, split by
analysis source and by signature. **Watch this number.** If a signature's accuracy is poor, its
regex is matching too broadly — fix the signature in `app/rca/signatures.py` rather than tolerating
it. Confidence that isn't calibrated is worse than no confidence score at all.

---

## Adding a signature

The highest-value maintenance task. When an unmatched failure turns out to be a recurring pattern,
add it to `SIGNATURES` in `app/rca/signatures.py`:

```python
S(
    key="celery_broker_unreachable",
    title="Celery cannot reach its broker",
    stage="runtime",
    severity="high",
    patterns=(r"consumer: Cannot connect to (?P<url>\S+)",),
    root_cause="The worker cannot reach its message broker at {url}.",
    explanation="...",              # for a reviewer
    student_explanation="...",      # for a second-year who has never used Docker
    fixes=(F("set_env_var", "Point CELERY_BROKER_URL at the platform Redis",
             "...", params={"key": "CELERY_BROKER_URL", "value": ""}),),
    sources=("docker",),
    confidence=0.9,
)
```

Then add a test in `tests/test_signatures.py` using a **real log line**, and re-run `make seed` so
it enters the retrieval index. A test asserts every fix references a real action, and another
asserts no unmatched `{placeholder}` leaks into user-facing text.

---

## Backups

- **Postgres** holds everything that matters: incidents, confirmed root causes, the port registry,
  Nginx config history, and the audit log. Back it up. The confirmed incidents in particular are
  irreplaceable — they're the dataset.
- Log events and metrics are pruned on a schedule (30 and 14 days). Incidents are kept forever.
- The knowledge base can be rebuilt with `seed_kb --force`, but incident-derived documents cannot —
  they come from Postgres.
