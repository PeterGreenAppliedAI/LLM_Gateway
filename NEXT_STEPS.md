# DevMesh Gateway - Next Steps & Roadmap

This document outlines the immediate next steps, deployment strategy, and future enhancements for DevMesh Gateway.

---

## 1. Deployment as a Proxy ("Sitting in the Middle")

### How It Works

The gateway acts as a **reverse proxy** for LLM APIs:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Your Apps      │────▶│  DevMesh Gateway │────▶│  LLM Providers  │
│  (unchanged)    │     │  (the control    │     │  - Ollama       │
│                 │◀────│   plane)         │◀────│  - OpenAI       │
│  - Internal     │     │                  │     │  - Anthropic    │
│  - Claude Code  │     │  localhost:8000  │     │  - vLLM         │
│  - Cursor       │     │                  │     │  - etc          │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

### Configuration for Existing Tools

**Option A: Environment Variable (Most Tools)**

Most OpenAI-compatible tools support `OPENAI_BASE_URL`:

```bash
# Point tools at the gateway instead of OpenAI directly
export OPENAI_BASE_URL=http://gateway-vm:8000/v1
export OPENAI_API_KEY=your-gateway-api-key

# Now run your tool normally - requests go through gateway
cursor .
claude-code
python my_script.py
```

**Option B: SDK Configuration**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://gateway-vm:8000/v1",
    api_key="your-gateway-api-key"
)

# Requests now routed through gateway
response = client.chat.completions.create(
    model="ollama/phi4:14b",  # or "openai/gpt-4"
    messages=[{"role": "user", "content": "Hello"}]
)
```

### Network Topology Options

**Option 1: Same Network (Simplest)**
```
[Dev Machine] ──────▶ [Gateway VM:8000] ──────▶ [Ollama Server]
                                       ──────▶ [OpenAI API]
```

**Option 2: VPN/Tailscale**
```
[Remote Laptop] ──VPN──▶ [Gateway VM:8000] ──────▶ [Internal Ollama]
                                           ──────▶ [Cloud APIs]
```

**Option 3: Reverse Proxy with TLS (Production)**
```
[Internet] ──HTTPS──▶ [nginx/caddy] ──▶ [Gateway:8000] ──▶ [Backends]
```

---

## 2. Cloud Provider Integration (OpenAI, Anthropic, etc.)

### Why This Works Out-of-the-Box

All major LLM providers use **OpenAI-compatible APIs**:

| Provider | API Compatibility | Base URL |
|----------|------------------|----------|
| OpenAI | Native | `https://api.openai.com` |
| Anthropic | OpenAI-compatible mode | `https://api.anthropic.com` |
| Google Gemini | OpenAI-compatible | `https://generativelanguage.googleapis.com` |
| Groq | OpenAI-compatible | `https://api.groq.com/openai` |
| Together AI | OpenAI-compatible | `https://api.together.xyz` |
| Fireworks | OpenAI-compatible | `https://api.fireworks.ai/inference` |

### Adding Cloud Providers

Create an `openai` adapter type (already architected for this):

```yaml
# config/providers.yaml
providers:
  # Local runtime
  - name: ollama
    type: ollama
    base_url: http://192.168.1.216:11434
    enabled: true

  # OpenAI
  - name: openai
    type: openai
    base_url: https://api.openai.com
    api_key: ${OPENAI_API_KEY}
    enabled: true
    models:
      - gpt-4
      - gpt-4-turbo
      - gpt-3.5-turbo

  # Anthropic (via OpenAI-compatible endpoint)
  - name: anthropic
    type: openai  # Uses same adapter!
    base_url: https://api.anthropic.com/v1
    api_key: ${ANTHROPIC_API_KEY}
    headers:
      anthropic-version: "2024-01-01"
    enabled: true
    models:
      - claude-3-opus
      - claude-3-sonnet

  # Groq (fast inference)
  - name: groq
    type: openai
    base_url: https://api.groq.com/openai
    api_key: ${GROQ_API_KEY}
    enabled: true
    models:
      - llama-3.1-70b-versatile
      - mixtral-8x7b-32768
```

### Implementation Required

```python
# src/gateway/adapters/openai.py (new file)
class OpenAIAdapter(BaseAdapter):
    """Adapter for OpenAI-compatible APIs (OpenAI, Anthropic, Groq, etc.)"""

    async def chat(self, request: InternalRequest) -> InternalResponse:
        # Forward request to cloud API
        # Handle auth headers
        # Normalize response
        pass
```

**Estimated effort**: 2-4 hours (straightforward since we already have the pattern)

---

## 3. Frontend Dashboard Options

### Option A: Grafana (Recommended for v0)

**Pros**:
- Already have Prometheus metrics
- Production-ready
- No custom code needed

**Setup**:
```yaml
# docker-compose.yaml addition
services:
  prometheus:
    image: prom/prometheus
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
    volumes:
      - ./monitoring/grafana/dashboards:/var/lib/grafana/dashboards
```

**Dashboard shows**:
- Requests per second by provider/model
- Latency histograms
- Token usage
- Error rates
- Provider health status

### Option B: Custom React Dashboard (v1+)

For a branded DevMesh experience:

```
┌─────────────────────────────────────────────────────────┐
│  DevMesh Gateway Dashboard                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Requests/hr │  │ Avg Latency │  │ Token Usage │     │
│  │   12,456    │  │   234ms     │  │  1.2M       │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│                                                         │
│  Provider Status                                        │
│  ┌─────────────────────────────────────────────────┐   │
│  │ ● Ollama      Healthy    2,341 req/hr           │   │
│  │ ● OpenAI      Healthy    8,923 req/hr           │   │
│  │ ○ Anthropic   Degraded   1,192 req/hr           │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  Recent Requests                  Cost Breakdown        │
│  ┌──────────────────────────┐    ┌─────────────────┐   │
│  │ ...                      │    │  OpenAI: $45.23 │   │
│  │ ...                      │    │  Groq:   $2.10  │   │
│  └──────────────────────────┘    └─────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Tech Stack**:
- React + TypeScript
- TailwindCSS
- Recharts/Tremor for visualizations
- API: Gateway `/metrics` + new `/v1/devmesh/stats` endpoint

### Option C: Terminal UI (Quick Win)

```bash
# Simple CLI dashboard
watch -n 1 'curl -s localhost:8000/health | jq'

# Or with rich TUI
pip install rich
python -m gateway.cli dashboard
```

---

## 4. Immediate Action Items

### This Week

- [ ] **Create `openai` adapter** for cloud providers
- [ ] **Add docker-compose.yaml** with Prometheus + Grafana
- [ ] **Create Grafana dashboard JSON** for gateway metrics
- [ ] **Test with real OpenAI/Anthropic keys**
- [ ] **Document VM deployment steps**

### Next 2 Weeks

- [ ] **Hybrid routing**: Route cheap tasks to local, expensive to cloud
- [ ] **Cost tracking**: Estimate costs per request
- [ ] **Request logging UI**: View recent requests/errors
- [ ] **API key management UI**: Create/revoke keys

### v1.0 Roadmap

- [ ] Multi-tenant support
- [ ] Custom React dashboard
- [ ] Webhook notifications
- [ ] Request/response caching
- [ ] Model evaluation harness

---

## 5. VM Deployment Checklist

```bash
# On your VM

# 1. Install Docker
curl -fsSL https://get.docker.com | sh

# 2. Clone the repo
git clone <repo-url> /opt/devmesh-gateway
cd /opt/devmesh-gateway

# 3. Configure providers
cp config/providers.yaml.example config/providers.yaml
vim config/providers.yaml  # Add your provider URLs and API keys

# 4. Set secrets
echo "OPENAI_API_KEY=sk-..." > .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo "GATEWAY_API_KEY=your-secure-key" >> .env

# 5. Start the stack
docker-compose up -d

# 6. Verify
curl http://localhost:8000/health

# 7. Configure firewall (allow from internal network only)
ufw allow from 192.168.1.0/24 to any port 8000

# 8. Point your apps at http://vm-ip:8000/v1
```

---

## 6. Testing the Proxy Setup

```bash
# 1. Start gateway locally
GATEWAY_CONFIG_PATH=config/gateway.yaml \
GATEWAY_PROVIDERS_CONFIG_PATH=config/providers.yaml \
uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# 2. Test direct curl
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"ollama/phi4:14b","messages":[{"role":"user","content":"hi"}]}'

# 3. Test with OpenAI SDK pointing at gateway
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=test-key
python -c "
from openai import OpenAI
c = OpenAI()
r = c.chat.completions.create(model='ollama/phi4:14b', messages=[{'role':'user','content':'hi'}])
print(r.choices[0].message.content)
"

# 4. Check metrics
curl http://localhost:8000/metrics | grep gateway_
```

---

## 7. Questions to Answer

Before proceeding, decide:

1. **Where will the gateway VM live?** (Same network as Ollama? DMZ? Cloud?)
2. **Do you need TLS?** (Internal network might not, external access does)
3. **Which cloud providers to add first?** (OpenAI most common)
4. **Grafana vs custom dashboard?** (Grafana faster, custom more branded)
5. **Multi-user?** (Need auth key management)

---

## 8. Integration with Open WebUI, LibreChat, etc.

### The Gateway Works as a Drop-in Proxy

Any tool that supports OpenAI-compatible APIs (which is almost everything) can use the gateway:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Open WebUI     │────▶│  DevMesh Gateway │────▶│  Ollama         │
│  localhost:3000 │     │  localhost:8000  │     │  192.168.1.216  │
│                 │◀────│                  │◀────│                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                              │
                              └────────────────▶ OpenAI/Anthropic/etc
```

### Open WebUI Configuration

```bash
# In your Open WebUI docker-compose.yaml or .env
OLLAMA_BASE_URL=http://gateway-ip:8000   # Point at gateway, not Ollama directly
```

Or if using docker-compose:
```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      - OLLAMA_BASE_URL=http://gateway:8000
    depends_on:
      - gateway
```

### LibreChat Configuration

```yaml
# librechat.yaml
endpoints:
  custom:
    - name: "DevMesh Gateway"
      apiKey: "${GATEWAY_API_KEY}"
      baseURL: "http://gateway-ip:8000/v1"
      models:
        default: ["ollama/phi4:14b", "openai/gpt-4"]
```

### What This Enables

1. **Unified Metrics**: All requests from Open WebUI flow through gateway - visible in one place
2. **Model Switching**: Users can request `ollama/phi4:14b` or `openai/gpt-4` - gateway routes appropriately
3. **Rate Limiting**: Prevent runaway costs even from chat UIs
4. **Fallback**: If Ollama is down, automatically route to cloud provider

### Example Full Stack

```yaml
# docker-compose.yaml
services:
  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      - GATEWAY_CONFIG_PATH=/config/gateway.yaml
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    volumes:
      - ./config:/config

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3000:8080"
    environment:
      - OLLAMA_BASE_URL=http://gateway:8000
    depends_on:
      - gateway

  prometheus:
    image: prom/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3001:3000"
```

---

## Summary

| Task | Effort | Priority |
|------|--------|----------|
| OpenAI adapter | 2-4 hrs | High |
| Docker Compose + monitoring | 1-2 hrs | High |
| Grafana dashboard | 1-2 hrs | High |
| VM deployment | 30 min | High |
| Open WebUI integration | 15 min | High |
| Custom React dashboard | 1-2 weeks | Medium |
| Cost tracking | 4-8 hrs | Medium |
| Multi-tenant | 1-2 weeks | Low (v1) |
