FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[voice,research,mcp]"
COPY alembic.ini ./
COPY alembic ./alembic
