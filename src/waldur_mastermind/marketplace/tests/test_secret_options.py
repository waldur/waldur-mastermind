"""Tests for at-rest encryption of ``Offering.secret_options``.

Covers the two properties the field must hold at once: sensitive values are
opaque in the database while the ORM still sees plaintext, and the plaintext
round-trip keeps FieldTracker from reporting phantom changes (which would fire
handlers on every save).
"""

import importlib
import json
from types import SimpleNamespace
from unittest import mock

import reversion
from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from reversion.models import Version

from waldur_core.core import encryption
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import enums, models, serializers
from waldur_mastermind.marketplace.secret_options import is_sensitive_key
from waldur_mastermind.marketplace.tests import factories

_backfill = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0269_encrypt_existing_secret_options"
)
_scrub = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0270_scrub_secret_options_from_reversion"
)


def _set_raw_secret_options(offering, data):
    """Force the column to a literal value, bypassing the field's encryption."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE marketplace_offering SET secret_options = %s WHERE id = %s",
            [json.dumps(data), offering.id],
        )


def _run_backfill():
    _backfill.encrypt_existing_secret_options(
        global_apps, SimpleNamespace(connection=connection)
    )


# Where the OpenStack IP-mapping handler's side effect lives; patched to prove the
# handler does (not) run without touching real OpenStack.
_IP_UPDATE = (
    "waldur_mastermind.marketplace_openstack.utils"
    ".update_external_addresses_of_offering_floating_ips"
)


def _raw_secret_options(offering):
    """The secret_options column as physically stored, bypassing the field."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT secret_options FROM marketplace_offering WHERE id = %s",
            [offering.id],
        )
        raw = cursor.fetchone()[0]
    # The raw jsonb column comes back as a JSON string here, not a parsed dict.
    return json.loads(raw) if isinstance(raw, str) else raw


class SecretOptionsEncryptionTest(TestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            secret_options={
                "password": "s3kret",
                "token": "t0ken",
                "customer_uuid": "cust-1",
                "language": "python",
            }
        )

    def test_sensitive_values_are_encrypted_at_rest(self):
        raw = _raw_secret_options(self.offering)
        self.assertTrue(raw["password"].startswith("gAAAA"))
        self.assertTrue(raw["token"].startswith("gAAAA"))

    def test_non_sensitive_values_stay_plaintext_at_rest(self):
        raw = _raw_secret_options(self.offering)
        # customer_uuid is queried by value elsewhere and must not be encrypted.
        self.assertEqual(raw["customer_uuid"], "cust-1")
        self.assertEqual(raw["language"], "python")

    def test_values_are_decrypted_on_read(self):
        fresh = type(self.offering).objects.get(pk=self.offering.pk)
        self.assertEqual(fresh.secret_options["password"], "s3kret")
        self.assertEqual(fresh.secret_options["token"], "t0ken")

    def test_has_key_query_still_works(self):
        # JSON keys stay plaintext, so key-existence lookups are unaffected.
        qs = type(self.offering).objects.filter(secret_options__has_key="password")
        self.assertIn(self.offering, qs)

    def test_plaintext_value_lookup_still_works(self):
        qs = type(self.offering).objects.filter(secret_options__customer_uuid="cust-1")
        self.assertIn(self.offering, qs)


class SecretOptionsTrackerTest(TestCase):
    """The crux: transparent plaintext attribute keeps FieldTracker honest."""

    def test_unchanged_resave_is_not_a_tracked_change(self):
        offering = factories.OfferingFactory(secret_options={"password": "s3kret"})
        fresh = type(offering).objects.get(pk=offering.pk)
        # Freshly loaded and untouched: the tracker must see no change, even though
        # re-saving would produce different ciphertext (random IV) in the column.
        self.assertFalse(fresh.tracker.has_changed("secret_options"))

    def test_real_plaintext_change_is_still_detected(self):
        offering = factories.OfferingFactory(secret_options={"password": "s3kret"})
        fresh = type(offering).objects.get(pk=offering.pk)
        fresh.secret_options = {"password": "rotated"}
        self.assertTrue(fresh.tracker.has_changed("secret_options"))


class SecretOptionsClassificationTest(TestCase):
    def test_password_suffix_encrypted_in_any_type_urls_and_usernames_are_not(self):
        offering = factories.OfferingFactory(
            type=enums.SUPPORT_OFFERING,
            secret_options={
                "keycloak_password": "kc",
                "api_url": "https://example.com",
                "username": "bob",
            },
        )
        raw = _raw_secret_options(offering)
        # *_password is a global credential, encrypted regardless of offering type.
        self.assertTrue(raw["keycloak_password"].startswith("gAAAA"))
        # Endpoint URLs and usernames are not secrets and stay plaintext.
        self.assertEqual(raw["api_url"], "https://example.com")
        self.assertEqual(raw["username"], "bob")

    def test_argocd_kubeconfig_encrypted_regardless_of_offering_type(self):
        # Classified by name, not offering type (encrypt/decrypt must agree), so it is
        # encrypted even on a non-Rancher offering, where it does not normally occur.
        for offering_type in (enums.RANCHER_OFFERING, enums.SUPPORT_OFFERING):
            offering = factories.OfferingFactory(
                type=offering_type,
                secret_options={"argocd_k8s_kubeconfig": "kube"},
            )
            raw = _raw_secret_options(offering)
            self.assertTrue(raw["argocd_k8s_kubeconfig"].startswith("gAAAA"))


class SecretOptionsOracleTest(TestCase):
    """A caller must not be able to use the field as a decryption oracle: submit a
    stolen ciphertext and read back its plaintext."""

    def test_token_shaped_value_under_non_sensitive_key_is_not_decrypted(self):
        stolen = encryption.encrypt_value("another-tenant-secret")
        offering = factories.OfferingFactory(secret_options={"customer_uuid": stolen})

        fresh = models.Offering.objects.get(pk=offering.pk)
        # customer_uuid is not sensitive → not decrypted → the token is returned as-is,
        # never its plaintext.
        self.assertEqual(fresh.secret_options["customer_uuid"], stolen)
        self.assertNotEqual(
            fresh.secret_options["customer_uuid"], "another-tenant-secret"
        )

    def test_token_shaped_value_under_sensitive_key_is_wrapped_not_passed_through(self):
        stolen = encryption.encrypt_value("another-tenant-secret")
        offering = factories.OfferingFactory(secret_options={"token": stolen})

        # Encrypted unconditionally on write → the injected token is wrapped, so at rest
        # it is not the injected value.
        self.assertNotEqual(_raw_secret_options(offering)["token"], stolen)
        # Reading decrypts one layer → back to the injected token, not its plaintext.
        fresh = models.Offering.objects.get(pk=offering.pk)
        self.assertEqual(fresh.secret_options["token"], stolen)
        self.assertNotEqual(fresh.secret_options["token"], "another-tenant-secret")


class SecretOptionsHandlerTest(TestCase):
    """Handlers gated on ``tracker.has_changed('secret_options')`` must not fire on a
    no-op save. This is the concrete proof of the tracker invariant: an offering whose
    secret_options carry an encrypted key (different ciphertext each write) must still
    read as 'unchanged' when its plaintext did not change."""

    def _openstack_offering(self):
        # An encrypted key (password) forces fresh ciphertext on every write; the
        # non-sensitive ipv4 mapping is what the handler actually compares.
        return factories.OfferingFactory(
            type=enums.OPENSTACK_TENANT_OFFERING,
            secret_options={
                "password": "s3kret",
                "ipv4_external_ip_mapping": [
                    {"floating_ip": "1.1.1.1", "external_ip": "2.2.2.2"}
                ],
            },
        )

    def test_unchanged_resave_does_not_trigger_ip_recompute(self):
        offering = self._openstack_offering()
        fresh = type(offering).objects.get(pk=offering.pk)
        with mock.patch(_IP_UPDATE) as ip_update:
            fresh.save()
        ip_update.assert_not_called()

    def test_ip_mapping_change_still_triggers_recompute(self):
        offering = self._openstack_offering()
        fresh = type(offering).objects.get(pk=offering.pk)
        fresh.secret_options = {
            **fresh.secret_options,
            "ipv4_external_ip_mapping": [
                {"floating_ip": "9.9.9.9", "external_ip": "8.8.8.8"}
            ],
        }
        with mock.patch(_IP_UPDATE) as ip_update:
            fresh.save()
        ip_update.assert_called_once()


class SecretOptionsBackfillMigrationTest(TestCase):
    """The 0265 data migration encrypts secret_options rows written before 0264."""

    def _plaintext_offering(self):
        offering = factories.OfferingFactory(
            type=enums.RANCHER_OFFERING, secret_options={}
        )
        # Simulate a legacy row: plaintext credentials at rest.
        _set_raw_secret_options(
            offering,
            {
                "password": "s3kret",  # global rule
                "argocd_k8s_kubeconfig": "kube",  # per-type Rancher
                "customer_uuid": "cust-1",  # not sensitive
            },
        )
        return offering

    def test_backfill_encrypts_plaintext_rows(self):
        offering = self._plaintext_offering()
        # Precondition: stored in the clear.
        raw = _raw_secret_options(offering)
        self.assertEqual(raw["password"], "s3kret")

        _run_backfill()

        raw = _raw_secret_options(offering)
        self.assertTrue(raw["password"].startswith("gAAAA"))
        self.assertTrue(raw["argocd_k8s_kubeconfig"].startswith("gAAAA"))
        self.assertEqual(raw["customer_uuid"], "cust-1")
        # And it round-trips: the ORM reads back the original plaintext.
        fresh = type(offering).objects.get(pk=offering.pk)
        self.assertEqual(fresh.secret_options["password"], "s3kret")
        self.assertEqual(fresh.secret_options["argocd_k8s_kubeconfig"], "kube")

    def test_backfill_is_idempotent(self):
        offering = self._plaintext_offering()
        _run_backfill()
        encrypted_once = _raw_secret_options(offering)

        _run_backfill()
        encrypted_twice = _raw_secret_options(offering)

        # Already-encrypted rows are skipped, so the ciphertext is left untouched
        # (a re-encrypt would have produced a different token).
        self.assertEqual(encrypted_once, encrypted_twice)


class SecretOptionsReversionTest(TestCase):
    """secret_options must not leak as plaintext into django-reversion history."""

    def _make_version(self, offering):
        with reversion.create_revision():
            reversion.add_to_revision(offering)
        return Version.objects.get_for_object(offering).first()

    def test_new_revisions_exclude_secret_options(self):
        offering = factories.OfferingFactory(secret_options={"password": "s3kret"})
        version = self._make_version(offering)
        fields = json.loads(version.serialized_data)[0]["fields"]
        # Excluded from registration → never serialized into history.
        self.assertNotIn("secret_options", fields)

    def test_scrub_removes_secret_options_from_existing_history(self):
        offering = factories.OfferingFactory(secret_options={"password": "s3kret"})
        version = self._make_version(offering)
        # Simulate a legacy version written before the exclude: inject plaintext.
        objects = json.loads(version.serialized_data)
        objects[0]["fields"]["secret_options"] = {"password": "leaked-plaintext"}
        version.serialized_data = json.dumps(objects)
        version.save(update_fields=["serialized_data"])

        _scrub.scrub_secret_options(global_apps, SimpleNamespace(connection=connection))

        version.refresh_from_db()
        fields = json.loads(version.serialized_data)[0]["fields"]
        self.assertNotIn("secret_options", fields)

    def test_revert_preserves_secret_options_encrypted(self):
        offering = factories.OfferingFactory(secret_options={"token": "keep-me"})
        original_name = offering.name
        with reversion.create_revision():
            reversion.add_to_revision(offering)
        offering.name = "renamed"
        offering.save()
        with reversion.create_revision():
            reversion.add_to_revision(offering)

        # Revert to the first version. It excludes secret_options, so a naive revert
        # would wipe the live credentials; the pre_save handler restores them.
        Version.objects.get_for_object(offering).last().revision.revert()

        fresh = models.Offering.objects.get(pk=offering.pk)
        self.assertEqual(fresh.name, original_name)  # revert happened...
        self.assertEqual(fresh.secret_options["token"], "keep-me")  # ...without a wipe
        self.assertTrue(_raw_secret_options(offering)["token"].startswith("gAAAA"))

    def test_raw_save_encrypts_plaintext_secret_options(self):
        # loaddata / deserialization save with raw=True, which skips the field's
        # pre_save; the handler must still encrypt a non-empty secret_options.
        offering = factories.OfferingFactory(secret_options={})
        offering.secret_options = {"token": "plain"}
        offering.save_base(raw=True)

        self.assertTrue(_raw_secret_options(offering)["token"].startswith("gAAAA"))
        self.assertEqual(
            models.Offering.objects.get(pk=offering.pk).secret_options["token"], "plain"
        )

    def test_raw_save_does_not_double_wrap_existing_ciphertext(self):
        # An already-encrypted value on a raw save (e.g. an undecryptable token restored
        # on revert) must not be wrapped a second time.
        token = encryption.encrypt_value("secret")
        offering = factories.OfferingFactory(secret_options={})
        offering.secret_options = {"token": token}
        offering.save_base(raw=True)

        self.assertEqual(_raw_secret_options(offering)["token"], token)


class OfferingAdminSecretOptionsTest(TestCase):
    def test_secret_options_is_not_editable_in_admin(self):
        offering = factories.OfferingFactory(secret_options={"password": "s3kret"})
        staff = structure_factories.UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(staff)

        url = reverse("admin:marketplace_offering_change", args=[offering.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        # A read-only field renders without a form input, so there is no editable
        # widget to change the stored credentials from the raw admin.
        self.assertNotContains(response, 'name="secret_options"')


# Every ``secret_options`` key that is deliberately stored in plaintext, with the
# reason it is safe. Anything not listed here must be classified sensitive by
# ``is_sensitive_key``; the test below fails on a key that is neither, so a new
# credential cannot reach the column in cleartext without somebody saying so here.
KNOWN_PLAINTEXT_KEYS = {
    # Endpoints and hosts — locations, not credentials.
    "api_url",
    "backend_url",
    "keycloak_url",
    "private_registry_url",
    "vault_host",
    "vault_port",
    # Identifiers and names — no access on their own, and customer_uuid is queried
    # by value (`secret_options__customer_uuid`), so it must stay readable.
    "customer_uuid",
    "argocd_k8s_namespace",
    "base_image_name",
    "k8s_version",
    "keycloak_realm",
    "keycloak_user_realm",
    "language",
    "node_disk_driver",
    # Usernames — half of a credential pair; the secret half is encrypted.
    "keycloak_username",
    "private_registry_user",
    "username",
    # Non-secret configuration flags and tuning.
    "keycloak_ssl_verify",
    "keycloak_sync_frequency",
    "vault_tls_verify",
    "dns_nameservers",
    "ipv4_external_ip_mapping",
    # Public material and free text.
    "openstack_api_tls_certificate",
    "cloud_init_template",
    "managed_rancher_load_balancer_cloud_init_template",
    "template_confirmation_comment",
    # Script offering hooks and their environment — explicitly out of scope for
    # this feature; see docs/field-encryption.md.
    "create",
    "update",
    "pull",
    "terminate",
    "environ",
}


class SecretOptionsClassificationDriftTest(TestCase):
    """Guard against a new credential key silently landing in plaintext.

    ``is_sensitive_key`` covers ``password`` / ``token`` / ``*_password`` / ``*_token``
    plus a named exception list. A plugin author who adds ``client_secret``,
    ``private_key`` or ``api_key`` to a secret-options serializer would match none of
    those and get no warning — the write would simply be stored in cleartext. This
    test turns that into a failing build: every declared key must be either classified
    sensitive or listed above as knowingly plaintext.
    """

    def _declared_keys(self):
        return set(serializers.MergedSecretOptionsSerializer().fields)

    def test_every_declared_key_is_classified(self):
        unclassified = {
            key
            for key in self._declared_keys()
            if not is_sensitive_key(key) and key not in KNOWN_PLAINTEXT_KEYS
        }

        self.assertEqual(
            unclassified,
            set(),
            "New secret_options key(s) are neither encrypted nor listed as knowingly "
            "plaintext. If the value is a credential, make is_sensitive_key match it "
            "(the *_password / *_token suffix does so automatically, otherwise add it "
            "to _EXTRA_SENSITIVE_KEYS). If it is not, add it to KNOWN_PLAINTEXT_KEYS "
            "with the reason.",
        )

    def test_plaintext_allowlist_does_not_contradict_the_classifier(self):
        contradictory = {key for key in KNOWN_PLAINTEXT_KEYS if is_sensitive_key(key)}

        self.assertEqual(
            contradictory,
            set(),
            "Key(s) listed as knowingly plaintext are in fact encrypted. Drop them "
            "from KNOWN_PLAINTEXT_KEYS so the list keeps describing reality.",
        )

    def test_plaintext_allowlist_has_no_stale_entries(self):
        stale = KNOWN_PLAINTEXT_KEYS - self._declared_keys()

        self.assertEqual(
            stale,
            set(),
            "KNOWN_PLAINTEXT_KEYS names key(s) no longer declared by any secret-options "
            "serializer. Delete them, so that re-adding a key with the same name goes "
            "through this review again instead of being silently pre-approved.",
        )
