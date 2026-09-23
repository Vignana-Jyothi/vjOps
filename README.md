# ViljaOps

**AI DevOps intelligence for a university startup incubator.**

Student teams transfer a repo into the org, a self-hosted runner builds it, Docker runs it,
Nginx fronts it. When something breaks, one person with DevOps knowledge has to go read four
different log streams and work out why.

ViljaOps does that reading. It analyses repositories before deployment, diagnoses failures by
correlating GitHub Actions + Docker + Nginx + application logs with resource metrics, and
proposes fixes that a human approves before anything runs. It also takes over the two jobs
currently done by hand: **assigning ports** and **writing Nginx configs**.

Everything runs on your own hardware. No logs leave the campus network.

---

## The three layers

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  LAYER 1 — BEFORE DEPLOY        "Can we deploy this?"            │
   │  Clone → detect stack → scan secrets → score /100 → plan         │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                    GitHub Actions ▼ self-hosted runner
   ┌──────────────────────────────────────────────────────────────────┐
   │  LAYER 2 — DURING DEPLOY        "Why did it fail?"               │
   │  Actions + Docker + Nginx + app logs + metrics                   │
   │            ↓ correlate → signature match → LLM → validate        │
   │  ROOT CAUSE + EVIDENCE + CONFIDENCE + FIX (approval required)    │
   └──────────────────────────────────────────────────────────────────┘
                                   │
   ┌──────────────────────────────────────────────────────────────────┐
   │  LAYER 3 — AFTER DEPLOY         "What will fail next?"           │
   │  Memory trend → OOM prediction · restart loops · disk · 5xx      │
   │  Project risk signals → mentor triage                            │
   └──────────────────────────────────────────────────────────────────┘
```

---

## What it actually produces

A student pushes. The build succeeds, the container dies, Nginx starts returning 502.
Instead of "❌ Deployment failed":

```
Container killed by the kernel out-of-memory killer
root cause : The container exceeded its memory limit and was killed (exit 137).
confidence : 0.94  | source: signature | stage: runtime

student explanation:
  Your app used more memory than it was allowed and the server stopped it.
  This usually happens when you load a big file or model fully into memory.

evidence cited:
  [E4] docker   team-alpha exited with code 137

cross-source correlation:
  proxy_follows_container_death   Nginx started returning 502 within 0s of the
                                  container dying. The proxy is a symptom; the
                                  container is the cause.
  coverage                        Correlated 2 actions, 2 docker, 1 nginx events.

proposed fixes (none executed):
  [moderate ] set_memory_limit   Raise the memory limit for this container
  [safe     ] code_change        Stream or paginate instead of loading everything
```

That output is from an actual run of this codebase, with the language model **switched off** —
the deterministic signature layer produced it alone.

---

## Two design decisions worth knowing up front

**1. The LLM is an enricher, never a dependency.**
Roughly 30 failure signatures cover most of what actually goes wrong in a student incubator.
Those match deterministically, with extracted values (which port, which package, which env var)
and a fix mapped to an allowed action. The model ranks candidates, writes the explanation, and
handles the long tail. When the GPU node is down, `/api/system/status` reports degraded mode and
diagnosis continues. A DevOps tool that stops working when a model server reboots is worse than
no tool.

**2. Nothing runs without a named human approving it.**
The model proposes an `action_type`. If it isn't in the whitelist (`app/rca/fixes.py`), the
proposal is downgraded to an escalation. Dangerous actions — stopping another team's container,
setting a secret, running migrations, rotating credentials — are flagged `never_auto` and cannot
run unattended even with `AUTO_REMEDIATION=true`. The agent has **no general shell**: it
implements twelve specific handlers and refuses anything else. Every execution records who
approved it, the exact parameters, and the output.

The model is also checked. Evidence it cites is validated against the real timeline; citations
that don't exist are dropped and confidence is penalised. If the rule engine is highly confident
and the model disagrees, the rules win.

---

## Quickstart

```bash
git clone <this repo> viljaops && cd viljaops
cp .env.example .env          # set JWT_SECRET, GITHUB_TOKEN, admin password
make up                       # postgres + redis + ollama + control plane + worker + UI
make models                   # pull qwen2.5-coder:14b + nomic-embed-text (once)
make seed                     # seed the knowledge base from the signature library
```

Dashboard at `http://localhost:5173`, API docs at `http://localhost:8000/docs`.
Sign in with `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`, then change the password.

**Point it at a GPU box** — Ollama and vLLM both speak the OpenAI `/v1` API, so it's one variable:

```bash
LLM_BASE_URL=http://gpu-node.internal:8000/v1
LLM_MODEL=Qwen/Qwen2.5-Coder-14B-Instruct
```

### Onboard a deployment server

1. Dashboard → Infrastructure → **Enroll server**. Copy the token (shown once).
2. On that server:
   ```bash
   sudo VILJAOPS_URL=https://ops.vnrvjiet.in \
        VILJAOPS_AGENT_TOKEN=vops_xxx \
        bash agent/install.sh
   ```
   Set `VILJAOPS_DRY_RUN=true` in `/etc/viljaops/agent.env` for a first run that reports what it
   *would* do without touching anything.

The agent connects outbound only — no inbound port needs opening on your deployment servers.

### Onboard a student project

1. Dashboard → Projects → **Register project** (slug, repo, domain, server).
2. Run a readiness analysis. Fix the blockers it reports.
3. The team adds a 12-line caller workflow (see `docs/STUDENT_GUIDE.md`) that calls the reusable
   workflow in `.github/workflows/viljaops-deploy.yml`.

From then on: ports are assigned automatically, Nginx is generated and validated automatically,
and failures are diagnosed automatically.

---

## How this replaces the manual steps

| Today | With ViljaOps |
|---|---|
| SSH in, `ss -tlnp`, pick a free port by eye | Port registry allocates from the server's range in one transaction against a unique constraint. Redeploys keep their port. Ports taken outside the platform are adopted from the agent's observations so they're never handed out twice. |
| Hand-write an Nginx vhost, hope `nginx -t` passes | Rendered from a template, statically linted, staged, `nginx -t`, swapped, `nginx -t` again, reloaded. Any failure restores the previous file and re-tests. Two teams cannot claim one domain. |
| Read four log streams to find out what broke | One correlated timeline, deduplicated by fingerprint, with the cross-source findings made explicit. |
| Explain the same ten mistakes every semester | Each diagnosis carries a plain-language explanation written for someone who has never used Docker. |

---

## Repository layout

```
control-plane/app/
  analyzers/     Layer 1 — detection, secret scanning, scoring, deployment plan,
                 generated Dockerfile/.dockerignore/.env.example/workflow
  rca/           Layer 2 — signature library, log collectors, correlation,
                 engine, and the action whitelist
  observability/ Layer 3 — anomaly detection (MAD z-score + trend), project risk
  infra/         Port registry, Nginx generation and safe-apply protocol
  llm/           OpenAI-compatible client (Ollama/vLLM), prompts, embeddings
  rag/           pgvector store + knowledge-base seeding
  dataset/       Anonymization and JSONL export for fine-tuning
  routers/       HTTP API
agent/           The daemon that runs on each deployment server
frontend/        React dashboard
tests/           80 tests
```

---

## Where the GPUs earn their place

Not "we have GPUs, let's use them". Three concrete jobs:

1. **Explanation and long-tail diagnosis.** A 14B coder model reads a correlated timeline and the
   rule engine's candidates, then picks, refines, and explains. Ranking candidates is far more
   reliable at this size than open-ended generation, which is why the prompt is built that way.
2. **Repository review.** Architectural risk that static analysis can't see.
3. **Eventually, a model specific to your environment.** Every incident a DevOps engineer confirms
   is stored with its correlated logs and the *actual* cause. `make export-dataset` produces
   anonymized train/eval JSONL split by failure signature, plus a data card.

On the last point — don't start by training. Start by confirming incidents. The dashboard tracks
how often the AI's first diagnosis was right (`/api/incidents/meta/accuracy`), broken down by
signature. That honest scoreboard tells you where a fine-tune would actually help, and around
200–300 confirmed incidents is where it starts to beat prompting. Until then the eval split is
already valuable on its own.

**Anonymization is not an afterthought.** Secrets are destroyed outright; emails, hostnames, IPs,
home paths and repo names are pseudonymized consistently within each record and inconsistently
across records. Technical structure — `ModuleNotFoundError`, `psycopg2`, `0.0.0.0:3000`,
`localhost:5432` — is deliberately preserved, because a dataset that scrubs the port teaches
nothing about port conflicts. `GET /api/dataset/preview` shows exactly what would leave the
building before you export anything.

---

## On the mentor board

Layer 3 scores **projects**, never students. Every signal is an engineering artifact the team
produces publicly: deployment outcomes, incident recurrence, resource anomalies. No message
reading, no individual attribution, no ranking of people. The output is always framed as "this
project needs X support", the strongest signal is *the same failure recurring* (a team retrying
rather than diagnosing — exactly the case where 30 minutes of help unblocks them), and the risk
history is visible to the team itself.

If that framing slips in practice, the feature is doing harm rather than good. It's worth
reviewing with students before switching it on.

---

## Tests

```bash
make test     # 80 tests, no external services required
```

They run with the model backend pointed at a dead port, so the degraded path is what's under test
by default. Coverage includes real log samples for every signature, port-allocation races and
exhaustion, Nginx lint and domain conflicts, the approval gate, anonymization, and a full
API lifecycle from `deployments/start` through to agent command dispatch.

---

## Current limits

- Signatures cover the common cases; novel failures escalate rather than guess, by design.
- Anomaly detection is statistical (MAD z-score + least-squares trend), not learned. It needs no
  training data and explains itself, but it won't catch subtle multivariate patterns.
- The agent manages Docker and Nginx. Kubernetes is not supported.
- `run_migration` executes inside the app's own container. It is never automatic and can lose
  data — treat the approval as the real safeguard.
- Nginx rollback is verified by re-running `nginx -t` after restoring. If the restore itself
  fails, the error says so explicitly and asks for a human. That case needs a manual runbook.
