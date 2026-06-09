FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY translator_app ./translator_app

RUN uv pip install --system .

EXPOSE 7860

CMD ["uvicorn", "translator_app.main:app", "--host", "0.0.0.0", "--port", "7860"]
