import json
from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import override_settings

from waldur_core.core import encryption
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()
LOST_KEY = Fernet.generate_key().decode()


def run(**kwargs):
    out = StringIO()
    call_command("reencrypt_fields", stdout=out, **kwargs)
    return out.getvalue()


class ReencryptFieldsTest(TestCase):
    def setUp(self):
        self.resource = factories.ResourceFactory()

    def _key(self, plaintext, key):
        """Store a secret written under an arbitrary encryption key."""
        with override_settings(
            FIELD_ENCRYPTION_KEY=key, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            ciphertext = encryption.encrypt_value(plaintext)
        return models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id=f"cid-{models.ResourceApiKey.objects.count() + 1}",
            key_ciphertext=ciphertext,
            state=models.ResourceApiKey.States.OK,
        )

    def test_rows_survive_retiring_the_old_key(self):
        """The point of the command: make the fallback droppable."""
        api_key = self._key("sk-secret", OLD_KEY)

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            output = run()

        self.assertIn("re-encrypted 1 row(s)", output)
        api_key.refresh_from_db()
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(
                encryption.decrypt_value(api_key.key_ciphertext), "sk-secret"
            )

    def test_dry_run_writes_nothing(self):
        api_key = self._key("sk-secret", OLD_KEY)
        before = api_key.key_ciphertext

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            output = run(dry_run=True)

        self.assertIn("would re-encrypt 1 row(s)", output)
        api_key.refresh_from_db()
        self.assertEqual(api_key.key_ciphertext, before)

    def test_undecryptable_rows_are_reported_not_lost(self):
        """The case that produced a 409 on reveal weeks after the key went missing."""
        lost = self._key("sk-gone", LOST_KEY)
        fine = self._key("sk-here", OLD_KEY)
        before = lost.key_ciphertext

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            output = run()

        self.assertIn("1 row(s) cannot be decrypted", output)
        self.assertIn("FIELD_ENCRYPTION_KEY_FALLBACKS", output)
        # The unreadable row is left exactly as it was rather than overwritten.
        lost.refresh_from_db()
        self.assertEqual(lost.key_ciphertext, before)
        # And a readable sibling in the same run still gets rotated.
        fine.refresh_from_db()
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(encryption.decrypt_value(fine.key_ciphertext), "sk-here")

    def test_rerunning_is_harmless(self):
        api_key = self._key("sk-secret", OLD_KEY)

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            run()
            run()

        api_key.refresh_from_db()
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            self.assertEqual(
                encryption.decrypt_value(api_key.key_ciphertext), "sk-secret"
            )

    def test_plaintext_is_flagged_and_left_alone(self):
        """A pre-encryption deployment's value must not be silently rewritten."""
        api_key = models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id="cid-plain",
            key_ciphertext="sk-not-a-token",
            state=models.ResourceApiKey.States.OK,
        )

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            output = run()

        self.assertIn("is not encrypted", output)
        api_key.refresh_from_db()
        self.assertEqual(api_key.key_ciphertext, "sk-not-a-token")


class ReencryptJsonFieldTest(TestCase):
    """secret_options is a JSON blob: only its encrypted values must rotate."""

    def _offering_with_secret(self, plaintext, key):
        offering = factories.OfferingFactory()
        with override_settings(
            FIELD_ENCRYPTION_KEY=key, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            token = encryption.encrypt_value(plaintext)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE marketplace_offering SET secret_options = %s WHERE id = %s",
                [json.dumps({"token": token, "customer_uuid": "u1"}), offering.id],
            )
        return offering

    def test_encrypted_values_rotate_and_plaintext_is_left(self):
        offering = self._offering_with_secret("sk-secret", OLD_KEY)

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            output = run()

        self.assertIn("re-encrypted 1 row(s)", output)
        # Readable under the new key alone → it was rotated; plaintext key untouched.
        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            fresh = models.Offering.objects.get(pk=offering.pk)
            self.assertEqual(fresh.secret_options["token"], "sk-secret")
            self.assertEqual(fresh.secret_options["customer_uuid"], "u1")


class ReencryptServiceSettingsTest(TestCase):
    def test_password_and_token_rotate(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=OLD_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            settings = structure_factories.ServiceSettingsFactory(
                password="s3kret", token="t0ken"
            )

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            run()

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            fresh = ServiceSettings.objects.get(pk=settings.pk)
            self.assertEqual(fresh.password, "s3kret")
            self.assertEqual(fresh.token, "t0ken")
