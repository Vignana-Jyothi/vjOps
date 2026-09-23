"""Score one or more models against the eval split app.dataset.export wrote.

This is step 2 of docs/MODELS_AND_DATASET.md's ordering: "use the eval split
to fix prompts and signatures" — and, once a LoRA adapter exists, to decide
whether it actually beats the base model before pointing production traffic
at it. It reuses the exact same OpenAI-compatible client the production RCA
engine uses (app.llm.client), so an eval run and a real diagnosis go through
identical code — the only thing that changes is which `--model` name you
point at (Ollama/vLLM both let you serve a LoRA adapter under its own model
tag alongside the base model).

Usage:
    # Compare the base model against a served LoRA adapter
    python scripts/eval_lora.py --eval /var/lib/viljaops/repos/incidents.eval.jsonl \
        --model qwen2.5-coder:14b --label base \
        --model viljaops-rca-lora --label fine-tuned \
        --base-url http://gpu-node.internal:8000/v1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app.*` imports work standalone

from app.llm.client import LLMClient  # noqa: E402
from app.rca.fixes import ACTION_CATALOG  # noqa: E402

log = logging.getLogger("viljaops.eval")
logging.basicConfig(level=logging.INFO, format="%(message)s")

VALID_ACTIONS = set(ACTION_CATALOG.keys()) | {"code_change", "no_action"}


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _gold(record: dict) -> dict:
    return json.loads(record["messages"][2]["content"])


def score_model(client: LLMClient, records: list[dict]) -> dict:
    n = len(records)
    top1 = 0
    valid_action_refs = 0
    llm_failures = 0
    per_signature: dict[str, dict] = {}

    for r in records:
        system = r["messages"][0]["content"]
        user = r["messages"][1]["content"]
        gold = _gold(r)
        gold_sig = gold.get("signature_key", "novel")
        per_signature.setdefault(gold_sig, {"n": 0, "top1": 0})
        per_signature[gold_sig]["n"] += 1

        pred = client.complete_json(system, user, fallback={})
        if not pred.get("_llm_used"):
            llm_failures += 1
            continue

        pred_sig = pred.get("signature_key")
        if pred_sig == gold_sig:
            top1 += 1
            per_signature[gold_sig]["top1"] += 1

        fix_text = str(pred.get("fix", ""))
        # Loose check: does the predicted fix even reference a real action the
        # agent knows how to run, for records where the fix names one.
        if any(action in fix_text for action in VALID_ACTIONS):
            valid_action_refs += 1

    return {
        "model": client.model,
        "records": n,
        "llm_failures": llm_failures,
        "top1_accuracy": round(top1 / n, 3) if n else None,
        "fix_names_known_action": round(valid_action_refs / n, 3) if n else None,
        "per_signature": {
            k: {"n": v["n"], "top1_accuracy": round(v["top1"] / v["n"], 3)}
            for k, v in sorted(per_signature.items(), key=lambda kv: kv[1]["n"], reverse=True)
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Score model(s) against the ViljaOps RCA eval split")
    ap.add_argument("--eval", required=True, help="the *.eval.jsonl file from app.dataset.export")
    ap.add_argument("--base-url", default=None, help="overrides LLM_BASE_URL for every --model in this run")
    ap.add_argument("--model", action="append", dest="models", required=True, help="repeatable: one model tag to score")
    ap.add_argument("--label", action="append", dest="labels", default=[], help="repeatable: friendly name per --model, same order")
    ap.add_argument("--limit", type=int, default=None, help="score only the first N records (fast smoke test)")
    args = ap.parse_args()

    records = _load_jsonl(Path(args.eval))
    if args.limit:
        records = records[: args.limit]
    if not records:
        log.error("No eval records found at %s", args.eval)
        sys.exit(1)

    labels = args.labels + args.models[len(args.labels):]
    results = []
    for model, label in zip(args.models, labels):
        log.info("Scoring %s (%s) on %d eval records ...", label, model, len(records))
        client = LLMClient(base_url=args.base_url, model=model)
        result = score_model(client, records)
        result["label"] = label
        results.append(result)
        log.info(
            "  top-1 signature accuracy: %s   fixes naming a real action: %s   LLM failures: %d/%d",
            result["top1_accuracy"], result["fix_names_known_action"], result["llm_failures"], result["records"],
        )

    print(json.dumps(results, indent=2))

    if len(results) >= 2:
        base, *rest = results
        for r in rest:
            if base["top1_accuracy"] and r["top1_accuracy"] is not None:
                delta = r["top1_accuracy"] - base["top1_accuracy"]
                verdict = "beats" if delta > 0.02 else "does not clearly beat" if delta > -0.02 else "is worse than"
                log.info("%s %s %s on top-1 accuracy (%+.1f pts)", r["label"], verdict, base["label"], delta * 100)


if __name__ == "__main__":
    main()
