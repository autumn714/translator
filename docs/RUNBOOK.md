# Runbook

All commands assume the current working directory is `$PROJECT_ROOT` (the project clone root). `$MODEL_PATH` is the absolute host path to the gemma-4-E4B-it model directory, set in `.env`.

## Startup

```bash
docker compose up -d --build
```

Default vLLM image tag is `gemma4`. `docker compose up -d --build` uses the locally cached `gemma4` image if it already exists. Run `docker compose pull vllm` only when you intentionally want to refresh that tag.

Before starting, verify the rendered command and volume mounts:

```bash
docker compose config
```

Expected `vllm` command shape:

```text
sh -lc ... vllm serve "$RUNTIME_DIR" --tokenizer "$RUNTIME_DIR" --served-model-name gemma-4-E4B-it --limit-mm-per-prompt '{"image":0,"audio":0}' --async-scheduling ...
```

Expected model bind mount (host path = whatever `MODEL_PATH` is set to):

```text
$MODEL_PATH:/models/source:ro
```

Expected vLLM work bind mount:

```text
./data/vllm-work:/vllm-work
```

To override the runtime image explicitly:

```bash
VLLM_IMAGE=vllm/vllm-openai:gemma4 docker compose up -d --build
```

Open:

```text
http://<server-ip>:7860
```

## Stop

```bash
docker compose stop
```

## Logs

```bash
docker compose logs -f --tail=200
```

Model server only:

```bash
docker compose logs -f --tail=200 vllm
```

If the model server fails to start, inspect what the container sees:

```bash
docker compose run --rm --no-deps --entrypoint sh vllm -lc "ls -lah /models/source && ls -lah /vllm-work && ls -lah /vllm-work/runtime-model || true"
```

## Clean Remove

Project-scoped Docker cleanup only:

```bash
docker compose down --volumes --remove-orphans
```

Then remove the project directory itself:

```bash
rm -rf $PROJECT_ROOT
```

## Important Notes

- The clean sequence above does not remove the separately managed model directory at `$MODEL_PATH`.
- Commands that can affect other containers or other projects' Docker cache are intentionally excluded.
- Docker daemon storage (images, metadata, logs) normally lives outside `$PROJECT_ROOT`, typically under Docker's own data root.
- Deleting `$PROJECT_ROOT` plus `docker compose down --volumes --remove-orphans` cleans this project safely, but does not guarantee a globally pristine Docker engine state.
- On first startup, `translator-vllm` can take several minutes to load the FP16 model from HDD and initialize GPU memory.
- `HF_HOME`-related log lines are deprecation warnings, not the direct cause of model startup failure.
- The runtime shim under `data/vllm-work` uses symlinks to the original model files, so it does not duplicate the 14 GB model file on host storage.
- `data/vllm-work/triton` and related cache directories can grow over time. Deleting `data/vllm-work` is safe when the stack is stopped; files are recreated on next start.

## Remove Unused vLLM Image

If you previously pulled an older vLLM image and no longer need it:

```bash
docker image rm vllm/vllm-openai:latest
```
