from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

_Event = TypeVar("_Event")


class OrderedCallbackDelivery(Generic[_Event]):
    """Serialize callbacks by sequence while deferring same-thread reentrancy."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._next_sequence = 1
        self._owner: int | None = None
        self._deferred: dict[int, tuple[_Event, Callable[[_Event], None] | None]] = {}

    def deliver(
        self,
        sequence: int,
        event: _Event,
        callback: Callable[[_Event], None] | None,
    ) -> None:
        delivery = (event, callback)
        owner = threading.get_ident()
        with self._condition:
            if self._owner == owner:
                self._deferred[sequence] = delivery
                return
            while self._owner is not None or sequence != self._next_sequence:
                self._condition.wait()
            self._owner = owner
        failure: BaseException | None = None
        while delivery is not None:
            current_event, current_callback = delivery
            try:
                if current_callback is not None:
                    current_callback(current_event)
            except BaseException as error:
                if failure is None:
                    failure = error
            with self._condition:
                self._next_sequence += 1
                delivery = self._deferred.pop(self._next_sequence, None)
                if delivery is None:
                    self._owner = None
                    self._condition.notify_all()
        if failure is not None:
            raise failure
