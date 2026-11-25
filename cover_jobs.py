"""Stable input snapshots and durable, resumable output publication."""

import hashlib
import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


class RetryableJobError(RuntimeError):
    """Leave the source in the watch folder and retry on a later scan."""


def state_root(cfg):
    return Path(cfg.get("state_dir") or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "thesis-cover")


@contextmanager
def job_lock(source: Path, cfg: dict):
    """Protect the journal when --once and a watcher target the same source."""
    key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    path = state_root(cfg) / "locks" / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetryableJobError(f"Another worker owns {source.name}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    """Publish one complete file with a same-directory rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def staged_pdf(source: Path):
    """Read the network file once; extraction uses an immutable local copy."""
    with tempfile.TemporaryDirectory(prefix="thesis-cover-") as temporary:
        local = Path(temporary) / source.name
        digest = hashlib.sha256()
        try:
            before = source.stat()
            with source.open("rb") as incoming, local.open("wb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    digest.update(chunk)
                    outgoing.write(chunk)
            after = source.stat()
        except OSError as exc:
            raise RetryableJobError(f"Could not stage {source.name}: {exc}") from exc
        if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or not after.st_size):
            raise RetryableJobError(f"Source changed while being staged: {source.name}")
        yield local, digest.hexdigest()


class JobState:
    """One journal per source path, source content, and processing configuration.

    Generated variants are checkpointed independently of source archival. A
    failed archive move therefore never re-runs inference or duplicates covers.
    """

    def __init__(self, source: Path, digest: str, cfg: dict):
        relevant = {k: v for k, v in cfg.items()
                    if not k.startswith("_") and k not in ("force", "state_dir", "notify")}
        identity = json.dumps([str(source.resolve()), digest, relevant], sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.path = state_root(cfg) / "jobs" / f"{key}.json"
        if self.path.exists() and not cfg.get("force"):
            self.record = json.loads(self.path.read_text(encoding="utf-8"))
            if self.record.get("version") != 1:
                raise RuntimeError(f"Unsupported job journal version: {self.path}")
        else:
            self.record = {"version": 1, "generation": uuid.uuid4().hex[:12],
                           "source": str(source), "source_sha256": digest,
                           "published": {}, "completed": False}
            self.save()

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.record, ensure_ascii=False, indent=2).encode())

    def prepared(self, value=None):
        if value is not None:
            self.record["prepared"] = value
            self.save()
        # Callers mutate fields while filling variants; keep the checkpoint pristine.
        return json.loads(json.dumps(self.record.get("prepared")))

    def publish(self, path: Path, data: bytes) -> None:
        key = str(path)
        sha = hashlib.sha256(data).hexdigest()
        previous = self.record["published"].get(key)
        if previous:
            if previous != sha:
                raise RuntimeError(f"Prepared output changed during retry: {path.name}")
            return
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RuntimeError(f"Refusing to overwrite a different output: {path}")
        else:
            atomic_write(path, data)
        # If the process crashes after the rename, the hash check above recovers.
        self.record["published"][key] = sha
        self.save()

    def complete(self, outputs: list[Path]) -> None:
        self.record["outputs"] = [str(p) for p in outputs]
        self.record["completed"] = True
        self.save()
