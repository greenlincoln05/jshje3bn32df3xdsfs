import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding

from btcbot.kalshi_client import KalshiAuth, signing_message

DIGEST_LEN = hashes.SHA256().digest_size  # 32: Kalshi requires salt length == digest length
DOCS_PATH = "/trade-api/v2/portfolio/balance"


def pss(salt_length: int) -> padding.PSS:
    return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=salt_length)


class TestSigningMessage:
    def test_matches_the_example_in_kalshis_docs(self):
        assert signing_message("1703123456789", "GET", DOCS_PATH) == "1703123456789GET/trade-api/v2/portfolio/balance"

    def test_query_string_is_stripped(self):
        message = signing_message("1", "GET", "/trade-api/v2/portfolio/orders?limit=5&cursor=abc")
        assert message == "1GET/trade-api/v2/portfolio/orders"

    def test_method_is_uppercased(self):
        assert signing_message("1", "post", "/trade-api/v2/x") == "1POST/trade-api/v2/x"


class TestKnownVector:
    """The signature in the fixture was produced by OpenSSL, independently of the `cryptography` library.

    PSS is randomised (fresh salt per signature), so byte-exact output cannot be asserted for our own
    signer. Instead: (1) an independent implementation's signature must verify under our parameters, and
    (2) our signatures must verify under strict parameters (see TestKalshiAuth). During development our
    signature was also verified by OpenSSL in the other direction.
    """

    def test_openssl_signature_verifies_under_kalshis_parameters(self, load_fixture):
        vector = load_fixture("signing_vector.json")
        public_key = serialization.load_pem_public_key(vector["public_key_pem"].encode())
        # verify() raises InvalidSignature on any mismatch
        public_key.verify(
            base64.b64decode(vector["signature_b64"]), vector["message"].encode(), pss(DIGEST_LEN), hashes.SHA256()
        )

    def test_vector_message_is_what_our_code_would_sign(self, load_fixture):
        assert load_fixture("signing_vector.json")["message"] == signing_message("1703123456789", "GET", DOCS_PATH)

    def test_vector_does_not_verify_for_a_different_message(self, load_fixture):
        vector = load_fixture("signing_vector.json")
        public_key = serialization.load_pem_public_key(vector["public_key_pem"].encode())
        with pytest.raises(InvalidSignature):
            public_key.verify(
                base64.b64decode(vector["signature_b64"]),
                signing_message("1703123456789", "POST", DOCS_PATH).encode(),
                pss(DIGEST_LEN),
                hashes.SHA256(),
            )


class TestKalshiAuth:
    def test_signature_verifies_with_digest_length_salt_and_no_other(self, rsa_key):
        signature = base64.b64decode(KalshiAuth("key-id", rsa_key).sign("hello"))
        public_key = rsa_key.public_key()
        public_key.verify(signature, b"hello", pss(DIGEST_LEN), hashes.SHA256())
        with pytest.raises(InvalidSignature):  # a strict verifier with a different salt length must reject it
            public_key.verify(signature, b"hello", pss(DIGEST_LEN - 12), hashes.SHA256())

    def test_signatures_are_randomised(self, rsa_key):
        auth = KalshiAuth("key-id", rsa_key)
        assert auth.sign("same message") != auth.sign("same message")

    def test_headers(self, rsa_key):
        auth = KalshiAuth("key-id-1234", rsa_key, clock_ms=lambda: 1_703_123_456_789)
        headers = auth.headers("GET", f"{DOCS_PATH}?limit=5")

        assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
        assert headers["KALSHI-ACCESS-KEY"] == "key-id-1234"
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"  # integer milliseconds
        rsa_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            b"1703123456789GET/trade-api/v2/portfolio/balance",  # query string not signed
            pss(DIGEST_LEN),
            hashes.SHA256(),
        )

    def test_default_clock_is_milliseconds(self, rsa_key):
        timestamp = int(KalshiAuth("key-id", rsa_key).headers("GET", DOCS_PATH)["KALSHI-ACCESS-TIMESTAMP"])
        assert 1_700_000_000_000 < timestamp < 4_000_000_000_000  # seconds would be ~1.7e9

    def test_repr_never_exposes_key_material(self, rsa_key):
        text = repr(KalshiAuth("abcd-1234-not-shown-9f3c", rsa_key))
        assert text == "KalshiAuth(key_id=...9f3c)"
        assert "not-shown" not in text and "PRIVATE" not in text

    def test_empty_key_id_is_rejected(self, rsa_key):
        with pytest.raises(ValueError, match="key_id"):
            KalshiAuth("", rsa_key)


class TestPrivateKeyLoading:
    @pytest.mark.parametrize(
        "fmt", [serialization.PrivateFormat.TraditionalOpenSSL, serialization.PrivateFormat.PKCS8], ids=["pkcs1", "pkcs8"]
    )
    def test_loads_rsa_pem_files(self, tmp_path, rsa_key, fmt):
        path = tmp_path / "kalshi.key"
        path.write_bytes(rsa_key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()))

        auth = KalshiAuth.from_pem_file("key-id", path)

        rsa_key.public_key().verify(base64.b64decode(auth.sign("m")), b"m", pss(DIGEST_LEN), hashes.SHA256())

    def test_rejects_non_rsa_keys(self, tmp_path):
        ec_key = ec.generate_private_key(ec.SECP256R1())
        path = tmp_path / "ec.key"
        path.write_bytes(
            ec_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        with pytest.raises(ValueError, match="RSA"):
            KalshiAuth.from_pem_file("key-id", path)

    def test_rejects_garbage(self, tmp_path):
        path = tmp_path / "junk.key"
        path.write_text("this is not a key")
        with pytest.raises(ValueError, match="could not load"):
            KalshiAuth.from_pem_file("key-id", path)

    def test_rejects_passphrase_protected_keys(self, tmp_path, rsa_key):
        path = tmp_path / "locked.key"
        path.write_bytes(
            rsa_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(b"pw"),
            )
        )
        with pytest.raises(ValueError, match="could not load"):
            KalshiAuth.from_pem_file("key-id", path)

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            KalshiAuth.from_pem_file("key-id", tmp_path / "absent.key")
