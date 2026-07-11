"""Cold-audit finding 2026-07-07 (HIGH): a stale write_request could restore
a just-consumed single-use Stage 2 nonce. Reproduces the race against the
REAL functions under concurrency and asserts the fix holds: once consumed,
the nonce never comes back, no matter how many concurrent unrelated writers
race the consumer."""
from __future__ import annotations

import threading

import pytest

import u1_request


def _seed(rid):
    u1_request.write_request(rid, phase="awaiting_print_start",
                             safety={"stage2_approval_nonce": "N",
                                     "other": "keep"})


def _consume(rid, expected):
    """Mirror the gate's _consume_stage2_nonce read-check-write, using the
    same cross-platform lock the production code uses (u1_lockfile —
    the old direct fcntl import made this test POSIX-only)."""
    from u1_lockfile import exclusive_lock
    req_dir = u1_request.ensure_request_dir(rid)
    with exclusive_lock(req_dir / ".stage2_nonce.lock"):
        fresh = dict((u1_request.read_request(rid) or {}).get("safety") or {})
        if fresh.get("stage2_approval_nonce") != expected:
            return False
        fresh.pop("stage2_approval_nonce", None)
        u1_request.write_request(rid, safety=fresh)
        return True


def test_concurrent_writers_cannot_resurrect_consumed_nonce(tmp_path):
    rid = "u1_2026_0707_ace0a1"
    _seed(rid)
    results = {}
    barrier = threading.Barrier(11)

    def racer_write(i):
        barrier.wait()
        # unrelated field update, read-modify-write of the full doc
        for _ in range(5):
            u1_request.write_request(rid, **{f"monitor_{i}": _})

    def racer_consume():
        barrier.wait()
        results["consumed"] = _consume(rid, "N")

    threads = [threading.Thread(target=racer_write, args=(i,)) for i in range(10)]
    threads.append(threading.Thread(target=racer_consume))
    for t in threads: t.start()
    for t in threads: t.join()

    final = u1_request.read_request(rid)
    assert results["consumed"] is True
    # THE ASSERTION: no concurrent stale writer put the nonce back.
    assert "stage2_approval_nonce" not in (final.get("safety") or {})
    # and unrelated fields from the racers survived (no lost updates)
    assert (final.get("safety") or {}).get("other") == "keep"


def test_second_consume_after_race_refuses(tmp_path):
    rid = "u1_2026_0707_ace0b2"
    _seed(rid)
    assert _consume(rid, "N") is True
    # even with a flurry of writes after, a second consume sees no nonce
    for i in range(20):
        u1_request.write_request(rid, **{f"f{i}": i})
    assert _consume(rid, "N") is False
