FROM python:3.12-slim

LABEL org.opencontainers.image.title="alphaess-energy-analyzer" \
      org.opencontainers.image.description="Self-hosted dashboard and analyzer for AlphaESS solar and battery systems" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && rm -rf /src

# Data folder: config.toml, .env (credentials), energy.db and report.html live here.
RUN useradd --create-home --uid 10001 analyzer && mkdir /data && chown analyzer /data
USER analyzer
WORKDIR /data
VOLUME ["/data"]

ENTRYPOINT ["energy-analyzer"]
CMD ["--help"]
