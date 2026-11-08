"""Bound slow public I/O without accumulating abandoned request threads."""

from threading import Event, Lock, Thread

MAX_QUOTE_AGE_MS = 60_000
MAX_RECEIPT_AGE_MS = 3_000
MAX_CLOCK_SKEW_MS = 2_000
MARKET_CYCLE_SECONDS = 3.0


class BoundedMarketCall:
    """At most one request per lane, with a deadline for the calling worker.

    A timed-out socket cannot be forcibly canceled safely in Python. Its daemon
    job retains the lane until it finishes; subsequent polls do not spawn more
    requests. Late quotes are discarded. Late history may be reused only for
    its exact revision/bar key, and must still pass candle validation.
    """

    def __init__(self):
        self._lock = Lock()
        self._job = None

    def run(self, key, operation, timeout, *, reuse_completed=False):
        if timeout <= 0:
            raise ValueError("market_cycle_deadline")
        with self._lock:
            job = self._job
            if job is not None:
                if not job["done"].is_set():
                    raise ValueError("market_request_pending")
                self._job = None
                if reuse_completed and job["key"] == key:
                    return self._result(job)
            job = {"key": key, "done": Event()}
            self._job = job

            def work():
                try:
                    job["result"] = operation()
                except Exception:
                    job["error"] = True
                finally:
                    job["done"].set()

            Thread(target=work, name="tradingbot-market-io", daemon=True).start()
        if not job["done"].wait(timeout):
            raise ValueError("market_request_timeout")
        with self._lock:
            if self._job is job:
                self._job = None
        return self._result(job)

    @staticmethod
    def _result(job):
        if job.get("error"):
            raise ValueError("market_data_unavailable")
        return job["result"]
