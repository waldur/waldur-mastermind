"""Personal access tokens that predate enforcement.

A staff PAT scoped STAFF.ACCESS is a long-lived privileged credential minted
before the policy existed — the same problem as the session tokens the
rollout command deletes. Creating a new one now requires a passkey-verified
session, but the ones already issued keep working until they expire.

They are reported by default and revoked only on request, because a PAT
usually drives CI: deleting one without warning takes pipelines down rather
than merely forcing a re-login.
"""

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework import test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import PersonalAccessToken
from waldur_core.structure.tests import factories as structure_factories


def make_pat(user, name="ci", scopes=None):
    expires = timezone.now() + timezone.timedelta(days=365)
    _full, prefix, digest = PersonalAccessToken.generate_token(expires)
    return PersonalAccessToken.objects.create(
        user=user,
        name=name,
        token_prefix=prefix,
        token_hash=digest,
        scopes=scopes or ["STAFF.ACCESS"],
        expires_at=expires,
    )


def run(**kwargs):
    out = StringIO()
    call_command("revoke_unverified_staff_tokens", stdout=out, **kwargs)
    return out.getvalue()


class RolloutPersonalAccessTokenTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.ordinary = structure_factories.UserFactory()
        for user in (self.staff, self.support, self.ordinary):
            Token.objects.get_or_create(user=user)

    def test_privileged_pats_are_reported_by_default(self):
        pat = make_pat(self.staff, name="deploy-bot")

        output = run()

        self.assertIn("deploy-bot", output)
        self.assertIn("--revoke-personal-access-tokens", output)
        pat.refresh_from_db()
        self.assertTrue(pat.is_active)

    def test_reporting_names_the_scopes_so_the_risk_is_visible(self):
        make_pat(self.staff, scopes=["STAFF.ACCESS"])

        self.assertIn("STAFF.ACCESS", run())

    def test_support_pats_are_reported_too(self):
        make_pat(self.support, name="support-bot")

        self.assertIn("support-bot", run())

    def test_ordinary_users_pats_are_left_out(self):
        make_pat(self.ordinary, name="user-bot")

        self.assertNotIn("user-bot", run())

    def test_the_flag_revokes_them(self):
        pat = make_pat(self.staff)

        run(revoke_personal_access_tokens=True)

        pat.refresh_from_db()
        self.assertFalse(pat.is_active)

    def test_dry_run_reports_but_does_not_revoke_even_with_the_flag(self):
        pat = make_pat(self.staff)

        output = run(dry_run=True, revoke_personal_access_tokens=True)

        self.assertIn("Would revoke", output)
        pat.refresh_from_db()
        self.assertTrue(pat.is_active)

    def test_already_revoked_pats_are_not_reported(self):
        pat = make_pat(self.staff, name="old-bot")
        pat.is_active = False
        pat.save()

        self.assertNotIn("old-bot", run())

    def test_the_pat_section_runs_even_when_no_session_tokens_need_deleting(self):
        """The two halves are independent; a clean session state must not hide
        a fleet of pre-existing PATs."""
        Token.objects.all().delete()
        make_pat(self.staff, name="lonely-bot")

        self.assertIn("lonely-bot", run())

    def test_a_deployment_with_no_privileged_pats_says_so(self):
        self.assertIn("No active privileged personal access tokens", run())
