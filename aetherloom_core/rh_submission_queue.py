"""FIFO retries for RunningHub submissions rejected while the service is busy.

Click/batch order is reserved before uploads start. Initial requests may overlap,
but retries follow that order regardless of response timing. A head stays first
until confirmed running, exhausted, or cancelled; followers have no retry polling loop.
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

    def reserve_orders(self, count):
        """Capture click/batch order before uploads or worker scheduling race."""
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
            self.condition.notify_all()

    def task_status(self, task_id, status):
        """Advance on confirmed execution (or its outcome), never on taskId alone."""
        if status not in {'RUNNING', 'DOWNLOADING', 'DOWNLOAD_FAILED', 'SUCCESS', 'FAILED', 'CANCELED'}:
            return
        with self.condition:
            order = self._awaiting_start.pop(str(task_id), None)
            if order is not None:
                self._pending_orders.discard(order)
                self.condition.notify_all()

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
                self._pending_orders.discard(item.get('_submission_order'))
                card = item.get('card')
                if card is not None:
                    card._rh_cancelled = True
            removed_ids = {id(item) for item in removed}
            self.owner._rh_retry_queue[:] = [item for item in self.owner._rh_retry_queue
                                            if id(item) not in removed_ids]
            self.condition.notify_all()
            return removed

    def cancel_all(self):
        return self.cancel_matching(lambda item: True)

    def wake(self):
        """Wake first submissions waiting for a slot after their card is cancelled."""
        with self.condition:
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            for entry in self.owner._rh_retry_queue:
                entry['_cancelled'] = True
            self.owner._rh_retry_queue.clear()
            self._pending_orders.clear()
            self._awaiting_start.clear()
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
                            if (not items or items[0] is not entry
                                    or any(previous < order for previous in self._pending_orders)):
                                # Followers have no retry timer: only a queue change wakes them.
                                self.condition.wait()
                                continue
                            remaining = due - time.monotonic()
                            if remaining > 0:
                                self.condition.wait(remaining)
                                continue
                        elif (any(item['_submission_order'] < order for item in self.owner._rh_retry_queue)
                              or any(previous < order for previous in self._awaiting_start.values())):
                            # New submissions cannot take slots ahead of an older retry.
                            self.condition.wait()
                            continue
                        if self.owner._rh_retry_active < max(1, int(concurrency)):
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
                    response = request()
                    if isinstance(response, dict):
                        retry_code = response.get('code', response.get('errcode', response.get('status')))
                except SubmissionCancelled as exc:
                    error = exc
                except Exception as exc:
                    error = exc
                    retry_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                try:
                    try:
                        retry_code = int(retry_code)
                    except (TypeError, ValueError, OverflowError):
                        retry_code = None
                    if retry_code in (415, 421) and attempt < max_retries:
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
                    if isinstance(response, dict) and str(response.get('code')) == '0':
                        data = response.get('data')
                        task_id = data.get('taskId') if isinstance(data, dict) else None
                        task_id = task_id if task_id is not None else response.get('taskId')
                        if (isinstance(task_id, (str, int)) and not isinstance(task_id, bool)
                                and str(task_id).strip()):
                            with self.condition:
                                if not self.closed:
                                    self._awaiting_start[str(task_id).strip()] = order
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
                if queued:
                    self._remove(entry)
                self.condition.notify_all()
