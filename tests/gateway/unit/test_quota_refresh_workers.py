import asyncio

import pytest

from autobuild_json.gateway.accounts.quota_refresh_jobs import _join_workers, _release_advisory_lock


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [True, False])
async def test_worker_cleanup_finishes_before_owner_returns_even_when_cancelled_again(failure):
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    calls = 0

    async def worker():
        nonlocal calls
        calls += 1
        if calls == 1:
            await entered.wait()
            if failure:
                raise ValueError('worker failed')
            await asyncio.Event().wait()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    owner = asyncio.create_task(_join_workers(worker, 2))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if not failure:
            owner.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        owner.cancel()
        # Drain scheduled cancellation callbacks, without a wall-clock sleep.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not owner.done(), 'Lock owner must not finish while a sibling is cleaning up'
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_advisory_lock_connection_is_invalidated_when_unlock_fails():
    class Connection:
        def __init__(self):
            self.invalidated = False

        async def execute(self, statement):
            raise RuntimeError("unlock failed")

        async def commit(self):
            raise AssertionError("commit must not run after failed unlock")

        async def invalidate(self):
            self.invalidated = True

    connection = Connection()
    await _release_advisory_lock(connection)
    assert connection.invalidated is True
