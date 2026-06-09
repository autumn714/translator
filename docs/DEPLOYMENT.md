# Deployment Guide

## 1. Server Layout

All project-owned files live under a single directory on the host, referred to here as `$PROJECT_ROOT`. Pick any location appropriate for your environment.

Recommended structure:

```text
$PROJECT_ROOT
  compose.yaml
  Dockerfile.app
  translator_app/
  data/
    glossary/
      default.json
      glossary-*.json
    vllm-work/        # created on first start
```

The model directory lives **outside** `$PROJECT_ROOT`. Set its absolute path via the `MODEL_PATH` environment variable in `.env`:

```text
MODEL_PATH=/absolute/path/to/gemma-4-E4B-it
```

Putting the model on HDD is acceptable (see §6 below).

## 2. Initial Setup

```bash
git clone <repo-url> $PROJECT_ROOT
cd $PROJECT_ROOT
cp .env.example .env
# edit .env and set MODEL_PATH
```

## 3. Recommended Runtime

Docker Compose only.

Start:

```bash
docker compose up -d --build
```

If you explicitly want to refresh the mutable vLLM image first:

```bash
docker compose pull vllm
docker compose up -d --build
```

Stop:

```bash
docker compose stop
```

Logs:

```bash
docker compose logs -f --tail=200
```

Open:

```text
http://<server-ip>:7860
```

## 4. GPU Selection

Default is GPU `0`.

To target GPU `1`:

```bash
VLLM_GPU_DEVICE=1 docker compose up -d --build
```

`VLLM_GPU_DEVICE` may also be set in `.env`.

## 5. Runtime Topology

The compose stack starts:

- `translator-vllm` — vLLM model server
- `translator-app` — FastAPI app + static UI

One-command deployment, but operationally separated from the model process.

## 6. Model Runtime

Current default:

- Model: `google/gemma-4-E4B-it`
- Precision: native FP16
- Runtime: `vllm/vllm-openai:gemma4`
- API style: OpenAI-compatible `/v1/chat/completions`
- Host model path: `$MODEL_PATH` (set in `.env`)
- Runtime work path (in-container): `/vllm-work`, bind-mounted from `$PROJECT_ROOT/data/vllm-work`

Gemma 4 E4B is a multimodal instruction model. It is launched here in text-only mode with `--limit-mm-per-prompt '{"image":0,"audio":0}'` so image and audio profiling are skipped.

The work directory also holds Triton and Hugging Face caches so compiled modules load from an executable bind mount instead of `/tmp`.

### HDD vs SSD

HDD is acceptable for this service.

- normal user request latency: usually unchanged once the model is loaded
- main impact: slower cold start / restart
- practical effect: noticeable mostly on first launch or after a restart

## 7. Glossary

Glossary files are stored under `$PROJECT_ROOT/data/glossary/` and surface as `/app/data/glossary/` inside the app container.

The UI can create multiple named glossaries and edit them directly.

Current implementation priority:

- direct vLLM path with chat-completions
- sentence-level parallel dispatch from the app backend

## 8. Minimal Start Sequence

1. Clone repo to `$PROJECT_ROOT`
2. `cp .env.example .env` and set `MODEL_PATH`
3. `docker compose up -d --build`
4. Open `http://<server-ip>:7860`

## 9. File Location Review

Persistent project-owned files:

- `$PROJECT_ROOT` (code, compose, docs)
- `$PROJECT_ROOT/data/glossary/`
- `$PROJECT_ROOT/data/vllm-work/`
- model files at `$MODEL_PATH` (host-supplied, never written to)

Not every Docker-generated file stays under `$PROJECT_ROOT`. The following normally live elsewhere:

- Docker images
- image layers
- build cache
- container metadata
- Docker-managed log files

These are stored under Docker daemon storage, typically `/var/lib/docker`, unless the administrator has changed Docker's `data-root`.
