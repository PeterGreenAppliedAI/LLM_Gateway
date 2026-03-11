# DevMesh Gateway Dashboard

React + TypeScript monitoring dashboard for the LLM Gateway.

## Features

- **Stats Overview**: Request counts, success rates, latency, token usage (24h)
- **Security Monitor**: Alerts, guard scan results, regex vs guard verdict comparison
- **Audit Log**: Recent requests with model, endpoint, latency, tokens (click for details)
- **Model Catalog**: All discovered models across endpoints with sizes and families
- **API Key Management**: Create and revoke database-managed API keys
- **Endpoint Health**: Per-endpoint status, model counts, health indicators

## Setup

```bash
npm install
```

## Development

```bash
# Default: connects to gateway at http://localhost:8001
npx vite --host 0.0.0.0 --port 5174

# Custom gateway URL:
VITE_API_URL=http://192.168.1.100:8001 npx vite --host 0.0.0.0 --port 5174
```

## Production Build

```bash
npm run build
# Output in dist/
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_URL` | `http://localhost:8001` | Gateway API base URL |

The gateway must have the dashboard's origin in its CORS configuration:

```bash
GATEWAY_CORS_ORIGINS='["http://localhost:5174","http://your-server:5174"]'
```
