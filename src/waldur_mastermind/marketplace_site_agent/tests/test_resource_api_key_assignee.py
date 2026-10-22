"""A resource API key changing hands asks the agent to sync that resource's users."""

import json
from unittest import mock

from django.db import transaction
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core import encryption
from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_site_agent.tests import fixtures

States = models.ResourceApiKey.States
Actions = models.ResourceApiKey.Actions
PUBLISH = "waldur_core.logging.tasks.publish_messages.delay"


class ApiKeyAssigneeResyncTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceSiteAgentFixture()
        self.resource = self.fixture.resource
        self.offering = self.resource.offering
        self.offering.plugin_options = {"enable_api_key_provisioning": True}
        self.offering.save()
        # Made now: granting them a role publishes a user_role message of its own.
        self.owner = self.fixture.owner
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_API_KEY)
        self.key = models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id="cid-1",
            key_ciphertext=encryption.encrypt_value("sk-cid-1"),
            state=States.OK,
        )
        subscription = logging_factories.EventSubscriptionFactory(
            user=structure_factories.UserFactory(is_staff=True),
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.USER_ROLE.value}
            ],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.USER_ROLE.value,
        )

    def request(self, data):
        self.client.force_authenticate(self.owner)
        return self.client.patch(
            reverse(
                "marketplace-resource-api-key-detail",
                kwargs={"uuid": self.key.uuid.hex},
            ),
            data,
            format="json",
        )

    def patch(self, data):
        """PATCH the key; return the response and the user-role payloads published.

        The suite's conftest runs on_commit callbacks at once, so what waits for
        the commit is published here too.
        """
        with mock.patch(PUBLISH) as publish:
            response = self.request(data)
        return response, self.user_role_payloads(publish)

    @staticmethod
    def user_role_payloads(publish):
        return [
            json.loads(message["payload"])
            for call in publish.call_args_list
            for message in call.args[0]
            if message["topic"].endswith("/user_role")
        ]

    def assert_resource_sync(self, payloads):
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["resource_uuid"], self.resource.uuid.hex)
        self.assertEqual(payloads[0]["project_uuid"], self.resource.project.uuid.hex)
        # A role change carries user_uuid; this is the resource-wide resync.
        self.assertNotIn("user_uuid", payloads[0])

    def test_assigning_a_key_syncs_its_resource(self):
        response, payloads = self.patch({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_resource_sync(payloads)

    def test_reassigning_a_key_syncs_its_resource(self):
        self.key.user = self.manager
        self.key.save()
        response, payloads = self.patch({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_resource_sync(payloads)

    def test_unassigning_a_key_syncs_its_resource(self):
        self.key.user = self.admin
        self.key.save()
        response, payloads = self.patch({"user": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_resource_sync(payloads)

    def test_a_failed_announcement_leaves_the_edit_saved(self):
        # The edit has committed by then: a broker failure is logged, not a 500.
        with (
            mock.patch(
                "waldur_mastermind.marketplace_site_agent.utils."
                "push_resource_user_role_sync_message",
                side_effect=ConnectionError("broker down"),
            ),
            self.assertLogs(
                "waldur_mastermind.marketplace.resource_api_keys", "ERROR"
            ) as logs,
        ):
            response = self.request({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.user, self.admin)
        self.assertIn(self.key.uuid.hex, logs.output[0])
        self.assertIn("broker down", logs.output[0])

    def test_unassigning_without_key_management_syncs(self):
        # Unassigning needs no governance: it is how an assignee set while it was
        # on gets lifted after it is turned off.
        self.key.user = self.admin
        self.key.save()
        self.offering.plugin_options = {}
        self.offering.save()
        response, payloads = self.patch({"user": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_resource_sync(payloads)

    def test_a_key_changing_hands_is_rotated_and_still_syncs(self):
        response, payloads = self.patch(
            {"user": self.admin.uuid.hex, "allowed_models": ["*"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.ROTATE)
        self.assert_resource_sync(payloads)

    def test_a_paused_key_cannot_change_hands_and_sends_nothing(self):
        self.key.state = States.PAUSED
        self.key.save()
        response, payloads = self.patch({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user", response.data)
        self.key.refresh_from_db()
        self.assertIsNone(self.key.user)
        self.assertEqual(payloads, [])

    def test_the_same_assignee_sends_nothing(self):
        self.key.user = self.admin
        self.key.save()
        response, payloads = self.patch({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payloads, [])

    def test_a_settings_change_alone_sends_no_sync(self):
        response, payloads = self.patch({"allowed_models": ["*"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payloads, [])

    def test_an_assignee_outside_the_project_is_refused_and_sends_nothing(self):
        response, payloads = self.patch(
            {"user": structure_factories.UserFactory().uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(payloads, [])

    def test_an_edit_refused_after_validation_sends_nothing(self):
        # A key whose failed command was a pause takes no new assignee or
        # settings; only retrying that pause clears it.
        self.key.state = States.ERRED
        self.key.pending_action = Actions.PAUSE
        self.key.save()
        response, payloads = self.patch(
            {"user": self.admin.uuid.hex, "allowed_models": ["*"]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.key.refresh_from_db()
        self.assertIsNone(self.key.user)
        self.assertEqual(payloads, [])

    def test_nothing_is_sent_before_the_change_commits(self):
        deferred = []
        with (
            mock.patch(PUBLISH) as publish,
            mock.patch.object(
                transaction,
                "on_commit",
                lambda func, using=None, robust=False: deferred.append(func),
            ),
        ):
            response = self.request({"user": self.admin.uuid.hex})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(self.user_role_payloads(publish), [])
            for func in deferred:
                func()
            self.assert_resource_sync(self.user_role_payloads(publish))

    def test_an_offering_without_a_site_agent_sends_nothing(self):
        self.offering.type = "Marketplace.Basic"
        self.offering.save()
        response, payloads = self.patch({"user": self.admin.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(payloads, [])
