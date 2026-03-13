# Contributing to DevMesh LLM Gateway

Thanks for your interest in contributing. This document covers how to get started.

## Development Setup

```bash
git clone https://github.com/PeterGreenAppliedAI/LLM_Gateway.git
cd LLM_Gateway

python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

cp config/gateway.yaml.example config/gateway.yaml
cp .env.example .env
```

## Running Tests

```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=gateway --cov-report=html

# Single file
pytest tests/test_pii.py -v
```

All tests must pass before submitting a PR.

## Project Structure

```
src/gateway/
├── catalog/         # Model discovery
├── dispatch/        # Routing and failover
├── models/          # Data models (internal, OpenAI, Ollama)
├── observability/   # Structured logging and metrics
├── policy/          # Rate limits, token budgets, enforcement
├── providers/       # Runtime adapters (Ollama, OpenAI, vLLM, etc.)
├── routes/          # API endpoints
├── security/        # Injection defense, PII scrubber, guard model
├── storage/         # Audit logging, API keys, DB engine
├── config.py        # YAML config loader
├── settings.py      # Pydantic settings (env vars)
└── main.py          # FastAPI application
```

## Code Style

- Python 3.10+ type hints
- Pydantic for configuration and validation
- Structured logging (no print statements)
- Security-first: validate inputs, sanitize keys, constant-time comparisons
- Tests for new features

## Making Changes

1. **Fork and branch** from `main`
2. **Write tests** for new functionality
3. **Run the full test suite** before submitting
4. **Keep PRs focused** — one feature or fix per PR
5. **Update configuration examples** if you add new settings (`gateway.yaml.example`, `.env.example`)

## Architecture Principles

- **Single Responsibility**: Each module does one thing
- **Explicit Boundaries**: Clear interfaces between layers
- **No Implicit Trust**: Validate at every boundary
- **Async by Default**: Non-blocking I/O throughout
- **Security in the fast path, analysis in the background**: Guard models run async, never block requests

## Areas for Contribution

- **Provider adapters**: vLLM, TRT-LLM, SGLang adapters need completion
- **Dashboard**: React frontend improvements
- **Documentation**: Usage examples, deployment guides
- **Testing**: Edge cases, integration tests
- **Security**: Additional detection patterns, guard model support

## Reporting Issues

Open an issue on GitHub with:
- What you expected
- What happened
- Steps to reproduce
- Gateway version and configuration (redact secrets)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
