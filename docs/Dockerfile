FROM python:3.11-slim

WORKDIR /app

# Install git (required for git-revision-date plugin)
RUN apt-get update && \
    apt-get install -y git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install requirements
COPY requirements-docs.txt .
RUN pip install --no-cache-dir -r requirements-docs.txt

# Create docs directory structure
RUN mkdir -p docs

# Copy mkdocs config to root
COPY mkdocs.yml .

# Copy all documentation files to docs subdirectory
COPY . ./docs/

# Expose MkDocs development server port
EXPOSE 8000

# Run MkDocs server
CMD ["mkdocs", "serve", "--dev-addr=0.0.0.0:8000"]