"""OMP Deck service package."""

try:
    from ._build_identity import REVISION as _revision
    from ._build_identity import VERSION as __version__
except ModuleNotFoundError as error:
    if error.name != f"{__name__}._build_identity":
        raise
    __version__ = "0+unbuilt"
    _revision = "unavailable"


def build_identity() -> dict[str, str]:
    """Return the immutable identity embedded in this package artifact."""
    return {"version": __version__, "revision": _revision}


def storage_compatibility() -> dict[str, dict[str, list[int] | int]]:
    """Advertise durable formats for pre-cutover rollback compatibility checks."""
    from ._request_ledger_schema import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION

    return {"requests": {"read": [1, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION], "write": SCHEMA_VERSION}}
