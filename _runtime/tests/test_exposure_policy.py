import sys
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from exposure_policy import NETWORK_ACK_ENV, NETWORK_ACK_VALUE, is_loopback_host, validate_bind_host


class ExposurePolicyTests(unittest.TestCase):
    def test_loopback_addresses_are_allowed_without_acknowledgement(self):
        for host in ("127.0.0.1", "127.42.0.8", "::1", "[::1]", "localhost", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
                validate_bind_host(host, {})

    def test_wildcard_lan_and_unknown_hosts_fail_closed(self):
        for host in ("0.0.0.0", "::", "192.168.1.25", "preview.internal", ""):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))
                with self.assertRaises(RuntimeError):
                    validate_bind_host(host, {})

    def test_non_loopback_requires_the_exact_acknowledgement(self):
        with self.assertRaises(RuntimeError):
            validate_bind_host("0.0.0.0", {NETWORK_ACK_ENV: "yes"})
        validate_bind_host("0.0.0.0", {NETWORK_ACK_ENV: NETWORK_ACK_VALUE})


if __name__ == "__main__":
    unittest.main()
