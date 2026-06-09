# Model Guide

## 1. Primary GPU Model

Recommended model:

- [google/gemma-4-E4B-it](https://huggingface.co/google/gemma-4-E4B-it)

Recommended runtime target:

- single `Tesla V100 32GB`
- native FP16

## 2. Server Placement

Copy the model directory to any absolute host path of your choice, then set that path in `.env`:

```text
MODEL_PATH=/absolute/path/to/gemma-4-E4B-it
```

The compose stack bind-mounts `$MODEL_PATH` read-only into the vLLM container at `/models/source`.

## 3. Files To Copy

Copy the full repository contents from the Hugging Face model page when possible.

Current main files visible in the repository:

- `chat_template.jinja`
- `config.json`
- `generation_config.json`
- `model.safetensors`
- `processor_config.json`
- `tokenizer.json`
- `tokenizer_config.json`

Source:

- [gemma-4-E4B-it Files](https://huggingface.co/google/gemma-4-E4B-it/tree/main)

## 4. Quick Verification On Server

Before starting Docker, verify that the model directory really contains the required files:

```bash
ls -lah "$MODEL_PATH"
```

At minimum, the directory should contain:

- `config.json`
- `generation_config.json`
- `model.safetensors`
- `processor_config.json`
- `tokenizer.json`
- `tokenizer_config.json`

If `tokenizer_config.json` or `processor_config.json` is missing, the runtime shim fails fast before vLLM starts:

```text
tokenizer_config.json not found in /models/source
```

Direct model source:

- [google/gemma-4-E4B-it](https://huggingface.co/google/gemma-4-E4B-it)

## 5. Tokenizer Metadata Note

This project handles tokenizer placement at runtime:

- source model directory stays read-only at `$MODEL_PATH` on the host
- container creates `/vllm-work/runtime-model/gemma-4-E4B-it` (backed by `$PROJECT_ROOT/data/vllm-work/runtime-model/gemma-4-E4B-it`)
- the runtime directory symlinks the original model repository contents
- Triton and Hugging Face cache files are also stored under `$PROJECT_ROOT/data/vllm-work`

## 6. Why FP16

Current project baseline:

- lower latency than the prior Seed-X setup
- one GPU only
- stable, simple runtime path

The default is the official Gemma 4 E4B instruction model with the official `gemma4` vLLM runtime image.

After the model is loaded, HDD placement usually does not make normal translation requests feel slower to end users. The main penalty is longer cold start or restart time.

## 7. Optional Future CPU Path

Reference candidate for a lightweight preview engine:

- [HPLT/translate-en-ko-v2.0-hplt_opus](https://huggingface.co/HPLT/translate-en-ko-v2.0-hplt_opus)

Known public files:

- `model.npz.best-chrf.npz`
- `model.en-ko.spm`
- `model.en-ko.vocab`

Not used as the primary runtime in this project.

## 8. References

- [Gemma 4 Usage Guide](https://docs.vllm.ai/projects/recipes/en/latest/Google/Gemma4.html)
- [google/gemma-4-E4B-it model card](https://huggingface.co/google/gemma-4-E4B-it)
