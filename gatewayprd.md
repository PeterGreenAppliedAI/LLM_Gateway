DevMesh Gateway v0

Product Requirements Document (PRD)

1. Overview

Product name: DevMesh LLM Gateway
Version: v0 (Production-capable baseline)
Owner: DevMesh Services
Target users: SMBs, MSPs, regulated or cost-sensitive teams deploying AI locally, in the cloud, or hybrid

Purpose

DevMesh Gateway is a deployable AI control plane that sits in front of inference runtimes and foundation model APIs. It provides a consistent interface, policy enforcement, routing, and observability so AI can be used predictably in real business environments.

The gateway is client-deployed, not a centralized SaaS.

2. Problem Statement

AI deployments fail in SMB environments due to:

Unpredictable costs

Vendor lock-in

Lack of visibility into usage

Model churn breaking integrations

No clear boundary between experimentation and production

Most teams integrate models directly into applications with no governance layer.

DevMesh Gateway solves this by enforcing consistency and control at the inference boundary.

3. Goals (v0)

DevMesh Gateway v0 must:

Provide a stable API surface independent of model or runtime

Support multiple local inference runtimes

Enforce basic governance and limits

Emit clear metrics and logs for visibility

Be easy to deploy on-prem via Docker

Be safe to expand later without rewrites

4. Non-Goals (Explicit)

v0 will not:

Train or fine-tune models

Provide agent orchestration

Provide long-term memory systems

Store prompts or outputs permanently

Replace full observability platforms like Datadog

Be multi-tenant SaaS

These are future add-ons.

5. Architecture Overview
Client Applications
        |
        v
DevMesh Gateway (FastAPI)
- Auth (basic / API key)
- Policy enforcement
- Routing
- Logging
- Metrics
        |
        v
Provider Adapters
- Ollama
- vLLM
- TRT-LLM
- SGLang
- (optional cloud APIs later)


The gateway owns:

Request normalization

Response normalization

Provider selection

Visibility

Providers are plugins, not core logic.

6. Supported Providers (v0)
Local runtimes (required)

Ollama

vLLM

Local runtimes (interface stubbed, minimal support)

TRT-LLM

SGLang

Adapters must follow a common interface.

7. API Design
External API

Gateway should expose OpenAI-compatible endpoints to reduce client friction:

POST /v1/chat/completions

POST /v1/completions

POST /v1/embeddings (optional in v0)

Internal DevMesh Extensions

GET /health

GET /metrics (Prometheus)

GET /v1/models

POST /v1/devmesh/route (debug routing decisions)

8. Request Normalization

All incoming requests must be normalized into an internal format:

Required fields:

request_id

task (chat, summarize, extract, classify, etc.)

input or messages

max_tokens

temperature

client_id

user_id

Optional:

preferred_provider

fallback_allowed

schema (structured output, optional)

9. Routing Logic (v0)

Routing is config-driven, not hardcoded.

Routing inputs:

Task type

Model capability requirements

Provider health

Optional client overrides

Routing outputs:

Selected provider

Selected model

Fallback chain (if allowed)

Example:

summarize → Ollama

long_context_chat → vLLM

Failure → fallback to secondary local provider

10. Policy Enforcement (v0)
Required policies:

Max tokens per request

Requests per minute (global and per user)

Allowed providers per task

Block execution if provider unhealthy

Out of scope (future):

Per-user billing

Advanced RBAC

Content moderation

11. Observability Requirements
Structured Logging (JSON)

Each request must log:

request_id

client_id

user_id

provider

model

task

latency_ms

token counts (if available)

error type (if any)

Metrics (Prometheus)

requests_total{provider,model,task,status}

request_latency_ms (histogram)

tokens_prompt_total

tokens_completion_total

provider_errors_total

active_requests{provider}

No raw prompts stored.

12. Provider Adapter Interface

Each adapter must implement:

health()

list_models()

chat(request)

generate(request) (optional)

embeddings(request) (optional)

Adapters must declare:

Capabilities

Max context length

Streaming support

Known limitations

13. Deployment Requirements
v0 Deployment

Docker Compose

Gateway container

Optional runtime containers

Prometheus container

Grafana container

Configuration

Environment variables

YAML config files for:

routing

limits

providers

14. Security (v0)

API key authentication

Network isolation via deployment

No data persistence by default

Configurable request logging redaction

15. Success Criteria (v0)

DevMesh Gateway v0 is successful if:

A client can deploy it on-prem in under 30 minutes

Applications can swap models without code changes

Usage and latency are visible immediately

Costs and limits are enforceable

Adding a new runtime does not require refactoring core logic

16. Future Extensions (Not v0)

Cloud provider adapters

Hybrid routing policies

Memory modules

Evaluation harness integration

Advanced dashboards

Managed support hooks

17. Guiding Principle

Models are plugins. Control and visibility are the product.