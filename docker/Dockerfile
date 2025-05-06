FROM python:3.9-slim-buster
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    python3-dev \
    git \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /opt/transcription-app/instance /opt/transcription-app/uploads
COPY docker/entrypoint.sh .
RUN chmod +x entrypoint.sh
EXPOSE 8899
ENTRYPOINT ["./entrypoint.sh"]
CMD ["flask", "run", "--host=0.0.0.0", "--port=8899"]
