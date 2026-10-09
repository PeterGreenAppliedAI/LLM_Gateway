"""Laya PII gate sidecar (D-052). Runs on the Mac next to the routing Laya.

    pip install laya fastapi uvicorn
    LAYA_MODEL=convaiinnovations/laya-multilingual uvicorn app:app --host 0.0.0.0 --port 8011

Contract (what the gateway's PIIGateClient sends):

    POST /classify
    {"texts": ["..."], "questions": {"CREDENTIAL": "Does the text ...?", ...}}
    -> {"results": [{"CREDENTIAL": 0.02, "CONTACT": 0.91, ...}, ...]}

    GET /health -> {"status": "ok", "model": ..., "max_len": ...}

The gateway owns the taxonomy and sends the questions with every request,
so changing a question never requires redeploying this service. Each text
is one Laya forward pass answering every question (Laya's typed `noul`
yes/no questions return a calibrated probability).

The Laya API calls below follow the model card (laya.load(model_id), then
predict(state, questions) with answers under result["answers"][name]["noul"]).
The startup self-test runs one prediction and fails loudly if the installed
laya version disagrees, rather than serving wrong numbers.
"""

import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("LAYA_MODEL", "convaiinnovations/laya-multilingual")
MAX_LEN = int(os.environ.get("LAYA_MAX_LEN", "8192"))
MAX_TEXTS = 64

app = FastAPI(title="Laya PII gate")
_agent = None


class ClassifyRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    questions: dict[str, str] = Field(min_length=1, max_length=32)


def _load():
    import laya

    try:
        return laya.load(MODEL_ID, max_len=MAX_LEN)
    except TypeError:  # versions without a max_len argument
        return laya.load(MODEL_ID)


def _probabilities(text: str, questions: dict[str, str]) -> dict[str, float]:
    typed = {name: {"type": "noul", "instructions": q} for name, q in questions.items()}
    result = _agent.predict(text, typed)
    answers = result["answers"]
    probs = {}
    for name in questions:
        value = answers[name]
        p = value.get("noul") if isinstance(value, dict) else value
        if not isinstance(p, (int, float)):
            raise ValueError(f"no probability for {name!r} in laya output: {value!r}")
        probs[name] = float(p)
    return probs


@app.on_event("startup")
def startup() -> None:
    global _agent
    _agent = _load()
    # Self-test: an obvious positive and an obvious negative. Wrong API or a
    # model that can't separate these should stop the service, not serve.
    probe = {"CONTACT": "Does the text contain a real person's email address?"}
    positive = _probabilities("Please email jane.doe@acme.com tomorrow.", probe)["CONTACT"]
    negative = _probabilities("The weather is mild today.", probe)["CONTACT"]
    print(f"laya self-test: positive={positive:.3f} negative={negative:.3f} ({MODEL_ID})")
    if not positive > negative:
        raise RuntimeError(
            "laya self-test failed: the email sentence did not score above the clean one. "
            "Check the installed laya version's predict() output against this file."
        )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _agent is not None else "loading",
        "model": MODEL_ID,
        "max_len": MAX_LEN,
    }


@app.post("/classify")
def classify(body: ClassifyRequest) -> dict:
    if _agent is None:
        raise HTTPException(503, "model loading")
    started = time.perf_counter()
    try:
        results = [_probabilities(text, body.questions) for text in body.texts]
    except Exception as e:
        raise HTTPException(500, f"laya prediction failed: {e}") from e
    return {
        "results": results,
        "model": MODEL_ID,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
