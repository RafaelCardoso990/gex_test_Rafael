# Single image for the receiver and both workers (command is overridden per service in compose).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Install only runtime deps, leveraging Docker layer caching for the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

CMD ["uvicorn", "app.receiver.main:app", "--host", "0.0.0.0", "--port", "8000"]
