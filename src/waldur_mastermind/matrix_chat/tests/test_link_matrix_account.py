import json
from io import StringIO
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.core.management import CommandError, call_command
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import matrix_client, models

PROFILE_URL = "https://matrix.example.com/_matrix/client/v3/profile/"
ADMIN_USERS_URL = "https://matrix.example.com/_synapse/admin/v2/users/"


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class LinkMatrixAccountTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(username="alice")

    def _link(self, *args):
        call_command("link_matrix_account", *args, stdout=StringIO())

    def _account(self, admin_status=200, admin=False):
        respx.get(f"{PROFILE_URL}%40alice%3Amatrix.example.com").mock(
            return_value=httpx.Response(200, json={"displayname": "Alice"})
        )
        respx.get(f"{ADMIN_USERS_URL}%40alice%3Amatrix.example.com").mock(
            return_value=httpx.Response(admin_status, json={"admin": admin})
        )

    @respx.mock
    def test_links_an_existing_account_deliberately(self):
        self._account()
        displayname = respx.put(
            f"{PROFILE_URL}%40alice%3Amatrix.example.com/displayname"
        ).mock(return_value=httpx.Response(200, json={}))

        self._link("alice", "@alice:matrix.example.com")

        profile = models.MatrixUserProfile.objects.get(user=self.user)
        self.assertEqual(profile.matrix_user_id, "@alice:matrix.example.com")
        self.assertTrue(profile.provisioned)
        # The owner's chosen name would otherwise show in every Waldur room.
        self.assertEqual(
            json.loads(displayname.calls.last.request.content),
            {"displayname": self.user.full_name or "alice"},
        )

    @respx.mock
    def test_refuses_a_homeserver_admin(self):
        self._account(admin=True)

        with self.assertRaisesRegex(CommandError, "homeserver admin"):
            self._link("alice", "@alice:matrix.example.com")

        self.assertFalse(
            models.MatrixUserProfile.objects.filter(user=self.user).exists()
        )

    @respx.mock
    def test_links_with_a_warning_when_the_homeserver_will_not_tell(self):
        # The admin API answers only once the bot is a homeserver admin.
        self._account(admin_status=403)
        err = StringIO()

        call_command(
            "link_matrix_account",
            "alice",
            "@alice:matrix.example.com",
            stdout=StringIO(),
            stderr=err,
        )

        self.assertTrue(
            models.MatrixUserProfile.objects.filter(user=self.user).exists()
        )
        self.assertIn("homeserver admin", err.getvalue())

    @respx.mock
    def test_refuses_an_account_the_homeserver_does_not_have(self):
        respx.get(f"{PROFILE_URL}%40ghost%3Amatrix.example.com").mock(
            return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        )

        with self.assertRaisesRegex(CommandError, "does not exist"):
            self._link("alice", "@ghost:matrix.example.com")

    def test_refuses_the_bot(self):
        with self.assertRaisesRegex(CommandError, "bot"):
            self._link("alice", "@waldur-bot:matrix.example.com")

    def test_refuses_another_homeservers_account(self):
        with self.assertRaisesRegex(CommandError, "matrix.example.com"):
            self._link("alice", "@alice:elsewhere.example")

    def test_refuses_a_user_already_linked(self):
        models.MatrixUserProfile.objects.create(
            user=self.user,
            matrix_user_id="@alice2:matrix.example.com",
            provisioned=True,
        )

        with self.assertRaisesRegex(CommandError, "already"):
            self._link("alice", "@alice:matrix.example.com")

    @respx.mock
    def test_a_link_that_lands_meanwhile_gives_a_clean_error(self):
        self._account()
        other = structure_factories.UserFactory(username="mallory")

        def provisioned_meanwhile(matrix_user_id):
            models.MatrixUserProfile.objects.create(
                user=other, matrix_user_id=matrix_user_id
            )
            return True

        with mock.patch.object(
            matrix_client, "account_exists", side_effect=provisioned_meanwhile
        ):
            with self.assertRaisesRegex(CommandError, "linked meanwhile"):
                self._link("alice", "@alice:matrix.example.com")

    def test_refuses_an_account_linked_to_someone_else(self):
        other = structure_factories.UserFactory(username="mallory")
        models.MatrixUserProfile.objects.create(
            user=other, matrix_user_id="@alice:matrix.example.com", provisioned=True
        )

        with self.assertRaisesRegex(CommandError, "mallory"):
            self._link("alice", "@alice:matrix.example.com")


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
    MATRIX_USER_ID_FORMAT="username",
)
class LinkAllMatrixAccountsTest(TestCase):
    # A Waldur database restored or reset against a homeserver that kept its
    # accounts: provisioning refuses every user, one link at a time is no fix.

    @respx.mock
    def test_links_every_user_whose_account_exists(self):
        alice = structure_factories.UserFactory(username="alice")
        structure_factories.UserFactory(username="nobody")
        linked = structure_factories.UserFactory(username="linked")
        models.MatrixUserProfile.objects.create(
            user=linked, matrix_user_id="@linked:matrix.example.com", provisioned=True
        )
        respx.get(url__startswith=PROFILE_URL).mock(
            side_effect=lambda request: httpx.Response(
                200 if "alice" in str(request.url) else 404, json={}
            )
        )
        respx.put(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(url__startswith=ADMIN_USERS_URL).mock(
            return_value=httpx.Response(200, json={"admin": False})
        )
        out = StringIO()

        call_command("link_matrix_account", "--all", stdout=out)

        self.assertEqual(
            models.MatrixUserProfile.objects.get(user=alice).matrix_user_id,
            "@alice:matrix.example.com",
        )
        self.assertFalse(
            models.MatrixUserProfile.objects.filter(user__username="nobody").exists()
        )
        self.assertIn("1 linked", out.getvalue())

    @respx.mock
    def test_skips_homeserver_admins(self):
        structure_factories.UserFactory(username="waldur-admin")
        bob = structure_factories.UserFactory(username="bob")
        respx.get(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(url__startswith=ADMIN_USERS_URL).mock(
            side_effect=lambda request: httpx.Response(
                200, json={"admin": "waldur-admin" in str(request.url)}
            )
        )
        out = StringIO()

        call_command("link_matrix_account", "--all", stdout=out, stderr=StringIO())

        self.assertFalse(
            models.MatrixUserProfile.objects.filter(
                user__username="waldur-admin"
            ).exists()
        )
        self.assertTrue(models.MatrixUserProfile.objects.filter(user=bob).exists())
        self.assertIn("1 homeserver admin(s) skipped", out.getvalue())

    def _accounts_exist(self, admin_status=200):
        respx.get(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.put(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(url__startswith=ADMIN_USERS_URL).mock(
            return_value=httpx.Response(admin_status, json={"admin": False})
        )

    def _link_all(self):
        out, err = StringIO(), StringIO()
        call_command("link_matrix_account", "--all", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @respx.mock
    def test_counts_an_id_another_user_holds(self):
        # Two users derive one ID; the holder keeps it.
        holder = structure_factories.UserFactory(username="holder")
        models.MatrixUserProfile.objects.create(
            user=holder, matrix_user_id="@carol:matrix.example.com", provisioned=True
        )
        carol = structure_factories.UserFactory(username="carol")
        self._accounts_exist()

        out, err = self._link_all()

        self.assertFalse(models.MatrixUserProfile.objects.filter(user=carol).exists())
        self.assertIn("1 already taken", out)
        self.assertIn("carol: @carol:matrix.example.com is already taken", err)

    @respx.mock
    def test_counts_a_user_whose_lookup_fails(self):
        structure_factories.UserFactory(username="dave")
        respx.get(url__startswith=PROFILE_URL).mock(
            return_value=httpx.Response(500, text="boom")
        )

        out, err = self._link_all()

        self.assertIn("1 failed", out)
        self.assertIn("dave: Could not look up @dave:matrix.example.com", err)

    @respx.mock
    def test_warns_when_the_homeserver_will_not_tell_who_is_an_admin(self):
        erin = structure_factories.UserFactory(username="erin")
        self._accounts_exist(admin_status=403)

        out, err = self._link_all()

        self.assertTrue(models.MatrixUserProfile.objects.filter(user=erin).exists())
        self.assertIn("Could not check for homeserver admin accounts", err)

    @respx.mock
    @override_config(MATRIX_USER_ID_FORMAT="email_local")
    def test_skips_an_id_several_unlinked_users_derive(self):
        # After a restore neither has a profile, and nothing says whose the
        # account is; the first by username must not get it.
        alice = structure_factories.UserFactory(username="alice", email="alice@b.org")
        zoe = structure_factories.UserFactory(username="zoe", email="alice@a.org")
        self._accounts_exist()

        out, err = self._link_all()

        self.assertFalse(
            models.MatrixUserProfile.objects.filter(user__in=[alice, zoe]).exists()
        )
        self.assertIn("2 sharing an ID", out)
        self.assertIn("@alice:matrix.example.com is derived by alice, zoe", err)

    def test_all_takes_no_user(self):
        with self.assertRaises(CommandError):
            call_command(
                "link_matrix_account", "alice", "@alice:matrix.example.com", "--all"
            )
