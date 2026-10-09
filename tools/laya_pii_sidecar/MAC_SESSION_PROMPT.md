# Prompt for a Claude Code session on the Mac mini (D-052)

Paste everything below the line into a Claude Code session running on the Mac mini
(192.168.1.187). Part 1 stands up the gateway's own Laya PII gate; Part 2 fine-tunes
it. Part 2 needs the labelled dataset, which is ready once the gateway box's
labelling run finishes.

---

You're on the Mac mini (192.168.1.187). I need you to set up a **second, separate Laya
model** that the DevMesh LLM Gateway uses as a PII gate, and then fine-tune it on our
labelled data. Work through the parts in order and report back at the end of each.

## Hard rules

- **Don't touch the existing Laya on port 8010.** It's the LocalClaw harness's routing
  model (`/v1/systemone`). Different process, different port, different virtualenv.
- **The dataset contains real personal data** from our traffic. Keep it on this Mac in a
  directory only my user can read (`chmod 700` dir, `600` files). Never upload it:
  no Hugging Face Hub pushes, no W&B or other experiment trackers, no cloud notebooks,
  no pasting samples into chat. Turn off any library telemetry you run into.
- Don't change the HTTP contract below. The gateway already speaks it.

## Context

The gateway (repo: https://github.com/PeterGreenAppliedAI/LLM_Gateway, runs on
`dev-services`, 192.168.1.184 / 10.0.0.20) sends each request's text to this sidecar,
which returns one probability per PII category. If any category scores at or above 0.3,
a bigger model (phi4-mini) extracts the actual values. The gate's job is to be **cheap
and to not miss things**: recall matters more than precision.

The nine categories, their definitions and the exact question text for each live in
`src/gateway/security/pii_taxonomy.py` in the repo (`CATEGORIES`, field `question`).
Read that file; it is the source of truth. The decision record is D-052 in
`docs/DECISIONS.md`.

### Sidecar contract (must stay exactly this)

```
GET  /health   -> {"status": "ok", "model": "<id or path>", "max_len": 8192}
POST /classify {"texts": ["...", ...] (1-64), "questions": {"CREDENTIAL": "Does the text ...?", ...}}
            -> {"results": [{"CREDENTIAL": 0.02, "CONTACT": 0.91, ...}, ...],   # one dict per text, same order
                "model": "...", "latency_ms": 12.3}
```

The gateway chunks long texts itself (24,000 characters, 200 overlap) and takes the max
per category over chunks.

## Part 1: stand up the gate on port 8011

1. Clone the repo (or pull it if it's already here) and use `tools/laya_pii_sidecar/`
   (`app.py`, `README.md`). Make a fresh virtualenv for it; don't reuse the harness's.
2. `pip install laya fastapi uvicorn`. **Check the installed `laya` API against
   `app.py` before trusting it.** I wrote `app.py` from the model card without running it:
   it assumes `laya.load(model_id, max_len=...)` and
   `agent.predict(text, {name: {"type": "noul", "instructions": question}})` returning
   `{"answers": {name: {"noul": p}}}`. Read the installed package's source and docs
   (`pip show -f laya`, its README, the model card for
   `convaiinnovations/laya-multilingual`). If the real API differs, fix `app.py`,
   keeping the HTTP contract identical. Use Apple's GPU (`mps`) if the library supports it.
3. Start it on `0.0.0.0:8011` with `LAYA_MODEL=convaiinnovations/laya-multilingual`. The
   startup self-test must pass: an email sentence has to score above a clean one.
4. Smoke test from this Mac: `/health`, then `/classify` with the nine questions from
   `pii_taxonomy.py` on 3 texts: one with an email and phone, one with an API key
   (`sk-ant-api03-` plus 40 random characters), one clean. Report the probabilities and
   latency per text.
5. Make it survive reboots: a `launchd` agent (`~/Library/LaunchAgents/`) with
   KeepAlive, logs to a file, and the virtualenv's python. Show me the plist.
6. Confirm the gateway box can reach it: `curl http://192.168.1.187:8011/health` from
   there, if you have SSH (`ssh tadeu718@192.168.1.184`). If not, tell me and I'll run it.
7. Commit any `app.py` fixes on a branch `laya-sidecar-mac` and push. Don't merge.

**Report back:** the actual laya API, any `app.py` changes, smoke-test numbers, and the
launchd setup. I'll then turn the gateway's shadow mode on.

## Part 2: fine-tune the gate on our data

### Data

On the gateway box, in `/home/tadeu718/llm_gateway/data/pii-dataset/`:

- `synthetic.jsonl`: 5,000 generated examples, exact labels, all nine categories plus
  hard negatives (placeholder keys, test cards, example.com, internal IPs).
- `backlog.clean.jsonl`: about 20,000 unique texts from real gateway traffic, labelled by
  phi4-mini. **These labels are noisy** ("silver" labels): about 13% of texts have
  spans. Known issues: business phone numbers and emails on scraped web pages are labelled
  CONTACT, and names, health and special-category data are under-labelled (phi4-mini
  missed about 60% of names in a test).

Copy them with `scp tadeu718@192.168.1.184:/home/tadeu718/llm_gateway/data/pii-dataset/{synthetic,backlog.clean}.jsonl <private dir>/`.
If `backlog.clean.jsonl` doesn't exist yet, the labelling run hasn't finished. Do
Part 1 and stop there.

Record format (one JSON object per line):

```json
{"id": "...", "shape": "...", "text": "...",
 "spans": [{"category": "CONTACT", "value": "...", "start": 6, "end": 23}],
 "source": "backlog" | "synthetic", "labeler": "phi4-mini" | "generator",
 "hallucinated": 0, "rejected": 0}
```

The target per (text, category) is **1 if any span of that category exists, else 0**.
Texts longer than ~6k tokens: chunk them the way the gateway does (24,000 characters,
200 overlap) and label each chunk by the spans that fall inside it.

### Training

1. **Find out how Laya is meant to be fine-tuned.** Look for a training/fine-tune API in
   the package or repo, and look at how the vendor's fine-tuned results were produced
   (their benchmark claims 0.362 base vs 0.766 fine-tuned). Prefer the supported route.
   If there isn't one, fine-tune the underlying encoder as a 9-label multi-label
   classifier (sigmoid head, binary cross-entropy) with Hugging Face `transformers` on
   `mps`, and adapt `app.py` to serve it under the same contract. In that case the
   `questions` field is accepted but unused; say so in `/health`.
2. **Splits:** hold out 10% of the backlog (grouped by `shape`, so near-duplicates don't
   leak across the split) and 10% of the synthetic set. Never train on the held-out data.
3. **Noisy labels:** weight synthetic examples above backlog ones, and try label smoothing
   or loss down-weighting on backlog positives. Report what you tried.
4. **Recall first:** pick the per-category threshold that gives at least 95% recall on the
   held-out synthetic set, and report the false-positive rate it costs. Calibrate
   (temperature scaling is fine) so 0.3 means something.

### Evaluate base vs tuned, same held-out data

A table per category: AUROC, recall and false-positive rate at the chosen threshold,
plus the false-positive rate on synthetic hard negatives. Add latency per text on this
Mac for both models (short ~200 characters, long ~20,000 characters).

### Deploy

Save the checkpoint to a local path (not the Hub). Run it on port 8011 by pointing
`LAYA_MODEL` at it, but keep the base model runnable (e.g. on 8012 on demand) so we can
compare. Update the launchd agent, and push sidecar changes on the `laya-sidecar-mac`
branch.

**Report back:** the eval table, the thresholds, which training route you used and
why, the checkpoint path, and anything in the data that looked wrong. If base Laya
already does well on some categories, say so. Not every category needs the fine-tune
to win.
