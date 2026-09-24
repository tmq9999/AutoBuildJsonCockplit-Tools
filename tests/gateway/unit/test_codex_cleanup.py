"""Exercise the real public deadline wrappers with controlled async I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autobuild_json.gateway.accounts.quota import CodexQuotaService
from autobuild_json.gateway.accounts.reset_credits import ResetCreditService
from autobuild_json.gateway.errors import GatewayError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('kind', ['reset', 'quota'])
@pytest.mark.parametrize('cancel', [False, True])
async def test_cleanup_expiry_preserves_signal_and_joins_owner(kind, cancel):
    entered, cleaning, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    baseline = asyncio.all_tasks()
    owners = []

    async def blocked_io(*args):
        owners.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                # The real five-second cleanup budget cancels this work. No
                # clock/sleep monkeypatch or production wrapper is replaced.
                await asyncio.Event().wait()
            finally:
                ended.set()

    end = datetime.now(timezone.utc) + timedelta(seconds=25 if cancel else .1)
    if kind == 'reset':
        service = ResetCreditService(SimpleNamespace(store=SimpleNamespace(replay_reset=blocked_io)))
        call = service.consume(uuid4(), uuid4(), 1, True, 'admin', end)
    else:
        service = CodexQuotaService(None, None, None, None, None, None)
        service.resolver = SimpleNamespace(policy=blocked_io)
        call = service.refresh(uuid4(), end)
    task = asyncio.create_task(call)
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    if cancel:
        # A second external cancellation must not detach or abort joining.
        task.cancel()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(GatewayError, match='deadline_exceeded') as caught:
            await task
        assert caught.value.status == 504
    assert ended.is_set() and all(owner.done() for owner in owners)
    assert not (asyncio.all_tasks() - baseline)


@pytest.mark.parametrize('cancel', [False, True])
async def test_reset_cleanup_storage_failure_is_safe_and_never_masks_cancel(cancel):
    entered, ended = asyncio.Event(), asyncio.Event()
    baseline = asyncio.all_tasks()

    async def worker(*args):
        args[-1]['preparing'] = True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    async def failed_recovery():
        raise RuntimeError('private storage connection sentinel')

    service = ResetCreditService(SimpleNamespace(store=SimpleNamespace(recover_expired=failed_recovery)))
    # Isolate the public wrapper's recovery path, not the unrelated state machine.
    service._consume = worker
    end = datetime.now(timezone.utc) + timedelta(seconds=25 if cancel else .1)
    task = asyncio.create_task(service.consume(uuid4(), uuid4(), 1, True, 'admin', end))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(GatewayError, match='storage_unavailable') as caught:
            await task
        assert caught.value.status == 503
        assert 'sentinel' not in str(caught.value)
    assert ended.is_set() and not (asyncio.all_tasks() - baseline)
