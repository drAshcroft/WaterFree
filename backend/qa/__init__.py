"""`waterfree qa` — a local-model QA agent that exercises a running web app.

The harness owns Playwright. The model only chooses one action per turn from a
small grammar (see `actions.py`), fed by a compact DOM snapshot (`snapshot.py`).
Everything deterministic -- console errors, failed requests, clipped controls --
is collected by the harness, not the model. See docs/19_QA_AGENT.md.
"""
