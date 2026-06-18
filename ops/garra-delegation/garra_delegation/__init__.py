"""garra_delegation — persistent, evidence-backed delegation for Garra.

Repairs the existing Garra→Flash/Alex flow (claude -p / hermes chat) by wrapping
it in a SQLite task store with explicit states, heartbeats, timeouts, retries,
dedup, a background worker and a recurring monitor — so Garra can only report
real, verifiable status (anti-hallucination).
"""
