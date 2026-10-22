"""A resource's model selection and how it narrows its API keys.

The resource's ``models`` option takes a key's three forms: ``["*"]`` every model,
``[]`` none, or names. A key's ``["*"]`` is every model of its resource, and the
site agent receives the effective list: the resource's selection, then the key's,
less any model the offering no longer lists.
"""

from importlib import import_module
from unittest import mock

from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import (
    model_lists,
    models,
    serializers,
    tasks,
    utils,
)
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.tests.test_resource_api_key_management import (
    MESSAGES,
    PREPARE,
    PUBLISH,
    ApiKeyManagementTestBase,
    detail_url,
    list_url,
)

States = models.ResourceApiKey.States
Actions = models.ResourceApiKey.Actions
ALL_MODELS = ["*"]
CHOICES = ["llama-3", "mistral", "qwen"]
MODELS_OPTION = {
    "type": "select_string_multi",
    "label": "Models",
    "choices": CHOICES,
}

selection_migration = import_module(
    "waldur_mastermind.marketplace.migrations.0311_resource_models_all_models"
)
outdated_migration = import_module(
    "waldur_mastermind.marketplace.migrations."
    "0312_resource_api_key_models_outdated_backfill"
)


def models_options(option_type="select_string_multi", choices=CHOICES):
    return {
        "options": {
            "models": {**MODELS_OPTION, "type": option_type, "choices": choices}
        },
        "order": ["models"],
    }


def offer_models(offering, managed=True, option_type="select_string_multi"):
    offering.resource_options = models_options(option_type)
    offering.plugin_options = {"enable_api_key_provisioning": managed}
    offering.save()


class NarrowTest(test.APISimpleTestCase):
    def test_the_resource_selection_then_the_key_list(self):
        cases = (
            (ALL_MODELS, ALL_MODELS, ALL_MODELS),
            (ALL_MODELS, ["a"], ["a"]),
            (ALL_MODELS, [], []),
            (["a", "b"], ALL_MODELS, ["a", "b"]),
            (["a", "b"], ["b", "c"], ["b"]),
            (["a", "b"], [], []),
            ([], ALL_MODELS, []),
            ([], ["a"], []),
        )
        for resource_list, key_list, expected in cases:
            with self.subTest(resource=resource_list, key=key_list):
                self.assertEqual(model_lists.narrow(resource_list, key_list), expected)

    def test_a_model_the_offering_no_longer_lists_is_dropped(self):
        cases = (
            (ALL_MODELS, ALL_MODELS, ALL_MODELS),
            (ALL_MODELS, ["a", "b"], ["a"]),
            (["a", "b"], ALL_MODELS, ["a"]),
            (["b"], ALL_MODELS, []),
        )
        for resource_list, key_list, expected in cases:
            with self.subTest(resource=resource_list, key=key_list):
                self.assertEqual(
                    model_lists.key_models(resource_list, key_list, ["a", "c"]),
                    expected,
                )
        # An offering that lists no models drops nothing.
        self.assertEqual(model_lists.key_models(ALL_MODELS, ["b"], None), ["b"])

    def test_a_resource_that_never_chose_is_on_every_model(self):
        self.assertEqual(model_lists.selection(None), ALL_MODELS)
        self.assertEqual(model_lists.selection({}), ALL_MODELS)
        # An unset single choice, kept when the option became a multiple one.
        self.assertEqual(model_lists.selection({"models": ""}), ALL_MODELS)
        self.assertEqual(model_lists.selection({"models": []}), [])
        self.assertEqual(model_lists.selection({"models": "a"}), ["a"])


class SelectionTestBase(ApiKeyManagementTestBase):
    def setUp(self):
        super().setUp()
        offer_models(self.offering)
        self.resource.state = ResourceStates.OK
        self.resource.options = {"models": ALL_MODELS}
        self.resource.save()
        self.key.allowed_models = ALL_MODELS
        self.key.save()

    def select(self, selection):
        """Save a new selection on the resource, as the option paths do."""
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            self.resource.options = {**(self.resource.options or {}), **selection}
            self.resource.save()
        return prepare

    def payloads(self, prepare):
        return [call.args[1] for call in prepare.call_args_list]


class ResourceOptionTest(SelectionTestBase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")

    def update(self, models_value):
        self.client.force_authenticate(self.fixture.owner)
        with mock.patch(PREPARE, return_value=MESSAGES), mock.patch(PUBLISH):
            return self.client.post(
                self.url, {"options": {"models": models_value}}, format="json"
            )

    def test_every_model_none_and_names_are_stored_as_given(self):
        for value in (["mistral"], [], ALL_MODELS):
            with self.subTest(value):
                response = self.update(value)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.resource.refresh_from_db()
                self.assertEqual(self.resource.options["models"], value)

    def test_every_model_cannot_be_combined_with_a_model(self):
        for value in (["*", "mistral"], ["mistral", "*"], ["*", "*"]):
            with self.subTest(value):
                response = self.update(value)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.resource.refresh_from_db()
                self.assertEqual(self.resource.options["models"], ALL_MODELS)

    def test_a_model_the_offering_does_not_list_is_refused(self):
        response = self.update(["gpt-5"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_every_model_is_a_choice_without_key_management_too(self):
        # So an offering that turns key management on later finds its
        # resources on every model.
        offer_models(self.offering, managed=False)
        response = self.update(ALL_MODELS)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_single_choice_models_option_has_no_every_model(self):
        offer_models(self.offering, option_type="select_string")
        response = self.update("*")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_provider_may_approve_every_model(self):
        self.offering.plugin_options = {
            "enable_api_key_provisioning": True,
            "create_orders_on_resource_option_change": True,
        }
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.update(["mistral"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        url = factories.OrderFactory.get_url(order, "approve_by_provider")
        self.client.force_authenticate(self.fixture.offering_owner)
        for value, expected in (
            (["*", "mistral"], status.HTTP_400_BAD_REQUEST),
            (ALL_MODELS, status.HTTP_200_OK),
        ):
            with self.subTest(value):
                response = self.client.post(
                    url,
                    {"attributes": {"new_options": {"models": value}}},
                    format="json",
                )
                self.assertEqual(response.status_code, expected)


class NewResourceTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offer_models(self.offering)
        self.plan = factories.PlanFactory(offering=self.offering)

    def post_order(self, attributes):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(self.project),
                "offering": factories.OfferingFactory.get_public_url(self.offering),
                "plan": factories.PlanFactory.get_public_url(self.plan),
                "attributes": attributes,
            },
            format="json",
        )

    def order(self, attributes):
        response = self.post_order(attributes)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return models.Order.objects.get(uuid=response.data["uuid"])

    def test_a_new_resource_is_on_every_model(self):
        order = self.order({})
        self.assertEqual(order.resource.options["models"], ALL_MODELS)

    def test_a_selection_ordered_with_the_resource_is_kept(self):
        for value in (["mistral"], []):
            with self.subTest(value):
                order = self.order({"models": value})
                self.assertEqual(order.resource.options["models"], value)

    def test_an_edited_order_leaves_its_resource_on_every_model(self):
        order = self.order({"models": ["mistral"]})
        order.attributes = {}
        order.save()
        utils.copy_order_options_to_resource(order)
        order.resource.refresh_from_db()
        self.assertEqual(order.resource.options["models"], ALL_MODELS)

    def test_a_new_resource_is_on_every_model_without_key_management_too(self):
        offer_models(self.offering, managed=False)
        order = self.order({})
        self.assertEqual(order.resource.options["models"], ALL_MODELS)

    def test_a_single_choice_models_option_gets_no_default(self):
        offer_models(self.offering, option_type="select_string")
        order = self.order({})
        self.assertNotIn("models", order.resource.options)

    def test_an_order_form_asking_for_the_models_takes_every_model(self):
        # The order form asks for the selection too: * is a choice there as well.
        self.offering.options = models_options()
        self.offering.save()
        order = self.order({"models": ALL_MODELS})
        self.assertEqual(order.resource.options["models"], ALL_MODELS)

        response = self.post_order({"models": ["*", "mistral"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("models", response.data)

    def test_an_order_form_without_a_model_selection_takes_no_star(self):
        self.offering.options = models_options()
        self.offering.resource_options = {}
        self.offering.save()
        response = self.post_order({"models": ALL_MODELS})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrderedSelectionChangeTest(test.APITestCase):
    """A pending create order's selection, edited by the consumer or set by the
    provider at approval, takes * as the ordered one does.
    """

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.offering = self.fixture.offering
        offer_models(self.offering)
        self.offering.options = models_options()
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.CREATING
        self.resource.options = {"models": ["mistral"]}
        self.resource.save()
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.resource.project,
            created_by=self.fixture.admin,
            type=OrderTypes.CREATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={"models": ["mistral"]},
        )

    def approve(self, attributes):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        return self.client.post(
            factories.OrderFactory.get_url(self.order, "approve_by_provider"),
            {"attributes": attributes},
            format="json",
        )

    def edit(self, attributes):
        self.order.state = OrderStates.PENDING_CONSUMER
        self.order.save()
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)
        return self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )

    def test_the_provider_may_approve_every_model(self):
        response = self.approve({"models": ALL_MODELS})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["models"], ALL_MODELS)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["models"], ALL_MODELS)

    def test_the_provider_may_not_mix_every_model_with_names(self):
        response = self.approve({"models": ["*", "qwen"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["models"], ["mistral"])

    def test_an_edited_order_takes_every_model_alone(self):
        for value, expected in (
            (["*", "qwen"], status.HTTP_400_BAD_REQUEST),
            (ALL_MODELS, status.HTTP_200_OK),
        ):
            with self.subTest(value):
                response = self.edit({"models": value})
                self.assertEqual(response.status_code, expected, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["models"], ALL_MODELS)


class KeyValidationTest(SelectionTestBase):
    def request(self, allowed_models):
        return self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "allowed_models": allowed_models},
        )

    def test_a_key_may_only_narrow_its_resource(self):
        self.select({"models": ["llama-3", "mistral"]})
        for allowed_models, expected in (
            (["mistral"], status.HTTP_201_CREATED),
            (["qwen"], status.HTTP_400_BAD_REQUEST),
            (ALL_MODELS, status.HTTP_201_CREATED),
            ([], status.HTTP_201_CREATED),
        ):
            with self.subTest(allowed_models):
                response, _ = self.request(allowed_models)
                self.assertEqual(response.status_code, expected)

    def test_a_resource_on_every_model_allows_what_the_offering_lists(self):
        for allowed_models, expected in (
            (["qwen"], status.HTTP_201_CREATED),
            (["gpt-5"], status.HTTP_400_BAD_REQUEST),
        ):
            with self.subTest(allowed_models):
                response, _ = self.request(allowed_models)
                self.assertEqual(response.status_code, expected)

    def patch(self, data):
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(detail_url(self.key), data, format="json")
        return response, prepare

    def test_an_edit_may_keep_names_the_resource_no_longer_allows(self):
        # The key keeps its list as chosen; the resource narrows what it sends.
        self.key.allowed_models = ["mistral", "qwen"]
        self.key.save()
        # Narrowed before this test: the update it sent has settled.
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["mistral"]}
        )

        response, prepare = self.patch(
            {"allowed_models": ["mistral", "qwen"], "limits": {"tokens": 7}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["mistral"])
        self.key.refresh_from_db()
        self.assertEqual(self.key.allowed_models, ["mistral", "qwen"])

    def test_an_edit_cannot_add_a_name_the_resource_does_not_allow(self):
        self.key.allowed_models = ["mistral"]
        self.key.save()
        # Narrowed before this test: the update it sent has settled.
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["mistral"]}
        )
        response, prepare = self.patch({"allowed_models": ["mistral", "qwen"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        prepare.assert_not_called()

    def test_a_model_the_offering_no_longer_lists_cannot_be_added(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["llama-3", "qwen"]}
        )
        models.Offering.objects.filter(pk=self.offering.pk).update(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        response, _ = self.request(["qwen"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The key that holds it may keep it.
        response, prepare = self.patch(
            {"allowed_models": ["llama-3", "qwen"], "limits": {"tokens": 7}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["llama-3"])

    def test_a_resource_with_no_models_allows_a_key_none(self):
        self.select({"models": []})
        response, _ = self.request(["mistral"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_models", response.data)


class EffectiveListTest(SelectionTestBase):
    def test_a_command_carries_the_effective_list_and_the_key_keeps_its_own(self):
        self.select({"models": ["llama-3", "mistral"]})
        response, prepare = self.post(
            self.fixture.owner,
            list_url(),
            {"resource": self.resource.uuid.hex, "allowed_models": ALL_MODELS},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.published_payload(prepare)["allowed_models"], ["llama-3", "mistral"]
        )
        self.assertEqual(response.data["allowed_models"], ALL_MODELS)
        self.assertEqual(response.data["effective_models"], ["llama-3", "mistral"])
        key = models.ResourceApiKey.objects.get(uuid=response.data["uuid"])
        self.assertEqual(key.allowed_models, ALL_MODELS)

    def test_the_listing_the_sweep_reads_has_the_effective_list(self):
        self.key.allowed_models = ["mistral", "qwen"]
        self.key.save()
        self.resource.options = {"models": ["llama-3", "mistral"]}
        self.resource.save()
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(
            list_url(), {"resource_uuid": self.resource.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        self.assertEqual(row["allowed_models"], ["mistral", "qwen"])
        self.assertEqual(row["effective_models"], ["mistral"])

    def test_a_model_the_offering_no_longer_lists_is_not_sent(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        models.Offering.objects.filter(pk=self.offering.pk).update(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        self.client.force_authenticate(self.fixture.owner)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            response = self.client.patch(
                detail_url(self.key), {"limits": {"tokens": 7}}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["llama-3"])
        self.assertEqual(response.data["effective_models"], ["llama-3"])
        self.key.refresh_from_db()
        self.assertEqual(self.key.allowed_models, ["llama-3", "qwen"])

    def test_every_model_stays_every_model_when_the_offering_drops_one(self):
        models.Offering.objects.filter(pk=self.offering.pk).update(
            resource_options=models_options(choices=["llama-3"])
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertEqual(response.data["effective_models"], ALL_MODELS)

    def test_the_effective_list_is_null_without_key_management(self):
        self.offering.plugin_options = {}
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(detail_url(self.key))
        self.assertIsNone(response.data["effective_models"])

    def test_without_key_management_the_selection_narrows_nothing(self):
        # A resume is the one settings command such a key still takes.
        self.offering.plugin_options = {}
        self.offering.save()
        self.resource.options = {"models": []}
        self.resource.save()
        self.key.state = States.PAUSED
        self.key.save()
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ALL_MODELS)


class SelectionChangeTest(SelectionTestBase):
    def ack(self, api_key, action, data=None):
        return self.post(self.fixture.offering_owner, detail_url(api_key, action), data)

    def test_keys_whose_effective_list_changes_get_an_update(self):
        narrowed = self.make_key("cid-2", allowed_models=["mistral"])
        unaffected = self.make_key("cid-3", allowed_models=["llama-3"])
        prepare = self.select({"models": ["llama-3"]})

        payloads = {payload["client_id"]: payload for payload in self.payloads(prepare)}
        self.assertEqual(set(payloads), {"cid-1", "cid-2"})
        self.assertEqual(payloads["cid-1"]["action"], Actions.UPDATE)
        self.assertEqual(payloads["cid-1"]["allowed_models"], ["llama-3"])
        self.assertEqual(payloads["cid-2"]["allowed_models"], [])
        for api_key in (self.key, narrowed):
            api_key.refresh_from_db()
            self.assertEqual(api_key.state, States.UPDATING)
            self.assertEqual(api_key.pending_action, Actions.UPDATE)
        unaffected.refresh_from_db()
        self.assertEqual(unaffected.state, States.OK)
        # The keys keep their own lists.
        self.assertEqual(self.key.allowed_models, ALL_MODELS)

    def test_a_change_of_other_options_sends_nothing(self):
        prepare = self.select({"tier": "gold"})
        prepare.assert_not_called()

    def test_a_paused_or_deleted_key_gets_no_command(self):
        self.key.state = States.PAUSED
        self.key.save()
        deleted = self.make_key(
            "cid-2", state=States.DELETED, allowed_models=ALL_MODELS
        )
        prepare = self.select({"models": []})
        prepare.assert_not_called()
        for api_key in (self.key, deleted):
            api_key.refresh_from_db()
            self.assertFalse(api_key.models_outdated)

    def test_a_paused_key_resumes_with_the_new_list(self):
        self.key.state = States.PAUSED
        self.key.save()
        self.select({"models": ["qwen"]})
        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["qwen"])

    def test_a_key_with_a_command_in_flight_is_updated_once_it_settles(self):
        self.key.set_updating(Actions.ROTATE)
        self.key.save()
        prepare = self.select({"models": ["qwen"]})
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertTrue(self.key.models_outdated)

        response, prepare = self.ack(self.key, "set-key", {"api_key": "sk-new"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.UPDATE)
        self.assertEqual(payload["allowed_models"], ["qwen"])
        self.key.refresh_from_db()
        self.assertEqual(self.key.pending_action, Actions.UPDATE)
        self.assertFalse(self.key.models_outdated)

    def test_an_update_in_flight_with_the_old_list_is_followed_by_another(self):
        self.key.set_updating(Actions.UPDATE)
        self.key.save()
        self.select({"models": ["qwen"]})
        response, prepare = self.ack(self.key, "set-ok")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["qwen"])

    def test_a_requested_key_is_updated_once_created(self):
        requested = models.ResourceApiKey.objects.create(
            resource=self.resource,
            state=States.CREATING,
            pending_action=Actions.CREATE,
            allowed_models=ALL_MODELS,
        )
        self.select({"models": ["qwen"]})
        response, prepare = self.ack(
            requested, "set-key", {"api_key": "sk-new", "client_id": "cid-9"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["client_id"], "cid-9")
        self.assertEqual(payload["allowed_models"], ["qwen"])

    def test_a_command_carrying_settings_clears_the_mark(self):
        self.key.set_updating(Actions.PAUSE)
        self.key.save()
        self.select({"models": ["qwen"]})
        response, prepare = self.ack(self.key, "set-paused")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()

        response, prepare = self.post(
            self.fixture.owner, detail_url(self.key, "resume")
        )
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["qwen"])
        self.key.refresh_from_db()
        self.assertFalse(self.key.models_outdated)
        response, prepare = self.ack(self.key, "set-ok")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        prepare.assert_not_called()

    def test_an_erred_key_is_updated_after_its_failed_command_is_retried(self):
        self.key.set_updating(Actions.ROTATE)
        self.key.set_erred()
        self.key.save()
        self.select({"models": ["qwen"]})
        self.key.refresh_from_db()
        self.assertTrue(self.key.models_outdated)
        self.assertEqual(self.key.state, States.ERRED)

        self.post(self.fixture.owner, detail_url(self.key, "retry"))
        response, prepare = self.ack(self.key, "set-key", {"api_key": "sk-new"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["qwen"])

    def report_created(self, client_id):
        return self.ack_list(
            "report-created",
            {
                "resource": self.resource.uuid.hex,
                "client_id": client_id,
                "api_key": "sk-new",
            },
        )

    def ack_list(self, action, data):
        return self.post(self.fixture.offering_owner, list_url(action), data)

    def test_a_key_reported_on_a_narrowed_resource_is_narrowed_at_once(self):
        self.select({"models": ["qwen"]})
        response, prepare = self.report_created("cid-9")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payload = self.published_payload(prepare)
        self.assertEqual(payload["action"], Actions.UPDATE)
        self.assertEqual(payload["client_id"], "cid-9")
        self.assertEqual(payload["allowed_models"], ["qwen"])
        key = self.resource.api_keys.get(client_id="cid-9")
        self.assertEqual(key.allowed_models, ALL_MODELS)
        self.assertEqual(key.pending_action, Actions.UPDATE)

    def test_a_key_reported_on_a_resource_on_every_model_gets_no_command(self):
        response, prepare = self.report_created("cid-9")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        prepare.assert_not_called()

    def test_a_rotation_reported_again_sends_the_outdated_models(self):
        self.key.set_updating(Actions.ROTATE)
        self.key.save()
        self.select({"models": ["qwen"]})
        response, prepare = self.report_created(self.key.client_id)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.published_payload(prepare)["allowed_models"], ["qwen"])

    def test_concurrent_selections_compare_with_the_stored_one(self):
        # Both edits read the resource before either saved; the second must
        # still see what the first stored, or it leaves the key wider.
        self.select({"models": ["llama-3"]})
        self.key.refresh_from_db()
        self.key.set_ok()
        self.key.save()
        first = models.Resource.objects.get(pk=self.resource.pk)
        second = models.Resource.objects.get(pk=self.resource.pk)
        sent = []
        for resource, value in ((first, ALL_MODELS), (second, ["llama-3"])):
            serializer = serializers.ResourceOptionsSerializer(
                instance=resource, data={"options": {"models": value}}
            )
            serializer.is_valid(raise_exception=True)
            with (
                mock.patch(PREPARE, return_value=MESSAGES) as prepare,
                mock.patch(PUBLISH),
            ):
                serializer.save()
            sent.append(self.payloads(prepare))
        self.assertEqual(sent[0][0]["allowed_models"], ALL_MODELS)
        # The key is busy with the first update, so the second is owed.
        self.key.refresh_from_db()
        self.assertTrue(self.key.models_outdated)

    def test_turning_key_management_on_sends_keys_their_narrowed_models(self):
        self.offering.plugin_options = {}
        self.offering.save()
        self.resource.options = {"models": ["qwen"]}
        self.resource.save()
        unaffected = factories.ResourceFactory(
            offering=self.offering, options={"models": ALL_MODELS}
        )
        models.ResourceApiKey.objects.create(
            resource=unaffected,
            client_id="cid-2",
            state=States.OK,
            allowed_models=ALL_MODELS,
        )
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            self.offering.plugin_options = {"enable_api_key_provisioning": True}
            self.offering.save()
        payloads = self.payloads(prepare)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["client_id"], "cid-1")
        self.assertEqual(payloads[0]["allowed_models"], ["qwen"])

    def test_saving_a_managed_offering_again_sends_nothing(self):
        self.resource.options = {"models": ["qwen"]}
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options=self.resource.options
        )
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            self.offering.plugin_options = {
                "enable_api_key_provisioning": True,
                "create_orders_on_resource_option_change": True,
            }
            self.offering.save()
        prepare.assert_not_called()

    def save_offering(self, **fields):
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            for field, value in fields.items():
                setattr(self.offering, field, value)
            self.offering.save()
        return self.payloads(prepare)

    def test_a_models_option_that_stops_narrowing_widens_the_keys(self):
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["qwen"]}
        )
        payloads = self.save_offering(
            resource_options={
                "options": {"models": {**MODELS_OPTION, "type": "select_string"}},
                "order": ["models"],
            }
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["allowed_models"], ALL_MODELS)

    def test_turning_key_management_off_sends_nothing(self):
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["qwen"]}
        )
        self.assertEqual(self.save_offering(plugin_options={}), [])

    def test_removing_a_model_from_the_offering_updates_the_keys_that_had_it(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        self.make_key("cid-2", allowed_models=["mistral"])
        self.make_key("cid-3", allowed_models=ALL_MODELS)
        narrowed = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            options={"models": ["qwen"]},
        )
        models.ResourceApiKey.objects.create(
            resource=narrowed,
            client_id="cid-4",
            state=States.OK,
            allowed_models=ALL_MODELS,
        )
        payloads = self.save_offering(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        sent = {payload["client_id"]: payload["allowed_models"] for payload in payloads}
        self.assertEqual(sent, {"cid-1": ["llama-3"], "cid-4": []})

    def test_listing_a_model_again_gives_it_back(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        models.Offering.objects.filter(pk=self.offering.pk).update(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        self.offering.refresh_from_db()
        payloads = self.save_offering(resource_options=models_options())
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["allowed_models"], ["llama-3", "qwen"])

    def test_turning_key_management_on_drops_models_no_longer_listed(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        self.offering.plugin_options = {}
        self.offering.save()
        self.save_offering(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        payloads = self.save_offering(
            plugin_options={"enable_api_key_provisioning": True}
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["allowed_models"], ["llama-3"])

    def test_removing_a_model_without_key_management_sends_nothing(self):
        self.key.allowed_models = ["llama-3", "qwen"]
        self.key.save()
        self.offering.plugin_options = {}
        self.offering.save()
        payloads = self.save_offering(
            resource_options=models_options(choices=["llama-3", "mistral"])
        )
        self.assertEqual(payloads, [])

    def test_a_reordered_selection_sends_nothing(self):
        self.select({"models": ["llama-3", "qwen"]})
        self.key.refresh_from_db()
        self.key.set_ok()
        self.key.save()
        prepare = self.select({"models": ["qwen", "llama-3"]})
        prepare.assert_not_called()

    def test_the_hourly_task_sends_settled_outdated_keys(self):
        models.Resource.objects.filter(pk=self.resource.pk).update(
            options={"models": ["qwen"]}
        )
        busy = self.make_key(
            "cid-2", state=States.UPDATING, pending_action=Actions.ROTATE
        )
        models.ResourceApiKey.objects.update(models_outdated=True)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            tasks.send_outdated_api_key_models()
        payload = self.published_payload(prepare)
        self.assertEqual(payload["client_id"], "cid-1")
        self.assertEqual(payload["allowed_models"], ["qwen"])
        self.key.refresh_from_db()
        self.assertFalse(self.key.models_outdated)
        busy.refresh_from_db()
        self.assertTrue(busy.models_outdated)

    def test_the_hourly_task_leaves_keys_without_key_management_marked(self):
        # They are sent their models once key management is turned on again.
        self.offering.plugin_options = {}
        self.offering.save()
        models.ResourceApiKey.objects.update(models_outdated=True)
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
            mock.patch.object(
                tasks.resource_api_keys,
                "send_outdated_models",
                wraps=tasks.resource_api_keys.send_outdated_models,
            ) as send,
        ):
            tasks.send_outdated_api_key_models()
        send.assert_not_called()
        prepare.assert_not_called()
        self.key.refresh_from_db()
        self.assertTrue(self.key.models_outdated)

    def test_a_change_through_an_order_updates_the_keys(self):
        self.offering.plugin_options = {
            "enable_api_key_provisioning": True,
            "create_orders_on_resource_option_change": True,
        }
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "update_options"),
            {"options": {"models": ["mistral"]}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        order.set_state_executing()
        order.save()
        with (
            mock.patch(PREPARE, return_value=MESSAGES) as prepare,
            mock.patch(PUBLISH),
        ):
            utils.process_order(order, self.fixture.owner)
        key_payloads = [
            payload for payload in self.payloads(prepare) if "api_key_uuid" in payload
        ]
        self.assertEqual(len(key_payloads), 1)
        self.assertEqual(key_payloads[0]["allowed_models"], ["mistral"])


# pytest may run with --no-migrations, which leaves the loader without them.
@override_settings(MIGRATION_MODULES={})
class SelectionMigrationTest(test.APITestCase):
    def test_a_resource_without_a_selection_is_put_on_every_model(self):
        offering = factories.OfferingFactory()
        offer_models(offering)
        unset = factories.ResourceFactory(offering=offering, options=None)
        empty = factories.ResourceFactory(offering=offering, options={"models": []})
        chosen = factories.ResourceFactory(
            offering=offering, options={"models": ["mistral"], "tier": "gold"}
        )
        unmanaged_offering = factories.OfferingFactory()
        offer_models(unmanaged_offering, managed=False)
        unmanaged = factories.ResourceFactory(
            offering=unmanaged_offering, options={"models": []}
        )
        single_offering = factories.OfferingFactory()
        offer_models(single_offering, option_type="select_string")
        single = factories.ResourceFactory(offering=single_offering, options=None)
        apps = MigrationLoader(None).project_state().apps
        selection_migration.empty_to_all_models(apps, None)

        for resource, expected in (
            (unset, {"models": ALL_MODELS}),
            (empty, {"models": ALL_MODELS}),
            (chosen, {"models": ["mistral"], "tier": "gold"}),
            (unmanaged, {"models": ALL_MODELS}),
            (single, None),
        ):
            resource.refresh_from_db()
            self.assertEqual(resource.options, expected)

    def test_keys_wider_than_their_resource_are_marked(self):
        offering = factories.OfferingFactory()
        offer_models(offering)
        narrowed = factories.ResourceFactory(
            offering=offering, options={"models": ["mistral"]}
        )
        open_resource = factories.ResourceFactory(
            offering=offering, options={"models": ALL_MODELS}
        )
        unmanaged_offering = factories.OfferingFactory()
        offer_models(unmanaged_offering, managed=False)
        unmanaged = factories.ResourceFactory(
            offering=unmanaged_offering, options={"models": ["mistral"]}
        )

        def key(resource, client_id, allowed_models, state=States.OK):
            return models.ResourceApiKey.objects.create(
                resource=resource,
                client_id=client_id,
                state=state,
                allowed_models=allowed_models,
            )

        marked = [
            key(narrowed, "cid-1", ALL_MODELS),
            key(narrowed, "cid-2", ["mistral", "qwen"]),
        ]
        retiring = factories.OfferingFactory()
        retiring.resource_options = models_options(
            option_type="select_string", choices=["llama-3", "mistral"]
        )
        retiring.plugin_options = {"enable_api_key_provisioning": True}
        retiring.save()
        on_retiring = factories.ResourceFactory(offering=retiring, options=None)
        marked.append(key(on_retiring, "cid-8", ["llama-3", "qwen"]))
        unmarked = [
            key(on_retiring, "cid-9", ["llama-3"]),
            key(narrowed, "cid-3", ["mistral"]),
            key(narrowed, "cid-4", []),
            key(narrowed, "cid-5", ALL_MODELS, state=States.DELETED),
            key(open_resource, "cid-6", ALL_MODELS),
            key(unmanaged, "cid-7", ALL_MODELS),
        ]
        apps = MigrationLoader(None).project_state().apps
        outdated_migration.mark_keys_wider_than_their_resource(apps, None)
        for api_key in marked:
            api_key.refresh_from_db()
            self.assertTrue(api_key.models_outdated, api_key.client_id)
        for api_key in unmarked:
            api_key.refresh_from_db()
            self.assertFalse(api_key.models_outdated, api_key.client_id)

    def test_reversed_every_model_is_empty_again(self):
        offering = factories.OfferingFactory()
        offer_models(offering)
        resource = factories.ResourceFactory(
            offering=offering, options={"models": ALL_MODELS}
        )
        apps = MigrationLoader(None).project_state().apps
        selection_migration.all_models_to_empty(apps, None)
        resource.refresh_from_db()
        self.assertEqual(resource.options, {"models": []})
