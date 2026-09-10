import base64
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omp_deck.auth import AuthenticationFailed
from omp_deck.fleet_protocol import issue_ticket, validate_enrollment, validate_origin, verify_ticket


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class FleetProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.private_key = encoded(self.key.private_bytes_raw())
        self.enrollment = {
            "version": 1,
            "machineId": "0123456789abcdef0123456789abcdef",
            "name": "workstation",
            "coordinatorOrigin": "https://deck.example.test:8443",
            "machineOrigin": "https://workstation.example.test",
            "publicKey": encoded(self.key.public_key().public_bytes_raw()),
        }
        self.device_id = "fedcba9876543210fedcba9876543210"

    def signed(self, claims, *, canonical=True):
        if canonical:
            payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("ascii")
        else:
            payload = json.dumps(claims).encode("ascii")
        return f"{encoded(payload)}.{encoded(self.key.sign(payload))}"

    def test_ticket_wire_claims_are_exact_and_expire_at_sixty_seconds(self):
        token, expires_at = issue_ticket(
            self.private_key,
            issuer=self.enrollment["coordinatorOrigin"],
            machine_id=self.enrollment["machineId"],
            device_id=self.device_id,
            now=1_700_000_000,
        )

        claims = verify_ticket(token, self.enrollment, now=1_700_000_059)

        self.assertEqual(expires_at, 1_700_000_060)
        self.assertEqual(
            claims,
            {
                "version": 1,
                "issuer": self.enrollment["coordinatorOrigin"],
                "audience": self.enrollment["machineId"],
                "origin": self.enrollment["coordinatorOrigin"],
                "deviceId": self.device_id,
                "issuedAt": 1_700_000_000,
                "expiresAt": 1_700_000_060,
            },
        )
        with self.assertRaises(AuthenticationFailed):
            verify_ticket(token, self.enrollment, now=expires_at)

    def test_signed_but_noncanonical_or_invalid_claims_are_rejected(self):
        valid = {
            "version": 1,
            "issuer": self.enrollment["coordinatorOrigin"],
            "audience": self.enrollment["machineId"],
            "origin": self.enrollment["coordinatorOrigin"],
            "deviceId": self.device_id,
            "issuedAt": 100,
            "expiresAt": 160,
        }
        invalid = []
        invalid.append(self.signed(valid, canonical=False))
        invalid.append(self.signed({**valid, "unexpected": True}))
        invalid.append(self.signed({**valid, "issuedAt": True}))
        invalid.append(self.signed({**valid, "expiresAt": 161}))
        invalid.append(self.signed({**valid, "issuedAt": 106, "expiresAt": 160}))
        invalid.append(self.signed({**valid, "audience": "1" * 32}))
        for token in invalid:
            with self.subTest(token=token[:24]):
                with self.assertRaises(AuthenticationFailed):
                    verify_ticket(token, self.enrollment, now=100)

    def test_tampering_wrong_key_and_oversize_fail_as_authentication(self):
        token, _ = issue_ticket(
            self.private_key,
            issuer=self.enrollment["coordinatorOrigin"],
            machine_id=self.enrollment["machineId"],
            device_id=self.device_id,
            now=100,
        )
        other = Ed25519PrivateKey.generate()
        wrong_enrollment = {**self.enrollment, "publicKey": encoded(other.public_key().public_bytes_raw())}
        tampered = ("A" if token[0] != "A" else "B") + token[1:]

        for candidate, enrollment in (
            (tampered, self.enrollment),
            (token, wrong_enrollment),
            ("A" * 4097, self.enrollment),
        ):
            with self.assertRaises(AuthenticationFailed):
                verify_ticket(candidate, enrollment, now=100)

    def test_deeply_nested_unsigned_payload_is_an_authentication_failure(self):
        payload = b"[" * 1200 + b"]" * 1200
        token = f"{encoded(payload)}.{encoded(bytes(64))}"
        with self.assertRaises(AuthenticationFailed):
            verify_ticket(token, self.enrollment, now=100)

    def test_origin_validation_canonicalizes_only_unambiguous_origins(self):
        self.assertEqual(validate_origin("HTTPS://Deck.Example:443/"), "https://deck.example")
        self.assertEqual(validate_origin("http://[0:0:0:0:0:0:0:1]:8790"), "http://[::1]:8790")
        for value in (
            "http://deck.example",
            "https://user@deck.example",
            "https://deck.example/path",
            "https://deck.example./",
            "https://*.example",
            " https://deck.example",
            "https://deck.example?secret=x",
            "https://deck.example?",
            "https://deck.example#",
            "https://127.000.000.001",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_origin(value)

    def test_enrollment_requires_exact_shape_and_raw_canonical_public_key(self):
        self.assertEqual(validate_enrollment(dict(self.enrollment)), self.enrollment)
        for enrollment in (
            {**self.enrollment, "privateKey": self.private_key},
            {**self.enrollment, "publicKey": self.enrollment["publicKey"] + "="},
            {**self.enrollment, "machineId": self.enrollment["machineId"].upper()},
            {**self.enrollment, "name": " workstation"},
            {**self.enrollment, "version": True},
            {**self.enrollment, "name": "work\u202estation"},
        ):
            with self.assertRaises(ValueError):
                validate_enrollment(enrollment)


if __name__ == "__main__":
    unittest.main()
