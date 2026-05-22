FROM python:3.11-slim

# ── Blender version ───────────────────────────────────────────────────────────
# Override with --build-arg BLENDER_VERSION=5.1.x if a newer patch is released.
ARG BLENDER_VERSION=5.1.1
ARG BLENDER_MAJOR_MINOR=5.1

# ── System runtime dependencies required by Blender headless ─────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        xz-utils \
        libgl1 \
        libglu1-mesa \
        libxi6 \
        libxrender1 \
        libxkbcommon0 \
        libxxf86vm1 \
        libxfixes3 \
        libxext6 \
        libsm6 \
        libice6 \
        libx11-6 \
        ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Download and install Blender ${BLENDER_VERSION} from the official mirror ──
RUN set -eux; \
    URL="https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    echo "Downloading Blender from: ${URL}"; \
    curl -fSL "${URL}" -o /tmp/blender.tar.xz; \
    echo "Extracting Blender tarball..."; \
    tar -xJf /tmp/blender.tar.xz -C /opt; \
    mv /opt/blender-${BLENDER_VERSION}-linux-x64 /opt/blender; \
    rm /tmp/blender.tar.xz; \
    echo "Blender install complete:"; \
    /opt/blender/blender --version

ENV BLENDER_BIN=/opt/blender/blender
ENV PATH="/opt/blender:${PATH}"

# ── Application ───────────────────────────────────────────────────────────────
WORKDIR /app

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY server/ /app/server/

EXPOSE 5000

CMD ["python", "server/app.py"]
