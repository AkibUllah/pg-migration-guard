FROM python:3.12-slim

RUN useradd --create-home --uid 1000 pgmg
WORKDIR /app
COPY pyproject.toml README.md ./
COPY migration_guard ./migration_guard
RUN pip install --no-cache-dir ".[probe]"

USER pgmg
WORKDIR /work
ENTRYPOINT ["pgmg"]
CMD ["--help"]
