"""v2.2.1 #3: single-use confirm-token and Stage-2 nonce consumption must be
concurrency-safe. Without atomic claim/lock, two simultaneous callers (double-
click, gateway retry, duplicate Telegram delivery, two workers) could both
consume the same token/nonce. These spawn concurrent threads on a barrier and
assert EXACTLY ONE wins, repeated to shake out the race."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import u1_form  # noqa: E402
import u1_request  # noqa: E402
import u1_print_start_gate as g  # noqa: E402


def _race(fn, n=4):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()  # release all threads together for max contention
        results[i] = fn()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_confirm_token_single_use_under_concurrency():
    for _ in range(25):
        rid = u1_request.generate_request_id()
        token = u1_form.new_confirm_token()
        u1_form.persist_confirm_token(token, rid)
        results = _race(lambda: u1_form.resolve_confirm_token(token))
        winners = [r for r in results if r == rid]
        assert len(winners) == 1, f"expected exactly one token winner, got {winners}"


def test_stage2_nonce_single_consume_under_concurrency():
    for _ in range(25):
        rid = u1_request.generate_request_id()
        nonce = "nonce_" + rid
        u1_request.write_request(rid, safety={'stage2_approval_nonce': nonce})
        results = _race(lambda: g._consume_stage2_nonce(rid, nonce))
        winners = [r for r in results if r is True]
        assert len(winners) == 1, f"expected exactly one nonce consumer, got {sum(1 for r in results if r)}"
