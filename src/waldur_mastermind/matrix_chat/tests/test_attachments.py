import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.test import SimpleTestCase

from waldur_mastermind.matrix_chat.crypto import attachments

# NIST SP 800-38A, F.5.5 CTR-AES256.Encrypt.
NIST_KEY = bytes.fromhex(
    "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
)
NIST_IV = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
NIST_PLAINTEXT = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
NIST_CIPHERTEXT = bytes.fromhex(
    "601ec313775789a5b7a7f504bbf3d228"
    "f443e3ca4d62b59aca84e990cacaf5c5"
    "2b0930daa23de94ce87017ba2d84988d"
    "dfc9c58db67aada613c2dd08457941a6"
)


def b64(data):
    return base64.b64encode(data).decode().rstrip("=")


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def file_object(ciphertext, key, iv, url="mxc://hs.example/abc"):
    return {
        "url": url,
        "key": {
            "kty": "oct",
            "alg": "A256CTR",
            "ext": True,
            "key_ops": ["encrypt", "decrypt"],
            "k": b64url(key),
        },
        "iv": b64(iv),
        "hashes": {"sha256": b64(hashlib.sha256(ciphertext).digest())},
        "v": "v2",
    }


def encrypt(plaintext):
    """Encrypt as the drawer does: random key, IV with a zero counter half."""
    key = os.urandom(32)
    iv = os.urandom(8) + bytes(8)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return ciphertext, file_object(ciphertext, key, iv)


NIST_FILE = file_object(NIST_CIPHERTEXT, NIST_KEY, NIST_IV)


class DecryptTest(SimpleTestCase):
    def test_nist_vector(self):
        parsed = attachments.parse_encrypted_file(NIST_FILE)
        self.assertEqual(
            attachments.decrypt_attachment(NIST_CIPHERTEXT, parsed), NIST_PLAINTEXT
        )

    def test_round_trip(self):
        plaintext = os.urandom(100_000)
        ciphertext, raw = encrypt(plaintext)
        parsed = attachments.parse_encrypted_file(raw)
        self.assertEqual(attachments.decrypt_attachment(ciphertext, parsed), plaintext)

    def test_a_swapped_file_is_not_decrypted(self):
        parsed = attachments.parse_encrypted_file(NIST_FILE)
        altered = bytes([NIST_CIPHERTEXT[0] ^ 1]) + NIST_CIPHERTEXT[1:]
        with self.assertRaises(attachments.AttachmentError):
            attachments.decrypt_attachment(altered, parsed)

    def test_the_counter_wraps_within_its_64_bits(self):
        # As browsers decrypt it (WebCrypto AES-CTR, length 64): the counter
        # half wraps to zero and never carries into the nonce.
        key = os.urandom(32)
        nonce = os.urandom(8)
        iv = nonce + (2**64 - 2).to_bytes(8)
        plaintext = os.urandom(16 * 4)
        ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305
        counters = [(2**64 - 2 + i) % 2**64 for i in range(4)]
        keystream = b"".join(ecb.update(nonce + c.to_bytes(8)) for c in counters)
        ciphertext = bytes(p ^ k for p, k in zip(plaintext, keystream))
        parsed = attachments.parse_encrypted_file(file_object(ciphertext, key, iv))
        self.assertEqual(attachments.decrypt_attachment(ciphertext, parsed), plaintext)

    def test_the_key_never_shows_in_a_repr(self):
        parsed = attachments.parse_encrypted_file(NIST_FILE)
        self.assertNotIn(NIST_FILE["key"]["k"], repr(parsed))
        self.assertNotIn(repr(NIST_KEY), repr(parsed))


class ParseEncryptedFileTest(SimpleTestCase):
    def test_accepts_a_well_formed_file(self):
        parsed = attachments.parse_encrypted_file(
            {**NIST_FILE, "mimetype": "text/html"}
        )
        self.assertEqual(parsed.url, NIST_FILE["url"])
        self.assertEqual(parsed.attachment_key, NIST_KEY)
        self.assertEqual(parsed.iv, NIST_IV)

    def test_accepts_a_padded_key_as_some_clients_send(self):
        raw = {**NIST_FILE, "key": {**NIST_FILE["key"], "k": b64url(NIST_KEY) + "="}}
        self.assertIsNotNone(attachments.parse_encrypted_file(raw))

    def test_rejects_malformed_files(self):
        key = NIST_FILE["key"]
        cases = {
            "not an object": "nope",
            "no key": {**NIST_FILE, "key": None},
            "no hashes": {**NIST_FILE, "hashes": None},
            "an https url": {**NIST_FILE, "url": "https://evil.example/x"},
            "a url without media id": {**NIST_FILE, "url": "mxc://hs.example/"},
            "a url with a path": {**NIST_FILE, "url": "mxc://hs.example/../admin"},
            "version v1": {**NIST_FILE, "v": "v1"},
            "no version": {k: v for k, v in NIST_FILE.items() if k != "v"},
            "another algorithm": {**NIST_FILE, "key": {**key, "alg": "A128CTR"}},
            "another key type": {**NIST_FILE, "key": {**key, "kty": "RSA"}},
            "no decrypt op": {**NIST_FILE, "key": {**key, "key_ops": ["encrypt"]}},
            "a 128-bit key": {
                **NIST_FILE,
                "key": {**key, "k": b64url(bytes(16))},
            },
            "a key in standard base64": {
                **NIST_FILE,
                "key": {**key, "k": "+" + key["k"][1:]},
            },
            "a short IV": {**NIST_FILE, "iv": b64(bytes(8))},
            "an IV with whitespace": {**NIST_FILE, "iv": " " + NIST_FILE["iv"]},
            "a short hash": {**NIST_FILE, "hashes": {"sha256": "AAAA"}},
            "a numeric hash": {**NIST_FILE, "hashes": {"sha256": 42}},
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assertIsNone(attachments.parse_encrypted_file(raw))


class ParseMxcTest(SimpleTestCase):
    def test_parses_server_and_media_id(self):
        self.assertEqual(
            attachments.parse_mxc("mxc://matrix.example.com/AbC_d-1"),
            ("matrix.example.com", "AbC_d-1"),
        )
        self.assertEqual(
            attachments.parse_mxc("mxc://[::1]:8448/abc"), ("[::1]:8448", "abc")
        )

    def test_rejects_anything_else(self):
        for url in (
            None,
            "https://hs.example/abc",
            "mxc://hs.example/a/b",
            "mxc://hs.example/a%2Fb",
            "mxc://hs.example/abc?x=1",
            "mxc://hs example/abc",
        ):
            with self.subTest(url):
                self.assertIsNone(attachments.parse_mxc(url))
