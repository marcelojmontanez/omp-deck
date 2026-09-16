import json
import os
import stat
import tempfile
import unittest
from email.message import Message
from http.cookies import SimpleCookie
from pathlib import Path

from omp_deck.auth import (
    COOKIE_NAME,
    AuthenticationFailed,
    AuthError,
    AuthManager,
    AuthStateCorrupt,
    PairingFailed,
    PairingRateLimited,
    validate_auth_state,
)
from omp_deck.auth_policy import AuthPolicy


class Clock:
    def __init__(self, value=1_700_000_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wall = Clock()
        self.monotonic = Clock(100)
        self.auth = AuthManager(
            self.temp.name,
            pairing_ttl=30,
            session_ttl=60,
            pairing_attempts=2,
            pairing_window=10,
            max_rate_limit_sources=2,
            now=self.wall,
            monotonic=self.monotonic,
        )

    def pair_device(self, source="phone"):
        secret = self.auth.begin_pairing()
        return secret, self.auth.pair(secret.token, source=source)

    def test_state_is_versioned_atomic_private_and_contains_no_raw_secrets(self):
        secret, paired = self.pair_device()
        state_path = Path(self.temp.name) / "auth.json"
        state_text = state_path.read_text()
        state = json.loads(state_text)

        self.assertEqual(state["version"], 1)
        if os.name == "nt":
            from omp_deck._windows_security import is_private_path

            self.assertTrue(is_private_path(state_path))
        else:
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertNotIn(secret.token, state_text)
        self.assertNotIn(paired.credential, state_text)
        self.assertEqual(list(Path(self.temp.name).glob(".auth.json.*.tmp")), [])

    def test_pairing_is_six_digits_single_use_and_expiring(self):
        secret = self.auth.begin_pairing()
        self.assertRegex(secret.token, r"^\d{6}$")
        paired = self.auth.pair(secret.token, source="phone")
        self.assertEqual(self.auth.authenticate(paired.credential).device_id, paired.device.device_id)
        with self.assertRaises(PairingFailed):
            self.auth.pair(secret.token, source="other-phone")

        expired = self.auth.begin_pairing()
        self.wall.advance(31)
        with self.assertRaises(PairingFailed):
            self.auth.pair(expired.token, source="third-phone")
        with self.assertRaises(PairingFailed):
            self.auth.pair(expired.token, source="fourth-phone")

    def test_one_failed_attempt_invalidates_pairing_code_globally(self):
        secret = self.auth.begin_pairing()
        with self.assertRaises(PairingFailed):
            self.auth.pair("000000" if secret.token != "000000" else "000001", source="attacker")
        with self.assertRaises(PairingFailed):
            self.auth.pair(secret.token, source="phone")

    def test_default_pairing_lifetime_is_one_minute(self):
        auth = AuthManager(Path(self.temp.name) / "default", now=self.wall, monotonic=self.monotonic)
        secret = auth.begin_pairing()
        self.assertEqual(secret.expires_at, self.wall.value + 60)
        self.wall.advance(61)
        with self.assertRaises(PairingFailed):
            auth.pair(secret.token, source="phone")

    def test_custom_cookie_name_is_used_for_pair_and_logout(self):
        auth = AuthManager(
            Path(self.temp.name) / "coordinator",
            session_ttl=60,
            cookie_name="omp_deck_coordinator_device",
            now=self.wall,
            monotonic=self.monotonic,
        )
        secret = auth.begin_pairing()
        paired = auth.pair(secret.token, source="browser")
        cookie = SimpleCookie()
        cookie.load(paired.set_cookie)

        self.assertEqual(cookie["omp_deck_coordinator_device"].value, paired.credential)
        self.assertNotIn(COOKIE_NAME, cookie)
        self.assertEqual(int(cookie["omp_deck_coordinator_device"]["max-age"]), 60)
        self.assertTrue(auth.logout(paired.credential).startswith("omp_deck_coordinator_device=;"))

    def test_authentication_survives_restart_but_expired_session_fails(self):
        _, paired = self.pair_device()
        restarted = AuthManager(
            self.temp.name,
            pairing_ttl=30,
            session_ttl=60,
            now=self.wall,
            monotonic=self.monotonic,
        )
        authenticated = restarted.authenticate(paired.credential)
        self.assertEqual(authenticated.device_id, paired.device.device_id)
        self.assertEqual(authenticated.expires_at, paired.device.expires_at)
        self.wall.advance(60)
        with self.assertRaises(AuthenticationFailed):
            restarted.authenticate(paired.credential)

    def test_running_manager_accepts_pairing_secret_created_by_cli_process(self):
        running_service = self.auth
        cli_process = AuthManager(
            self.temp.name,
            pairing_ttl=30,
            session_ttl=60,
            now=self.wall,
            monotonic=self.monotonic,
        )
        secret = cli_process.begin_pairing()

        paired = running_service.pair(secret.token, source="phone")

        self.assertEqual(
            running_service.authenticate(paired.credential).device_id,
            paired.device.device_id,
        )

    def test_csrf_is_bound_to_device_credential_and_compared_at_boundary(self):
        _, first = self.pair_device()
        _, second = self.pair_device(source="tablet")
        self.assertEqual(
            self.auth.validate_csrf(first.credential, first.device.csrf_token).device_id,
            first.device.device_id,
        )
        with self.assertRaises(AuthenticationFailed):
            self.auth.validate_csrf(first.credential, second.device.csrf_token)
        with self.assertRaises(AuthenticationFailed):
            self.auth.validate_csrf(first.credential, None)

    def test_logout_revokes_only_present_device_and_expires_cookie(self):
        _, first = self.pair_device()
        _, second = self.pair_device(source="tablet")
        clear = self.auth.logout(first.credential)

        self.assertIn(f"{COOKIE_NAME}=", clear)
        self.assertIn("Max-Age=0", clear)
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate(first.credential)
        self.assertEqual(self.auth.authenticate(second.credential).device_id, second.device.device_id)

    def test_revoke_all_rotates_epoch_and_invalidates_every_device(self):
        _, first = self.pair_device()
        _, second = self.pair_device(source="tablet")
        old_epoch = json.loads((Path(self.temp.name) / "auth.json").read_text())["epoch"]
        self.auth.revoke_all(first.credential, first.device.csrf_token)
        new_epoch = json.loads((Path(self.temp.name) / "auth.json").read_text())["epoch"]

        self.assertNotEqual(old_epoch, new_epoch)
        for credential in (first.credential, second.credential):
            with self.assertRaises(AuthenticationFailed):
                self.auth.authenticate(credential)

    def test_pairing_attempts_are_rate_limited_and_source_table_is_bounded(self):
        for _ in range(2):
            self.auth.begin_pairing()
            with self.assertRaises(PairingFailed):
                self.auth.pair("wrong", source="attacker")
        secret = self.auth.begin_pairing()
        with self.assertRaises(PairingRateLimited):
            self.auth.pair(secret.token, source="attacker")

        # Independent sources do not consume each other's allowance; source state stays bounded.
        self.auth.pair(secret.token, source="owner")
        with self.assertRaises(PairingFailed):
            self.auth.pair("wrong", source="third")
        self.assertLessEqual(len(self.auth._limiter.sources), 2)

    def test_rate_limit_window_recovers(self):
        for _ in range(2):
            self.auth.begin_pairing()
            with self.assertRaises(PairingFailed):
                self.auth.pair("wrong", source="phone")
        secret = self.auth.begin_pairing()
        with self.assertRaises(PairingRateLimited):
            self.auth.pair(secret.token, source="phone")
        self.monotonic.advance(11)
        paired = self.auth.pair(secret.token, source="phone")
        self.assertEqual(self.auth.authenticate(paired.credential).device_id, paired.device.device_id)

    def test_corrupt_state_is_quarantined_without_silent_reset(self):
        state_path = Path(self.temp.name) / "auth.json"
        state_path.write_text("not-json")
        with self.assertRaises(AuthStateCorrupt) as caught:
            AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)

        self.assertFalse(state_path.exists())
        self.assertTrue(caught.exception.quarantine_path.exists())
        self.assertEqual(list(Path(self.temp.name).glob("auth.json.corrupt-*")), [caught.exception.quarantine_path])

    def test_oversized_state_is_bounded_and_quarantined(self):
        state_path = Path(self.temp.name) / "auth.json"
        state_path.write_bytes(b"x" * (1024 * 1024 + 1))
        os.chmod(state_path, 0o600)

        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            validate_auth_state(self.temp.name)
        self.assertTrue(state_path.exists())

        with self.assertRaises(AuthStateCorrupt) as caught:
            AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)
        self.assertFalse(state_path.exists())
        self.assertTrue(caught.exception.quarantine_path.exists())

    def test_device_capacity_configuration_is_bounded_before_state_creation(self):
        root = Path(self.temp.name) / "excessive-capacity"
        with self.assertRaisesRegex(ValueError, "bounded"):
            AuthManager(root, max_devices=1025)
        self.assertFalse(root.exists())

    def test_persisted_device_count_is_bounded(self):
        self.pair_device()
        state_path = Path(self.temp.name) / "auth.json"
        state = json.loads(state_path.read_text())
        record = next(iter(state["devices"].values()))
        state["devices"] = {f"{index:032x}": dict(record) for index in range(1025)}
        state_path.write_text(json.dumps(state))

        with self.assertRaises(AuthStateCorrupt):
            AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)
        self.assertFalse(state_path.exists())

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_existing_state_permissions_are_repaired(self):
        state_path = Path(self.temp.name) / "auth.json"
        os.chmod(state_path, 0o644)
        AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_permissive_existing_state_root_is_rejected_without_mutation(self):
        root = Path(self.temp.name) / "permissive"
        root.mkdir()
        sentinel = root / "unrelated"
        sentinel.write_text("preserve")
        root.chmod(0o777)

        with self.assertRaisesRegex(AuthError, "state root is unsafe"):
            AuthManager(root, now=self.wall, monotonic=self.monotonic)

        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o777)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((root / ".auth.lock").exists())
        self.assertFalse((root / "auth.json").exists())

    def test_read_only_validation_does_not_create_missing_state(self):
        missing = Path(self.temp.name) / "missing"
        with self.assertRaisesRegex(RuntimeError, "missing"):
            validate_auth_state(missing)
        self.assertFalse(missing.exists())

    def test_read_only_validation_does_not_quarantine_corrupt_state(self):
        state_path = Path(self.temp.name) / "auth.json"
        state_path.write_text("not-json")
        os.chmod(state_path, 0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            validate_auth_state(self.temp.name)
        self.assertEqual(state_path.read_text(), "not-json")
        self.assertEqual(list(Path(self.temp.name).glob("auth.json.corrupt-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_read_only_validation_rejects_unsafe_lock(self):
        lock = Path(self.temp.name) / ".auth.lock"
        os.chmod(lock, 0o644)
        with self.assertRaisesRegex(RuntimeError, "lock is unsafe"):
            validate_auth_state(self.temp.name)

    @unittest.skipUnless(os.name == "nt", "Windows ACL semantics")
    def test_read_only_validation_rejects_inherited_windows_acl(self):
        import subprocess

        lock = Path(self.temp.name) / ".auth.lock"
        subprocess.run(
            ["icacls", str(lock), "/inheritance:e"],
            check=True,
            capture_output=True,
            text=True,
        )
        with self.assertRaisesRegex(RuntimeError, "lock is unsafe"):
            validate_auth_state(self.temp.name)

    def test_cross_process_pairing_preserves_existing_devices(self):
        stale = AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)
        secret = self.auth.begin_pairing()
        paired = self.auth.pair(secret.token, source="owner")

        stale.begin_pairing()

        reloaded = AuthManager(self.temp.name, now=self.wall, monotonic=self.monotonic)
        self.assertEqual(
            reloaded.authenticate(paired.credential).device_id,
            paired.device.device_id,
        )

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires elevated privileges")
    def test_symlink_state_root_is_rejected_without_mutating_target(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "target"
            target.mkdir(mode=0o755)
            link = parent / "state"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(AuthError, "unsafe"):
                AuthManager(link)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertFalse((target / "auth.json").exists())


class AuthPolicyTests(unittest.TestCase):
    @staticmethod
    def headers(*values):
        headers = Message()
        for value in values:
            headers.add_header("Tailscale-User-Login", value)
        return headers

    def test_tailscale_identity_rejects_untrusted_peer_and_oversized_header(self):
        policy = AuthPolicy("tailscale", "owner@example.com")
        rejected = (
            (self.headers("owner@example.com"), ("192.0.2.1", 1234)),
            (self.headers("x" * 4097), ("127.0.0.1", 1234)),
        )
        for headers, peer in rejected:
            with self.subTest(headers=headers.get_all("Tailscale-User-Login"), peer=peer):
                with self.assertRaises(AuthenticationFailed):
                    policy.require_request_identity(headers, peer)


if __name__ == "__main__":
    unittest.main()
