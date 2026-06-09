# Translator App

Internal multilingual translation app with automatic preview UX.

- Backend: FastAPI
- Translation engine: `google/gemma-4-E4B-it` served on vLLM (OpenAI-compatible API)
- Deployment: Docker Compose (single command)
- GPU target: single `Tesla V100 32GB`, native FP16

## Features

- Automatic translation after typing pause
- Parallel chunk translation against the model backend
- Request debounce and cancellation
- Aligned source/translation segment view
- Server-stored named glossaries editable in the UI
- Two-container runtime with one command

## Quick Start

### 1. Prerequisites

- Docker Engine with Compose v2
- NVIDIA Container Toolkit and a CUDA-capable GPU (FP16; 32 GB VRAM recommended)
- A local copy of the `google/gemma-4-E4B-it` model directory — see [docs/MODELS.md](docs/MODELS.md) for the file list and download source

### 2. Clone and Configure

Clone the repository to any location on the host (this directory is referred to as `$PROJECT_ROOT` below), then create a `.env` from the template:

```bash
cp .env.example .env
```

Edit `.env` and set the one required variable, `MODEL_PATH`, to the absolute host path of your model directory:

```
MODEL_PATH=/absolute/path/to/gemma-4-E4B-it
```

All other settings have sensible defaults.

### 3. Run

```bash
docker compose up -d --build
```

Open `http://<server-ip>:7860`.

To target a non-default GPU index:

```bash
VLLM_GPU_DEVICE=1 docker compose up -d --build
```

### 4. Stop

```bash
docker compose stop
```

## Layout

```
$PROJECT_ROOT
  compose.yaml
  Dockerfile.app
  translator_app/        # FastAPI app
  data/
    glossary/            # server-stored glossaries, editable from the UI
    vllm-work/           # runtime cache + symlinks (created on first start)
```

- `data/glossary/` is bind-mounted into the app container at `/app/data/glossary`.
- `data/vllm-work/` holds the vLLM runtime shim, Triton cache, and HF cache. Safe to delete when the stack is stopped — it will be recreated on next start.
- The model directory at `MODEL_PATH` is bind-mounted **read-only**; original files are never written to.

## Docs

- [Deployment Guide](docs/DEPLOYMENT.md)
- [Model Guide](docs/MODELS.md)
- [Offline Notes](docs/OFFLINE_SETUP.md)
- [Runbook](docs/RUNBOOK.md)

## Notes

- Host-level `pip` / `uv` are not required — the app image is built inside Docker.
- Leave `CORS_ALLOW_ORIGINS` empty for same-origin use. Set it only when cross-origin access is required.
- Default container paths (`/app`, `/vllm-work`, `/models/source`) are fixed; you only need to set host-side paths via `.env`.
- Docker-generated image layers and build cache live under Docker's own data root, not under `$PROJECT_ROOT`.
