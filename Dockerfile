# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM dxflrs/garage:v2.5.0@sha256:677e5b758e4425a325b811c425c026bf35a4bf42f7ddbaa773de3f4fd779a5a5 AS garage

FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS app
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 10001 youtube \
    && useradd --uid 10001 --gid youtube --home-dir /nonexistent --shell /usr/sbin/nologin youtube

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg=7:5.1.9-0+deb12u1 libseccomp2=2.5.4-1+deb12u1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache/uv

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/seed.py ./scripts/seed.py

ENV PATH="/app/.venv/bin:${PATH}"
USER 10001:10001

FROM app AS garage-init
USER root
COPY --from=garage /garage /usr/local/bin/garage
ENTRYPOINT ["python", "-m", "youtube_copy.infrastructure.garage_init"]

FROM app AS tests
USER root
RUN uv sync --frozen --extra dev --no-editable \
    && rm -rf /root/.cache/uv
COPY tests ./tests
USER 10001:10001
CMD ["pytest", "-q"]
