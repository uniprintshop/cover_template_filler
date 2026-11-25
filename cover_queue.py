"""One bounded, deduplicating worker shared by file events and rescans."""

import logging
import queue
import threading
import time
from pathlib import Path

LOG = logging.getLogger("thesis-cover")


class PdfQueue:
    def __init__(self, ready, handle, *, retry_seconds=60, limit=256):
        self.ready, self.handle = ready, handle
        self.retry_seconds, self.limit = retry_seconds, limit
        self.items = queue.Queue(maxsize=limit)
        self.pending = set()
        self.delayed = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="pdf-worker", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, path: Path, origin="watch"):
        if path.suffix.lower() != ".pdf" or self.stopping.is_set():
            return False
        path = path.absolute()
        with self.lock:
            if path in self.pending or len(self.pending) >= self.limit:
                return False
            self.pending.add(path)
            self.items.put_nowait((path, origin, time.monotonic()))
        return True

    def _run(self):
        while not self.stopping.is_set():
            with self.lock:
                for path, (due, origin) in list(self.delayed.items()):
                    if due <= time.monotonic() and not self.items.full():
                        self.items.put_nowait((path, origin, time.monotonic()))
                        del self.delayed[path]
            try:
                path, origin, submitted = self.items.get(timeout=.2)
            except queue.Empty:
                continue
            retry = False
            try:
                queue_seconds = time.monotonic() - submitted
                started = time.monotonic()
                if path.exists():
                    if self.ready(path):
                        retry = self.handle(path, origin, {
                            "queue": queue_seconds, "settle": time.monotonic() - started,
                        }) is False
                    else:
                        retry = True
            except Exception:
                LOG.exception("Worker failed on %s; retrying later", path)
                retry = True
            finally:
                with self.lock:
                    if retry:
                        self.delayed[path] = (time.monotonic() + self.retry_seconds, origin)
                    else:
                        self.pending.discard(path)
                self.items.task_done()

    def stop(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join()
