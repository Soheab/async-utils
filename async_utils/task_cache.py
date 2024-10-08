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
from collections.abc import Callable, Coroutine, Hashable
from functools import partial
from typing import Any, ParamSpec, TypeVar
from weakref import WeakKeyDictionary

from ._cpython_stuff import make_key

__all__ = ("taskcache", "clear_cache", "remove_cache_entry")


P = ParamSpec("P")
T = TypeVar("T")


_caches: WeakKeyDictionary[Hashable, dict[Hashable, asyncio.Task[Any]]] = WeakKeyDictionary()


def taskcache(
    ttl: float | None = None,
) -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, asyncio.Task[T]]]:
    """
    Decorator to modify coroutine functions to instead act as functions returning cached tasks.

    For general use, this leaves the end user API largely the same,
    while leveraging tasks to allow preemptive caching.

    Note: This uses the args and kwargs of the original coroutine function as a cache key.
    This includes instances (self) when wrapping methods.
    Consider not wrapping instance methods, but what those methods call when feasible in cases where this may matter.

    The ordering of args and kwargs matters.
    """

    def wrapper(coro: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, asyncio.Task[T]]:
        internal_cache: dict[Hashable, asyncio.Task[T]] = {}

        def wrapped(*args: P.args, **kwargs: P.kwargs) -> asyncio.Task[T]:
            key = make_key(args, kwargs)
            try:
                return internal_cache[key]
            except KeyError:
                internal_cache[key] = task = asyncio.create_task(coro(*args, **kwargs))
                if ttl is not None:
                    # This results in internal_cache.pop(key, task) later
                    # while avoiding a late binding issue with a lambda instead
                    call_after_ttl = partial(
                        asyncio.get_running_loop().call_later,
                        ttl,
                        internal_cache.pop,
                        key,
                    )
                    task.add_done_callback(call_after_ttl)
                return task

        _caches[wrapped] = internal_cache
        return wrapped

    return wrapper


def clear_cache(f: Callable[..., Any]) -> None:
    """
    Clear the cache of a decorated function
    """
    cache = _caches.get(f)
    if cache is None:
        raise RuntimeError(f"{f:!r} is not a function wrapped with taskcache")
    cache.clear()


def remove_cache_entry(f: Callable[..., Any], *args: Hashable, **kwargs: Hashable):
    """
    Remove the cache entry for a specific arg/kwarg combination.

    The ordering of args and kwargs must match.

    Will not error under a missing key under the assumption that a race condition on removal
    for various reasons (such as ttl) could occur
    """

    cache = _caches.get(f)
    if cache is None:
        raise RuntimeError(f"{f:!r} is not a function wrapped with taskcache")
    cache.pop(make_key(args, kwargs))
