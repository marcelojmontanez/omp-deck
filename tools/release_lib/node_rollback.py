from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from . import node_plugin
from .node_common import ReleaseError, digest, quiescent


def _directory(path, parent, label):
    if path.parent != parent or path.is_symlink():
        raise ReleaseError(f"unsafe {label} path")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseError(f"{label} directory unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ReleaseError(f"unsafe {label} directory")


def _paths(p, root, manifest):
    backup = Path(manifest["originals"]["environmentBackup"])
    expected_backup = root / "guard/snapshots/uv-tool-environment"
    if backup != expected_backup:
        raise ReleaseError("retained rollback environment is outside this transaction")
    _directory(backup, expected_backup.parent, "retained rollback environment")
    plugin_backup = Path(manifest["originals"]["pluginBackup"])
    expected_plugin = root / "guard/snapshots/plugin"
    if plugin_backup != expected_plugin:
        raise ReleaseError("retained rollback plugin is outside this transaction")
    _directory(plugin_backup, expected_plugin.parent, "retained rollback plugin")
    failed = p.tool_root.with_name(f".{p.tool_root.name}.failed-{manifest['transaction']}")
    staging = p.tool_root.with_name(f".{p.tool_root.name}.restore-{manifest['transaction']}")
    return backup, plugin_backup, failed, staging


def _existing_directory(path, parent, label):
    if path.exists() or path.is_symlink():
        _directory(path, parent, label)
        return True
    return False


def _stop(node, p, manifest, cookie):
    if node.running(p):
        with node._admit(p, cookie) as current:
            if current["registryStable"] != manifest["cutover"]["registryStable"]:
                raise ReleaseError("registry identities changed; refusing rollback")
            node.stop(p, current["pid"])
    elif not quiescent(p):
        raise ReleaseError("service ownership uncertain; refusing rollback")


def _restore_environment(node, p, root, manifest, backup, failed, staging):
    expected = {
        "version": manifest["rollback"]["wheelVersion"],
        "revision": manifest["rollback"]["revision"],
    }
    parent = p.tool_root.parent
    phase = manifest.get("rollbackPhase")
    failed_exists = _existing_directory(failed, parent, "failed environment")
    staging_exists = _existing_directory(staging, parent, "staged rollback environment")

    # An exact failed sibling identifies the old, pre-phase swap. Ambiguous
    # partial active environments are preserved for operator inspection.
    if phase == "service-stopped" and failed_exists:
        if staging_exists:
            raise ReleaseError("ambiguous legacy rollback staging directory")
        if not p.tool_root.exists() and not p.tool_root.is_symlink():
            phase = "copying-environment"
            manifest["rollbackPhase"] = phase
            node._save(root, manifest)
        else:
            try:
                restored = node._environment_identity(p, p.tool_root) == expected
            except (OSError, ReleaseError, ValueError):
                restored = False
            if not restored:
                raise ReleaseError("ambiguous legacy rollback environment; refusing replacement")
            phase = "environment-restored"
            manifest["rollbackPhase"] = phase
            node._save(root, manifest)

    if phase == "service-stopped":
        if staging_exists:
            raise ReleaseError("unowned rollback staging directory already exists")
        manifest["rollbackPhase"] = "copying-environment"
        node._save(root, manifest)
        phase = "copying-environment"

    if phase == "copying-environment":
        if staging_exists:
            shutil.rmtree(staging)
        shutil.copytree(backup, staging, symlinks=True)
        _directory(staging, parent, "staged rollback environment")
        if node._environment_identity(p, staging) != expected:
            raise ReleaseError("retained rollback environment identity mismatch")
        manifest["rollbackPhase"] = "environment-staged"
        node._save(root, manifest)
        phase = "environment-staged"
        staging_exists = True

    if phase == "environment-staged":
        if not staging_exists:
            raise ReleaseError("staged rollback environment is missing")
        if node._environment_identity(p, staging) != expected:
            raise ReleaseError("staged rollback environment identity mismatch")
        staged_info = staging.stat(follow_symlinks=False)
        manifest["rollbackEnvironmentEvidence"] = {
            "device": staged_info.st_dev,
            "inode": staged_info.st_ino,
        }
        manifest["rollbackPhase"] = "swapping-environment"
        node._save(root, manifest)
        phase = "swapping-environment"

    if phase == "swapping-environment":
        _publish_environment(node, p, root, manifest, failed, staging, expected)


def _publish_environment(node, p, root, manifest, failed, staging, expected):
    parent = p.tool_root.parent
    failed_exists = _existing_directory(failed, parent, "failed environment")
    staging_exists = _existing_directory(staging, parent, "staged rollback environment")
    active_exists = p.tool_root.exists() or p.tool_root.is_symlink()
    if not failed_exists and active_exists:
        _directory(p.tool_root, parent, "active tool environment")
        if not staging_exists:
            active_info = p.tool_root.stat(follow_symlinks=False)
            evidence = manifest.get("rollbackEnvironmentEvidence")
            restored_publish = evidence == {
                "device": active_info.st_dev,
                "inode": active_info.st_ino,
            }
            if not restored_publish or node._environment_identity(p, p.tool_root) != expected:
                raise ReleaseError("ambiguous incomplete rollback environment; refusing replacement")
            manifest["rollbackPhase"] = "environment-restored"
            node._save(root, manifest)
            return
        os.replace(p.tool_root, failed)
    if staging_exists:
        if p.tool_root.exists() or p.tool_root.is_symlink():
            raise ReleaseError("active environment appeared during rollback swap")
        os.replace(staging, p.tool_root)
    elif not p.tool_root.exists() or p.tool_root.is_symlink():
        raise ReleaseError("rollback environment swap is incomplete")
    _directory(p.tool_root, parent, "restored tool environment")
    if node._environment_identity(p, p.tool_root) != expected:
        raise ReleaseError("retained rollback environment identity mismatch")
    manifest["rollbackPhase"] = "environment-restored"
    node._save(root, manifest)


def _restore_service(node, p, root, manifest, evidence):
    if manifest.get("rollbackPhase") == "plugin-restored":
        if quiescent(p):
            node.start(p)
        elif not node.running(p):
            raise ReleaseError("service ownership uncertain; refusing rollback")
        manifest["rollbackPhase"] = "service-started"
        node._save(root, manifest)
    if manifest.get("rollbackPhase") == "service-started":
        if quiescent(p):
            node.start(p)
        elif not node.running(p):
            raise ReleaseError("service ownership uncertain; refusing rollback")
    if manifest.get("rollbackPhase") != "service-started":
        raise ReleaseError("unsupported rollback recovery phase")
    expected = {
        "version": manifest["rollback"]["wheelVersion"],
        "revision": manifest["rollback"]["revision"],
    }
    if node._installed(p) != expected:
        raise ReleaseError("running rollback environment identity mismatch")
    node.health(p, base_url=manifest.get("loopbackHTTP"))
    if node.service_enabled(p) != manifest["originals"]["serviceEnabled"]:
        raise ReleaseError("service enabled policy changed during rollback")
    if digest(p.auth) != evidence["authSha256"] or node.registry(p)[1] != evidence["registryStable"]:
        raise ReleaseError("durable identity changed during rollback")


def rollback_locked(p, root, manifest, cookie, reason):
    from . import node

    if manifest["state"] == "rolledback":
        return
    if manifest["state"] == "verified":
        raise ReleaseError("verified node release cannot be rolled back by its expired guard")
    if not manifest.get("touched"):
        manifest.update(state="rolledback", deadline=None, lastError=reason)
        node._save(root, manifest)
        return
    node._guard_files(p, manifest)
    node._verify_staged(manifest)
    backup, plugin_backup, failed, staging = _paths(p, root, manifest)
    evidence = manifest.get("rollbackEvidence")
    current_registry = node.registry(p)[1]
    current_auth = digest(p.auth)
    if evidence is None:
        expected_auth = manifest["cutover"].get("authSha256", current_auth)
        if current_auth != expected_auth or current_registry != manifest["cutover"]["registryStable"]:
            raise ReleaseError("durable identity changed before rollback")
        evidence = {
            "authSha256": expected_auth,
            "registryStable": current_registry,
        }
        manifest.update(rollbackEvidence=evidence, rollbackPhase="stopping-service")
        node._save(root, manifest)
    if (
        not isinstance(evidence, dict)
        or evidence.get("authSha256") != current_auth
        or evidence.get("registryStable") != current_registry
        or evidence.get("registryStable") != manifest["cutover"]["registryStable"]
    ):
        raise ReleaseError("durable identity changed during rollback")
    phase = manifest.get("rollbackPhase")
    swap_phases = {
        "stopping-service",
        "service-stopped",
        "copying-environment",
        "environment-staged",
        "swapping-environment",
    }
    if phase in swap_phases:
        _stop(node, p, manifest, cookie)
        if phase == "stopping-service":
            manifest["rollbackPhase"] = "service-stopped"
            node._save(root, manifest)
        _restore_environment(node, p, root, manifest, backup, failed, staging)
    if manifest.get("rollbackPhase") == "environment-restored":
        if node.running(p) or not quiescent(p):
            raise ReleaseError("service started before plugin rollback completed")
        restored_plugin = node_plugin.restore_plugin(
            p,
            manifest["rollback"]["pluginSha256"],
            manifest["originals"]["serviceEnvironment"],
            manifest["originals"].get("pluginSource", p.plugin_expected),
            plugin_backup,
        )
        manifest["rollbackPluginSource"] = str(restored_plugin)
        manifest["rollbackPhase"] = "plugin-restored"
        node._save(root, manifest)
    _restore_service(node, p, root, manifest, evidence)
    if _existing_directory(failed, p.tool_root.parent, "failed environment"):
        shutil.rmtree(failed)
    if _existing_directory(staging, p.tool_root.parent, "staged rollback environment"):
        shutil.rmtree(staging)
    manifest.update(
        state="rolledback",
        deadline=None,
        lastError=reason,
        pluginActivation="unchanged",
        rollbackPhase="complete",
    )
    node._save(root, manifest)
