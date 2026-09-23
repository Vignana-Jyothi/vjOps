.PHONY: up down logs models test dev-api dev-ui seed fmt export-dataset train eval-model

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f control-plane worker

## Pull the self-hosted models into the Ollama volume (run once)
models:
	docker compose exec ollama ollama pull $${LLM_MODEL:-qwen2.5-coder:14b}
	docker compose exec ollama ollama pull $${EMBED_MODEL:-nomic-embed-text}

test:
	cd control-plane && python -m pytest ../tests -q

dev-api:
	cd control-plane && uvicorn app.main:app --reload --port 8000

dev-ui:
	cd frontend && npm run dev

seed:
	docker compose exec control-plane python -m app.rag.seed_kb

export-dataset:
	docker compose exec control-plane python -m app.dataset.export --out /var/lib/viljaops/repos/incidents.jsonl

## Phase 5 — run on the GPU training node, not inside the control-plane container.
## pip install -r control-plane/scripts/requirements-train.txt --break-system-packages
train:
	cd control-plane && python scripts/train_lora.py \
		--data-card /var/lib/viljaops/repos/incidents.card.json \
		--out-dir /var/lib/viljaops/models/viljaops-rca-lora

## Compare a served model (base or fine-tuned) against the eval split.
## Override MODEL / LABEL / BASE_URL, e.g.:
##   make eval-model MODEL=viljaops-rca-lora LABEL=fine-tuned BASE_URL=http://gpu-node.internal:8000/v1
eval-model:
	cd control-plane && python scripts/eval_lora.py \
		--eval /var/lib/viljaops/repos/incidents.eval.jsonl \
		--model $${MODEL:-qwen2.5-coder:14b} --label $${LABEL:-base} \
		$(if $(BASE_URL),--base-url $(BASE_URL),)
