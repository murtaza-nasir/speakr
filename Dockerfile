###############################################################################
# Stage 1: Builder — install Python deps and download vendor assets
###############################################################################
FROM python:3.11-slim AS builder

ARG PRODUCTION=0
ARG LIGHTWEIGHT=0

WORKDIR /app

# gcc is needed to compile C extensions during pip install
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-embeddings.txt constraints.txt ./
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt -r requirements.txt && \
    if [ "$LIGHTWEIGHT" = "0" ]; then \
        pip install --no-cache-dir --prefix=/install -c constraints.txt -r requirements-embeddings.txt; \
    fi

# Download vendor assets (JS/CSS/fonts)
RUN mkdir -p /app/static/vendor
COPY scripts/download_offline_deps.py scripts/
RUN pip install --no-cache-dir requests && \
    PRODUCTION=${PRODUCTION} python scripts/download_offline_deps.py && \
    echo "✓ Vendor dependencies downloaded successfully"

###############################################################################
# Stage 2: FFmpeg — download static binaries (much smaller than apt ffmpeg)
#
# Source: BtbN/FFmpeg-Builds. We moved off the johnvansickle static builds
# because that mirror is frozen at 7.0.2 (2024) and therefore ships the MagicYUV
# decoder flaw CVE-2026-8461 ("PixelSmash", heap out-of-bounds write, RCE via
# crafted media), fixed upstream in 8.1.2.
#
# Supply-chain hardening: we pin a dated release (not BtbN's rolling `latest`
# tag) and verify each arch tarball's SHA-256, so a swapped or tampered binary
# fails the build. NOTE: BtbN deletes autobuild assets after roughly two weeks,
# so any rebuild after that window fails with a wget 404 until the pin is
# refreshed. To refresh, bump BTBN_TAG, FFMPEG_VER and BOTH checksums together
# (read the new values from the release's checksums.sha256). BtbN binaries nest
# under bin/, hence the adjusted move paths.
###############################################################################
FROM python:3.11-slim AS ffmpeg-stage

ARG BTBN_TAG=autobuild-2026-07-12-13-16
ARG FFMPEG_VER=n8.1.2-22-g94138f6973
ARG FFMPEG_SHA256_amd64=516b60bad3df2dedea23594c60e7afaecf3e6a440ca9091ef95ee1f62deba71e
ARG FFMPEG_SHA256_arm64=0a34477fb47a9c108b869fccc9919e00d0c7ebf886e8d45301c74d2d46640d64

RUN apt-get update && apt-get install -y --no-install-recommends wget xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(dpkg --print-architecture)" in \
         amd64) BTBN_ARCH=linux64;    SHA256="${FFMPEG_SHA256_amd64}" ;; \
         arm64) BTBN_ARCH=linuxarm64; SHA256="${FFMPEG_SHA256_arm64}" ;; \
         *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
       esac \
    && ASSET="ffmpeg-${FFMPEG_VER}-${BTBN_ARCH}-gpl-8.1.tar.xz" \
    && wget -q "https://github.com/BtbN/FFmpeg-Builds/releases/download/${BTBN_TAG}/${ASSET}" -O /tmp/ff.tar.xz \
    && echo "${SHA256}  /tmp/ff.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/ffmpeg-dir \
    && tar xf /tmp/ff.tar.xz -C /tmp/ffmpeg-dir --strip-components=1 \
    && mv /tmp/ffmpeg-dir/bin/ffmpeg /usr/local/bin/ffmpeg \
    && mv /tmp/ffmpeg-dir/bin/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ff.tar.xz /tmp/ffmpeg-dir

###############################################################################
# Stage 3: Runtime — lean final image with only what's needed
###############################################################################
FROM python:3.11-slim

WORKDIR /app

# Copy static ffmpeg binaries (~150MB vs ~450MB from apt)
COPY --from=ffmpeg-stage /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-stage /usr/local/bin/ffprobe /usr/local/bin/ffprobe

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy downloaded vendor assets from builder
COPY --from=builder /app/static/vendor /app/static/vendor

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /data/uploads /data/instance && chmod 755 /data/uploads /data/instance

# Set environment variables
ENV FLASK_APP=src/app.py
ENV SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
ENV UPLOAD_FOLDER=/data/uploads
ENV PYTHONPATH=/app
ENV HF_HOME=/data/instance/huggingface

# Add entrypoint script
COPY scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8899

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8899", "--timeout", "600", "src.app:app"]
