"""One FIFO for every activated App submission, independent of its source.

The configured first N tasks upload, try credentials and submit/retry. Each
admitted task keeps its slot until confirmed running or terminal; running cloud
tasks continue concurrently in the independent lifecycle status/download pools.
"""

import threading
import time


class SubmissionCancelled(RuntimeError):
    """No remote task was accepted before a local submission was cancelled."""


_INIT_LOCK = threading.Lock()


def get_submission_queue(owner):
    """Return one coordinator, also supporting the isolated worker test owner."""
    with _INIT_LOCK:
        queue = getattr(owner, '_rh_submission_queue', None)
        if queue is None:
            queue = SubmissionQueue(owner)
            owner._rh_submission_queue = queue
        return queue


class SubmissionQueue:
    MAX_ADMISSION_LIMIT = 16

    def __init__(self, owner):
        self.owner = owner
        if getattr(owner, '_rh_retry_lock', None) is None:
            owner._rh_retry_lock = threading.Lock()
        if not isinstance(getattr(owner, '_rh_retry_queue', None), list):
            owner._rh_retry_queue = []
        if not hasattr(owner, '_rh_retry_active'):
            owner._rh_retry_active = 0
        self.condition = threading.Condition(owner._rh_retry_lock)
        self.closed = False
        self._next_order = 0
        self._pending_orders = set()
        self._awaiting_start = {}
        self._admitted_orders = set()
        self._dispatch_listeners = []
        self.admission_limit = 1
        self.set_admission_limit(getattr(owner, 'rh_retry_head_count', 1))

    def set_admission_limit(self, value):
        """Resize safely: existing admissions survive a reduction of the limit."""
        try:
            limit = max(1, min(self.MAX_ADMISSION_LIMIT, int(value)))
        except (ValueError, TypeError, OverflowError):
            limit = 1
        with self.condition:
            self.admission_limit = limit
            self.condition.notify_all()
        self._notify_dispatch()
        return limit

    def subscribe_dispatch(self, callback):
        with self.condition:
            self._dispatch_listeners.append(callback)
        def unsubscribe():
            with self.condition:
                if callback in self._dispatch_listeners:
                    self._dispatch_listeners.remove(callback)
        return unsubscribe

    def _notify_dispatch(self):
        # Never invoke a service callback while holding the queue lock.
        with self.condition:
            callbacks = tuple(self._dispatch_listeners)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def _eligible(self, order):
        if order in self._admitted_orders:
            return True
        return (order in self._pending_orders and
                len(self._admitted_orders) < self.admission_limit and
                not any(previous < order and previous not in self._admitted_orders
                        for previous in self._pending_orders))

    def can_dispatch(self, order):
        with self.condition:
            return not self.closed and self._eligible(order)

    def reserve_orders(self, count):
        """Reserve only actual submissions, never unactivated workflow nodes."""
        with self.condition:
            orders = tuple(range(self._next_order, self._next_order + count))
            self._next_order += count
            self._pending_orders.update(orders)
            return orders

    def release_order(self, order):
        with self.condition:
            if order in self._awaiting_start.values():
                return  # Returning from submit or pausing its worker is not RUNNING.
            self._pending_orders.discard(order)
            self._admitted_orders.discard(order)
            self.condition.notify_all()
        self._notify_dispatch()

    def release_orders(self, orders):
        """Drop local waiting positions in one wake-up during bulk shutdown."""
        with self.condition:
            retained = set(self._awaiting_start.values())
            released = set(orders) - retained
            self._pending_orders.difference_update(released)
            self._admitted_orders.difference_update(released)
            self.condition.notify_all()
        self._notify_dispatch()

    def task_status(self, task_id, status):
        """Advance on confirmed execution (or its outcome), never on taskId alone."""
        if status not in {'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'SUCCESS', 'FAILED', 'CANCELED', 'INTERRUPTED', 'UNKNOWN'}:
            return
        with self.condition:
            order = self._awaiting_start.pop(str(task_id), None)
            if order is not None:
                self._pending_orders.discard(order)
                self._admitted_orders.discard(order)
                self.condition.notify_all()
        if order is not None:
            self._notify_dispatch()

    def waiting_for_start(self, order):
        with self.condition:
            return any(previous < order for previous in self._awaiting_start.values())

    def _stopped(self, cancelled):
        return (self.closed or getattr(self.owner, '_closing', False)
                or getattr(self.owner, '_rh_retry_cancel_all', False) or cancelled())

    def _remove(self, entry):
        self.owner._rh_retry_queue[:] = [item for item in self.owner._rh_retry_queue if item is not entry]
        self.condition.notify_all()

    def cancel_matching(self, predicate):
        """Cancel pending entries and immediately wake the next eligible head."""
        with self.condition:
            removed = [item for item in self.owner._rh_retry_queue if predicate(item)]
            for item in removed:
                item['_cancelled'] = True
                if not item.get('_submitting'):
                    self._pending_orders.discard(item.get('_submission_order'))
                    self._admitted_orders.discard(item.get('_submission_order'))
                # A canceled POST can still return an accepted taskId. Its
                # gate stays until cloud start/termination is confirmed; card
                # flags are projections, never evidence of cloud cancellation.
            removed_ids = {id(item) for item in removed}
            self.owner._rh_retry_queue[:] = [item for item in self.owner._rh_retry_queue
                                            if id(item) not in removed_ids]
            self.condition.notify_all()
        self._notify_dispatch()
        return removed

    def cancel_all(self):
        return self.cancel_matching(lambda item: True)

    def wake(self):
        """Wake first submissions waiting for a slot after their card is cancelled."""
        with self.condition:
            self.condition.notify_all()
        self._notify_dispatch()

    def close(self):
        with self.condition:
            self.closed = True
            for entry in self.owner._rh_retry_queue:
                entry['_cancelled'] = True
            self.owner._rh_retry_queue.clear()
            self._pending_orders.clear()
            self._awaiting_start.clear()
            self._admitted_orders.clear()
            self.condition.notify_all()

    def submit(self, request, entry, *, max_retries, delay, concurrency, cancelled,
               on_wait=None, on_submit=None):
        entry = dict(entry)
        with self.condition:
            order = entry.get('_submission_order')
            if order not in self._pending_orders:
                order = self._next_order
                self._next_order += 1
                self._pending_orders.add(order)
            entry['_submission_order'] = order
        queued = False
        due = 0.0
        try:
            for attempt in range(max(0, int(max_retries)) + 1):
                with self.condition:
                    while True:
                        if self._stopped(cancelled) or entry.get('_cancelled', False):
                            raise SubmissionCancelled('Submission cancelled')
                        if queued:
                            items = self.owner._rh_retry_queue
                            if not any(item is entry for item in items):
                                raise SubmissionCancelled('Submission removed from retry queue')
                            remaining = due - time.monotonic()
                            if remaining > 0:
                                self.condition.wait(remaining)
                                continue
                        if not self._eligible(order):
                            self.condition.wait()
                            continue
                        if self.owner._rh_retry_active < max(1, int(concurrency)):
                            self._admitted_orders.add(order)
                            self.owner._rh_retry_active += 1
                            entry['_submitting'] = True
                            break
                        self.condition.wait()

                response = None
                error = None
                retry_code = None
                try:
                    if self._stopped(cancelled) or entry.get('_cancelled', False):
                        raise SubmissionCancelled('Submission cancelled')
                    if on_submit is not None:
                        on_submit()
                    self._notify_dispatch()
                    response = request()
                    if isinstance(response, dict):
                        retry_code = response.get('code', response.get('errcode', response.get('status')))
                except SubmissionCancelled as exc:
                    error = exc
                except Exception as exc:
                    error = exc
                    # HTTP 415/421 are media/misdirection errors, not the RH
                    # business envelope's busy codes. An ambiguous POST is final.
                try:
                    try:
                        retry_code = int(retry_code)
                    except (TypeError, ValueError, OverflowError):
                        retry_code = None
                    from api_calls.call_rh import accepted_task_id
                    task_id = accepted_task_id(response)
                    if not task_id and retry_code in (415, 421) and attempt < max_retries:
                        with self.condition:
                            if self._stopped(cancelled) or entry.get('_cancelled', False):
                                raise SubmissionCancelled('Submission cancelled')
                            if not queued:
                                self.owner._rh_retry_queue.append(entry)
                                self.owner._rh_retry_queue.sort(key=lambda item: item['_submission_order'])
                                queued = True
                            entry.update(reason=str(retry_code), attempt=attempt + 1)
                            due = time.monotonic() + max(0.0, float(delay))
                            self.condition.notify_all()
                        if on_wait is not None:
                            on_wait(attempt + 1, str(retry_code))
                        continue
                    if error is not None:
                        raise error
                    if task_id:
                        with self.condition:
                            if not self.closed:
                                self._awaiting_start[task_id] = order
                    # An accepted task ID must reach the caller even during close/cancel.
                    return response
                finally:
                    with self.condition:
                        entry['_submitting'] = False
                        self.owner._rh_retry_active = max(0, self.owner._rh_retry_active - 1)
                        self.condition.notify_all()
        finally:
            with self.condition:
                if order not in self._awaiting_start.values():
                    self._pending_orders.discard(order)
                    self._admitted_orders.discard(order)
                if queued:
                    self._remove(entry)
                self.condition.notify_all()
            self._notify_dispatch()
