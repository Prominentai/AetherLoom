"""FIFO retries for RunningHub submissions rejected while the service is busy.

Initial submissions retain their concurrency limit. Once rejected with 415/421,
an entry keeps the head position until submitted, exhausted, or cancelled. Other
entries sleep on a condition and never run their own retry polling loops.
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
            self.condition.notify_all()

    def submit(self, request, entry, *, max_retries, delay, concurrency, cancelled,
               on_wait=None, on_submit=None):
        entry = dict(entry)
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
                            if not items or items[0] is not entry:
                                # Followers have no retry timer: only a queue change wakes them.
                                self.condition.wait()
                                continue
                            remaining = due - time.monotonic()
                            if remaining > 0:
                                self.condition.wait(remaining)
                                continue
                        if self.owner._rh_retry_active < max(1, int(concurrency)):
                            self.owner._rh_retry_active += 1
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
                except SubmissionCancelled:
                    raise
                except Exception as exc:
                    error = exc
                    retry_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                finally:
                    with self.condition:
                        self.owner._rh_retry_active = max(0, self.owner._rh_retry_active - 1)
                        self.condition.notify_all()
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
                            queued = True
                        entry.update(reason=str(retry_code), attempt=attempt + 1)
                        due = time.monotonic() + max(0.0, float(delay))
                        self.condition.notify_all()
                    if on_wait is not None:
                        on_wait(attempt + 1, str(retry_code))
                    continue
                if error is not None:
                    raise error
                # An in-flight request can succeed after cancellation. Its taskId
                # must reach the caller so it can be persisted and cancelled remotely.
                return response
        finally:
            if queued:
                with self.condition:
                    self._remove(entry)
