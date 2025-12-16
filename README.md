# DevMesh LLM Gateway

A deployable AI control plane that sits in front of inference runtimes and foundation model APIs. Provides a consistent interface, policy enforcement, routing, and observability for AI in production environments.

## Overview

DevMesh Gateway is **client-deployed, not a centralized SaaS**. It enables:

- **Consistent API Surface**: OpenAI-compatible endpoints regardless of backend
- **Multiple Runtime Support**: Ollama, vLLM, TRT-LLM, SGLang (and cloud APIs)
- **Governance & Limits**: Rate limiting, token limits, policy enforcement
- **Full Visibility**: Structured logging, Prometheus metrics
- **Easy Deployment**: Docker Compose with ~30 minute setup

## Quick Start

### Prerequisites

- Python 3.10+
- Docker & Docker Compose (for production)
- At least one inference runtime (e.g., Ollama)

### Local Development

```bash
# Clone and setup
git clone <repo-url>
cd llm_gateway

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Configure providers
cp config/providers.yaml.example config/providers.yaml
# Edit config/providers.yaml with your runtime URLs

# Run the gateway
GATEWAY_CONFIG_PATH=config/gateway.yaml \
GATEWAY_PROVIDERS_CONFIG_PATH=config/providers.yaml \
uvicorn gateway.main:app --host 0.0.0.0 --port 8000
```

### Docker Deployment

```bash
docker-compose up -d
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `0.0.0.0` | Bind address |
| `GATEWAY_PORT` | `8000` | Listen port |
| `GATEWAY_CONFIG_PATH` | `config/gateway.yaml` | Main config file |
| `GATEWAY_PROVIDERS_CONFIG_PATH` | `config/providers.yaml` | Providers config |
| `GATEWAY_DEBUG` | `false` | Enable debug mode |

### Provider Configuration

```yaml
# config/providers.yaml
providers:
  - name: ollama
    type: ollama
    base_url: http://localhost:11434
    enabled: true
    timeout: 60.0
    models:
      - llama3.2
      - phi4:14b

  - name: openai
    type: openai
    base_url: https://api.openai.com
    api_key: ${OPENAI_API_KEY}
    enabled: true
    models:
      - gpt-4
      - gpt-3.5-turbo
```

## API Endpoints

### OpenAI-Compatible (Drop-in replacement)

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions (streaming supported) |
| `POST /v1/completions` | Text completions |
| `POST /v1/embeddings` | Generate embeddings |

### DevMesh Extensions

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with provider status |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/models` | List all available models |
| `POST /v1/devmesh/route` | Debug routing decisions |
| `GET /v1/devmesh/providers` | List configured providers |

## Usage Examples

### Chat Completion

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "ollama/llama3.2",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### With Provider Routing

```bash
# Route to specific provider
curl -X POST http://localhost:8000/v1/chat/completions \
  -d '{
    "model": "openai/gpt-4",
    "messages": [{"role": "user", "content": "Explain quantum computing"}]
  }'
```

### Check Health

```bash
curl http://localhost:8000/health | jq
```

## Architecture

```
Client Applications
        |
        v
DevMesh Gateway (FastAPI)
├── Auth (API key)
├── Policy enforcement
├── Routing
├── Logging
└── Metrics
        |
        v
Provider Adapters
├── Ollama
├── vLLM
├── TRT-LLM
├── SGLang
└── OpenAI/Anthropic/etc
```

## Monitoring

### Prometheus Metrics

The gateway exposes metrics at `/metrics`:

- `gateway_requests_total{provider,model,task,status}`
- `gateway_request_latency_seconds`
- `gateway_tokens_prompt_total`
- `gateway_tokens_completion_total`
- `gateway_provider_errors_total`
- `gateway_active_requests`

### Grafana Dashboard

Import the included dashboard from `monitoring/grafana/dashboards/`.

## Security

- **API Key Authentication**: Bearer token or X-API-Key header
- **Input Validation**: All inputs validated against schemas
- **Rate Limiting**: Per-user and global limits
- **No Data Persistence**: Prompts/outputs not stored by default
- **Constant-Time Auth**: Timing-attack resistant key comparison

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=gateway --cov-report=html
```

## Project Structure

```
llm_gateway/
├── src/gateway/
│   ├── adapters/       # Provider adapters (Ollama, vLLM, etc.)
│   ├── dispatch/       # Routing and registry
│   ├── models/         # Data models (internal, OpenAI)
│   ├── observability/  # Logging and metrics
│   ├── policy/         # Rate limiting, enforcement
│   ├── routes/         # API endpoints
│   ├── config.py       # Configuration loading
│   └── main.py         # FastAPI application
├── config/
│   ├── gateway.yaml
│   └── providers.yaml
├── tests/
└── docker-compose.yaml
```

## License

MIT License - see LICENSE file for details.
