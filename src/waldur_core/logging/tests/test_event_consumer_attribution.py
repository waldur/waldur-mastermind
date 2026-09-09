"""How and by what right an EventConsumer was registered.

The credential kind (PAT / DRF token / session) is otherwise known only while
the request is in flight, inside resolve_consumer_rmq_password, and the
permission branch that authorized the registration is discarded by
ActionsPermission. Both are recorded on the consumer so an operator can tell a
queue running on a staff session from one on a scoped PAT.
"""

from unittest import mock

from constance.test import override_config
from rest_framework import status, test

from waldur_core.core.authentication import refresh_token
from waldur_core.core.tests.helpers import create_pat
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import ConsumerAuthorization, EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

URL = "/api/event-consumers/register/"


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
class StandaloneConsumerAttributionTest(test.APITestCase):
    def _register(self, body=None, expected=status.HTTP_201_CREATED):
        response = self.client.post(URL, body or {}, format="json")
        self.assertEqual(response.status_code, expected, response.content)
        return response

    def _authenticate_with_pat(self, user, name="agent token"):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        pat, plaintext = create_pat(user, name=name)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {plaintext}")
        return pat

    def _authenticate_with_drf_token(self, user):
        # UserFactory already provisions a token for the user, so get-or-create.
        token = refresh_token(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return token

    # ---- auth_kind ----

    def test_pat_registration_records_the_token(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        pat = self._authenticate_with_pat(staff, name="ci agent")

        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.auth_kind, "pat")
        self.assertEqual(consumer.auth_token_prefix, pat.token_prefix)
        self.assertEqual(consumer.auth_token_name, "ci agent")

    def test_drf_token_registration_records_token(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self._authenticate_with_drf_token(staff)

        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.auth_kind, "token")
        # Only a PAT is identified; a DRF token leaves the fields blank.
        self.assertEqual(consumer.auth_token_prefix, "")
        self.assertEqual(consumer.auth_token_name, "")

    def test_session_registration_records_session(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.auth_kind, "session")

    # ---- authorized_via ----

    def test_staff_branch_is_recorded(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.authorized_via, ConsumerAuthorization.STAFF)

    def test_support_branch_is_recorded(self, *mocks):
        support = structure_factories.UserFactory()
        support.is_support = True
        support.save()
        self.client.force_authenticate(support)

        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=support)
        self.assertEqual(consumer.authorized_via, ConsumerAuthorization.SUPPORT)

    def test_self_branch_is_recorded(self, *mocks):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)

        self._register({"scopes": [{"type": "user", "uuid": user.uuid.hex}]})

        consumer = logging_models.EventConsumer.objects.get(user=user)
        self.assertEqual(consumer.authorized_via, ConsumerAuthorization.SELF)

    def test_scope_role_branch_is_recorded(self, *mocks):
        fixture = structure_fixtures.ProjectFixture()
        manager = fixture.manager  # holds a role on the project
        self.client.force_authenticate(manager)

        self._register(
            {"scopes": [{"type": "project", "uuid": fixture.project.uuid.hex}]}
        )

        consumer = logging_models.EventConsumer.objects.get(user=manager)
        self.assertEqual(consumer.authorized_via, ConsumerAuthorization.SCOPE_ROLE)

    # ---- re-registration ----

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
    )
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    def test_reregistration_with_another_credential_updates_attribution(
        self, mock_get_user, mock_vhost_perms, *mocks
    ):
        """The fields describe the credential the queue runs on NOW, so the
        already-provisioned fast path must refresh them too."""
        staff = structure_factories.UserFactory(is_staff=True)
        self._authenticate_with_drf_token(staff)
        self._register()

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.auth_kind, "token")

        # Second call takes the fast path: RMQ user and vhost permission exist.
        mock_get_user.return_value = {"name": consumer.rmq_username}
        mock_vhost_perms.return_value = [consumer.rmq_username]
        pat = self._authenticate_with_pat(staff, name="rotated")
        self._register(expected=status.HTTP_200_OK)

        consumer.refresh_from_db()
        self.assertEqual(consumer.auth_kind, "pat")
        self.assertEqual(consumer.auth_token_prefix, pat.token_prefix)
        self.assertEqual(consumer.auth_token_name, "rotated")

    def test_failed_provisioning_records_no_attribution(
        self, mock_vhost, mock_user, mock_perms, mock_queue
    ):
        """The row must not claim a credential the queue never got: provisioning
        runs outside the transaction and 400s on any RMQ failure."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        mock_vhost.return_value = False

        self._register(expected=status.HTTP_400_BAD_REQUEST)

        # The consumer row is committed before provisioning is attempted.
        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertEqual(consumer.auth_kind, "")
        self.assertEqual(consumer.authorized_via, "")
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.EVENT_CONSUMER_REGISTERED_WITH_BROAD_CREDENTIAL
            ).exists()
        )

    # ---- audit ----

    def _broad_credential_events(self):
        return Event.objects.filter(
            event_type=EventType.EVENT_CONSUMER_REGISTERED_WITH_BROAD_CREDENTIAL
        )

    def test_non_pat_registration_is_audited(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        self._register()

        event = self._broad_credential_events().get()
        self.assertEqual(event.context["auth_kind"], "session")
        self.assertEqual(event.context["authorized_via"], ConsumerAuthorization.STAFF)

    def test_staff_pat_registration_is_audited(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self._authenticate_with_pat(staff)

        self._register()

        event = self._broad_credential_events().get()
        self.assertEqual(event.context["auth_kind"], "pat")

    def test_support_pat_registration_is_audited(self, *mocks):
        """A support user may register the same global PII firehose as staff, so
        that half of the gate must be audited too."""
        support = structure_factories.UserFactory()
        support.is_support = True
        support.save()
        self._authenticate_with_pat(support)

        self._register()

        event = self._broad_credential_events().get()
        self.assertEqual(event.context["authorized_via"], ConsumerAuthorization.SUPPORT)

    def test_scoped_pat_registration_is_not_audited(self, *mocks):
        fixture = structure_fixtures.ProjectFixture()
        manager = fixture.manager
        self._authenticate_with_pat(manager)

        self._register(
            {"scopes": [{"type": "project", "uuid": fixture.project.uuid.hex}]}
        )

        self.assertFalse(self._broad_credential_events().exists())

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
    )
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    def test_unchanged_attribution_is_not_audited_again(
        self, mock_get_user, mock_vhost_perms, *mocks
    ):
        """An agent re-registers on every restart; only a CHANGE is worth an
        audit event, otherwise the signal drowns in restarts."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        self._register()
        self.assertEqual(self._broad_credential_events().count(), 1)

        consumer = logging_models.EventConsumer.objects.get(user=staff)
        mock_get_user.return_value = {"name": consumer.rmq_username}
        mock_vhost_perms.return_value = [consumer.rmq_username]
        self._register(expected=status.HTTP_200_OK)

        self.assertEqual(self._broad_credential_events().count(), 1)
