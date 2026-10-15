FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf build

COPY samples ./samples

# Run as an unprivileged user; the catalog and the SQLite database live in /app/data.
RUN useradd --create-home listaya && mkdir -p /app/data && chown listaya /app/data
USER listaya
VOLUME ["/app/data"]

CMD ["python", "-m", "listaya"]
