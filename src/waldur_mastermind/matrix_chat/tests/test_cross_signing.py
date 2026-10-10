import copy

from django.test import SimpleTestCase
from nio.crypto import OlmAccount

from waldur_mastermind.matrix_chat.crypto import cross_signing

USER = "@alice:test"


def _device_keys(user_id, device_id, account):
    """A device's keys as its client uploads them, signed by the device itself."""
    keys = {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {
            f"curve25519:{device_id}": account.identity_keys["curve25519"],
            f"ed25519:{device_id}": account.identity_keys["ed25519"],
        },
    }
    signature = account.sign(cross_signing.canonical_json(keys).decode())
    keys["signatures"] = {user_id: {f"ed25519:{device_id}": signature}}
    return keys


class Identity:
    """A user with cross-signing keys and some devices, as /keys/query shows them."""

    def __init__(self, user_id=USER):
        self.user_id = user_id
        self.master = cross_signing.SigningKey.generate()
        self.self_signing = cross_signing.SigningKey.generate()
        self.devices = {}

    def add_device(self, device_id, signed=True):
        keys = _device_keys(self.user_id, device_id, OlmAccount())
        if signed:
            keys = self.self_signing.sign(keys, self.user_id)
        self.devices[device_id] = keys
        return keys

    def response(self):
        return {
            "device_keys": {self.user_id: copy.deepcopy(self.devices)},
            "master_keys": {
                self.user_id: cross_signing.cross_signing_key(
                    self.user_id, cross_signing.MASTER, self.master
                )
            },
            "self_signing_keys": {
                self.user_id: cross_signing.cross_signing_key(
                    self.user_id,
                    cross_signing.SELF_SIGNING,
                    self.self_signing,
                    signed_by=self.master,
                )
            },
        }


class SigningTest(SimpleTestCase):
    def test_reads_signatures_made_by_a_device_through_vodozemac(self):
        account = OlmAccount()
        keys = _device_keys(USER, "DEVICE", account)
        self.assertTrue(
            cross_signing.has_signature(
                keys, USER, account.identity_keys["ed25519"], key_id="ed25519:DEVICE"
            )
        )

    def test_the_seed_restores_the_same_key(self):
        key = cross_signing.SigningKey.generate()
        self.assertEqual(cross_signing.SigningKey(key.seed).public_key, key.public_key)

    def test_signature_ignores_unsigned_and_survives_key_order(self):
        key = cross_signing.SigningKey.generate()
        signed = key.sign({"b": 1, "a": [1, "é"]}, USER)
        reordered = {"unsigned": {"x": 1}, "a": [1, "é"], **signed}
        self.assertTrue(cross_signing.has_signature(reordered, USER, key.public_key))

    def test_keeps_the_signatures_already_there(self):
        first = cross_signing.SigningKey.generate()
        second = cross_signing.SigningKey.generate()
        signed = second.sign(first.sign({"a": 1}, USER), USER)
        self.assertTrue(cross_signing.has_signature(signed, USER, first.public_key))
        self.assertTrue(cross_signing.has_signature(signed, USER, second.public_key))

    def test_a_changed_object_fails(self):
        key = cross_signing.SigningKey.generate()
        signed = key.sign({"a": 1}, USER)
        signed["a"] = 2
        self.assertFalse(cross_signing.has_signature(signed, USER, key.public_key))

    def test_garbage_fails_without_raising(self):
        key = cross_signing.SigningKey.generate()
        for obj in (
            None,
            {},
            {"signatures": "x"},
            {"signatures": {USER: {key.key_id: "not base64!"}}},
            {"signatures": {USER: {key.key_id: 5}}},
        ):
            with self.subTest(obj=obj):
                self.assertFalse(cross_signing.has_signature(obj, USER, key.public_key))
        self.assertFalse(cross_signing.has_signature({"a": 1}, USER, "short"))


class CrossSignedDevicesTest(SimpleTestCase):
    def setUp(self):
        self.identity = Identity()
        self.signed = self.identity.add_device("SIGNED")
        self.identity.add_device("UNSIGNED", signed=False)

    def test_only_devices_the_identity_signs(self):
        self.assertEqual(
            cross_signing.cross_signed_devices(USER, self.identity.response()),
            {"SIGNED": self.signed["keys"]["ed25519:SIGNED"]},
        )

    def test_a_self_signing_key_the_master_does_not_sign_vouches_for_nothing(self):
        response = self.identity.response()
        response["self_signing_keys"][USER] = cross_signing.cross_signing_key(
            USER,
            cross_signing.SELF_SIGNING,
            self.identity.self_signing,
            signed_by=cross_signing.SigningKey.generate(),
        )
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

    def test_another_users_identity_vouches_for_nothing(self):
        mallory = Identity("@mallory:test")
        response = self.identity.response()
        response["master_keys"][USER] = mallory.response()["master_keys"][
            "@mallory:test"
        ]
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

    def test_a_device_listed_under_another_id_is_refused(self):
        response = self.identity.response()
        response["device_keys"][USER]["OTHER"] = response["device_keys"][USER]["SIGNED"]
        self.assertEqual(
            set(cross_signing.cross_signed_devices(USER, response)), {"SIGNED"}
        )

    def test_a_signed_device_of_another_user_is_refused(self):
        # Signed by this user's self-signing key, but it says it is someone
        # else's device.
        response = self.identity.response()
        device = response["device_keys"][USER]["SIGNED"]
        device["user_id"] = "@mallory:test"
        device.pop("signatures")
        response["device_keys"][USER]["SIGNED"] = self.identity.self_signing.sign(
            device, USER
        )
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

    def test_a_device_whose_keys_were_swapped_is_refused(self):
        response = self.identity.response()
        response["device_keys"][USER]["SIGNED"]["keys"]["ed25519:SIGNED"] = (
            OlmAccount().identity_keys["ed25519"]
        )
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

    def test_wrong_usage_or_extra_keys_are_refused(self):
        response = self.identity.response()
        response["self_signing_keys"][USER]["usage"] = ["user_signing"]
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

        response = self.identity.response()
        response["master_keys"][USER]["keys"]["ed25519:extra"] = "extra"
        self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})

    def test_no_identity_or_malformed_response(self):
        for response in (
            {},
            {"master_keys": None, "self_signing_keys": None, "device_keys": None},
            {**self.identity.response(), "device_keys": {USER: ["x"]}},
        ):
            with self.subTest(response=response):
                self.assertEqual(cross_signing.cross_signed_devices(USER, response), {})
