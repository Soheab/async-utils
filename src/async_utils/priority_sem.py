#   Copyright 2020-present Michael Hall
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.


from __future__ import annotations

import asyncio
import contextvars
import heapq
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager

from ._typings import Any, Self

__all__ = ["PrioritySemaphore", "priority_context"]

_global_lock = threading.Lock()

_priority: contextvars.ContextVar[int] = contextvars.ContextVar("_priority", default=0)


class PriorityWaiter(tuple[int, float, asyncio.Future[None]]):
    __slots__ = ()

    def __new__(cls, priority: int, ts: float, future: asyncio.Future[None]) -> Self:
        return super().__new__(cls, (priority, ts, future))

    @property
    def priority(self) -> int:
        return self[0]

    @property
    def ts(self) -> float:
        return self[1]

    @property
    def future(self) -> asyncio.Future[None]:
        return self[2]

    @property
    def cancelled(self) -> Callable[[], bool]:
        return self.future.cancelled

    @property
    def done(self) -> Callable[[], bool]:
        return self.future.done

    def __await__(self) -> Generator[Any, Any, None]:
        return self.future.__await__()

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, PriorityWaiter):
            return NotImplemented
        return self[:2] < other[:2]


@contextmanager
def priority_context(priority: int, /) -> Generator[None, None, None]:
    """Set the priority for all PrioritySemaphore use in this context.

    Parameters
    ----------
    priority: int
        The priority to use. Lower values are of a higher priority.
    """
    token = _priority.set(priority)
    try:
        yield None
    finally:
        _priority.reset(token)


_default: Any = object()


class PrioritySemaphore:
    """A Semaphore with priority-based aquisition ordering.

    Provides a semaphore with similar semantics as asyncio.Semaphore,
    but using an underlying priority. priority is shared within a context
    manager's logical scope, but the context can be nested safely.

    Lower priority values are a higher logical priority

    Parameters
    ----------
    value: int
        The initial value of the internal counter.
        The number of things that can concurrently acquire it.

    Examples
    --------
    >>> sem = PrioritySemaphore(1)
    >>> with priority_ctx(10):
            async with sem:
                ...

    """

    _loop: asyncio.AbstractEventLoop | None = None

    def __init__(self, value: int = 1) -> None:
        if value < 0:
            msg = "Semaphore initial value must be >= 0"
            raise ValueError(msg)
        self._waiters: list[PriorityWaiter] | None = None
        self._value: int = value

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()

        if self._loop is None:
            with _global_lock:
                if self._loop is None:
                    self._loop = loop
        if loop is not self._loop:
            msg = f"{self!r} is bound to a different event loop"
            raise RuntimeError(msg)
        return loop

    def __repr__(self) -> str:
        res = super().__repr__()
        extra = "locked" if self.__locked() else f"unlocked, value:{self._value}"
        if self._waiters:
            extra = f"{extra}, waiters:{len(self._waiters)}"
        return f"<{res[1:-1]} [{extra}]>"

    def __locked(self) -> bool:
        # Must do a comparison based on priority then FIFO
        # in the case of existing waiters
        # not guaranteed to be immediately available
        return self._value == 0 or (any(not w.cancelled() for w in (self._waiters or ())))

    async def __aenter__(self) -> None:
        prio = _priority.get()
        await self.__acquire(prio)

    async def __aexit__(self, *dont_care: object) -> None:
        self.__release()

    async def __acquire(self, priority: int = _default) -> bool:
        if priority is _default:
            priority = _priority.get()
        if not self.__locked():
            self._value -= 1
            return True

        if self._waiters is None:
            self._waiters = []

        loop = self._get_loop()

        fut = loop.create_future()
        now = loop.time()
        waiter = PriorityWaiter(priority, now, fut)

        heapq.heappush(self._waiters, waiter)

        try:
            await waiter
            # unlike a normal semaphore, we don't remove ourselves
            # we need to maintain the heap invariants
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._value += 1
            raise

        finally:
            self._maybe_wake()
        return True

    def _maybe_wake(self) -> None:
        while self._value > 0 and self._waiters:
            next_waiter = heapq.heappop(self._waiters)

            if not (next_waiter.done() or next_waiter.cancelled()):
                self._value -= 1
                next_waiter.future.set_result(None)

        while self._waiters:
            # cleanup maintaining heap invariant
            # This will only fully empty the heap when
            # all things remaining in the heap after waking tasks in
            # above section are all done.
            next_waiter = heapq.heappop(self._waiters)
            if not (next_waiter.done() or next_waiter.cancelled()):
                heapq.heappush(self._waiters, next_waiter)
                break

    def __release(self) -> None:
        self._value += 1
        self._maybe_wake()
