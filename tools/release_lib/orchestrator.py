from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from omp_deck._private_state import PrivateStateFile

from .build import artifact, compatible, prepare_build
from .model import configuration, deployment_order
from .release_lease import operator_lock
from .storage import MAX_JSON, ReleaseError, absolute, directory, identifier, load, save
from .transport import Transport, run_browser


class Release:
    def __init__(self, config: dict, repository: Path, transaction: str, *, transport=Transport):
        self.config = config
        self.repository = repository
        self.transaction = identifier(transaction)
        self.state = directory(absolute(config["state_root"]))
        self.root = self.state / self.transaction
        self.path = self.root / "release.json"
        self.transport_class = transport
        self.journal = load(self.path) if self.path.exists() else None

    def save(self):
        self.journal["updatedAt"] = int(time.time())
        save(self.path, self.journal)

    @contextlib.contextmanager
    def lease(self, *, initialize=False):
        """A durable fleet claim paired with an OS-owned process lifetime lock."""
        store = PrivateStateFile(self.state / "fleet.json", repair_root=False)
        nonce = secrets.token_hex(16)
        with operator_lock(self.state / "operator.lock"):
            with store.locked():
                raw = store.read(MAX_JSON)
                fleet = json.loads(raw) if raw else {"version": 1, "targets": {}}
                if fleet.get("active") not in {None, self.transaction}:
                    raise ReleaseError("an unfinished release must be resumed before preparing another")
                if initialize:
                    if self.journal:
                        raise ReleaseError("release already exists; use status or resume")
                    self.journal = {
                        "version": 1,
                        "transaction": self.transaction,
                        "state": "preparing",
                        "targets": [],
                    }
                    try:
                        self.save()
                    except BaseException:
                        self.journal = None
                        raise
                fleet.update(active=self.transaction, owner={"pid": os.getpid(), "nonce": nonce})
                store.replace(json.dumps(fleet).encode())
            try:
                yield fleet
            finally:
                with store.locked():
                    current = json.loads(store.read(MAX_JSON))
                    if current.get("owner", {}).get("nonce") == nonce:
                        current["owner"] = None
                        # On uncertainty retain active ownership: another release must
                        # not overwrite rollback assets while a watchdog may be working.
                        if self.journal and self.journal["state"] in {
                            "verified",
                            "rolledback",
                            "prepare_failed",
                        }:
                            current["active"] = None
                        current["targets"] = fleet["targets"]
                        store.replace(json.dumps(current).encode())

    def transport(self, entry: dict) -> Transport:
        local = directory(self.root / entry["id"])
        return self.transport_class(entry["target"], entry["transaction"], local, self.repository)

    def prepare_target(self, entry: dict, fleet: dict) -> dict:
        target = entry["target"]
        remote = self.transport(entry)
        previous = fleet["targets"].get(entry["id"])
        source = Path(previous["artifact"]["wheelPath"] if previous else target["bootstrap_rollback"])
        rollback = directory(self.root / entry["id"]) / source.name
        if previous:
            original = Path(previous["artifact"]["wheelPath"])
            payload = original.read_bytes()
            with PrivateStateFile(rollback, repair_root=False).locked() as store:
                store.replace(payload)
            if target["kind"] == "coordinator":
                target["profile"]["tool_root"] = previous["tool_root"]
        elif not rollback.exists():
            remote.download(target["bootstrap_rollback"], rollback)
        old = artifact(rollback)
        if previous and any(
            old[key] != previous["artifact"][key] for key in ("wheelSha256", "revision", "wheelVersion", "pluginSha256")
        ):
            raise ReleaseError("retained rollback artifact changed")
        candidate = self.journal["candidate"]
        compatible(Path(candidate["wheelPath"]), rollback)
        old_name = Path(old["wheelPath"]).name
        candidate_name = Path(candidate["wheelPath"]).name
        # The worker owns a new transaction namespace, not the old ad-hoc guards.
        profile = {**target["profile"], "rollback_root": target["work_root"]}
        request = {
            "version": 1,
            "transaction": entry["transaction"],
            "root": remote.root,
            "kind": target["kind"],
            "profile": profile,
            "origin": target["origin"],
            "machine_ids": target.get("machine_ids", []),
            "candidate": {key: value for key, value in candidate.items() if key != "workerSha256"},
            "rollback": old,
        }
        request["candidate"] = {**request["candidate"], "wheelPath": f"{remote.root}/candidate/{candidate_name}"}
        request["rollback"] = {**old, "wheelPath": f"{remote.root}/rollback/{old_name}"}
        remote.stage(self.root / "worker.pyz", Path(candidate["wheelPath"]), rollback, request)
        result = remote.action("prepare")
        return {"state": result["state"], "rollback": old, "profile": profile}

    def retry_target(self, entry: dict, fleet: dict):
        attempt = entry["attempt"] + 1
        if attempt > 32:
            raise ReleaseError("release attempt retention limit reached; prepare a new release")
        # A killed preparation may have partial immutable snapshots. Never
        # overwrite them or reuse their transaction namespace.
        entry.update(attempt=attempt, transaction=f"{self.transaction}-{entry['id']}-{attempt}", state="staging")
        self.save()
        entry.update(self.prepare_target(entry, fleet))
        entry.pop("error", None)
        self.save()

    def prepare(self, message: str | None = None):
        directory(self.root)
        with self.lease(initialize=True) as fleet:
            try:
                candidate = prepare_build(self.repository, self.root, message=message)
                run_browser(self.repository, "--preflight")
                self.journal["candidate"] = candidate
                for target in deployment_order(self.config["targets"]):
                    entry = {
                        "id": target["id"],
                        "target": target,
                        "attempt": 1,
                        "state": "staging",
                        "transaction": f"{self.transaction}-{target['id']}-1",
                    }
                    identifier(entry["transaction"])
                    self.journal["targets"].append(entry)
                self.save()
                errors = []
                with ThreadPoolExecutor(max_workers=3) as pool:
                    jobs = {pool.submit(self.prepare_target, entry, fleet): entry for entry in self.journal["targets"]}
                    for job in as_completed(jobs):
                        entry = jobs[job]
                        try:
                            entry.update(job.result())
                        except Exception as error:
                            entry.update(state="prepare_failed", error=type(error).__name__)
                            errors.append(error)
                        self.save()
                if errors:
                    raise ReleaseError("release preparation failed; no service was restarted") from errors[0]
                self.journal["state"] = "prepared"
                self.save()
            except BaseException:
                self.journal["state"] = "prepare_failed"
                self.save()
                raise
        return self.summary()

    def recover_missing_journal(self):
        store = PrivateStateFile(self.state / "fleet.json", repair_root=False)
        with operator_lock(self.state / "operator.lock"):
            with store.locked():
                raw = store.read(MAX_JSON)
                fleet = json.loads(raw) if raw else {"version": 1, "targets": {}}
                if fleet.get("active") != self.transaction:
                    raise ReleaseError("release not found")
                if not self.root.exists():
                    raise ReleaseError("active release has no journal or owned release directory")
                directory(self.root)
                if any(self.root.iterdir()):
                    raise ReleaseError("active release has no journal; preserving unknown release assets")
                self.journal = {
                    "version": 1,
                    "transaction": self.transaction,
                    "state": "prepare_failed",
                    "targets": [],
                    "abandonedAt": int(time.time()),
                }
                self.save()
                fleet.update(active=None, owner=None)
                store.replace(json.dumps(fleet).encode())
        return self.summary()

    def observe(self, entry: dict, *, persist: bool = True) -> dict:
        result = self.transport(entry).action("status")
        state = result.get("state")
        if state not in {"absent", "prepared", "pending", "verified", "rolledback", "rollback_failed"}:
            raise ReleaseError("worker reported unsupported release state")
        if state == "verified":
            expected = self.journal["candidate"]
            if result.get("installed") != {"version": expected["wheelVersion"], "revision": expected["revision"]}:
                raise ReleaseError("verified target no longer runs the candidate identity")
            entry.pop("error", None)
        entry.update(state=state, pluginActivation=result.get("pluginActivation", "unchanged"))
        if persist:
            self.save()
        return result

    def settle_failure(self, entry: dict):
        if entry["state"] == "staging":
            entry["state"] = "prepare_failed"
            self.journal["state"] = "prepare_failed"
            self.save()
            return
        remote = self.transport(entry)
        try:
            result = self.observe(entry)
            if result["state"] == "pending":
                remote.action("rollback")
                self.observe(entry)
            if entry["state"] in {"prepared", "rolledback", "verified"}:
                remote.action("cleanup")
        except Exception:
            entry["state"] = "recovery_required"
        self.journal["state"] = "stopped"
        if entry["state"] == "rolledback":
            self.journal["state"] = "rolledback"
        self.save()

    def deploy(self, *, resume: bool = False):
        if not self.journal:
            if resume:
                return self.recover_missing_journal()
            raise ReleaseError("release not found")
        if not self.journal.get("candidate") or not self.journal["targets"]:
            recoverable = (
                resume
                and not self.journal.get("candidate")
                and self.journal["targets"] == []
                and self.journal["state"] in {"preparing", "prepare_failed"}
            )
            if not recoverable:
                raise ReleaseError("preparation did not finish building; prepare a new release")
            with self.lease():
                self.journal["state"] = "prepare_failed"
                self.journal["abandonedAt"] = int(time.time())
                self.save()
            return self.summary()
        if self.journal["state"] in {"preparing", "prepare_failed"} or any(
            entry["state"] in {"staging", "prepare_failed"} for entry in self.journal["targets"]
        ):
            if not resume:
                raise ReleaseError("preparation was interrupted; use resume")
            with self.lease() as fleet:
                for entry in self.journal["targets"]:
                    if entry["state"] in {"staging", "prepare_failed"}:
                        self.retry_target(entry, fleet)
                self.journal["state"] = "prepared"
                self.save()
        with self.lease() as fleet:
            run_browser(self.repository, "--preflight")
            self.journal["state"] = "deploying"
            self.save()
            for entry in self.journal["targets"]:
                try:
                    state = self.observe(entry)["state"]
                    if state == "verified":
                        self.transport(entry).action("cleanup")
                        self.remember(entry, fleet)
                        continue
                    if state == "rolledback" and resume:
                        self.transport(entry).action("cleanup")
                        self.retry_target(entry, fleet)
                        state = entry["state"]
                    remote = self.transport(entry)
                    if state == "prepared":
                        remote.action("arm")
                        remote.action("deploy")
                    elif state != "pending":
                        raise ReleaseError("target requires recovery before deployment can proceed")
                    result = remote.verify()
                    if result.get("state") != "verified":
                        raise ReleaseError("verification did not finalize the target")
                    self.observe(entry)
                    remote.action("cleanup")
                    self.remember(entry, fleet)
                except BaseException as error:
                    entry["error"] = str(error) if isinstance(error, ReleaseError) else type(error).__name__
                    self.settle_failure(entry)
                    raise ReleaseError(f"{entry['id']}: release stopped; inspect status for recovery") from error
            self.journal["state"] = "verified"
            self.save()
        return self.summary()

    def remember(self, entry: dict, fleet: dict):
        target = entry["target"]
        tool_root = target["profile"]["tool_root"]
        if target["kind"] == "coordinator":
            tool_root = str(Path(target["profile"]["releases_root"]) / entry["transaction"])
        fleet["targets"][entry["id"]] = {"artifact": self.journal["candidate"], "tool_root": tool_root}

    def status(self):
        if not self.journal:
            raise ReleaseError("release not found")
        # Status never performs a cutover or rollback.
        for entry in self.journal["targets"]:
            try:
                self.observe(entry, persist=False)
            except Exception:
                entry["state"] = "unreachable_or_changed"
        result = self.summary()
        states = {entry["state"] for entry in self.journal["targets"]}
        for state in ("unreachable_or_changed", "rollback_failed", "rolledback", "pending"):
            if state in states:
                result["state"] = state
                break
        if states == {"verified"}:
            result["state"] = "verified"
        return result

    def summary(self):
        return {
            "release": self.transaction,
            "state": self.journal["state"],
            "targets": [
                {key: entry[key] for key in ("id", "state", "pluginActivation", "error") if key in entry}
                for entry in self.journal["targets"]
            ],
        }


def open_release(config_path: Path, repository: Path, transaction: str | None = None) -> Release:
    config = configuration(config_path)
    if transaction is None:
        transaction = f"r{int(time.time())}-{secrets.token_hex(4)}"
    return Release(config, repository, transaction)
