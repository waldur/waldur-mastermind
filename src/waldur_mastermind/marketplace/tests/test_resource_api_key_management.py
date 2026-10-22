"""Per-key governance of resource API keys: request, assign, limit, pause, resume,
delete — each a command the site agent carries out and acknowledges."""

import datetime
from importlib import import_module
from unittest import mock

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied
from rest_framework.reverse import reverse

from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.core.tests.helpers import create_pat
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import (
    models,
    resource_api_keys,
    serializers,
    tasks,
    utils,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture

States = models.ResourceApiKey.States
Actions = models.ResourceApiKey.Actions
PUBLISH = "waldur_mastermind.marketplace.utils.logging_tasks"
PREPARE = "waldur_mastermind.marketplace.utils.prepare_messages"
MESSAGES = [{"vhost": "v", "topic": "t", "payload": "{}"}]

THIS_MONTH = models.ResourceApiKey.current_period()
LAST_MONTH = (THIS_MONTH - datetime.timedelta(days=1)).replace(day=1)
NEXT_MONTH = (THIS_MONTH + datetime.timedelta(days=31)).replace(day=1)

migration = import_module(
    "waldur_mastermind.marketplace.migrations.0302_resource_api_key_backfill"
)
all_models_migration = import_module(
    "waldur_mastermind.marketplace.migrations.0308_resource_api_key_all_models"
)
ALL_MODELS = [models.ResourceApiKey.ALL_MODELS]


def list_url(action=None):
    name = "marketplace-resource-api-key-list"
    if action:
        name = f"marketplace-resource-api-key-{action}"
    return reverse(name)


def detail_url(api_key, action=None):
    name = "marketplace-resource-api-key-detail"
    if action:
        name = f"marketplace-resource-api-key-{action}"
    return reverse(name, kwargs={"uuid": api_key.uuid.hex})


class ApiKeyManagementTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.resource.offering
        self.offering.plugin_options = {"enable_api_key_provisioning": True}
        self.offering.save()
        factories.OfferingComponentFactory(offering=self.offering, type="tokens")
        CustomerRole.OWNER.add_permission(
            PermissionEnum.MANAGE_RESOURCE_CONSUMER_API_KEY
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_API_KEY)
        self.key = self.make_key("cid-1")

    def make_key(self, client_id, state=States.OK, **kwargs):
        return models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id=client_id,
            key_ciphertext=encryption.encrypt_value(f"sk-{client_id}"),
            state=state,
            **kwargs,
        )

    def post(self, user, url, data=None):
        self.client.force_authenticate(user)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.post(url, data, format="json")
        return response, prepare

    def published_payload(self, prepare):
        prepare.assert_called_once()
        return prepare.call_args.args[1]


class RequestKeyTest(ApiKeyManagementTestBase):
    def test_request_creates_a_row_and_publishes_a_command_without_key_material(self):
        response, prepare = self.post(
            self.fixture.owner,
            list_url(),
            {
                "resource": self.resource.uuid.hex,
                "user": self.fixture.admin.uuid.hex,
                "limits": {"tokens": 1000},
                "allowed_models": ["llama-3"],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        key = models.ResourceApiKey.objects.get(uuid=response.data["uuid"])
        self.assertEqual(key.state, States.CREATING)
        self.assertEqual(key.pending_action, Actions.CREATE)
        self.assertEqual(key.key_ciphertext, "")
        self.assertEqual(key.client_id, "")
        self.assertEqual(key.user, self.fixture.admin)
        self.assertEqual(response.data["user_uuid"], self.fixture.admin.uuid)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.CREATE)
        self.assertEqual(payload["api_key_uuid"], key.uuid.hex)
        self.assertEqual(payload["limits"], {"tokens": 1000})
        self.assertEqual(payload["allowed_models"], ["llama-3"])
        self.assertNotIn("api_key", payload)

    def test_settings_are_optional(self):
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "limits": {}, "allowed_models": []},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        key = models.ResourceApiKey.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(key.user)
        self.assertIsNone(key.spending_limits)
        self.assertEqual(key.allowed_models, [])

    def test_several_requests_can_wait_for_a_client_id_at_once(self):
        for _ in range(2):
            response, _ = self.post(
                self.fixture.owner, list_url(), {"resource": self.resource.uuid.hex}
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.resource.api_keys.filter(client_id="").count(), 2)

    def test_member_without_permission_cannot_request(self):
        response, prepare = self.post(
            self.fixture.member, list_url(), {"resource": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_an_outsider_learns_nothing_from_validation(self):
        # Validation errors on the assignee or limits would reveal who belongs to
        # the resource and which components it has; the permission comes first.
        outsider = self.fixture.user
        for body in (
            {"user": self.fixture.staff.uuid.hex},
            {"user": self.fixture.admin.uuid.hex},
            {"limits": {"gpu": 1}},
        ):
            with self.subTest(body=body):
                response, prepare = self.post(
                    outsider, list_url(), {"resource": self.resource.uuid.hex, **body}
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()

    def test_assignee_must_belong_to_the_resource(self):
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "user": self.fixture.user.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user", response.data)

    def test_an_organization_member_outside_the_project_cannot_be_assigned(self):
        response, prepare = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "user": self.fixture.owner.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user", response.data)
        prepare.assert_not_called()

    def request_for(self, user):
        return self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "user": user.uuid.hex},
        )

    def test_a_person_holds_one_key_per_resource(self):
        self.key.user = self.fixture.admin
        self.key.save()
        response, prepare = self.request_for(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cid-1", str(response.data["user"]))
        prepare.assert_not_called()
        self.assertEqual(self.resource.api_keys.count(), 1)

    def test_a_requested_key_holds_its_assignee_too(self):
        self.make_key("", state=States.CREATING, user=self.fixture.admin)
        response, prepare = self.request_for(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()

    def test_a_key_being_deleted_still_holds_its_assignee(self):
        # Its delete may yet fail and leave it serving.
        self.key.user = self.fixture.admin
        self.key.state = States.DELETING
        self.key.save()
        response, _ = self.request_for(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_deleted_key_frees_its_assignee(self):
        self.key.user = self.fixture.admin
        self.key.state = States.DELETED
        self.key.save()
        response, _ = self.request_for(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_key_on_another_resource_does_not_count(self):
        other = factories.ResourceFactory(
            project=self.fixture.project, offering=self.offering
        )
        models.ResourceApiKey.objects.create(
            resource=other, client_id="cid-9", state=States.OK, user=self.fixture.admin
        )
        response, _ = self.request_for(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_limits_must_name_offering_components(self):
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "limits": {"gpu": 5}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)

    def test_models_must_be_offered_when_the_offering_lists_them(self):
        self.offering.resource_options = {
            "options": {"models": {"choices": ["llama-3", "mistral"]}}
        }
        self.offering.save()
        response, _ = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "allowed_models": ["gpt-9"]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_models", response.data)

    def test_a_request_body_must_be_an_object(self):
        response, _ = self.post(self.fixture.owner, list_url(), [])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_finite_limits_are_rejected(self):
        # jsonb cannot store NaN or Infinity; they must be a 400, not a 500.
        for value in ("NaN", "Infinity"):
            with self.subTest(value):
                response, _ = self.post(
                    self.fixture.owner,
                    list_url(),
                    {"resource": self.resource.uuid.hex, "limits": {"tokens": value}},
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejected_for_a_dead_resource(self):
        self.resource.state = self.resource.States.TERMINATING
        self.resource.save()
        response, prepare = self.post(
            self.fixture.owner, list_url(), {"resource": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        prepare.assert_not_called()

    def test_agent_reports_the_requested_key_with_its_client_id(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(requested, "set-key"),
            {"api_key": "sk-new", "client_id": "cid-9"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.OK)
        self.assertEqual(requested.client_id, "cid-9")
        self.assertEqual(requested.pending_action, "")

    def test_a_requested_key_needs_its_client_id(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(requested, "set-key"),
            {"api_key": "sk-new"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_failed_request_can_be_requested_again(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource,
            state=States.ERRED,
            pending_action=Actions.CREATE,
            spending_limits={"tokens": {"month": 5}},
        )
        response, prepare = self.post(
            self.fixture.owner, detail_url(requested, "retry")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.CREATING)
        self.assertEqual(requested.pending_action, Actions.CREATE)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.CREATE)
        self.assertEqual(payload["limits"], {"tokens": 5})

    def test_a_failed_request_cannot_be_rotated(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, state=States.ERRED, pending_action=Actions.CREATE
        )
        response, prepare = self.post(
            self.fixture.owner, detail_url(requested, "rotate")
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        prepare.assert_not_called()


class CancelRequestTest(ApiKeyManagementTestBase):
    """A requested key with no client_id holds nothing at the backend: deleting
    it needs no command, even while its creation is still pending."""

    def delete(self, key):
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.delete(detail_url(key))
        return response, prepare

    def test_a_pending_request_is_deleted_at_once(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        response, prepare = self.delete(requested)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        prepare.assert_not_called()
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.DELETED)
        self.assertEqual(requested.pending_action, "")

    def test_a_failed_request_is_deleted_at_once(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, state=States.ERRED, pending_action=Actions.CREATE
        )
        response, prepare = self.delete(requested)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        prepare.assert_not_called()
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.DELETED)

    def test_a_creation_reported_after_it_is_refused(self):
        # The agent withdraws a key Waldur refuses, so nothing is left live.
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        self.delete(requested)
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(requested, "set-key"),
            {"api_key": "sk-late", "client_id": "cid-late"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.DELETED)
        self.assertEqual(requested.key_ciphertext, "")

    def test_a_created_key_still_waits_for_the_agent(self):
        response, prepare = self.delete(self.key)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.DELETE)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.DELETING)

    def test_it_is_audited_as_done(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        # Lazily created fixture users emit their own events first.
        self.fixture.owner
        with mock.patch("waldur_mastermind.marketplace.log.event_logger.emit") as emit:
            self.delete(requested)
        emit.assert_called_once()
        message = emit.call_args.args[0]
        self.assertIn("has been deleted by", message)
        self.assertEqual(
            emit.call_args.kwargs["event_type"],
            EventType.MARKETPLACE_RESOURCE_API_KEY_DELETED,
        )

    def test_member_without_permission_cannot_cancel(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        self.client.force_authenticate(self.fixture.member)
        response = self.client.delete(detail_url(requested))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        requested.refresh_from_db()
        self.assertEqual(requested.state, States.CREATING)


class KeySettingsTest(ApiKeyManagementTestBase):
    def patch(self, user, key, data):
        self.client.force_authenticate(user)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(detail_url(key), data, format="json")
        return response, prepare

    def test_an_assignee_cannot_be_edited(self):
        # A key changes hands by requesting one for the new holder and
        # deleting the old one, so the edit is refused, not dropped.
        self.key.user = self.fixture.admin
        self.key.save()
        for user in (
            self.fixture.manager.uuid.hex,
            self.fixture.admin.uuid.hex,
            None,
        ):
            with self.subTest(user=user):
                with mock.patch(
                    "waldur_mastermind.marketplace.resource_api_keys.log."
                    "log_resource_api_key_command"
                ) as log_command:
                    response, prepare = self.patch(
                        self.fixture.owner,
                        self.key,
                        {"user": user, "limits": {"tokens": 50}},
                    )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(
                    "Request a key for the new holder", str(response.data["user"])
                )
                prepare.assert_not_called()
                log_command.assert_not_called()
                self.key.refresh_from_db()
                self.assertEqual(self.key.user, self.fixture.admin)
                self.assertIsNone(self.key.spending_limits)
                self.assertEqual(self.key.state, States.OK)
                self.assertEqual(self.key.pending_action, "")

    def test_an_edit_offers_no_assignee(self):
        self.assertNotIn("user", serializers.ResourceApiKeyUpdateSerializer().fields)
        self.assertIn("user", serializers.ResourceApiKeyCreateSerializer().fields)

    def test_an_edit_of_limits_is_an_update(self):
        response, prepare = self.patch(
            self.fixture.owner, self.key, {"limits": {"tokens": 50}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.UPDATE)
        self.assertEqual(payload["limits"], {"tokens": 50})
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.UPDATE)
        self.assertEqual(self.key.spending_limits, {"tokens": {"month": 50}})

    def test_agent_acknowledges_an_update(self):
        self.key.set_updating(Actions.UPDATE)
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-ok")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)
        self.assertEqual(self.key.pending_action, "")

    def test_a_paused_key_takes_its_settings_on_resume(self):
        self.key.state = States.PAUSED
        self.key.save()
        response, prepare = self.patch(
            self.fixture.owner, self.key, {"allowed_models": ["mistral"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)
        self.assertEqual(self.key.allowed_models, ["mistral"])
        prepare.assert_not_called()

        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["mistral"])

    def test_non_finite_limits_are_rejected(self):
        response, _ = self.patch(
            self.fixture.owner, self.key, {"limits": {"tokens": "NaN"}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_editing_a_paused_key_is_audited_but_leaves_modified_alone(self):
        # The agent's sweep reads modified as "in this state since"; an edit
        # that sends no command must not make a key look freshly paused.
        self.key.state = States.PAUSED
        self.key.save()
        before = self.key.modified
        with mock.patch(
            "waldur_mastermind.marketplace.resource_api_keys.log."
            "log_resource_api_key_command"
        ) as log_command:
            response, prepare = self.patch(
                self.fixture.owner, self.key, {"limits": {"tokens": 50}}
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.spending_limits, {"tokens": {"month": 50}})
        self.assertEqual(self.key.state, States.PAUSED)
        self.assertEqual(self.key.modified, before)
        log_command.assert_called_once_with(
            mock.ANY, Actions.UPDATE, self.fixture.owner, applied=True
        )

    def test_unchanged_settings_are_not_a_command(self):
        self.key.spending_limits = {"tokens": {"month": 50}}
        self.key.save()
        response, prepare = self.patch(
            self.fixture.owner, self.key, {"limits": {"tokens": 50}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()

    def test_member_without_permission_cannot_edit(self):
        response, _ = self.patch(
            self.fixture.member, self.key, {"limits": {"tokens": 50}}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_deleted_key_cannot_be_edited(self):
        self.key.state = States.DELETED
        self.key.save()
        response, _ = self.patch(
            self.fixture.owner, self.key, {"limits": {"tokens": 50}}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_put_is_not_offered(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.put(detail_url(self.key), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class ModelListTest(ApiKeyManagementTestBase):
    """A key's model list is never null: ``["*"]`` is every model, ``[]`` none."""

    def setUp(self):
        super().setUp()
        self.offering.resource_options = {
            "options": {"models": {"choices": ["llama-3", "mistral"]}}
        }
        self.offering.save()

    def request(self, **body):
        return self.post(
            self.fixture.owner, list_url(), {"resource": self.resource.uuid.hex, **body}
        )

    def patch(self, key, data):
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(detail_url(key), data, format="json")
        return response, prepare

    def test_each_form_is_stored_returned_and_sent_as_given(self):
        for allowed_models in (ALL_MODELS, [], ["llama-3", "mistral"]):
            with self.subTest(allowed_models):
                response, prepare = self.request(allowed_models=allowed_models)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(response.data["allowed_models"], allowed_models)
                key = models.ResourceApiKey.objects.get(uuid=response.data["uuid"])
                self.assertEqual(key.allowed_models, allowed_models)
                self.assertEqual(
                    self.published_payload(prepare)["allowed_models"], allowed_models
                )

    def test_a_request_without_a_model_list_gets_no_models(self):
        response, prepare = self.request()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["allowed_models"], [])
        key = models.ResourceApiKey.objects.get(uuid=response.data["uuid"])
        self.assertEqual(key.allowed_models, [])
        self.assertEqual(self.published_payload(prepare)["allowed_models"], [])

    def test_every_model_cannot_be_combined_with_a_model(self):
        for allowed_models in (["*", "llama-3"], ["llama-3", "*"], ["*", "*"]):
            with self.subTest(allowed_models):
                response, prepare = self.request(allowed_models=allowed_models)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("allowed_models", response.data)
                prepare.assert_not_called()
                response, prepare = self.patch(
                    self.key, {"allowed_models": allowed_models}
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                prepare.assert_not_called()

    def test_null_is_refused(self):
        response, prepare = self.request(allowed_models=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_models", response.data)
        prepare.assert_not_called()

        response, prepare = self.patch(self.key, {"allowed_models": None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_models", response.data)
        prepare.assert_not_called()

    def test_an_edit_without_a_model_list_keeps_it(self):
        self.key.allowed_models = ["mistral"]
        self.key.save()
        response, prepare = self.patch(self.key, {"limits": {"tokens": 50}})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["allowed_models"], ["mistral"])
        self.key.refresh_from_db()
        self.assertEqual(self.key.allowed_models, ["mistral"])
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["mistral"])

    def test_an_edit_can_take_every_model_away_and_give_them_back(self):
        self.key.allowed_models = ALL_MODELS
        self.key.save()
        for allowed_models in ([], ALL_MODELS):
            with self.subTest(allowed_models):
                response, prepare = self.patch(
                    self.key, {"allowed_models": allowed_models}
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["allowed_models"], allowed_models)
                self.assertEqual(
                    self.published_payload(prepare)["allowed_models"], allowed_models
                )
                self.key.refresh_from_db()
                self.assertEqual(self.key.allowed_models, allowed_models)
                self.key.set_ok()
                self.key.save()

    def test_a_key_reported_without_a_request_may_call_every_model(self):
        response, _ = self.post(
            self.fixture.offering_owner,
            list_url("report-created"),
            {
                "resource": self.resource.uuid.hex,
                "client_id": "cid-9",
                "api_key": "sk-new",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["allowed_models"], ALL_MODELS)
        key = self.resource.api_keys.get(client_id="cid-9")
        self.assertEqual(key.allowed_models, ALL_MODELS)

    def test_a_report_for_a_key_already_held_leaves_its_models_alone(self):
        self.key.allowed_models = ["mistral"]
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner,
            list_url("report-created"),
            {
                "resource": self.resource.uuid.hex,
                "client_id": self.key.client_id,
                "api_key": "sk-new",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.allowed_models, ["mistral"])


class AssigneeRevealTest(ApiKeyManagementTestBase):
    def setUp(self):
        super().setUp()
        self.key.user = self.fixture.admin
        self.key.save()

    def reveal(self, user):
        self.client.force_authenticate(user)
        return self.client.get(detail_url(self.key, "reveal"))

    def test_assignee_can_reveal(self):
        response = self.reveal(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["api_key"], "sk-cid-1")

    def test_other_members_cannot_reveal_an_assigned_key(self):
        for user in (self.fixture.owner, self.fixture.manager):
            with self.subTest(user=user):
                response = self.reveal(user)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_and_support_can_reveal(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            with self.subTest(user=user):
                response = self.reveal(user)
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_an_assignee_who_left_the_project_cannot_reveal(self):
        self.fixture.project.remove_user(self.fixture.admin)
        response = self.reveal(self.fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AssigneeRotationTest(ApiKeyManagementTestBase):
    """The assignee is trusted with the key's value, so with replacing it — and
    with nothing else a manager does to the key."""

    def setUp(self):
        super().setUp()
        # A project member: the role does not manage the resource's API keys.
        self.assignee = self.fixture.member
        self.key.user = self.assignee
        self.key.save()

    def fail_command(self, start, **fields):
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(**fields)
        self.key.refresh_from_db()
        start(self.key)
        self.key.set_erred()
        self.key.save()

    def test_assignee_can_rotate(self):
        response, prepare = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.ROTATE)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.ROTATE)

    def test_assignee_can_retry_a_failed_rotation(self):
        self.fail_command(lambda key: key.set_updating())
        response, prepare = self.post(self.assignee, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.ROTATE)

    def test_assignee_cannot_retry_any_other_command(self):
        cases = (
            (States.OK, lambda key: key.set_pausing(), Actions.PAUSE),
            (States.PAUSED, lambda key: key.set_resuming(), Actions.RESUME),
            (States.OK, lambda key: key.set_updating(Actions.UPDATE), Actions.UPDATE),
            (States.OK, lambda key: key.set_deleting(), Actions.DELETE),
        )
        for state, start, action in cases:
            with self.subTest(action):
                self.fail_command(start, state=state, pending_action="")
                response, prepare = self.post(
                    self.assignee, detail_url(self.key, "retry")
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()
                self.key.refresh_from_db()
                self.assertEqual(self.key.state, States.ERRED)
                self.assertEqual(self.key.pending_action, action)

    def test_assignee_cannot_retry_a_failed_request(self):
        self.fail_command(
            lambda key: None,
            state=States.CREATING,
            pending_action=Actions.CREATE,
            client_id="",
        )
        response, prepare = self.post(self.assignee, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_assignee_cannot_pause_resume_delete_or_edit(self):
        for action, state in (("pause", States.OK), ("resume", States.PAUSED)):
            with self.subTest(action):
                models.ResourceApiKey.objects.filter(pk=self.key.pk).update(state=state)
                response, prepare = self.post(
                    self.assignee, detail_url(self.key, action)
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()
        self.client.force_authenticate(self.assignee)
        response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(
            detail_url(self.key), {"limits": {"tokens": 50}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.key.refresh_from_db()
        self.assertEqual(self.key.user, self.assignee)
        self.assertEqual(self.key.pending_action, "")

    def test_a_member_who_is_not_the_assignee_cannot_rotate_or_retry(self):
        other = structure_factories.UserFactory()
        self.fixture.project.add_user(other, ProjectRole.MEMBER)
        response, prepare = self.post(other, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()
        self.fail_command(lambda key: key.set_updating())
        response, prepare = self.post(other, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_a_member_cannot_rotate_an_unassigned_key(self):
        self.key.user = None
        self.key.save()
        response, prepare = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_a_manager_can_still_rotate_an_assigned_key(self):
        response, _ = self.post(self.fixture.owner, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_an_assignee_who_is_also_a_manager_keeps_every_command(self):
        self.key.user = self.fixture.owner
        self.key.save()
        self.fail_command(lambda key: key.set_pausing())
        response, _ = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_an_assignee_who_left_the_project_cannot_rotate(self):
        self.fixture.project.remove_user(self.assignee)
        response, prepare = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        prepare.assert_not_called()

    def test_an_assignee_left_with_provider_access_alone_cannot_rotate(self):
        # The provider organization lists the key, as it reaches the write
        # actions, but is not on the consumer side of the resource.
        self.key.user = self.fixture.offering_owner
        self.key.save()
        response, prepare = self.post(
            self.fixture.offering_owner, detail_url(self.key, "rotate")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_an_assignee_left_with_a_role_on_the_organization_can_rotate(self):
        # Reveal admits them for it too.
        self.fixture.project.remove_user(self.assignee)
        self.fixture.customer.add_user(self.assignee, CustomerRole.SUPPORT)
        response, _ = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @override_config(PAT_ENABLED=True)
    def test_a_personal_access_token_cannot_rotate_as_the_assignee(self):
        # No scope stands for being the assignee, so a token limited to
        # reading would otherwise replace the key.
        self.assignee.can_use_personal_access_tokens = True
        self.assignee.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(
            self.assignee, scopes=[PermissionEnum.LIST_RESOURCES.value]
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        # The token itself is good: it reads the key it may not rotate.
        response = self.client.get(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with mock.patch(PREPARE, return_value=MESSAGES) as prepare:
            response = self.client.post(detail_url(self.key, "rotate"))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.fail_command(lambda key: key.set_updating())
            response = self.client.post(detail_url(self.key, "retry"))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_assignee_cannot_rotate_a_key_over_its_limit(self):
        # A usage report pauses only a settled key, so a rotation in flight
        # would hold the limit pause off for the one the limit is set for.
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            spending_limits={"tokens": {"month": 5}},
            current_usages={"tokens": 5},
            usage_period=THIS_MONTH,
        )
        response, prepare = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)
        response, _ = self.post(self.fixture.owner, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_assignee_can_retry_a_failed_rotation_of_a_key_over_its_limit(self):
        # An Erred key is never paused for its limit, so the retry holds
        # nothing off: it is what settles the key for the next report.
        self.fail_command(
            lambda key: key.set_updating(),
            spending_limits={"tokens": {"month": 5}},
            current_usages={"tokens": 9},
            usage_period=THIS_MONTH,
        )
        response, _ = self.post(self.assignee, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_limit_reached_during_a_rotation_pauses_the_key_as_it_settles(self):
        # A usage report holds off while the rotation is in flight; the
        # acknowledgement must not then settle the key unpaused.
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            spending_limits={"tokens": {"month": 5}},
            current_usages={"tokens": 4},
            usage_period=THIS_MONTH,
        )
        response, _ = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        response, prepare = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "report-usage"),
            {"usages": {"tokens": 50}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()

        response, prepare = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "set-key"),
            {"api_key": "sk-rotated"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.PAUSE)
        self.assertTrue(self.key.paused_by_limit)
        self.assertEqual(
            encryption.decrypt_value(self.key.key_ciphertext), "sk-rotated"
        )

    def test_last_months_usage_does_not_stop_the_assignee(self):
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            spending_limits={"tokens": {"month": 5}},
            current_usages={"tokens": 9},
            usage_period=LAST_MONTH,
        )
        response, _ = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_minimal_visibility_viewer_cannot_rotate(self):
        with mock.patch(
            "waldur_mastermind.marketplace.utils.is_resource_project_only_viewer",
            return_value=True,
        ):
            response, prepare = self.post(self.assignee, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_the_assignee_is_checked_again_under_the_lock(self):
        # The gate admitted the assignee on the row it read; the command is
        # decided on the row it locks.
        stale = models.ResourceApiKey.objects.get(pk=self.key.pk)
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            user=self.fixture.manager
        )
        with mock.patch(PREPARE, return_value=MESSAGES) as prepare:
            with self.assertRaises(PermissionDenied):
                resource_api_keys.issue_command(
                    stale, Actions.ROTATE, self.assignee, assignee_only=True
                )
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)


class ProjectRoleKeyManagementTest(ApiKeyManagementTestBase):
    """Project managers and administrators govern the resource's keys, as they
    govern the resource itself; a project member does not."""

    def setUp(self):
        super().setUp()
        # As permissions.yaml grants it.
        for role in (ProjectRole.ADMIN, ProjectRole.MANAGER):
            role.add_permission(PermissionEnum.MANAGE_RESOURCE_CONSUMER_API_KEY)

    def managers(self):
        return (
            ("manager", self.fixture.manager),
            ("admin", self.fixture.admin),
        )

    def patch(self, user, data):
        self.client.force_authenticate(user)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(detail_url(self.key), data, format="json")
        return response, prepare

    def delete(self, user):
        self.client.force_authenticate(user)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.delete(detail_url(self.key))
        return response, prepare

    def reset_key(self, state=States.OK):
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            state=state, pending_action="", user=None, spending_limits=None
        )
        self.key.refresh_from_db()

    def test_project_roles_can_request_a_key(self):
        for name, user in self.managers():
            with self.subTest(name):
                response, prepare = self.post(
                    user,
                    list_url(),
                    {"resource": self.resource.uuid.hex, "user": user.uuid.hex},
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(
                    self.published_payload(prepare)["action"], Actions.CREATE
                )

    def test_project_roles_can_edit_a_key(self):
        for name, user in self.managers():
            with self.subTest(name):
                self.reset_key()
                response, prepare = self.patch(user, {"limits": {"tokens": 50}})
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    self.published_payload(prepare)["action"], Actions.UPDATE
                )

    def test_project_roles_can_issue_every_command(self):
        cases = (
            ("pause", States.OK, Actions.PAUSE),
            ("resume", States.PAUSED, Actions.RESUME),
            ("rotate", States.OK, Actions.ROTATE),
        )
        for name, user in self.managers():
            for action, state, command in cases:
                with self.subTest(role=name, action=action):
                    self.reset_key(state)
                    response, prepare = self.post(user, detail_url(self.key, action))
                    self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
                    self.assertEqual(self.published_payload(prepare)["action"], command)

    def test_project_roles_can_retry_a_failed_command(self):
        for name, user in self.managers():
            with self.subTest(name):
                self.reset_key()
                self.key.set_pausing()
                self.key.set_erred()
                self.key.save()
                response, prepare = self.post(user, detail_url(self.key, "retry"))
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
                self.assertEqual(
                    self.published_payload(prepare)["action"], Actions.PAUSE
                )

    def test_project_roles_can_delete_a_key(self):
        for name, user in self.managers():
            with self.subTest(name):
                self.reset_key()
                response, prepare = self.delete(user)
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
                self.assertEqual(
                    self.published_payload(prepare)["action"], Actions.DELETE
                )

    def test_a_project_member_can_do_none_of_it(self):
        member = self.fixture.member
        calls = (
            (
                "request",
                lambda: self.post(
                    member, list_url(), {"resource": self.resource.uuid.hex}
                ),
            ),
            ("edit", lambda: self.patch(member, {"limits": {"tokens": 50}})),
            ("pause", lambda: self.post(member, detail_url(self.key, "pause"))),
            ("rotate", lambda: self.post(member, detail_url(self.key, "rotate"))),
            ("delete", lambda: self.delete(member)),
        )
        for action, call in calls:
            with self.subTest(action):
                response, prepare = call()
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()
        self.reset_key(States.PAUSED)
        response, prepare = self.post(member, detail_url(self.key, "resume"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()
        self.reset_key()
        self.key.set_pausing()
        self.key.set_erred()
        self.key.save()
        response, prepare = self.post(member, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        prepare.assert_not_called()

    def test_project_roles_of_another_project_cannot_reach_the_keys(self):
        # The permission is held on the project, not on its customer.
        other = structure_factories.ProjectFactory(customer=self.fixture.customer)
        for role in (ProjectRole.MANAGER, ProjectRole.ADMIN):
            with self.subTest(role.name):
                user = structure_factories.UserFactory()
                other.add_user(user, role)
                response, prepare = self.post(user, detail_url(self.key, "pause"))
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                response, prepare = self.post(
                    user, list_url(), {"resource": self.resource.uuid.hex}
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()

    def test_reveal_is_unchanged(self):
        # Managing a key is not reading it: an assigned key is still revealed
        # to its assignee alone.
        self.key.user = self.fixture.member
        self.key.save()
        for name, user in self.managers():
            with self.subTest(name):
                self.client.force_authenticate(user)
                response = self.client.get(detail_url(self.key, "reveal"))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PauseResumeDeleteTest(ApiKeyManagementTestBase):
    def test_pause_waits_for_the_agent(self):
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.PAUSE)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)

        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-paused")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)
        self.assertEqual(self.key.pending_action, "")

    def test_resume_waits_for_the_agent(self):
        self.key.state = States.PAUSED
        self.key.save()
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.RESUME)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.RESUME)

        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-ok")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)

    def test_delete_waits_for_the_agent_and_keeps_the_row(self):
        self.key.current_usages = {"tokens": 70}
        self.key.save()
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.DELETING)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.DELETE)

        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-deleted")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.DELETED)
        # The backend revoked it: the value is gone, the usage is not.
        self.assertEqual(self.key.key_ciphertext, "")
        self.assertEqual(self.key.current_usages, {"tokens": 70})

    def test_a_paused_key_can_be_deleted(self):
        self.key.state = States.PAUSED
        self.key.save()
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
            response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_paused_key_cannot_be_revealed_or_rotated(self):
        self.key.state = States.PAUSED
        self.key.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response, _ = self.post(self.fixture.owner, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_member_without_permission_cannot_command(self):
        for action in ("pause", "resume"):
            with self.subTest(action):
                response, _ = self.post(
                    self.fixture.admin, detail_url(self.key, action)
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_consumer_cannot_acknowledge(self):
        self.key.set_pausing()
        self.key.save()
        for action in ("set-paused", "set-ok", "set-deleted"):
            with self.subTest(action):
                response, _ = self.post(
                    self.fixture.owner, detail_url(self.key, action)
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_pending_command_refuses_another(self):
        response, _ = self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        for action in ("rotate", "pause", "resume"):
            with self.subTest(action):
                response, prepare = self.post(
                    self.fixture.owner, detail_url(self.key, action)
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.PAUSE)

    def test_a_failed_command_can_be_retried(self):
        self.key.set_pausing()
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "set-erred"),
            {"error_message": "gateway down"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.ERRED)
        # Kept, so the portal can say which command failed.
        self.assertEqual(self.key.pending_action, Actions.PAUSE)

        response, _ = self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


class RetryTest(ApiKeyManagementTestBase):
    """An Erred key accepts its failed command again, or a delete — nothing that
    would silently stand in for the command that failed."""

    def fail(self, start, **fields):
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(**fields)
        self.key.refresh_from_db()
        start(self.key)
        self.key.set_erred()
        self.key.error_message = "gateway down"
        self.key.save()

    def test_retry_repeats_the_failed_command(self):
        cases = (
            (States.OK, lambda key: key.set_pausing(), Actions.PAUSE),
            (States.PAUSED, lambda key: key.set_resuming(), Actions.RESUME),
            (States.OK, lambda key: key.set_updating(Actions.UPDATE), Actions.UPDATE),
            (States.OK, lambda key: key.set_updating(), Actions.ROTATE),
            (States.OK, lambda key: key.set_deleting(), Actions.DELETE),
        )
        for state, start, action in cases:
            with self.subTest(action):
                self.fail(start, state=state, pending_action="")
                response, prepare = self.post(
                    self.fixture.owner, detail_url(self.key, "retry")
                )
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
                self.key.refresh_from_db()
                self.assertEqual(self.key.pending_action, action)
                self.assertEqual(self.published_payload(prepare)["action"], action)

    def test_a_retried_update_carries_the_stored_settings(self):
        self.fail(
            lambda key: key.set_updating(Actions.UPDATE),
            spending_limits={"tokens": {"month": 50}},
            allowed_models=["mistral"],
        )
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["limits"], {"tokens": 50})
        self.assertEqual(payload["allowed_models"], ["mistral"])

    def test_a_key_that_is_not_erred_cannot_be_retried(self):
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        prepare.assert_not_called()

    def test_a_key_erred_before_commands_were_recorded_retries_a_rotation(self):
        self.fail(lambda key: key.set_updating(), pending_action="")
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(pending_action="")
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.ROTATE)

    def test_another_command_cannot_replace_the_failed_one(self):
        self.fail(lambda key: key.set_pausing())
        for action in ("rotate", "resume"):
            with self.subTest(action):
                response, prepare = self.post(
                    self.fixture.owner, detail_url(self.key, action)
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.ERRED)
        self.assertEqual(self.key.pending_action, Actions.PAUSE)

    def test_an_edit_cannot_replace_a_failed_rotation(self):
        self.fail(lambda key: key.set_updating())
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES) as prepare, mock.patch(PUBLISH):
            response = self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 50}}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertIsNone(self.key.spending_limits)
        self.assertEqual(self.key.pending_action, Actions.ROTATE)

    def test_an_edit_retries_a_failed_update(self):
        self.fail(
            lambda key: key.set_updating(Actions.UPDATE),
            spending_limits={"tokens": {"month": 5}},
        )
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES) as prepare, mock.patch(PUBLISH):
            response = self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 50}}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["limits"], {"tokens": 50})

    def test_a_failed_key_can_be_deleted(self):
        self.fail(lambda key: key.set_pausing())
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
            response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_member_without_permission_cannot_retry(self):
        self.fail(lambda key: key.set_pausing())
        response, _ = self.post(self.fixture.admin, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IssuedAtTest(ApiKeyManagementTestBase):
    """issued_at is the age of the value in use: only a new value moves it."""

    def test_a_rotated_value_is_issued_now(self):
        self.key.set_updating()
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "set-key"),
            {"api_key": "sk-new"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.issued_at)
        self.assertEqual(response.data["issued_at"], self.key.issued_at)

    def test_a_reported_key_is_issued_now(self):
        response, _ = self.post(
            self.fixture.offering_owner,
            list_url("report-created"),
            {
                "resource": self.resource.uuid.hex,
                "client_id": "cid-9",
                "api_key": "sk-new",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data["issued_at"])

    def test_a_pause_leaves_it_alone(self):
        self.key.issued_at = self.key.created
        self.key.save()
        self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.post(self.fixture.offering_owner, detail_url(self.key, "set-paused"))
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)
        self.assertEqual(self.key.issued_at, self.key.created)
        self.assertGreater(self.key.modified, self.key.issued_at)


class LateReportTest(ApiKeyManagementTestBase):
    """A late or duplicated agent report must not resurrect or re-settle a key."""

    def test_a_rotation_report_cannot_revive_a_paused_or_deleted_key(self):
        for state in (States.PAUSED, States.DELETED):
            with self.subTest(state):
                self.key.state = state
                self.key.save()
                response, _ = self.post(
                    self.fixture.offering_owner,
                    detail_url(self.key, "set-key"),
                    {"api_key": "sk-late"},
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.key.refresh_from_db()
                self.assertEqual(self.key.state, state)

    def test_a_creation_report_cannot_revive_a_paused_or_deleted_key(self):
        for state in (States.PAUSED, States.DELETED):
            with self.subTest(state):
                self.key.state = state
                self.key.save()
                response, _ = self.post(
                    self.fixture.offering_owner,
                    list_url("report-created"),
                    {
                        "resource": self.resource.uuid.hex,
                        "client_id": self.key.client_id,
                        "api_key": "sk-late",
                    },
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.key.refresh_from_db()
                self.assertEqual(self.key.state, state)

    def test_an_erred_report_cannot_touch_a_settled_key(self):
        for state in (States.PAUSED, States.DELETED):
            with self.subTest(state):
                self.key.state = state
                self.key.save()
                response, _ = self.post(
                    self.fixture.offering_owner,
                    detail_url(self.key, "set-erred"),
                    {"error_message": "late"},
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_report_cannot_settle_another_command(self):
        # A rotation report must not settle a pause, and a pause report must not
        # settle a rotation.
        self.key.set_pausing()
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "set-key"),
            {"api_key": "sk-late"},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-ok")
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

        other = self.make_key("cid-2")
        other.set_updating()
        other.save()
        response, _ = self.post(
            self.fixture.offering_owner, detail_url(other, "set-paused")
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_duplicate_acknowledgement_is_refused(self):
        self.key.set_deleting()
        self.key.save()
        for expected in (status.HTTP_200_OK, status.HTTP_409_CONFLICT):
            response, _ = self.post(
                self.fixture.offering_owner, detail_url(self.key, "set-deleted")
            )
            self.assertEqual(response.status_code, expected)

    def test_a_deleted_client_id_is_not_handed_out_again(self):
        self.key.state = States.DELETED
        self.key.save()
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource, pending_action=Actions.CREATE
        )
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(requested, "set-key"),
            {"api_key": "sk-new", "client_id": self.key.client_id},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class KeyUsageTest(ApiKeyManagementTestBase):
    def report(self, key, usages, billing_period=None):
        data = {"usages": usages}
        if billing_period:
            data["billing_period"] = billing_period.isoformat()
        return self.post(
            self.fixture.offering_owner, detail_url(key, "report-usage"), data
        )

    def totals(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            list_url("usage-totals"), {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["usages"]

    def test_usage_is_readable_per_key(self):
        response, _ = self.report(self.key, {"tokens": 12})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertEqual(response.data["current_usages"], {"tokens": 12})

    def test_a_report_leaves_modified_alone(self):
        # The agent's sweep reads modified as the time the key entered its state;
        # periodic usage reports must not hide a key stuck mid-command.
        before = self.key.modified
        self.report(self.key, {"tokens": 12})
        self.key.refresh_from_db()
        self.assertEqual(self.key.modified, before)

    def test_usage_aggregates_to_the_resource_total(self):
        other = self.make_key("cid-2")
        self.report(self.key, {"tokens": 12})
        self.report(other, {"tokens": 30})
        self.assertEqual(self.totals(), {"tokens": 42})

    def test_deleting_a_key_leaves_the_total_unchanged(self):
        other = self.make_key("cid-2")
        self.report(self.key, {"tokens": 12})
        self.report(other, {"tokens": 30})
        self.resource.current_usages = {"tokens": 42}
        self.resource.save()

        self.key.refresh_from_db()
        self.key.set_deleting()
        self.key.set_deleted()
        self.key.save()

        self.assertEqual(self.totals(), {"tokens": 42})
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages, {"tokens": 42})

    def test_a_deleted_key_still_accepts_usage(self):
        self.key.state = States.DELETED
        self.key.save()
        response, _ = self.report(self.key, {"tokens": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"tokens": 5})
        self.assertEqual(self.key.state, States.DELETED)

    def test_non_finite_usage_is_rejected(self):
        for value in ("NaN", "Infinity"):
            with self.subTest(value):
                response, _ = self.report(self.key, {"tokens": value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usage_of_an_unknown_component_is_rejected(self):
        response, _ = self.report(self.key, {"gpu": 5})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_consumer_cannot_report_usage(self):
        response, _ = self.post(
            self.fixture.owner,
            detail_url(self.key, "report-usage"),
            {"usages": {"tokens": 1}},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_usage_totals_need_a_resource(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(list_url("usage-totals"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usage_belongs_to_the_current_month_by_default(self):
        self.report(self.key, {"tokens": 12})
        self.key.refresh_from_db()
        self.assertEqual(self.key.usage_period, THIS_MONTH)

    def test_a_report_for_the_same_month_merges(self):
        factories.OfferingComponentFactory(offering=self.offering, type="images")
        self.report(self.key, {"tokens": 12})
        self.report(self.key, {"images": 3})
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"tokens": 12, "images": 3})

    def test_a_new_month_starts_afresh(self):
        factories.OfferingComponentFactory(offering=self.offering, type="images")
        self.report(self.key, {"tokens": 500, "images": 3}, LAST_MONTH)
        self.report(self.key, {"tokens": 5})
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"tokens": 5})
        self.assertEqual(self.key.usage_period, THIS_MONTH)

    def test_any_day_names_its_month(self):
        self.report(self.key, {"tokens": 5}, LAST_MONTH + datetime.timedelta(days=9))
        self.key.refresh_from_db()
        self.assertEqual(self.key.usage_period, LAST_MONTH)

    def test_an_earlier_month_is_refused_once_a_later_one_is_held(self):
        self.report(self.key, {"tokens": 5})
        response, _ = self.report(self.key, {"tokens": 50}, LAST_MONTH)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.key.refresh_from_db()
        self.assertEqual(self.key.current_usages, {"tokens": 5})

    def test_a_future_month_is_rejected(self):
        response, _ = self.report(self.key, {"tokens": 5}, NEXT_MONTH)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_totals_cover_the_current_month(self):
        other = self.make_key("cid-2")
        self.report(self.key, {"tokens": 12})
        self.report(other, {"tokens": 30}, LAST_MONTH)
        self.assertEqual(self.totals(), {"tokens": 12})


class KeyLimitTest(ApiKeyManagementTestBase):
    def setUp(self):
        super().setUp()
        self.key.spending_limits = {"tokens": {"month": 100}}
        self.key.save()
        self.sibling = self.make_key("cid-2")

    def report(self, usages, billing_period=None):
        data = {"usages": usages}
        if billing_period:
            data["billing_period"] = billing_period.isoformat()
        return self.post(
            self.fixture.offering_owner, detail_url(self.key, "report-usage"), data
        )

    def pause_for_limit(self, usage=150, period=THIS_MONTH):
        self.key.state = States.PAUSED
        self.key.paused_by_limit = True
        self.key.current_usages = {"tokens": usage}
        self.key.usage_period = period
        self.key.save()

    def test_reaching_the_limit_pauses_that_key_only(self):
        resource_state = self.resource.state
        response, prepare = self.report({"tokens": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.PAUSE)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.PAUSE)
        self.assertEqual(payload["api_key_uuid"], self.key.uuid.hex)

        self.sibling.refresh_from_db()
        self.assertEqual(self.sibling.state, States.OK)
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertEqual(self.resource.state, resource_state)

    def test_usage_under_the_limit_changes_nothing(self):
        response, prepare = self.report({"tokens": 99})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)

    def test_a_zero_monthly_limit_is_no_limit(self):
        # The flat monthly shape keeps its meaning: zero sets no limit.
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
            response = self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 0}}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertIsNone(self.key.spending_limits)

    def test_a_key_already_paused_is_not_paused_again(self):
        self.key.state = States.PAUSED
        self.key.save()
        response, prepare = self.report({"tokens": 500})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()

    def test_a_key_mid_command_is_left_to_finish(self):
        self.key.set_updating()
        self.key.save()
        response, prepare = self.report({"tokens": 500})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.ROTATE)

    def test_a_limit_pause_is_marked_as_such(self):
        self.report({"tokens": 100})
        self.key.refresh_from_db()
        self.assertTrue(self.key.paused_by_limit)

    def test_a_pause_by_hand_is_not(self):
        self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.key.refresh_from_db()
        self.assertFalse(self.key.paused_by_limit)

    def test_usage_of_an_earlier_month_counts_against_no_limit(self):
        _, prepare = self.report({"tokens": 500}, LAST_MONTH)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)

    def test_a_new_months_report_resumes_a_key_paused_for_its_limit(self):
        self.pause_for_limit(period=LAST_MONTH)
        response, prepare = self.report({"tokens": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.RESUME)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.UPDATING)
        self.assertEqual(self.key.pending_action, Actions.RESUME)
        self.assertFalse(self.key.paused_by_limit)

    def test_a_report_still_over_the_limit_leaves_it_paused(self):
        self.pause_for_limit()
        _, prepare = self.report({"tokens": 160})
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)

    def test_a_key_paused_by_hand_stays_paused(self):
        self.pause_for_limit(period=LAST_MONTH)
        self.key.paused_by_limit = False
        self.key.save()
        _, prepare = self.report({"tokens": 10})
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)

    def test_raising_the_limit_resumes_the_key_with_it(self):
        self.pause_for_limit()
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 200}}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.RESUME)
        self.assertEqual(payload["limits"], {"tokens": 200})

    def test_a_limit_still_under_the_usage_leaves_it_paused(self):
        self.pause_for_limit()
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 120}}, format="json"
            )
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)
        self.assertEqual(self.key.spending_limits, {"tokens": {"month": 120}})

    def test_a_resume_by_hand_clears_the_mark(self):
        self.pause_for_limit()
        self.post(self.fixture.owner, detail_url(self.key, "resume"))
        self.key.refresh_from_db()
        self.assertFalse(self.key.paused_by_limit)

    def test_a_resume_by_hand_over_the_limit_is_paused_again_as_it_settles(self):
        # A resume by hand does not override the limit.
        self.pause_for_limit()
        self.post(self.fixture.owner, detail_url(self.key, "resume"))
        response, prepare = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-ok")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.PAUSE)
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.PAUSE)
        self.assertTrue(self.key.paused_by_limit)

    def test_a_key_under_its_limit_settles_ok(self):
        self.key.current_usages = {"tokens": 99}
        self.key.usage_period = THIS_MONTH
        self.key.set_updating(Actions.UPDATE)
        self.key.save()
        response, prepare = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-ok")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)

    def test_an_unenforced_limit_lets_the_key_settle_ok(self):
        self.offering.plugin_options = {}
        self.offering.save()
        self.key.current_usages = {"tokens": 150}
        self.key.usage_period = THIS_MONTH
        self.key.set_updating()
        self.key.save()
        response, prepare = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "set-key"),
            {"api_key": "sk-rotated"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.OK)

    def test_the_month_turning_resumes_keys_paused_for_their_limit(self):
        self.pause_for_limit(period=LAST_MONTH)
        over = self.make_key(
            "cid-3",
            state=States.PAUSED,
            paused_by_limit=True,
            spending_limits={"tokens": {"month": 100}},
            current_usages={"tokens": 150},
            usage_period=THIS_MONTH,
        )
        by_hand = self.make_key(
            "cid-4",
            state=States.PAUSED,
            spending_limits={"tokens": {"month": 100}},
            current_usages={"tokens": 150},
            usage_period=LAST_MONTH,
        )
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            tasks.resume_api_keys_under_limit()
        payload = self.published_payload(prepare)
        self.assertEqual(payload["api_key_uuid"], self.key.uuid.hex)
        self.assertEqual(payload["action"], Actions.RESUME)
        for key in (over, by_hand):
            key.refresh_from_db()
            self.assertEqual(key.state, States.PAUSED)

    def test_an_automatic_resume_says_why(self):
        self.pause_for_limit(period=LAST_MONTH)
        self.fixture.offering_owner
        with mock.patch("waldur_mastermind.marketplace.log.event_logger.emit") as emit:
            self.report({"tokens": 10})
        emit.assert_called_once()
        message = emit.call_args.args[0]
        self.assertIn("automatically", message)
        self.assertIn("under its limit again", message)


class AuditTest(ApiKeyManagementTestBase):
    """Every command is audited with its own event, naming the key."""

    def emitted(self, call):
        with mock.patch("waldur_mastermind.marketplace.log.event_logger.emit") as emit:
            call()
        # Lazily created fixture users and roles emit their own events.
        return [
            (c.args[0], c.kwargs["event_type"])
            for c in emit.call_args_list
            if c.kwargs["event_type"].startswith("marketplace_resource_api_key")
        ]

    def test_each_command_emits_its_event(self):
        cases = (
            (
                "pause",
                States.OK,
                EventType.MARKETPLACE_RESOURCE_API_KEY_PAUSED,
            ),
            (
                "resume",
                States.PAUSED,
                EventType.MARKETPLACE_RESOURCE_API_KEY_RESUMED,
            ),
            (
                "rotate",
                States.OK,
                EventType.MARKETPLACE_RESOURCE_API_KEY_ROTATED,
            ),
        )
        for action, state, event_type in cases:
            with self.subTest(action):
                models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
                    state=state, pending_action=""
                )
                events = self.emitted(
                    lambda: self.post(self.fixture.owner, detail_url(self.key, action))
                )
                self.assertEqual([event for _, event in events], [event_type], events)
                self.assertIn("cid-1", events[0][0])
                self.assertIn(str(self.fixture.owner), events[0][0])
                # Logged before the agent has done it.
                self.assertIn("has been requested", events[0][0])

    def test_a_rotation_by_the_assignee_names_them_as_the_actor(self):
        self.key.user = self.fixture.admin
        self.key.save()
        events = self.emitted(
            lambda: self.post(self.fixture.admin, detail_url(self.key, "rotate"))
        )
        self.assertEqual(
            [event for _, event in events],
            [EventType.MARKETPLACE_RESOURCE_API_KEY_ROTATED],
        )
        self.assertIn(f"requested by {self.fixture.admin}", events[0][0])

    def test_a_failure_report_is_audited(self):
        self.key.set_pausing()
        self.key.save()
        events = self.emitted(
            lambda: self.post(
                self.fixture.offering_owner,
                detail_url(self.key, "set-erred"),
                {"error_message": "gateway down"},
            )
        )
        self.assertEqual(
            [event for _, event in events],
            [EventType.MARKETPLACE_RESOURCE_API_KEY_FAILED],
        )
        self.assertIn("Pause of API key cid-1", events[0][0])
        self.assertIn("gateway down", events[0][0])

    def test_request_and_delete_emit_their_events(self):
        events = self.emitted(
            lambda: self.post(
                self.fixture.owner, list_url(), {"resource": self.resource.uuid.hex}
            )
        )
        self.assertEqual(
            [event for _, event in events],
            [EventType.MARKETPLACE_RESOURCE_API_KEY_REQUESTED],
        )

        def delete():
            self.client.force_authenticate(self.fixture.owner)
            with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
                self.client.delete(detail_url(self.key))

        events = self.emitted(delete)
        self.assertEqual(
            [event for _, event in events],
            [EventType.MARKETPLACE_RESOURCE_API_KEY_DELETED],
        )

    def test_an_automatic_pause_says_why(self):
        self.key.spending_limits = {"tokens": {"month": 10}}
        self.key.save()
        events = self.emitted(
            lambda: self.post(
                self.fixture.offering_owner,
                detail_url(self.key, "report-usage"),
                {"usages": {"tokens": 10}},
            )
        )
        self.assertEqual(len(events), 1)
        message, event_type = events[0]
        self.assertEqual(event_type, EventType.MARKETPLACE_RESOURCE_API_KEY_PAUSED)
        self.assertIn("automatically", message)
        self.assertIn("month limit of tokens", message)


class ListingTest(ApiKeyManagementTestBase):
    def setUp(self):
        super().setUp()
        self.deleted = self.make_key("cid-2", state=States.DELETED)
        self.client.force_authenticate(self.fixture.owner)

    def test_deleted_keys_are_hidden_by_default(self):
        response = self.client.get(
            list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual([key["uuid"] for key in response.data], [self.key.uuid.hex])

    def test_deleted_keys_list_when_asked_for(self):
        response = self.client.get(list_url(), {"state": States.DELETED})
        self.assertEqual(
            [key["uuid"] for key in response.data], [self.deleted.uuid.hex]
        )

    def test_filter_by_pending_action(self):
        self.key.set_pausing()
        self.key.save()
        response = self.client.get(list_url(), {"pending_action": Actions.PAUSE})
        self.assertEqual([key["uuid"] for key in response.data], [self.key.uuid.hex])

    def test_filter_by_whether_a_command_is_pending(self):
        settled = self.make_key("cid-3")
        self.key.set_pausing()
        self.key.save()
        for value, expected in (("false", settled), ("true", self.key)):
            with self.subTest(value):
                response = self.client.get(list_url(), {"has_pending_action": value})
                self.assertEqual(
                    [key["uuid"] for key in response.data], [expected.uuid.hex]
                )

    def test_unknown_action_is_a_bad_request(self):
        response = self.client.get(list_url(), {"pending_action": "explode"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_by_assignee(self):
        self.key.user = self.fixture.admin
        self.key.save()
        response = self.client.get(
            list_url(), {"user_uuid": self.fixture.admin.uuid.hex}
        )
        self.assertEqual([key["uuid"] for key in response.data], [self.key.uuid.hex])


class CommandEnumTest(test.APITestCase):
    def test_an_unknown_action_is_never_published(self):
        key = models.ResourceApiKey.objects.create(
            resource=factories.ResourceFactory(), client_id="cid-1"
        )
        with mock.patch(PREPARE) as prepare, self.assertRaises(ValueError):
            utils.publish_api_key_event(key, "explode")
        prepare.assert_not_called()

    @staticmethod
    def managed_resource():
        # A rotation carries settings only where the offering manages keys.
        resource = factories.ResourceFactory()
        resource.offering.plugin_options = {"enable_api_key_provisioning": True}
        resource.offering.save()
        return resource

    def test_a_resource_without_a_model_selection_leaves_the_key_list_as_stored(self):
        # The list sent is the effective one (test_resource_model_selection); a
        # resource whose offering has no model selection narrows nothing.
        for allowed_models in (ALL_MODELS, [], ["llama-3"]):
            key = models.ResourceApiKey.objects.create(
                resource=self.managed_resource(), allowed_models=allowed_models
            )
            for action in Actions.CARRY_SETTINGS:
                with (
                    self.subTest(allowed_models=allowed_models, action=action),
                    mock.patch(PREPARE, return_value=[]) as prepare,
                ):
                    utils.publish_api_key_event(key, action)
                    payload = prepare.call_args.args[1]
                    self.assertEqual(payload["allowed_models"], allowed_models)

    def test_settings_travel_only_with_commands_that_configure_the_key(self):
        key = models.ResourceApiKey.objects.create(
            resource=self.managed_resource(),
            client_id="cid-1",
            spending_limits={"tokens": {"month": 5}},
        )
        for action in Actions.VALUES:
            with self.subTest(action), mock.patch(PREPARE, return_value=[]) as prepare:
                utils.publish_api_key_event(key, action)
                payload = prepare.call_args.args[1]
                self.assertEqual(payload["action"], action)
                self.assertEqual("limits" in payload, action in Actions.CARRY_SETTINGS)


class CapabilityGatingTest(ApiKeyManagementTestBase):
    """A backend that cannot govern keys one by one exposes none of it."""

    def setUp(self):
        super().setUp()
        self.offering.plugin_options = {}
        self.offering.save()
        self.key.user = self.fixture.admin
        self.key.spending_limits = {"tokens": {"month": 5}}
        self.key.allowed_models = ALL_MODELS
        self.key.save()

    def test_managed_fields_are_null(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in (
            "limits",
            "allowed_models",
            "current_usages",
            "usage_period",
            "paused_by_limit",
        ):
            self.assertIsNone(response.data[field], field)

    def test_the_assignee_stays_visible(self):
        """Reveal still honours it, so the portal must know whose key it is."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertEqual(response.data["user_uuid"], self.fixture.admin.uuid)
        self.assertEqual(response.data["user_full_name"], self.fixture.admin.full_name)
        response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_managed_actions_are_refused(self):
        response, prepare = self.post(
            self.fixture.owner, list_url(), {"resource": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "pause"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(detail_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(
            detail_url(self.key), {"limits": {"tokens": 9}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self.post(
            self.fixture.offering_owner,
            detail_url(self.key, "report-usage"),
            {"usages": {"tokens": 1}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_command_in_flight_still_settles(self):
        # Switching the capability off must not strand a key mid-command.
        self.key.set_pausing()
        self.key.save()
        response, _ = self.post(
            self.fixture.offering_owner, detail_url(self.key, "set-paused")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.key.refresh_from_db()
        self.assertEqual(self.key.state, States.PAUSED)

    def test_a_paused_key_can_still_be_resumed(self):
        # Paused while governance was on: without resume it would be stranded.
        self.key.state = States.PAUSED
        self.key.save()
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.RESUME)

    def test_a_failed_governed_command_cannot_be_retried(self):
        self.key.set_pausing()
        self.key.set_erred()
        self.key.save()
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()

    def test_a_failed_governed_command_gives_way_to_an_ungoverned_one(self):
        self.key.set_pausing()
        self.key.set_erred()
        self.key.save()
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.RESUME)

    def test_a_failed_rotation_can_be_retried(self):
        self.key.set_updating()
        self.key.set_erred()
        self.key.save()
        response, prepare = self.post(self.fixture.owner, detail_url(self.key, "retry"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.ROTATE)

    def test_rotation_still_works(self):
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "rotate")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.ROTATE)
        self.assertNotIn("limits", payload)

    def test_the_assignee_can_still_rotate(self):
        # Rotation is not governance, and the assignee stays the only one who
        # can reveal the key.
        response, prepare = self.post(
            self.fixture.admin, detail_url(self.key, "rotate")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["action"], Actions.ROTATE)

    def test_a_limit_no_longer_enforced_does_not_stop_the_assignee(self):
        # Usage is no longer reported and the limit cannot be edited, so a key
        # that reached it while governance was on would stay shut for good.
        models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
            current_usages={"tokens": 9}, usage_period=THIS_MONTH
        )
        response, _ = self.post(self.fixture.admin, detail_url(self.key, "rotate"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_the_assignee_cannot_rotate_in_place_of_a_failed_governed_command(self):
        # A manager may let a rotation take a stranded key's failed pause's
        # place; for the assignee it would undo a pause meant for them.
        for start in (
            lambda key: key.set_pausing(),
            lambda key: key.set_deleting(),
            lambda key: key.set_updating(Actions.UPDATE),
        ):
            models.ResourceApiKey.objects.filter(pk=self.key.pk).update(
                state=States.OK, pending_action=""
            )
            self.key.refresh_from_db()
            start(self.key)
            failed = self.key.pending_action
            self.key.set_erred()
            self.key.save()
            with self.subTest(failed):
                response, prepare = self.post(
                    self.fixture.admin, detail_url(self.key, "rotate")
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                prepare.assert_not_called()
                response, prepare = self.post(
                    self.fixture.owner, detail_url(self.key, "rotate")
                )
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_the_assignee_can_be_neither_removed_nor_set(self):
        # Without key management a key can be neither requested nor deleted,
        # but the assignee is refused the same way, not with governance.
        self.client.force_authenticate(self.fixture.owner)
        for user in (None, self.fixture.manager.uuid.hex):
            with self.subTest(user=user):
                response = self.client.patch(
                    detail_url(self.key), {"user": user}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(
                    "Request a key for the new holder", str(response.data["user"])
                )
                self.key.refresh_from_db()
                self.assertEqual(self.key.user, self.fixture.admin)

    def test_an_assignee_still_restricts_reveal(self):
        # Switching the capability off hides the assignee but must not hand a
        # personal key to everyone in the project.
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key, "reveal"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# pytest may run with --no-migrations, which empties MIGRATION_MODULES and with
# it the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class MigrationTest(test.APITestCase):
    def test_existing_keys_are_unchanged_and_in_flight_ones_get_their_command(self):
        resource = factories.ResourceFactory()
        settled = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-1", state=States.OK
        )
        rotating = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-2", state=States.UPDATING
        )
        apps = MigrationLoader(None).project_state().apps
        migration.backfill_pending_action(apps, None)

        settled.refresh_from_db()
        self.assertEqual(settled.pending_action, "")
        self.assertIsNone(settled.user)
        self.assertIsNone(settled.spending_limits)
        self.assertEqual(settled.state, States.OK)
        settled.set_updating()  # still rotatable
        rotating.refresh_from_db()
        self.assertEqual(rotating.pending_action, Actions.ROTATE)

    def test_a_stored_value_was_issued_when_its_row_last_changed(self):
        resource = factories.ResourceFactory()
        stored = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-1", key_ciphertext="x", state=States.OK
        )
        awaited = models.ResourceApiKey.objects.create(resource=resource)
        apps = MigrationLoader(None).project_state().apps
        migration.backfill_pending_action(apps, None)

        stored.refresh_from_db()
        self.assertEqual(stored.issued_at, stored.modified)
        awaited.refresh_from_db()
        self.assertIsNone(awaited.issued_at)

    def nullable_model_lists(self):
        # The column no longer takes null; the migration ran while it still did.
        # Before any key exists: Postgres refuses to alter a table with rows the
        # open transaction wrote.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE marketplace_resourceapikey "
                "ALTER COLUMN allowed_models DROP NOT NULL"
            )

    def set_null(self, *keys):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE marketplace_resourceapikey SET allowed_models = NULL "
                "WHERE id = ANY(%s)",
                [[key.pk for key in keys]],
            )

    def test_a_key_without_a_model_list_keeps_calling_every_model(self):
        self.nullable_model_lists()
        resource = factories.ResourceFactory()
        unrestricted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-1", state=States.OK
        )
        deleted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-2", state=States.DELETED
        )
        restricted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-3", allowed_models=["mistral"]
        )
        self.set_null(unrestricted, deleted)
        apps = MigrationLoader(None).project_state().apps
        all_models_migration.null_to_all_models(apps, None)

        for key in (unrestricted, deleted):
            key.refresh_from_db()
            self.assertEqual(key.allowed_models, ALL_MODELS)
        restricted.refresh_from_db()
        self.assertEqual(restricted.allowed_models, ["mistral"])

    def test_reversed_every_model_is_null_again(self):
        self.nullable_model_lists()
        resource = factories.ResourceFactory()
        unrestricted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-1", allowed_models=ALL_MODELS
        )
        restricted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-2", allowed_models=["mistral"]
        )
        apps = MigrationLoader(None).project_state().apps
        all_models_migration.all_models_to_null(apps, None)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT allowed_models IS NULL FROM marketplace_resourceapikey "
                "WHERE id = %s",
                [unrestricted.pk],
            )
            self.assertTrue(cursor.fetchone()[0])
        restricted.refresh_from_db()
        self.assertEqual(restricted.allowed_models, ["mistral"])

    def test_reversing_refuses_while_a_key_may_call_no_model(self):
        # The earlier code reads an empty list as every model, so rolling back
        # would let this key call every one.
        self.nullable_model_lists()
        resource = factories.ResourceFactory()
        no_models = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-1", allowed_models=[]
        )
        unrestricted = models.ResourceApiKey.objects.create(
            resource=resource, client_id="cid-2", allowed_models=ALL_MODELS
        )
        apps = MigrationLoader(None).project_state().apps
        with self.assertRaisesMessage(RuntimeError, no_models.uuid.hex):
            all_models_migration.all_models_to_null(apps, None)

        unrestricted.refresh_from_db()
        self.assertEqual(unrestricted.allowed_models, ALL_MODELS)
