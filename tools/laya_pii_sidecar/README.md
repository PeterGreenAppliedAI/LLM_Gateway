# Laya PII gate sidecar (D-052)

Runs the PII gate on the Mac alongside the harness's routing Laya (`:8010`).
Separate instance, separate port (`:8011`): the routing Laya belongs to the
harness and its checkpoint isn't PII-tuned.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install laya fastapi uvicorn
LAYA_MODEL=convaiinnovations/laya-multilingual uvicorn app:app --host 0.0.0.0 --port 8011
```

On startup it runs a self-test (an email sentence must score above a clean one)
and refuses to start if the installed `laya` API returns something unexpected.

Point the gateway at it (gateway box `.env`):

```bash
GATEWAY_PII_GATE_ENABLED=true
GATEWAY_PII_GATE_URL=http://192.168.1.187:8011
GATEWAY_PII_GATE_FINDER_URL=http://10.0.0.14:11434   # 3060, serves phi4-mini
GATEWAY_PII_GATE_FINDER_MODEL=phi4-mini
```

Shadow mode only measures: requests are never changed. Results appear on
`/health` (`pii_gate`) and in the `pii_gate_shadow` table.

**Base vs tuned model.** The stock checkpoint has never been trained on PII;
expect weak separation until the D-052 fine-tune lands. That's what shadow
mode is for: it measures the stock model now and gives the baseline the
tuned one has to beat. Swap `LAYA_MODEL` to the fine-tuned checkpoint path
when it exists.
