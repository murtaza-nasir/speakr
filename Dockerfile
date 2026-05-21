###############################################################################
# Stage 1: Builder — install Python deps + CUDA 12.8 PyTorch + vendor assets
###############################################################################
FROM python:3.10-slim AS builder

ARG PRODUCTION=0
ARG LIGHTWEIGHT=0

WORKDIR /app

# gcc is needed to compile C extensions during pip install
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy CUDA 12.8 PyTorch wheels for reference
COPY cu128/*.whl /tmp/cu128/

# Install Python dependencies first
COPY requirements.txt requirements-embeddings.txt constraints.txt ./
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt -r requirements.txt && \
    if [ "$LIGHTWEIGHT" = "0" ]; then \
        pip install --no-cache-dir --prefix=/install -c constraints.txt -r requirements-embeddings.txt; \
    fi

# Override PyTorch with CUDA 12.8 versions (for RTX 5090 sm_120 support)
RUN find /install/lib/python3.10/site-packages -maxdepth 1 -name 'torch*' -exec rm -rf {} + && \
    pip install --no-cache-dir --prefix=/install --no-deps \
    /tmp/cu128/torch-*.whl \
    /tmp/cu128/torchaudio-*.whl \
    /tmp/cu128/torchvision-*.whl && \
    rm -rf /tmp/cu128

# Vendor assets are pre-downloaded on host, copied directly (no network needed at build time)

###############################################################################
# Stage 2: FFmpeg — download static binaries (much smaller than apt ffmpeg)
###############################################################################
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04 AS ffmpeg-stage

RUN apt-get update && apt-get install -y --no-install-recommends wget xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH=$(dpkg --print-architecture) \
    && wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${ARCH}-static.tar.xz -O /tmp/ff.tar.xz \
    && mkdir -p /tmp/ffmpeg-dir \
    && tar xf /tmp/ff.tar.xz -C /tmp/ffmpeg-dir --strip-components=1 \
    && mv /tmp/ffmpeg-dir/ffmpeg /usr/local/bin/ffmpeg \
    && mv /tmp/ffmpeg-dir/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ff.tar.xz /tmp/ffmpeg-dir

###############################################################################
# Stage 3: Runtime — NVIDIA CUDA 12.8 runtime + Python 3.10 + PyTorch
###############################################################################
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

# Install Python 3.10 + system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-dev \
    python3.10-distutils \
    python3.10-venv \
    libsndfile1 \
    libsox-fmt-all \
    sox \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && ln -sf /usr/bin/python3.10 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.10 /usr/local/bin/python3.10 \
    && echo "/usr/local/cuda/lib64" > /etc/ld.so.conf.d/cuda.conf \
    && echo "/usr/local/cuda-12.8/targets/x86_64-linux/lib" >> /etc/ld.so.conf.d/cuda.conf \
    && ldconfig

WORKDIR /app

# Copy static ffmpeg binaries
COPY --from=ffmpeg-stage /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-stage /usr/local/bin/ffprobe /usr/local/bin/ffprobe

# Copy installed Python packages from builder (includes CUDA 12.8 PyTorch)
# Builder installs to /install/lib/python3.10/site-packages
# NVIDIA base image's deadsnakes Python looks in dist-packages, so create symlink
COPY --from=builder /install /usr/local
RUN rm -rf /usr/local/lib/python3.10/dist-packages && \
    ln -s /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/dist-packages

# Register CUDA libs from NVIDIA CUDA toolkit + PyTorch nvidia packages for ldconfig
# Find all directories containing .so files under site-packages and register them
RUN find /usr/local/lib/python3.10/site-packages -type f -name '*.so*' | \
    xargs -I{} dirname {} | sort -u | \
    tee /etc/ld.so.conf.d/pytorch-cuda.conf > /dev/null && \
    echo "/usr/local/cuda/lib64" >> /etc/ld.so.conf.d/pytorch-cuda.conf && \
    echo "/usr/local/cuda-12.8/targets/x86_64-linux/lib" >> /etc/ld.so.conf.d/pytorch-cuda.conf && \
    ldconfig

# Copy pre-downloaded vendor assets from build context
COPY static/vendor /app/static/vendor

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /data/uploads /data/instance && chmod 755 /data/uploads /data/instance

# Set environment variables
ENV FLASK_APP=src/app
ENV SQLALCHEMY_DATABASE_URI=sqlite:////data/instance/transcriptions.db
ENV UPLOAD_FOLDER=/data/uploads
ENV PYTHONPATH=/app
ENV HF_HOME=/data/instance/huggingface
ENV SENSEVOICE_CACHE_DIR=/data/instance/huggingface
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# Add entrypoint script
COPY scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8899

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8899", "--timeout", "600", "src.app:app"]
