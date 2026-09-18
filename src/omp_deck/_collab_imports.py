import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ._terminal_commands import TerminalCommandConflict, fingerprint
from .collab_protocol import parse_collab_link
from .collab_store import CollabStore
from .fleet_model import FleetModel
from .registry_store import RegistryStore
from .request_ledger import RequestConflictError, RequestLedger


class CollabImportError(RuntimeError):
    def __init__(self, message: str, *, outcome_known: bool) -> None:
        super().__init__(message)
        self.outcome_known = outcome_known


class CollabImportCoordinator:
    """Creates Collab sessions and deduplicates imports without storing links."""

    def __init__(
        self,
        requests: RequestLedger,
        registry: RegistryStore,
        credentials: CollabStore,
        model: FleetModel,
        project_root: Path,
        connect: Callable[..., None],
        runtime: Callable[[str], Any],
        rollback: Callable[[str], list[str]],
    ) -> None:
        self.requests = requests
        self.registry = registry
        self.credentials = credentials
        self.model = model
        self.project_root = project_root
        self.connect = connect
        self.runtime = runtime
        self.rollback = rollback
        self._locks = tuple(threading.Lock() for _ in range(64))

    def dispatch(
        self,
        request_id: str,
        link: str,
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        parse_collab_link(link)
        request_fingerprint = fingerprint({"link": link, "title": title})
        scope = "collab-import"
        import_lock = self._locks[int(request_fingerprint[:2], 16) % len(self._locks)]
        with import_lock:
            try:
                persisted = self.requests.lookup(scope, request_id, request_fingerprint)
            except RequestConflictError:
                raise TerminalCommandConflict("requestId was already used for another Collab import") from None
            if persisted is not None:
                return self._result(persisted)
            with self.requests.admission(scope, request_id, request_fingerprint) as persisted:
                if persisted is not None:
                    return self._result(persisted)
                try:
                    session = self.add(link, title=title)
                except CollabImportError as error:
                    if not error.outcome_known:
                        raise
                    result = {
                        "requestId": request_id,
                        "status": "failed",
                        "errorCode": "collab_unavailable",
                    }
                    self.requests.complete(scope, request_id, request_fingerprint, result)
                    return result
                durable = {
                    "requestId": request_id,
                    "status": "accepted",
                    "sessionId": session["id"],
                }
                self.requests.complete(scope, request_id, request_fingerprint, durable)
                return {**durable, "session": session}

    def add(
        self,
        link: str,
        *,
        title: str | None = None,
        guest_name: str = "OMP Deck",
    ) -> dict[str, Any]:
        parse_collab_link(link)
        record = self.registry.create(
            adapter_kind="collab",
            project_name="Terminal",
            project_path=str(self.project_root),
            title=title or "Terminal session",
        )
        try:
            self.model.load_record(record)
            self.credentials.put(record["id"], link)
            self.connect(record, link, guest_name=guest_name)
            snapshot = self.runtime(record["id"]).adapter.snapshot()
            header = snapshot.header
            state = snapshot.state
            project_path = state["cwd"] if isinstance(state.get("cwd"), str) else str(self.project_root)
            project_name = Path(project_path).name or "Terminal"
            updated = self.registry.update(
                record["id"],
                omp_session_id=(header.get("id") if isinstance(header.get("id"), str) else None),
                project_name=project_name,
                project_path=project_path,
                title=(
                    header["title"]
                    if isinstance(header.get("title"), str) and header["title"]
                    else title or project_name
                ),
            )
            self.model.load_record(updated)
            runtime = self.runtime(record["id"])
            runtime.generation = updated["generation"]
            self.model.apply_collab_snapshot(
                record["id"],
                instance_id=runtime.instance_id,
                generation=runtime.generation,
                snapshot=snapshot,
            )
        except Exception as error:
            cleanup_errors = self.rollback(record["id"])
            message = "collaboration connection unavailable"
            if cleanup_errors:
                message += f"; rollback failed: {'; '.join(cleanup_errors)}"
            raise CollabImportError(
                message,
                outcome_known=not cleanup_errors,
            ) from error
        return self.model.get(record["id"])

    def _result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        response = dict(result)
        if response.get("status") != "accepted":
            return response
        try:
            response["session"] = self.model.get(response["sessionId"])
        except KeyError:
            return {
                "requestId": response["requestId"],
                "status": "stale",
                "errorCode": "session_removed",
            }
        return response
