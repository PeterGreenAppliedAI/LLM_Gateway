# DevMesh Gateway Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY pyproject.toml .
COPY src/ src/

# Install the package
# The redis client is small and lets `--profile ha` work without a rebuild;
# it's only used when GATEWAY_REDIS_URL is set (D-035)
RUN pip install --no-cache-dir -e ".[redis]"

# Copy configuration
COPY config/ config/

# Create non-root user. /app/data holds the SQLite database and the audit
# intent log; it's a volume in Compose, and must be writable by this user
RUN useradd -m -u 1000 gateway && mkdir -p /app/data && chown -R gateway:gateway /app
USER gateway
VOLUME ["/app/data"]

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" || exit 1

# Run the application
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
