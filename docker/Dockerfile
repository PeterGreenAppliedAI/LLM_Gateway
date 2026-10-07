# DevMesh Gateway Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY pyproject.toml .
COPY src/ src/

# Install the package
# The redis client is small and lets `--profile ha` work without a rebuild;
# it's only used when GATEWAY_REDIS_URL is set (D-035)
RUN pip install --no-cache-dir -e ".[redis]"

# Copy configuration
COPY config/ config/

# Create non-root user
RUN useradd -m -u 1000 gateway && chown -R gateway:gateway /app
USER gateway

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
