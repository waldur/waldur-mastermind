from django.test import TestCase
from django.utils import timezone

from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import (
    CEREMONY_MAX_ATTEMPTS,
    PasskeyCeremony,
)
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import RP_ID, enable_passkeys
from waldur_core.structure.tests import factories as structure_factories


@enable_passkeys()
class PasskeyCredentialTest(TestCase):
    def test_revoke_records_who_and_why(self):
        credential = PasskeyCredentialFactory()
        actor = structure_factories.UserFactory()

        credential.revoke(revoked_by=actor, reason="Compromised")

        self.assertFalse(credential.is_active)
        self.assertEqual(credential.revoked_by, actor)
        self.assertEqual(credential.revocation_reason, "Compromised")
        self.assertIsNotNone(credential.revoked_at)

    def test_register_use_increments_the_counter(self):
        credential = PasskeyCredentialFactory()

        credential.register_use(sign_count=7, ip_address="203.0.113.4")
        credential.refresh_from_db()

        self.assertEqual(credential.sign_count, 7)
        self.assertEqual(credential.use_count, 1)
        self.assertEqual(credential.last_used_ip, "203.0.113.4")

    def test_credential_matching_the_rp_id_is_not_orphaned(self):
        self.assertFalse(PasskeyCredentialFactory(rp_id=RP_ID).is_orphaned)

    def test_credential_from_a_previous_rp_id_is_orphaned(self):
        self.assertTrue(PasskeyCredentialFactory(rp_id="old.example.com").is_orphaned)


class PasskeyCeremonyTest(TestCase):
    def test_challenges_are_unique_per_ceremony(self):
        challenges = {
            PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID).challenge
            for _ in range(25)
        }
        self.assertEqual(len(challenges), 25)

    def test_a_fresh_ceremony_is_usable(self):
        ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID)
        self.assertTrue(ceremony.is_usable)

    def test_signin_ceremony_has_no_user(self):
        """Usernameless sign-in must not know who is authenticating up front."""
        ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID)
        self.assertIsNone(ceremony.user_id)

    def test_consumed_ceremony_is_not_usable(self):
        ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID)
        ceremony.consume()
        self.assertFalse(ceremony.is_usable)

    def test_expired_ceremony_is_not_usable(self):
        ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID)
        ceremony.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.assertFalse(ceremony.is_usable)

    def test_exhausted_ceremony_is_not_usable(self):
        ceremony = PasskeyCeremony.start(kind=CeremonyKind.SIGNIN, rp_id=RP_ID)
        ceremony.attempts = CEREMONY_MAX_ATTEMPTS
        self.assertFalse(ceremony.is_usable)

    def test_ceremony_holds_no_token(self):
        """Unlike TokenExchangeCode, this row must never be redeemable."""
        field_names = {f.name for f in PasskeyCeremony._meta.get_fields()}
        self.assertFalse(any("token" in name for name in field_names))
