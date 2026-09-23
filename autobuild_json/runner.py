"""Bounded scheduling with per-account/per-proxy reservations."""
import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .errors import FlowError
from .models import AttemptContext, RunResult


class Runner:
    def __init__(self, store, processor):
        self.store, self.processor = store, processor
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = None
        self._closed = False
        self._errors = {}

    def start(self, report, proxies, mode, workers, timeout):
        if (mode not in {"sequential", "parallel"} or not isinstance(workers, int)
                or not 1 <= workers <= 16 or not math.isfinite(timeout) or not 1 <= timeout <= 600
                or not report.accounts):
            raise FlowError("INVALID_INPUT", "start")
        with self._lock:
            if self._closed:
                raise FlowError("CONFIGURATION_ERROR", "start")
            if self._active is not None:
                raise FlowError("BATCH_CONFLICT", "start")
            job = self.store.create(report)
            cancelled = threading.Event()
            thread = threading.Thread(target=self._run,
                args=(job, list(report.accounts), list(proxies), 1 if mode == "sequential" else workers, timeout, cancelled),
                name="oauth-coordinator", daemon=True)
            self._jobs[job] = (thread, cancelled)
            self._active = job
            thread.start()
            return job

    def _execute(self, account, proxy, context):
        try:
            context.check()
            result = self.processor(account, proxy, context)
            if not isinstance(result, RunResult):
                raise TypeError()
            return result
        except FlowError as exc:
            status = "phone_verify" if exc.code == "PHONE_VERIFY" else "cancelled" if exc.code == "CANCELLED" else "error"
            return RunResult(status, error=exc.with_stage(exc.stage))
        except Exception:
            return RunResult("error", error=FlowError("UNEXPECTED_ERROR"))

    def _run(self, job, pending, proxies, workers, timeout, cancelled):
        futures, emails, busy_proxies = {}, set(), set()
        proxy_cursor = 0
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="oauth-account")
        try:
            self.store.set_batch(job, "running")
            while pending or futures:
                if cancelled.is_set():
                    while pending:
                        account = pending.pop(0)
                        self.store.update(job, account.account_job_id, status="cancelled", stage="queued", attempt=0,
                            result=RunResult("cancelled", error=FlowError("CANCELLED", "queued")))
                while pending and len(futures) < workers and not cancelled.is_set():
                    index = next((i for i,a in enumerate(pending) if a.email.casefold() not in emails), None)
                    if index is None:
                        break
                    proxy = None
                    if proxies:
                        chosen = next(((proxy_cursor + offset) % len(proxies) for offset in range(len(proxies))
                            if proxies[(proxy_cursor + offset) % len(proxies)].key not in busy_proxies), None)
                        if chosen is None:
                            break
                        proxy = proxies[chosen]
                        proxy_cursor = (chosen + 1) % len(proxies)
                        busy_proxies.add(proxy.key)
                    account = pending.pop(index)
                    emails.add(account.email.casefold())
                    self.store.update(job, account.account_job_id, status="running", stage="initialize", attempt=1)
                    def progress(stage, attempt, account_id=account.account_job_id):
                        self.store.update(job, account_id, status="running", stage=stage, attempt=attempt)
                    context = AttemptContext(time.monotonic() + timeout, cancelled, progress)
                    future = pool.submit(self._execute, account, proxy, context)
                    futures[future] = (account.account_job_id, account.email.casefold(), proxy.key if proxy else None)
                    del account
                if not futures:
                    continue
                done, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    account_id, email, proxy_key = futures.pop(future)
                    result = future.result()
                    if result.error and result.error.code == "STORAGE_ERROR":
                        raise FlowError("STORAGE_ERROR", "storage")
                    self.store.update(job, account_id, status=result.status,
                        stage=result.error.stage if result.error else "complete", attempt=result.attempt, result=result)
                    emails.remove(email)
                    busy_proxies.discard(proxy_key)
                done.clear()
            self.store.set_batch(job, "cancelled" if cancelled.is_set() else "completed")
        except Exception as exc:
            cancelled.set()
            error = FlowError("STORAGE_ERROR" if isinstance(exc, (OSError, FlowError)) else "UNEXPECTED_ERROR", "batch")
            with self._lock:
                self._errors[job] = {"code":error.code, "reason":error.reason}
            try:
                self.store.fail_batch(job, error)
            except FlowError:
                pass
        finally:
            pending.clear()
            pool.shutdown(wait=True, cancel_futures=True)
            futures.clear()
            with self._lock:
                if self._active == job:
                    self._active = None

    def get(self, job_id):
        snapshot = self.store.snapshot(job_id)
        with self._lock:
            if job_id in self._errors:
                snapshot.update(status="error", batch_error=dict(self._errors[job_id]))
        return snapshot

    def stop(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                self.store.snapshot(job_id)
                return
            thread, event = self._jobs[job_id]
            if thread.is_alive():
                event.set()
                self.store.set_batch(job_id, "stopping")

    def wait(self, job_id, timeout):
        with self._lock:
            pair = self._jobs.get(job_id)
        if pair:
            pair[0].join(timeout)
            if pair[0].is_alive():
                raise TimeoutError("Batch still running")

    def close(self):
        with self._lock:
            self._closed = True
            pairs = list(self._jobs.values())
            for _, event in pairs:
                event.set()
        deadline = time.monotonic() + 35
        for thread, _ in pairs:
            thread.join(max(0, deadline - time.monotonic()))
