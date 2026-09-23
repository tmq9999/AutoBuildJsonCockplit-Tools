"""Private, atomically replaced snapshots; public views exclude token records."""
import copy
import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .errors import FlowError

STATUSES = ("queued", "running", "success", "error", "phone_verify", "cancelled")
EXPORTS = {"success", "errors", "phone_verify", "filtered", "cancelled"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise FlowError("STORAGE_ERROR", "storage")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def atomic_json(path, data):
    """Caller supplies a controlled path in a private directory."""
    path = Path(path)
    temporary = None
    try:
        if path.is_symlink():
            raise OSError("Symlink target forbidden")
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except (OSError, TypeError, ValueError):
        raise FlowError("STORAGE_ERROR", "storage") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class RunStore:
    def __init__(self, root: Path):
        self.root = private_directory(root)
        self._lock = threading.RLock()
        self._cache = {}

    def _path(self, job_id):
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise FlowError("NOT_FOUND", "storage") from None
        directory = self.root / job_id
        path = directory / "run.json"
        if directory.is_symlink() or path.is_symlink():
            raise FlowError("NOT_FOUND", "storage")
        return path

    def _read(self, job_id):
        path = self._path(job_id)
        if job_id not in self._cache:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("id") != job_id or not isinstance(data.get("rows"), list):
                    raise ValueError()
                self._cache[job_id] = data
            except FileNotFoundError:
                raise FlowError("NOT_FOUND", "storage") from None
            except (OSError, ValueError):
                raise FlowError("STORAGE_ERROR", "storage") from None
        return self._cache[job_id]

    def _save(self, data):
        atomic_json(self._path(data["id"]), data)
        self._cache[data["id"]] = data

    def create(self, report):
        job_id = str(uuid.uuid4())
        data = {"id":job_id, "status":"queued", "batch_error":None, "created_at":timestamp(),
                "filtered":[asdict(row) for row in report.rejected], "rows":[], "records":{}}
        for account in report.accounts:
            data["rows"].append({"account_job_id":account.account_job_id, "line_number":account.line_number,
                "email":account.email, "status":"queued", "stage":"queued", "attempt":0,
                "code":None, "reason":None, "timestamp":timestamp()})
        with self._lock:
            try:
                private_directory(self.root / job_id)
                self._save(data)
            except OSError:
                raise FlowError("STORAGE_ERROR", "storage") from None
        return job_id

    def update(self, job_id, account_job_id, *, status, stage, attempt, result=None):
        if status not in STATUSES:
            raise ValueError("Invalid status")
        if status == "success" and (result is None or result.record is None):
            raise ValueError("Successful status requires a record")
        with self._lock:
            data = copy.deepcopy(self._read(job_id))
            row = next((r for r in data["rows"] if r["account_job_id"] == account_job_id), None)
            if row is None:
                raise FlowError("NOT_FOUND", "storage")
            if row["status"] in {"success", "error", "phone_verify", "cancelled"}:
                raise ValueError("Terminal result is immutable")
            row.update(status=status, stage=stage, attempt=attempt, timestamp=timestamp())
            if result and result.error:
                row.update(code=result.error.code, reason=result.error.reason)
            if result and result.record:
                data["records"][account_job_id] = result.record.model_dump()
            self._save(data)

    def set_batch(self, job_id, status, error=None):
        if status not in {"queued", "running", "stopping", "completed", "cancelled", "error"}:
            raise ValueError("Invalid batch status")
        with self._lock:
            data = copy.deepcopy(self._read(job_id))
            if data["status"] in {"completed", "cancelled", "error"} and status in {"queued", "running", "stopping"}:
                return
            data["status"] = status
            data["batch_error"] = {"code":error.code, "reason":error.reason} if error else None
            self._save(data)

    def snapshot(self, job_id):
        with self._lock:
            data = self._read(job_id)
            rows = copy.deepcopy(data["rows"])
            return {"id":job_id, "status":data["status"], "batch_error":copy.deepcopy(data["batch_error"]),
                    "rows":rows, "counts":{status:sum(row["status"] == status for row in rows) for status in STATUSES}}

    def list_runs(self):
        """Small metadata-only history for recovery without an in-memory job ID."""
        with self._lock:
            result = []
            for entry in self.root.iterdir():
                if not entry.is_dir() or entry.is_symlink():
                    continue
                try:
                    data = self._read(entry.name)
                    result.append({"id":data["id"], "status":data["status"], "created_at":data["created_at"]})
                except FlowError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
            return sorted(result, key=lambda item:item["created_at"], reverse=True)

    def export(self, job_id, kind):
        if kind not in EXPORTS:
            raise FlowError("NOT_FOUND", "export")
        with self._lock:
            data = self._read(job_id)
            if kind == "filtered":
                rows = data["filtered"]
            elif kind == "success":
                rows = [data["records"][r["account_job_id"]] for r in data["rows"] if r["status"] == "success"]
            else:
                status = "error" if kind == "errors" else kind
                rows = [r for r in data["rows"] if r["status"] == status]
            return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")

    def recover_interrupted(self, *, read_only_fallback=False):
        storage_ready = True
        with self._lock:
            for item in self.list_runs():
                data = copy.deepcopy(self._read(item["id"]))
                changed = data["status"] in {"queued", "running", "stopping"}
                for row in data["rows"]:
                    if row["status"] in {"queued", "running"}:
                        row.update(status="cancelled", code="INTERRUPTED", reason="Interrupted by application restart", timestamp=timestamp())
                        changed = True
                if changed:
                    data["status"] = "cancelled"
                    try:
                        self._save(data)
                    except FlowError as exc:
                        if not read_only_fallback or exc.code != "STORAGE_ERROR":
                            raise
                        # Preserve durable records; report interruption/error in a
                        # read-only in-memory view until the disk is writable again.
                        data["status"] = "error"
                        data["batch_error"] = {"code":exc.code, "reason":exc.reason}
                        self._cache[data["id"]] = data
                        storage_ready = False
        return storage_ready
