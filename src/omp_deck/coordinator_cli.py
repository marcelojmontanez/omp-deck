from __future__ import annotations

import json
from pathlib import Path

from ._private_state import PrivateStateFile
from .auth import AuthManager
from .coordinator_settings import COORDINATOR_COOKIE_NAME, load_coordinator_settings
from .coordinator_store import CoordinatorStore


def add_parser(subparsers) -> None:
    coordinator = subparsers.add_parser("coordinator", help="run and administer a fleet coordinator")
    actions = coordinator.add_subparsers(dest="coordinator_action", required=True)

    serve = actions.add_parser("serve", help="run the coordinator in the foreground")
    serve.add_argument("--config", required=True, help="owner-only coordinator configuration file")

    pair = actions.add_parser("pair", help="create a coordinator device pairing code")
    pair.add_argument("--config", required=True, help="owner-only coordinator configuration file")

    machine = actions.add_parser("machine", help="manage enrolled machines")
    machine_actions = machine.add_subparsers(dest="machine_action", required=True)

    add = machine_actions.add_parser("add", help="enroll a machine without contacting it")
    add.add_argument("name", metavar="NAME")
    add.add_argument("--origin", required=True, help="browser-reachable machine origin")
    add.add_argument("--out", required=True, help="new owner-only enrollment bundle")
    add.add_argument("--config", required=True, help="owner-only coordinator configuration file")

    listing = machine_actions.add_parser("list", help="list enrolled machines")
    listing.add_argument("--config", required=True, help="owner-only coordinator configuration file")

    remove = machine_actions.add_parser("remove", help="remove a machine and prevent ticket renewal")
    remove.add_argument("id", metavar="ID")
    remove.add_argument("--config", required=True, help="owner-only coordinator configuration file")


def _write_enrollment(path_value: str, enrollment: dict) -> Path:
    path = Path(path_value).expanduser().absolute()
    storage = PrivateStateFile(path, repair_root=False)
    payload = (json.dumps(enrollment, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with storage.locked():
        storage.replace(payload, overwrite=False)
    return path


def _store(settings) -> CoordinatorStore:
    return CoordinatorStore(settings.state_root, settings.origin)


def _add_machine(args, settings) -> None:
    output = Path(args.out).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"refusing to replace enrollment output: {output}")
    store = _store(settings)
    enrollment = store.add_machine(args.name, args.origin)
    try:
        written = _write_enrollment(args.out, enrollment)
    except Exception as error:
        try:
            store.remove_machine(enrollment["machineId"])
        except Exception as rollback_error:
            raise RuntimeError(f"{error}; enrollment rollback failed: {rollback_error}") from error
        raise
    print(f"Enrolled machine {enrollment['name']} ({enrollment['machineId']}).")
    print(f"Enrollment bundle: {written}")
    print("Reload an open coordinator dashboard before using the new machine origin.")


def _list_machines(settings) -> None:
    machines = _store(settings).machines()
    print(json.dumps({"version": 1, "machines": list(machines)}, sort_keys=True, separators=(",", ":")))


def _remove_machine(args, settings) -> None:
    removed = _store(settings).remove_machine(args.id)
    print(f"Removed machine {removed['name']} ({removed['id']}).")


def _pair(settings) -> None:
    secret = AuthManager(settings.state_root, cookie_name=COORDINATOR_COOKIE_NAME).begin_pairing()
    print(f"Pairing code: {secret.token}")
    print(f"Expires at Unix time {secret.expires_at}.")
    print("Reload the dashboard after pairing so its authenticated fleet policy takes effect.")


def run(args) -> int:
    settings = load_coordinator_settings(args.config)
    if args.coordinator_action == "serve":
        from .coordinator_server import serve

        serve(settings)
    elif args.coordinator_action == "pair":
        _pair(settings)
    elif args.machine_action == "add":
        _add_machine(args, settings)
    elif args.machine_action == "list":
        _list_machines(settings)
    else:
        _remove_machine(args, settings)
    return 0
