# Security Analyzer Observations

Field notes from running the prompt injection defense system in production.

## Overview

The security analyzer runs asynchronously on all requests, scanning for prompt injection patterns without blocking the response pipeline. These are observations from real-world usage.

## Confirmed Detections

### Prompt Injection (True Positive)
- **Threat level**: High
- **Pattern**: Deliberate injection attempt using "ignore previous instructions" style prompts
- **Result**: Correctly flagged and alerted
- **Behavior**: Alert generated, request still processed (scan is non-blocking)

## False Positives

### Embedding Model Output
- **Model**: `qwen3-embedding`
- **Threat level**: High (flagged as `delimiter_attack`)
- **Pattern**: `instruction_tag` - model output contains `[system]` tokens
- **Match count**: 2 occurrences at different positions in the response
- **Root cause**: Embedding models include structural tokens like `[system]` in their vocabulary/output metadata. The injection detector interprets these as delimiter injection attempts.
- **Impact**: None on functionality - security scan is async and non-blocking

### Correct Fix: Scan Inputs, Skip Output Scanning for Embeddings
- **Do NOT skip scanning embedding inputs** - poisoned text embedded into a vector DB is a real attack vector (indirect prompt injection via retrieval). Malicious instructions get embedded, stored, retrieved by similarity search, and injected into a future chat context.
- **Do skip scanning embedding responses** - the output is float vectors and model metadata tokens like `[system]`, not user content. These are model artifacts that trigger false positives.
- **Implementation**: In the security analysis pipeline, check `task == TaskType.EMBEDDINGS` and only scan the request payload, not the response.

## Usage Stats

- **Date**: 2026-02-01
- **Tokens processed**: 16M+ and counting
- **Alert noise**: Low overall, embedding model flags are the main false positive source

## Notes

- Security alerts are stored in-memory only. Gateway restarts clear all alerts.
- The analyzer uses pattern matching (not ML), so false positives from structured model tokens are expected.
- Config-based allow-lists or per-task-type scan profiles would reduce noise without weakening detection on chat/completion tasks.
