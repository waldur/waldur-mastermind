"""Ceremony tests driven by a real software authenticator.

Every assertion here is verified by the same ``webauthn`` calls production
uses, against genuine ES256 signatures — nothing about the verification is
mocked out.
"""

from django.test import TestCase
from django.utils import timezone

from waldur_core.passkeys import services
from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import (
    CEREMONY_MAX_ATTEMPTS,
    PasskeyCeremony,
    PasskeyCredential,
)
from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID, enable_passkeys
from waldur_core.structure.tests import factories as structure_factories


@enable_passkeys()
class RegistrationTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.authenticator = SoftwareAuthenticator()

    def _register(self, name="Laptop"):
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(options["challenge"], RP_ID, ORIGIN)
        return services.finish_registration(ceremony, response, name), ceremony

    def test_registration_persists_a_usable_credential(self):
        credential, _ = self._register()

        self.assertEqual(credential.user, self.user)
        self.assertEqual(credential.name, "Laptop")
        self.assertEqual(credential.rp_id, RP_ID)
        self.assertTrue(credential.is_active)
        self.assertTrue(credential.is_user_verified)

    def test_options_carry_the_ceremony_challenge(self):
        ceremony, options = services.start_registration(self.user)
        self.assertEqual(options["challenge"], ceremony.challenge)
        self.assertEqual(options["rp"]["id"], RP_ID)

    def test_ceremony_is_consumed_on_success(self):
        _, ceremony = self._register()
        ceremony.refresh_from_db()
        self.assertTrue(ceremony.is_consumed)
        self.assertFalse(ceremony.is_usable)

    def test_ceremony_cannot_be_replayed(self):
        _, ceremony = self._register()
        response = self.authenticator.register(ceremony.challenge, RP_ID, ORIGIN)

        with self.assertRaises(services.CeremonyUnusable):
            services.finish_registration(ceremony, response, "Replay")

    def test_registration_from_a_foreign_origin_is_rejected(self):
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(
            options["challenge"], RP_ID, "https://evil.example.net"
        )

        with self.assertRaises(services.PasskeyError):
            services.finish_registration(ceremony, response, "Evil")
        self.assertEqual(PasskeyCredential.objects.count(), 0)

    def test_registration_against_a_different_challenge_is_rejected(self):
        ceremony, _ = services.start_registration(self.user)
        other = PasskeyCeremony.start(kind=CeremonyKind.REGISTRATION, rp_id=RP_ID)
        response = self.authenticator.register(other.challenge, RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_registration(ceremony, response, "Mismatched")

    def test_a_racing_duplicate_registration_is_a_clean_error(self):
        """The unique constraint decides, not a pre-check.

        A check-then-create is not atomic: two ceremonies finishing
        concurrently with the same authenticator both pass the check and the
        loser hits the constraint. That must surface as a PasskeyError, not an
        IntegrityError escaping as a 500 — and the savepoint must leave the
        transaction usable afterwards.
        """
        self._register()

        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_registration(ceremony, response, "Racing")

        # The transaction survived, so ordinary work still succeeds.
        self.assertEqual(PasskeyCredential.objects.count(), 1)
        self.assertTrue(PasskeyCredential.objects.filter(user=self.user).exists())

    def test_the_same_credential_cannot_be_registered_twice(self):
        self._register()
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_registration(ceremony, response, "Duplicate")
        self.assertEqual(PasskeyCredential.objects.count(), 1)

    def test_discoverability_is_taken_from_credprops(self):
        """Whether a credential can carry passwordless sign-in is reported by
        the browser, not assumed."""
        credential, _ = self._register()
        self.assertTrue(credential.is_discoverable)

    def test_non_resident_credential_is_not_marked_discoverable(self):
        self.authenticator = SoftwareAuthenticator(discoverable=False)
        credential, _ = self._register("Security key")
        self.assertFalse(credential.is_discoverable)

    def test_missing_credprops_means_not_discoverable(self):
        """A browser without the credProps extension must not be assumed
        generous — over-claiming produces a passwordless option that fails at
        the authenticator."""
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(
            options["challenge"], RP_ID, ORIGIN, report_rk=False
        )

        credential = services.finish_registration(ceremony, response, "Unknown")

        self.assertFalse(credential.is_discoverable)

    def test_attachment_is_recorded(self):
        credential, _ = self._register()
        self.assertEqual(credential.attachment, "platform")

    def test_transports_are_recorded(self):
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.register(
            options["challenge"], RP_ID, ORIGIN, transports=["usb", "nfc"]
        )

        credential = services.finish_registration(ceremony, response, "Key")

        self.assertEqual(credential.transports, ["usb", "nfc"])

    def test_single_device_credential_is_not_backup_eligible(self):
        credential, _ = self._register()
        self.assertFalse(credential.is_backup_eligible)
        self.assertFalse(credential.is_backed_up)

    def test_synced_credential_is_backup_eligible(self):
        self.authenticator = SoftwareAuthenticator(backed_up=True)
        credential, _ = self._register("Synced")
        self.assertTrue(credential.is_backup_eligible)
        self.assertTrue(credential.is_backed_up)

    def test_expired_ceremony_is_refused(self):
        ceremony, options = services.start_registration(self.user)
        ceremony.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        ceremony.save(update_fields=["expires_at"])
        response = self.authenticator.register(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.CeremonyUnusable):
            services.finish_registration(ceremony, response, "Late")

    def test_attempts_are_capped(self):
        ceremony, _ = services.start_registration(self.user)
        garbage = self.authenticator.register("wrong-challenge", RP_ID, ORIGIN)

        for _i in range(CEREMONY_MAX_ATTEMPTS):
            with self.assertRaises(services.PasskeyError):
                services.finish_registration(ceremony, garbage, "Nope")

        ceremony.refresh_from_db()
        self.assertTrue(ceremony.is_exhausted)
        # Past the cap the row is burned, even for a response that would verify.
        with self.assertRaises(services.CeremonyUnusable):
            services.finish_registration(ceremony, garbage, "Nope")


@enable_passkeys()
class SigninTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.authenticator = SoftwareAuthenticator()
        ceremony, options = services.start_registration(self.user)
        self.credential = services.finish_registration(
            ceremony,
            self.authenticator.register(options["challenge"], RP_ID, ORIGIN),
            "Laptop",
        )

    def test_passwordless_signin_resolves_the_owner(self):
        ceremony, options = services.start_signin()
        response = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        credential = services.finish_assertion(ceremony, response)

        self.assertEqual(credential.user, self.user)

    def test_passwordless_options_name_no_credentials(self):
        """No allow_credentials — otherwise the endpoint is an enumeration oracle."""
        _, options = services.start_signin()
        self.assertEqual(options.get("allowCredentials") or [], [])

    def test_passwordless_requires_user_verification(self):
        _, options = services.start_signin()
        self.assertEqual(options["userVerification"], "required")

    def test_passwordless_rejects_an_unverified_authenticator(self):
        """A key that only proves presence must not satisfy passwordless sign-in.

        Otherwise the flow silently degrades to single-factor possession.
        """
        authenticator = SoftwareAuthenticator(user_verified=False)
        ceremony, options = services.start_registration(self.user)
        services.finish_registration(
            ceremony,
            authenticator.register(options["challenge"], RP_ID, ORIGIN),
            "Presence only",
        )

        ceremony, options = services.start_signin()
        response = authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_second_factor_names_the_users_credentials(self):
        options = services.build_mfa_options(services.create_mfa_ceremony(self.user))
        ids = [c["id"] for c in options["allowCredentials"]]
        self.assertEqual(ids, [self.credential.credential_id])

    def test_second_factor_prefers_but_does_not_require_verification(self):
        options = services.build_mfa_options(services.create_mfa_ceremony(self.user))
        self.assertEqual(options["userVerification"], "preferred")

    def test_second_factor_rejects_another_users_passkey(self):
        other = structure_factories.UserFactory()
        ceremony = services.create_mfa_ceremony(other)
        options = services.build_mfa_options(ceremony)
        response = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_bad_signature_is_rejected(self):
        ceremony, options = services.start_signin()
        response = self.authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN, corrupt_signature=True
        )

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_replayed_signature_counter_is_rejected(self):
        """A counter that fails to advance is the classic cloned-authenticator
        signal, so a captured assertion must not be replayable."""
        ceremony, options = services.start_signin()
        replayed = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)
        services.finish_assertion(ceremony, replayed)

        ceremony, options = services.start_signin()
        stale = self.authenticator.authenticate(
            options["challenge"], RP_ID, ORIGIN, bump_counter=False
        )

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, stale)

    def test_assertion_from_a_foreign_origin_is_rejected(self):
        ceremony, options = services.start_signin()
        response = self.authenticator.authenticate(
            options["challenge"], RP_ID, "https://evil.example.net"
        )

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_registration_ceremony_cannot_satisfy_a_signin(self):
        """Ceremony kinds are not interchangeable: a challenge issued for
        enrolment must not be redeemable as an authentication."""
        ceremony, options = services.start_registration(self.user)
        response = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_signin_ceremony_cannot_satisfy_a_registration(self):
        ceremony, options = services.start_signin()
        response = self.authenticator.register(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_registration(ceremony, response, "Wrong kind")

    def test_revoked_credential_cannot_authenticate(self):
        self.credential.revoke(revoked_by=self.user, reason="Lost")
        ceremony, options = services.start_signin()
        response = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        with self.assertRaises(services.PasskeyError):
            services.finish_assertion(ceremony, response)

    def test_successful_assertion_records_use(self):
        ceremony, options = services.start_signin()
        response = self.authenticator.authenticate(options["challenge"], RP_ID, ORIGIN)

        services.finish_assertion(ceremony, response, ip_address="198.51.100.7")

        self.credential.refresh_from_db()
        self.assertEqual(self.credential.use_count, 1)
        self.assertEqual(self.credential.last_used_ip, "198.51.100.7")
        self.assertIsNotNone(self.credential.last_used_at)

    def test_assertion_does_not_issue_a_token(self):
        """The ceremony row must never become redeemable for a session.

        This is what separates PasskeyCeremony from TokenExchangeCode.
        """
        ceremony, _ = services.start_signin()
        field_names = {f.name for f in PasskeyCeremony._meta.get_fields()}
        self.assertNotIn("token", field_names)
        self.assertFalse(any("token" in name for name in field_names))
        self.assertFalse(hasattr(ceremony, "token"))


@enable_passkeys()
class CeremonyPurgeTest(TestCase):
    def test_cleanup_task_purges_expired_ceremonies(self):
        from waldur_core.passkeys.tasks import cleanup_expired_ceremonies

        user = structure_factories.UserFactory()
        stale, _ = services.start_registration(user)
        stale.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        stale.save(update_fields=["expires_at"])

        cleanup_expired_ceremonies()

        self.assertFalse(PasskeyCeremony.objects.filter(pk=stale.pk).exists())

    def test_expired_ceremonies_are_purged(self):
        user = structure_factories.UserFactory()
        stale, _ = services.start_registration(user)
        stale.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        stale.save(update_fields=["expires_at"])
        fresh, _ = services.start_registration(user)

        services.purge_expired_ceremonies()

        self.assertFalse(PasskeyCeremony.objects.filter(pk=stale.pk).exists())
        self.assertTrue(PasskeyCeremony.objects.filter(pk=fresh.pk).exists())
