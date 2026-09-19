import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Load a JSON fixture. api fixtures are real, public payloads captured from Kalshi prod on 2026-09-19."""

    def _load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    # 2048-bit keys generate quickly and are what Kalshi issues. Generated per test session, never stored.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
