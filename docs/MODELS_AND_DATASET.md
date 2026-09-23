# Self-hosted models and the incident dataset

Everything here runs on your hardware. No logs, no source code and no incident data leave the
campus network.

---

## Model backends

Ollama and vLLM both expose an OpenAI-compatible `/v1` API, so switching is one environment
variable. Use Ollama to get started, vLLM when you want throughput on a real GPU.

```bash
# Ollama (bundled in docker-compose, good for a single GPU or CPU testing)
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5-coder:14b

# vLLM on a dedicated GPU node
LLM_BASE_URL=http://gpu-node.internal:8000/v1
LLM_MODEL=Qwen/Qwen2.5-Coder-14B-Instruct
LLM_API_KEY=whatever-you-configured
```

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-14B-Instruct \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90
```

### Choosing a size

| VRAM | Suggested | Notes |
|---|---|---|
| 8–12 GB | `qwen2.5-coder:7b` | Fine for explanations. Weaker at ranking candidates. |
| 16–24 GB | `qwen2.5-coder:14b` | The sweet spot. What the prompts are tuned for. |
| 40 GB+ | `qwen2.5-coder:32b` | Better on novel failures; diminishing returns on known ones. |

A correlated timeline plus candidates runs 4–10k tokens, so **16k context is the real floor**.

### Why the prompts look the way they do

Small models are much better at *ranking* than at open-ended generation. So the RCA prompt hands
the model the rule engine's candidate causes and asks it to pick, refine, and justify — not to
invent a diagnosis from raw logs. It's also forced to cite timeline IDs, which makes hallucinated
evidence visible and removable rather than persuasive.

`app/llm/prompts.py` is where to tune this. If you change a prompt, re-run the eval split before
and after — that's what it's for.

### Embeddings

`nomic-embed-text` via Ollama, used for incident retrieval. If it isn't pulled, embeddings fall
back to a deterministic hashed bag-of-words vector — worse retrieval, but it keeps working. On
Postgres with pgvector the search is a vector index; without it, an in-Python cosine fallback.

---

## The dataset

This is the part no general model has: **how deployments fail in your specific environment.**

### The pipeline

```
incident diagnosed  →  engineer confirms the real cause  →  anonymize  →  JSONL
                              ↓
                       retrieval index (helps immediately, no training)
```

Confirmation is the whole thing. An unconfirmed diagnosis is a guess, and training on your own
guesses compounds your own errors — `build_records` filters them out by default.

### What survives anonymization, and what doesn't

Destroyed outright (never pseudonymized, because a consistent alias for a live key still leaks its
structure): AWS keys, GitHub PATs, OpenAI/Anthropic keys, Slack tokens, Stripe live keys, private
keys, passwords inside connection strings.

Pseudonymized consistently **within** a record and inconsistently **across** records: emails,
internal hostnames, IPs, home directory paths, repository and org names. The same person maps to
one alias in a record whether they appear as `rakesh@vnrvjiet.in` or `/home/rakesh`.

Deliberately preserved: `ModuleNotFoundError`, `psycopg2`, `0.0.0.0:3000`, `localhost:5432`,
`/app/main.py`, exit codes, stack frames. Public registries (`pypi.org`, `github.com`,
`npmjs.com`) stay as-is.

That last group is the point. A dataset that scrubs the port number teaches nothing about port
conflicts.

```bash
GET  /api/dataset/preview        # see exactly what would leave, before exporting
POST /api/dataset/export
GET  /api/dataset/download/train
```

### The split

Stratified **by failure signature**, not randomly. Held-out examples are different instances of a
class the model has seen, so the eval measures generalization rather than memorization. Signatures
with fewer than three examples go entirely into train — one example of a class can't measure
anything.

A data card is written alongside, with class distribution and caveats.

### Diagnosis vs. outcome

Each record's `metadata.intervention` records what actually happened after the diagnosis — which
fix(es) were approved, and whether the Verification Agent (see the runbook) found they held
(`verified`, `failed_verification`, `applied_unverified`, `proposed_not_approved`,
`no_remediation_attempted`). This is provenance for evaluating the pipeline end to end — "of the
incidents diagnosed as X and given fix Y, how many actually held" — not a second training target. It
never appears inside `messages`, only in `metadata`: the model still has to produce a diagnosis from
logs alone, the same information available at inference time, before any outcome exists.

### When to actually fine-tune

**Not yet.** In order:

1. **Confirm incidents** for a semester. Watch `/api/incidents/meta/accuracy`.
2. **Use the eval split to fix prompts and signatures.** This is where the early wins are. If a
   signature's accuracy is poor, its regex is too broad — that's a code fix, not a training problem.
   `scripts/eval_lora.py` runs this comparison against whatever's already served — no GPU-hours spent:
   ```bash
   make eval-model MODEL=qwen2.5-coder:14b LABEL=base
   ```
3. **Around 200–300 confirmed incidents** spread across a good range of signatures, a LoRA fine-tune
   on a 7B/14B coder model starts to beat prompting. Below that you'll mostly memorize.
   `scripts/train_lora.py` (`make train`) enforces this ordering itself — it refuses to run below the
   threshold or a decent spread of signatures unless you pass `--force`, because a fine-tune on too
   little data doesn't just fail to help, it quietly makes things worse in a way that's easy to miss.
   It reads the `*.card.json` and `*.jsonl` files `app.dataset.export` already writes, trains a LoRA
   adapter with the model's own chat template (so training format matches serving format), and writes
   a `training_run.json` alongside the adapter recording exactly what it was trained on.
4. **Before switching production traffic to it**, serve the adapter under its own model tag (Ollama
   and vLLM both support this) and run `scripts/eval_lora.py` again with both `--model` flags to get
   a real base-vs-fine-tuned comparison on the same eval split, not a vibe check:
   ```bash
   make eval-model MODEL=viljaops-rca-lora LABEL=fine-tuned BASE_URL=http://gpu-node.internal:8000/v1
   ```

Runs on GPU #2 (see the two-server split above) — install the heavier deps first:
```bash
pip install -r control-plane/scripts/requirements-train.txt --break-system-packages
```

Expect the class balance to be skewed — port conflicts, missing dependencies and localhost
references dominate real incubator traffic. That's honest signal about your environment, but it
means a naive fine-tune will over-predict those three. Weight or resample deliberately.

Keep the rule engine either way. It's faster, free, explainable, and it's what runs when the GPU
node is rebooting.

### Honest caveats for anyone who publishes this

- One incubator's infrastructure. Not representative of DevOps generally.
- Labels are the confirming engineer's judgement, which can be wrong.
- Class balance follows real incident frequency, not a designed distribution.

All three are written into the data card automatically.
