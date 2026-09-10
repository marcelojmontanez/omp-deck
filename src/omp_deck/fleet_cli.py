from __future__ import annotations

import json
from pathlib import Path

from ._fleet_trust import FleetTrustStore
from .fleet_protocol import validate_enrollment
from .settings import _read_config, load_settings


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("fleet", help="manage this Deck node's fleet enrollment")
    actions = parser.add_subparsers(dest="fleet_action", required=True)

    join = actions.add_parser("join", help="trust an explicit coordinator enrollment bundle")
    join.add_argument("--file", required=True, help="owner-only enrollment bundle")
    join.add_argument("--config", help="use this local Deck configuration file")

    for action, help_text in (
        ("leave", "remove the trusted coordinator"),
        ("status", "show the trusted coordinator enrollment"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("--config", help="use this local Deck configuration file")


def run(args) -> int:
    settings = load_settings(args.config)
    trust = FleetTrustStore(settings.state_root)
    if args.fleet_action == "join":
        bundle_path = Path(args.file).expanduser()
        try:
            enrollment = validate_enrollment(_read_config(bundle_path))
        except RuntimeError:
            raise
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"cannot read enrollment bundle {bundle_path}: {error}") from error
        enrolled = trust.join(enrollment)
        print(
            json.dumps(
                {
                    "enrolled": True,
                    "coordinatorOrigin": enrolled["coordinatorOrigin"],
                    "machineId": enrolled["machineId"],
                    "machineOrigin": enrolled["machineOrigin"],
                    "name": enrolled["name"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.fleet_action == "leave":
        removed = trust.leave()
        print(json.dumps({"enrolled": False, "removed": removed}, sort_keys=True))
        return 0
    enrollment = trust.load()
    if enrollment is None:
        print(json.dumps({"enrolled": False}, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "enrolled": True,
                    "coordinatorOrigin": enrollment["coordinatorOrigin"],
                    "machineId": enrollment["machineId"],
                    "machineOrigin": enrollment["machineOrigin"],
                    "name": enrollment["name"],
                    "publicKey": enrollment["publicKey"],
                },
                sort_keys=True,
            )
        )
    return 0
