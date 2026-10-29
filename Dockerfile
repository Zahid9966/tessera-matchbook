# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY tessera ./tessera
RUN pip install --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TESSERA_HOME=/app MATCHBOOK_HOST=0.0.0.0 MATCHBOOK_PORT=8788
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin matchbook \
 && mkdir -p /app/data && chown -R matchbook /app
WORKDIR /app
COPY --from=build /install /usr/local
COPY --chown=matchbook models ./models
USER matchbook
EXPOSE 8788
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8788/api/health', headers={'Host': 'localhost'}), timeout=3)" || exit 1
CMD ["python", "-m", "tessera.recon.app"]
