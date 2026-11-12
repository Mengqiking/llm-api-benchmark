"""Run all tests with network sockets blocked; no dependency installation."""
from pathlib import Path
import os
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    os.chdir(ROOT)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    with mock.patch("socket.socket", side_effect=AssertionError("offline test opened a socket")), mock.patch(
        "socket.getaddrinfo", side_effect=AssertionError("offline test resolved an address")
    ):
        suite = unittest.defaultTestLoader.discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
