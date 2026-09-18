from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from omp_deck._plugin_lifecycle import (
    authorized_plugin_source,
    discard_plugin_backup,
)
from omp_deck._plugin_lifecycle import (
    install_plugin as install_stored_plugin,
)

from .node_common import ReleaseError, run, tree_digest


def _registered_path(profile):
    try:
        data = json.loads(run(profile, [profile.omp, "plugin", "list", "--json"], timeout=30).stdout)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("OMP plugin inventory returned invalid JSON") from error
    records = data.get("npm") if isinstance(data, dict) else None
    matches = [item for item in records or [] if isinstance(item, dict) and item.get("name") == "omp-deck"]
    if not isinstance(records, list) or len(matches) != 1:
        raise ReleaseError("expected exactly one omp-deck plugin")
    value = matches[0].get("path")
    registered = Path(value) if isinstance(value, str) else Path()
    if not registered.is_absolute() or ".." in registered.parts:
        raise ReleaseError("installed plugin source path is invalid")
    return registered


def plugin_path(profile, expected=None):
    try:
        actual = _registered_path(profile).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise ReleaseError("installed plugin source is unavailable") from error
    try:
        configured = profile.plugin_expected.resolve(strict=True)
    except (OSError, RuntimeError):
        configured = None
    durable = authorized_plugin_source(actual)
    if actual != configured and durable != actual:
        raise ReleaseError("plugin path is not an authorized installation source")
    if expected is not None:
        try:
            authorized = Path(expected).resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ReleaseError("authorized plugin source is unavailable") from error
        if actual != authorized:
            raise ReleaseError("plugin path differs from authorized transaction source")
    return actual


def install_plugin(profile, expected, environment, current_source, *, source=None, require_durable=False):
    current = None
    try:
        current = plugin_path(profile)
        if tree_digest(current) == expected and (not require_durable or authorized_plugin_source(current) == current):
            return current
    except ReleaseError:
        registered = _registered_path(profile)
        previous = Path(current_source)
        if registered != previous or registered.exists() or registered.is_symlink():
            raise
    if current is not None:
        try:
            authorized_current = Path(current_source).resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ReleaseError("previous plugin source is unavailable") from error
        if current != authorized_current:
            raise ReleaseError("plugin registration changed during release")
    command = [profile.tool_python, "-m", "omp_deck", "plugin", "install", "--json"]
    if source is not None:
        command.extend(["--source", source])
    preserve = authorized_plugin_source(current_source)
    if preserve is not None:
        command.extend(["--preserve-source", preserve])
    result = run(profile, command, env=environment, timeout=180)
    try:
        report = json.loads(result.stdout)
        reported = report["pluginSource"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("plugin installation did not report its durable source") from error
    if not isinstance(report, dict) or set(report) != {"version", "pluginSource"} or report["version"] != 1:
        raise ReleaseError("plugin installation returned an invalid source report")
    actual = plugin_path(profile, reported)
    if authorized_plugin_source(actual) != actual or tree_digest(actual) != expected:
        raise ReleaseError("plugin identity mismatch after installation")
    return actual


def restore_plugin(profile, expected, environment, original_source, backup):
    current = plugin_path(profile)
    if tree_digest(current) == expected and authorized_plugin_source(current) == current:
        return current
    original = authorized_plugin_source(original_source)
    if original is not None and tree_digest(original) == expected:
        run(profile, [profile.omp, "plugin", "install", original], env=environment, timeout=180)
        restored = plugin_path(profile, original)
    else:

        def lifecycle_run(command, *, check=True, **_options):
            return run(profile, command, check=check, env=environment, timeout=180)

        try:
            reported = install_stored_plugin(
                SimpleNamespace(omp_bin=str(profile.omp)),
                runner=lifecycle_run,
                source=backup,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise ReleaseError("retained plugin installation failed") from error
        restored = plugin_path(profile, reported)
        if tree_digest(restored) != expected:
            raise ReleaseError("restored plugin identity mismatch")
    if current is not None and current != restored:
        discard_plugin_backup(current)
    return restored
