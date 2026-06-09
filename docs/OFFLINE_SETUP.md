# Offline Notes

The current default deployment assumes the GPU server has internet access.

If you need a restricted-network setup:

- copy the model files manually to your chosen `$MODEL_PATH` on the host
- pre-pull and transfer the `vllm/vllm-openai:gemma4` Docker image as a tar file (`docker save` / `docker load`)
- build the app image in an environment that can reach package registries, then transfer it if needed

This document is intentionally minimal because the current project baseline is online Docker deployment.
