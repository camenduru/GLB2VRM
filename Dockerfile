# syntax=docker/dockerfile:1
#
# GLB2VRM - rigged GLB -> VRM web UI
#
#   docker build -t camenduru/glb2vrm:latest .
#   docker run --rm -p 8020:8020 -v glb2vrm-data:/app/glb2vrm/data camenduru/glb2vrm:latest
#
# Blender is only used as the glTF importer/exporter for the conversion;
# the VRM itself is written by VRMify's own Python converter.

ARG BLENDER_VERSION=4.5.1
ARG BLENDER_SERIES=4.5

# ---------------------------------------------------------------- Blender
FROM debian:bookworm-slim AS blender
ARG BLENDER_VERSION
ARG BLENDER_SERIES
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/blender \
 && curl -fsSL "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /opt/blender --strip-components=1 \
 && test -x /opt/blender/blender

# ------------------------------------------------------------------ app
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="GLB2VRM" \
      org.opencontainers.image.description="Rigged GLB to VRM avatar converter with a three.js preview and VRMA player" \
      org.opencontainers.image.source="https://github.com/JiGuang283/VRMify" \
      org.opencontainers.image.licenses="GPL-3.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    GLB2VRM_BLENDER=/opt/blender/blender \
    HOME=/root

# Blender's shared-library dependencies (background mode still links them),
# plus jsonschema's runtime needs are installed below with pip.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libx11-6 libx11-xcb1 libxext6 libxi6 libxxf86vm1 libxfixes3 libxrender1 \
      libxrandr2 libxinerama1 libxcursor1 libsm6 libice6 libxkbcommon0 \
      libfontconfig1 libgl1 libglu1-mesa libegl1 libgles2 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=blender /opt/blender /opt/blender
RUN ln -sf /opt/blender/blender /usr/local/bin/blender

WORKDIR /app

RUN pip install --no-cache-dir "jsonschema>=4.18" "Pillow>=10"

COPY glb2vrm ./glb2vrm
COPY convert ./convert
COPY formats ./formats
COPY tools/qa ./tools/qa
COPY ui/viewer/vendor ./ui/viewer/vendor

# Never bake local uploads / conversion outputs into the image.
RUN rm -rf glb2vrm/data \
 && mkdir -p glb2vrm/data/models glb2vrm/data/jobs \
 && python -c "import jsonschema, PIL; print('python deps ok')" \
 && python -c "import sys; sys.path.insert(0, 'tools/qa'); import check_vrm; v, reason = check_vrm.load_validators(); print(reason or ('VRM schema cache ready: %d validators' % len(v)))"

EXPOSE 8020
VOLUME ["/app/glb2vrm/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8020/api/status', timeout=4).read()" || exit 1

CMD ["python", "glb2vrm/server.py", "--host", "0.0.0.0", "--port", "8020"]
