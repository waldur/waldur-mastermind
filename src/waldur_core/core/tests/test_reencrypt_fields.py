import json
import re
from io import StringIO
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import override_settings

from waldur_core.core import encryption
from waldur_core.core.management.commands.reencrypt_fields import (
    encrypted_json_fields,
    encrypted_scalar_fields,
)
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


def _labels(fields):
    return {f"{model._meta.label}.{field.name}" for model, field in fields}


class EncryptedFieldDiscoveryTest(SimpleTestCase):
    """The command finds encrypted columns by field class, not from a list."""

    def test_finds_every_encrypted_column(self):
        self.assertEqual(
            _labels(encrypted_scalar_fields()),
            {
                "marketplace.ResourceApiKey.key_ciphertext",
                "matrix_chat.MatrixUserProfile.recovery_key",
                "structure.ServiceSettings.password",
                "structure.ServiceSettings.token",
            },
        )
        self.assertEqual(
            _labels(encrypted_json_fields()),
            {
                "marketplace.Offering.secret_options",
                "structure.ServiceSettings.options",
            },
        )

    def test_docs_list_exactly_the_encrypted_columns(self):
        """docs/field-encryption.md is the operator's list; it must not drift."""
        docs = Path(__file__).resolve().parents[4] / "docs" / "field-encryption.md"
        table = (
            docs.read_text().split("## What is encrypted", 1)[1].split("\n## ", 1)[0]
        )
        documented = set(re.findall(r"^\| `([\w.]+)` \|", table, re.MULTILINE))
        discovered = _labels(encrypted_scalar_fields()) | _labels(
            encrypted_json_fields()
        )
        self.assertEqual(documented, discovered)


class ReencryptDeletedRowTest(TestCase):
    def test_a_row_deleted_mid_run_is_skipped(self):
        resource = factories.ResourceFactory()
        with override_settings(
            FIELD_ENCRYPTION_KEY=OLD_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            ciphertext = encryption.encrypt_value("sk-secret")
        api_key = models.ResourceApiKey.objects.create(
            resource=resource,
            client_id="cid-gone",
            key_ciphertext=ciphertext,
            state=models.ResourceApiKey.States.OK,
        )
        manager = models.ResourceApiKey._base_manager
        original_filter = manager.filter

        def deleted_meanwhile(*args, **kwargs):
            models.ResourceApiKey.objects.filter(pk=api_key.pk).delete()
            return original_filter(*args, **kwargs)

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]
        ):
            with mock.patch.object(manager, "filter", deleted_meanwhile):
                output = run()  # must not raise

        self.assertIn("re-encrypted", output)
