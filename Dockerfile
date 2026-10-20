FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ARG OPENMASKIT_VERSION=development
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="OpenMaskit" \
      org.opencontainers.image.description="MCP proxy with reusable privacy checks; maintained OpenMaskit fork" \
      org.opencontainers.image.url="https://github.com/ricardomartins-gravitee/openmaskit" \
      org.opencontainers.image.source="https://github.com/ricardomartins-gravitee/openmaskit" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${OPENMASKIT_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
COPY --from=builder /opt/venv /opt/venv
COPY LICENSE NOTICE /usr/share/doc/openmaskit/
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin openmaskit \
    && install -d -m 0700 -o openmaskit -g openmaskit /home/openmaskit/.openmaskit
ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/home/openmaskit \
    OPENMASKIT_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /home/openmaskit
VOLUME ["/home/openmaskit/.openmaskit"]
EXPOSE 9473 9474
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD python -c "import os,urllib.request; u='http://127.0.0.1:'+os.environ.get('OPENMASKIT_WEB_PORT','9473')+'/health'; urllib.request.build_opener(urllib.request.ProxyHandler({})).open(u,timeout=3).read()"
ENTRYPOINT ["openmaskit"]
