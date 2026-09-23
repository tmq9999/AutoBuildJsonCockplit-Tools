import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID


@dataclass(frozen=True)
class LeaseToken:
    resource: str
    owner: UUID
    generation: int
    deadline: datetime


class MemoryLeaseStore:
    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._rows = {}
        self._disabled = set()

    async def disable_resource(self, resource):
        async with self._lock:
            self._disabled.add(resource)

    async def is_disabled(self, resource):
        async with self._lock:
            return resource in self._disabled

    async def claim(self, resource, owner, deadline):
        async with self._lock:
            now = self.clock()
            if deadline <= now or deadline > now + timedelta(seconds=600):
                raise ValueError("invalid_deadline")
            old = self._rows.get(resource)
            if old and old[1] and old[0].deadline + timedelta(seconds=15) > now:
                return None
            token = LeaseToken(resource, owner, old[0].generation + 1 if old else 1, deadline)
            self._rows[resource] = (token, True)
            return token

    async def release(self, token):
        async with self._lock:
            old = self._rows.get(token.resource)
            if old and old[0] == token:
                self._rows[token.resource] = (token, False)

    async def assert_owner(self, token):
        async with self._lock:
            return self._rows.get(token.resource) == (token, True) and token.deadline > self.clock()

    async def renew(self, token):
        return await self.assert_owner(token)
