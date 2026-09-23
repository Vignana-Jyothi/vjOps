"""LoRA fine-tune on the exported incident dataset.

Deliberately NOT wired into anything automatic. Per docs/MODELS_AND_DATASET.md
the right order is: confirm incidents for a while -> use the eval split to
fix prompts and signatures (cheap, no GPU-hours) -> only once you have
roughly 200-300 confirmed incidents across a decent spread of failure
signatures, fine-tune. This script enforces that ordering by default, with
a --force escape hatch for a deliberate experiment or demo.

Usage:
    python -m app.dataset.export --out /var/lib/viljaops/repos/incidents.jsonl
    python scripts/train_lora.py \
        --data-card /var/lib/viljaops/repos/incidents.card.json \
        --base-model Qwen/Qwen2.5-Coder-7B-Instruct \
        --out-dir /var/lib/viljaops/models/viljaops-rca-lora

Reads {out}.train.jsonl / {out}.eval.jsonl exactly as app/dataset/export.py
writes them: one JSON object per line with a `messages` list
(system/user/assistant) already in chat format, so the model is trained on
literally the same prompt shape it will be served with in production.

Heavy deps (torch/transformers/peft/trl) live in requirements-train.txt, not
the main control-plane image — this only needs to run on the training GPU.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("viljaops.train")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MIN_RECOMMENDED_TRAIN = 200
MIN_SIGNATURES = 4  # a fine-tune that has only seen 1-2 failure classes will not generalize


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _check_readiness(card: dict, force: bool) -> None:
    train_n = card.get("train", 0)
    sig_n = len(card.get("signatures", {}))
    problems = []
    if train_n < MIN_RECOMMENDED_TRAIN:
        problems.append(
            f"only {train_n} training records (the docs suggest ~{MIN_RECOMMENDED_TRAIN}+ before a "
            "fine-tune reliably beats prompting — below that you mostly memorize)"
        )
    if sig_n < MIN_SIGNATURES:
        problems.append(f"only {sig_n} distinct failure signature(s) represented in the export")
    if not card.get("confirmed_only", True):
        problems.append("the export included unconfirmed diagnoses — that trains on your own guesses")

    if not problems:
        return
    msg = "This dataset is not ready to fine-tune on:\n  - " + "\n  - ".join(problems)
    if force:
        log.warning("%s\n(--force given, continuing anyway)", msg)
    else:
        log.error(
            "%s\n\nUse `scripts/eval_lora.py` against the base model on the eval split to fix prompts/"
            "signatures instead — that's free and faster to iterate on. Pass --force if this is a "
            "deliberate experiment.",
            msg,
        )
        sys.exit(1)


def build_dataset(rows: list[dict], tokenizer):
    """One training string per record, built from the model's own chat
    template so the training format matches how it's prompted at inference
    time instead of an ad hoc format the tokenizer never sees again."""
    from datasets import Dataset

    texts = [tokenizer.apply_chat_template(r["messages"], tokenize=False) for r in rows]
    return Dataset.from_dict({"text": texts})


def main():
    ap = argparse.ArgumentParser(description="LoRA fine-tune ViljaOps' RCA model on confirmed incidents")
    ap.add_argument("--data-card", required=True, help="the *.card.json written by app.dataset.export")
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-Coder-7B-Instruct")
    ap.add_argument("--out-dir", default="/var/lib/viljaops/models/viljaops-rca-lora")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-seq-len", type=int, default=8192)
    ap.add_argument("--load-in-4bit", action="store_true", default=True)
    ap.add_argument("--no-4bit", dest="load_in_4bit", action="store_false")
    ap.add_argument("--force", action="store_true", help="skip the dataset-readiness check")
    args = ap.parse_args()

    card_path = Path(args.data_card)
    card = json.loads(card_path.read_text())
    _check_readiness(card, args.force)

    train_path = Path(card["files"]["train"])
    eval_path = Path(card["files"]["eval"])
    train_rows = _load_jsonl(train_path)
    eval_rows = _load_jsonl(eval_path)
    log.info("Loaded %d train / %d eval records from %s", len(train_rows), len(eval_rows), card_path.parent)

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    log.info("Loading base model %s ...", args.base_model)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quant_config = None
    if args.load_in_4bit:
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=quant_config,
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )

    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        # Standard attention + MLP projection targets for the Qwen/Llama family.
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    train_ds = build_dataset(train_rows, tokenizer)
    eval_ds = build_dataset(eval_rows, tokenizer) if eval_rows else None

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sft_config = SFTConfig(
        output_dir=str(out_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_ds else "no",
        bf16=True,
        max_seq_length=args.max_seq_len,
        dataset_text_field="text",
        report_to=[],
        gradient_checkpointing=True,
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        processing_class=tokenizer,
    )

    log.info("Training on %d examples (%d signature classes) ...", len(train_rows), len(card.get("signatures", {})))
    trainer.train()

    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))

    run_card = {
        "base_model": args.base_model,
        "data_card": str(card_path),
        "train_records": len(train_rows),
        "eval_records": len(eval_rows),
        "signatures": card.get("signatures", {}),
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "target_modules": lora_config.target_modules},
        "epochs": args.epochs,
        "learning_rate": args.lr,
    }
    (out_dir / "training_run.json").write_text(json.dumps(run_card, indent=2))
    log.info("Adapter saved to %s", out_dir)
    log.info(
        "Next: serve it (e.g. `vllm ... --enable-lora --lora-modules viljaops-rca-lora=%s`) and run "
        "scripts/eval_lora.py --model viljaops-rca-lora --model %s to see whether it actually beats "
        "the base model on the eval split before pointing production traffic at it.",
        out_dir, args.base_model,
    )


if __name__ == "__main__":
    main()
