"""A member who leaves and comes back gets the same account back.

The provider parks the directory entry on departure (same uid, same username)
and re-enables it when Waldur presents the account live again. Waldur's half is
to bring the record -- and the provider account it reads through -- back under
the same name, and to refuse a deletion acknowledgement that arrives for an
account that has already come back.
"""

from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    AccountScopes,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class RegrantFixture(test.APITestCase):
    """Provider-scoped, anonymized offering with a pool: the derived-name case."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_options["account_scope"] = AccountScopes.PROVIDER
        self.provider.account_options["username_generation_policy"] = "anonymized"
        self.provider.account_options["username_anonymized_prefix"] = "hpc_"
        self.provider.save()
        factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_uid=9000,
            max_uid=9999,
            next_uid=9001,
            min_gid=9000,
            max_gid=9999,
            next_gid=9001,
        )
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "offering_user_auto_deletion": True,
        }
        self.offering.save()
        self.project = self.fixture.project
        factories.ResourceFactory(
            offering=self.offering, project=self.project, state=ResourceStates.OK
        )
        self.user = structure_factories.UserFactory()

    def _grant(self):
        self.project.add_user(self.user, ProjectRole.MEMBER)
        tasks.create_or_restore_offering_users_for_user(
            self.user.uuid.hex, self.project.uuid.hex
        )

    def _revoke(self):
        self.project.remove_user(self.user)
        tasks.request_offering_user_deletion_for_user(self.user.uuid.hex)

    def _offering_user(self):
        return models.OfferingUser.objects.get(user=self.user, offering=self.offering)

    def _account(self):
        return models.ServiceProviderAccount.objects.get(
            user=self.user, service_provider=self.provider
        )

    def _agent_deletes(self, offering_user):
        offering_user.set_deleting()
        offering_user.save(update_fields=["state"])
        offering_user.set_deleted()
        offering_user.save(update_fields=["state"])
        tasks.request_provider_account_deletion_for_user(self.user)


class RestoreOnRegrantTest(RegrantFixture):
    def test_leave_and_return_gives_the_same_account_back(self):
        self._grant()
        offering_user = self._offering_user()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        username, uid = (
            offering_user.username,
            offering_user.backend_metadata["uidnumber"],
        )
        self.assertEqual(username, f"hpc_{uid}")

        self._revoke()
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)
        self._agent_deletes(offering_user)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)
        self.assertEqual(self._account().state, OfferingUserStates.DELETED)

        self._grant()

        offering_user.refresh_from_db()
        account = self._account()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(account.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, username)
        self.assertEqual(account.username, username)
        self.assertEqual(offering_user.backend_metadata["uidnumber"], uid)
        self.assertEqual(account.backend_metadata["uidnumber"], uid)
        self.assertEqual(
            models.ServiceProviderAccount.objects.filter(user=self.user).count(), 1
        )
        self.assertEqual(
            models.PosixIdentity.objects.filter(
                user=self.user, released_at__isnull=True
            ).count(),
            1,
        )

    def test_provider_account_in_requested_deletion_comes_back_too(self):
        self._grant()
        self._revoke()
        offering_user = self._offering_user()
        account = self._account()
        self.assertEqual(account.state, OfferingUserStates.DELETION_REQUESTED)

        self._grant()

        offering_user.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(account.state, OfferingUserStates.OK)

    def test_restore_is_a_no_op_for_a_live_account(self):
        self._grant()
        offering_user = self._offering_user()
        self.assertFalse(utils.restore_offering_user(offering_user))


class ProviderAccountCompletionTest(RegrantFixture):
    """The provider account is a projection: it completes when its last reader does."""

    def test_completes_when_the_last_offering_user_is_deleted(self):
        self._grant()
        self._revoke()
        offering_user = self._offering_user()
        self.assertEqual(self._account().state, OfferingUserStates.DELETION_REQUESTED)

        self._agent_deletes(offering_user)

        self.assertEqual(self._account().state, OfferingUserStates.DELETED)

    def test_waits_while_a_sibling_offering_user_is_still_being_deleted(self):
        sibling = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, customer=self.provider.customer
        )
        self._grant()
        offering_user = self._offering_user()
        other, _ = utils.create_offering_user(self.user, sibling)
        self.assertEqual(other.service_provider_account, self._account())

        for row in (offering_user, other):
            row.request_deletion()
            row.save(update_fields=["state"])
        self._agent_deletes(offering_user)

        # The sibling's deletion is still pending, so the account only has its
        # deletion requested; the sibling's acknowledgement completes it.
        self.assertEqual(self._account().state, OfferingUserStates.DELETION_REQUESTED)
        self._agent_deletes(other)
        self.assertEqual(self._account().state, OfferingUserStates.DELETED)

    def test_the_uid_is_kept_for_the_parked_entry(self):
        self._grant()
        self._revoke()
        self._agent_deletes(self._offering_user())
        self.assertEqual(
            models.PosixIdentity.objects.filter(
                user=self.user, released_at__isnull=True
            ).count(),
            1,
        )


class DeletionAckAfterRestoreTest(RegrantFixture):
    """The provider's acknowledgement races the member's return."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

    def _post(self, offering_user, action):
        return self.client.post(
            factories.OfferingUserFactory.get_url(
                offering_user, action.replace("_", "-")
            )
        )

    def test_set_deleting_after_the_account_came_back_is_a_conflict(self):
        self._grant()
        self._revoke()
        offering_user = self._offering_user()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

        self._grant()

        response = self._post(offering_user, "set_deleting")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(self._account().state, OfferingUserStates.OK)

    def test_set_deleted_after_the_account_came_back_is_a_conflict(self):
        self._grant()
        self._revoke()
        offering_user = self._offering_user()
        response = self._post(offering_user, "set_deleting")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self._grant()

        for action in ("set_deleted", "set_error_deleting"):
            response = self._post(offering_user, action)
            self.assertEqual(
                response.status_code, status.HTTP_409_CONFLICT, response.data
            )
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(self._account().state, OfferingUserStates.OK)

    def test_a_deletion_that_still_stands_is_acknowledged_normally(self):
        self._grant()
        self._revoke()
        offering_user = self._offering_user()
        self.assertEqual(
            self._post(offering_user, "set_deleting").status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self._post(offering_user, "set_deleted").status_code, status.HTTP_200_OK
        )
        self.assertEqual(self._account().state, OfferingUserStates.DELETED)
