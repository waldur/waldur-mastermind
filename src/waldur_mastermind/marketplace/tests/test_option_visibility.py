import copy

import yaml
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import enums, models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest
from waldur_mastermind.marketplace_support import utils as support_utils

ACCOUNT_CHOICES = ["Own account", "Request a new account on behalf of the project"]

# The motivating case: each backup kind is a boolean, and only when it is
# ticked does the offering ask which storage account to use for it.
BACKUP_OPTIONS = {
    "order": [
        "etcd_snapshots",
        "etcd_snapshots_account",
        "velero_backups",
        "velero_backups_account",
        "longhorn_backups",
        "longhorn_backups_account",
    ],
    "options": {
        **{
            kind: {"type": "boolean", "label": kind, "required": False}
            for kind in ("etcd_snapshots", "velero_backups", "longhorn_backups")
        },
        **{
            f"{kind}_account": {
                "type": "select_string",
                "label": f"{kind} storage account",
                "choices": ACCOUNT_CHOICES,
                "required": True,
                "visible_if": {"field": kind, "values": [True]},
            }
            for kind in ("etcd_snapshots", "velero_backups", "longhorn_backups")
        },
    },
}

# A follow-up that is asked only while a box is left unticked.
UNCHECKED_OPTIONS = {
    "order": ["backups", "no_backups_reason"],
    "options": {
        "backups": {"type": "boolean", "label": "Backups"},
        "no_backups_reason": {
            "type": "select_string",
            "label": "Why no backups?",
            "choices": ["Not needed", "Handled elsewhere"],
            "required": True,
            "visible_if": {"field": "backups", "values": [False]},
        },
    },
}

# A controls B and B controls C; C is also limited by a cross-field validator.
CHAIN_OPTIONS = {
    "order": ["features", "tier", "size", "min_size"],
    "options": {
        "features": {
            "type": "select_string_multi",
            "label": "Features",
            "choices": ["backup", "monitoring", "logging"],
        },
        "tier": {
            "type": "select_string",
            "label": "Tier",
            "choices": ["basic", "premium"],
            "required": True,
            "visible_if": {"field": "features", "values": ["backup", "logging"]},
        },
        "size": {
            "type": "integer",
            "label": "Size",
            "required": True,
            "visible_if": {"field": "tier", "values": ["premium"]},
            "validators": [{"type": "gte", "target_field": "min_size"}],
        },
        "min_size": {"type": "integer", "label": "Minimum size"},
    },
}


class OfferingVisibleIfSaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

    def create_offering(self, options, field="options"):
        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": enums.SUPPORT_OFFERING,
            field: options,
        }
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def options_with_rule(self, rule, order=None):
        options = copy.deepcopy(BACKUP_OPTIONS)
        options["options"]["velero_backups_account"]["visible_if"] = rule
        if order is not None:
            options["order"] = order
        return options

    def assert_rejected(self, options):
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options", response.data)

    def test_rule_is_saved_and_returned(self):
        response = self.create_offering(BACKUP_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.options["options"]["velero_backups_account"]["visible_if"],
            {"field": "velero_backups", "values": [True]},
        )
        self.assertEqual(
            response.data["options"]["options"]["velero_backups_account"]["visible_if"],
            {"field": "velero_backups", "values": [True]},
        )

    def test_chain_with_multi_select_parent_is_saved(self):
        response = self.create_offering(CHAIN_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_rule_is_saved_for_resource_options(self):
        response = self.create_offering(BACKUP_OPTIONS, field="resource_options")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.resource_options["options"]["etcd_snapshots_account"][
                "visible_if"
            ],
            {"field": "etcd_snapshots", "values": [True]},
        )

    def test_resource_options_rule_is_validated(self):
        options = self.options_with_rule({"field": "unknown", "values": [True]})
        response = self.create_offering(options, field="resource_options")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resource_options", response.data)

    def test_unknown_field_is_rejected(self):
        self.assert_rejected(
            self.options_with_rule({"field": "unknown", "values": [True]})
        )

    def test_field_later_in_order_is_rejected(self):
        order = list(BACKUP_OPTIONS["order"])
        order.remove("velero_backups")
        order.append("velero_backups")
        self.assert_rejected(
            self.options_with_rule(
                {"field": "velero_backups", "values": [True]}, order=order
            )
        )

    def test_field_missing_from_order_is_rejected(self):
        order = [key for key in BACKUP_OPTIONS["order"] if key != "velero_backups"]
        self.assert_rejected(
            self.options_with_rule(
                {"field": "velero_backups", "values": [True]}, order=order
            )
        )

    def test_self_reference_is_rejected(self):
        self.assert_rejected(
            self.options_with_rule(
                {"field": "velero_backups_account", "values": ["Own account"]}
            )
        )

    def test_unsupported_field_type_is_rejected(self):
        options = copy.deepcopy(CHAIN_OPTIONS)
        options["order"] = ["min_size", "features", "tier", "size"]
        options["options"]["tier"]["visible_if"] = {
            "field": "min_size",
            "values": ["1"],
        }
        self.assert_rejected(options)

    def test_empty_values_are_rejected(self):
        self.assert_rejected(
            self.options_with_rule({"field": "velero_backups", "values": []})
        )

    def test_missing_values_are_rejected(self):
        self.assert_rejected(self.options_with_rule({"field": "velero_backups"}))

    def test_string_value_for_boolean_field_is_rejected(self):
        self.assert_rejected(
            self.options_with_rule({"field": "velero_backups", "values": ["true"]})
        )

    def test_unlisted_choice_is_rejected(self):
        options = copy.deepcopy(CHAIN_OPTIONS)
        options["options"]["tier"]["visible_if"]["values"] = ["backup", "storage"]
        self.assert_rejected(options)

    def test_boolean_value_for_select_field_is_rejected(self):
        options = copy.deepcopy(CHAIN_OPTIONS)
        options["options"]["size"]["visible_if"]["values"] = [True]
        self.assert_rejected(options)

    def test_non_scalar_value_is_rejected(self):
        self.assert_rejected(
            self.options_with_rule({"field": "velero_backups", "values": [{}]})
        )


class OrderVisibleIfTest(BaseOrderCreateTest):
    def create_offering(self, options, resource_options=None):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=copy.deepcopy(options),
            resource_options=copy.deepcopy(
                resource_options or {"options": {}, "order": []}
            ),
        )

    def order(self, offering, attributes):
        return self.create_order(
            self.fixture.staff,
            offering,
            add_payload={"attributes": attributes},
        )

    def stored_order(self, response):
        return models.Order.objects.get(uuid=response.data["uuid"])

    def test_hidden_required_option_may_be_omitted(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(offering, {"velero_backups": False})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_options_without_parent_value_are_hidden(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(offering, {"name": "cluster"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_omitted_boolean_counts_as_unchecked(self):
        offering = self.create_offering(UNCHECKED_OPTIONS)
        response = self.order(offering, {"name": "cluster"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no_backups_reason", response.data)

        response = self.order(
            offering, {"name": "cluster", "no_backups_reason": "Not needed"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.stored_order(response).attributes,
            {"name": "cluster", "no_backups_reason": "Not needed"},
        )

    def test_ticked_boolean_hides_unchecked_dependent(self):
        offering = self.create_offering(UNCHECKED_OPTIONS)
        response = self.order(
            offering, {"backups": True, "no_backups_reason": "Not needed"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.stored_order(response).attributes, {"backups": True})

    def test_visible_required_option_must_be_given(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(offering, {"velero_backups": True})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("velero_backups_account", str(response.data))

    def test_visible_option_is_validated_and_stored(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        attributes = {
            "velero_backups": True,
            "velero_backups_account": "Own account",
            "longhorn_backups": False,
        }
        response = self.order(offering, attributes)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = self.stored_order(response)
        self.assertEqual(order.attributes, attributes)
        self.assertEqual(order.resource.attributes, attributes)

    def test_visible_option_with_invalid_value_is_rejected(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(
            offering,
            {"velero_backups": True, "velero_backups_account": "Somebody else's"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_value_of_hidden_option_is_accepted_and_not_stored(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(
            offering,
            {
                "name": "cluster",
                "velero_backups": False,
                "velero_backups_account": "Own account",
                "etcd_snapshots_account": "not even a valid choice",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = self.stored_order(response)
        expected = {"name": "cluster", "velero_backups": False}
        self.assertEqual(order.attributes, expected)
        self.assertEqual(order.resource.attributes, expected)
        self.assertEqual(response.data["attributes"], expected)

    def test_hidden_options_have_no_line_in_ticket_description(self):
        offering = self.create_offering(BACKUP_OPTIONS)
        response = self.order(
            offering,
            {
                "velero_backups": True,
                "velero_backups_account": "Own account",
                "longhorn_backups": False,
                "longhorn_backups_account": "Own account",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        description = support_utils.format_create_description(
            self.stored_order(response)
        )
        self.assertIn("velero_backups storage account: 'Own account'", description)
        self.assertNotIn("longhorn_backups storage account", description)
        self.assertNotIn("etcd_snapshots storage account", description)

    def test_multi_select_parent_shows_option_when_any_value_matches(self):
        offering = self.create_offering(CHAIN_OPTIONS)
        response = self.order(offering, {"features": ["monitoring", "logging"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("tier", response.data)

        response = self.order(offering, {"features": ["monitoring"], "tier": "basic"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.stored_order(response).attributes, {"features": ["monitoring"]}
        )

    def test_hiding_cascades_down_a_chain(self):
        offering = self.create_offering(CHAIN_OPTIONS)
        # tier is hidden, so size is hidden even though its rule would match.
        response = self.order(
            offering, {"features": [], "tier": "premium", "size": 1, "min_size": 5}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.stored_order(response).attributes, {"features": [], "min_size": 5}
        )

        response = self.order(offering, {"features": ["backup"], "tier": "premium"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("size", response.data)

    def test_cross_field_validator_is_skipped_for_hidden_option(self):
        offering = self.create_offering(CHAIN_OPTIONS)
        response = self.order(
            offering,
            {"features": ["backup"], "tier": "basic", "size": 1, "min_size": 5},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        response = self.order(
            offering,
            {"features": ["backup"], "tier": "premium", "size": 1, "min_size": 5},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("size", response.data)

    def test_cross_field_validator_is_skipped_when_target_is_hidden(self):
        options = copy.deepcopy(CHAIN_OPTIONS)
        options["order"] = ["features", "tier", "min_size", "size"]
        options["options"]["size"] = {"type": "integer", "label": "Size"}
        options["options"]["min_size"]["visible_if"] = {
            "field": "tier",
            "values": ["premium"],
        }
        options["options"]["size"]["validators"] = [
            {"type": "gte", "target_field": "min_size"}
        ]
        offering = self.create_offering(options)
        response = self.order(
            offering,
            {"features": ["backup"], "tier": "basic", "size": 1, "min_size": 5},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("min_size", self.stored_order(response).attributes)

    def test_hidden_resource_options_are_not_stored_on_resource(self):
        offering = self.create_offering({}, resource_options=BACKUP_OPTIONS)
        response = self.order(
            offering,
            {
                "velero_backups": True,
                "velero_backups_account": "Own account",
                "longhorn_backups": False,
                "longhorn_backups_account": "Own account",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.stored_order(response).resource.options,
            {
                "velero_backups": True,
                "velero_backups_account": "Own account",
                "longhorn_backups": False,
            },
        )


class OrderUpdateVisibleIfTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            customer=self.fixture.customer,
            options=copy.deepcopy(BACKUP_OPTIONS),
        )
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=offering,
            plan=factories.PlanFactory(offering=offering),
            state=OrderStates.PENDING_CONSUMER,
            attributes={
                "velero_backups": True,
                "velero_backups_account": "Own account",
            },
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def test_update_drops_values_of_hidden_options(self):
        response = self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {
                "attributes": {
                    "name": "cluster",
                    "velero_backups": False,
                    "velero_backups_account": "Own account",
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes, {"name": "cluster", "velero_backups": False}
        )

    def test_update_keeps_values_of_visible_options(self):
        attributes = {"velero_backups": True, "velero_backups_account": "Own account"}
        response = self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, attributes)


class OrderApproveByProviderVisibleIfTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(BACKUP_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.options = {
            "velero_backups": True,
            "velero_backups_account": "Own account",
        }
        self.resource.save()
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.fixture.offering,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={
                "old_options": dict(self.resource.options),
                "new_options": {"velero_backups": False},
            },
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        self.url = factories.OrderFactory.get_url(self.order, "approve_by_provider")

    def approve(self, new_options):
        return self.client.post(
            self.url, {"attributes": {"new_options": new_options}}, format="json"
        )

    def test_values_of_options_hidden_by_new_values_are_dropped(self):
        response = self.approve(
            {"velero_backups": False, "velero_backups_account": "not a choice"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes["new_options"], {"velero_backups": False}
        )

    def test_visibility_uses_the_stored_resource_options(self):
        # velero_backups is ticked on the resource, so the account is visible
        # and its value is validated and kept.
        response = self.approve({"velero_backups_account": "not a choice"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        new_options = {
            "velero_backups_account": "Request a new account on behalf of the project"
        }
        response = self.approve(new_options)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["new_options"], new_options)


class ResourceOptionsVisibleIfTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(BACKUP_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.options = {
            "velero_backups": True,
            "velero_backups_account": "Own account",
        }
        self.resource.save()
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)

    def update(self, options):
        return self.client.post(self.url, {"options": options}, format="json")

    def test_hiding_an_option_drops_its_stored_value(self):
        response = self.update({"velero_backups": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"velero_backups": False})

    def test_value_for_visible_option_is_validated_against_stored_parent(self):
        response = self.update({"velero_backups_account": "Somebody else's"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.update(
            {
                "velero_backups_account": (
                    "Request a new account on behalf of the project"
                )
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options["velero_backups_account"],
            "Request a new account on behalf of the project",
        )

    def test_value_for_hidden_option_is_not_stored(self):
        response = self.update(
            {"etcd_snapshots": False, "etcd_snapshots_account": "invalid"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options,
            {
                "velero_backups": True,
                "velero_backups_account": "Own account",
                "etcd_snapshots": False,
            },
        )

    def test_order_for_option_change_drops_hidden_values(self):
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()
        response = self.update({"velero_backups": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.attributes["new_options"], {"velero_backups": False})

        order.set_state_executing()
        order.save()
        marketplace_utils.process_order(order, self.fixture.owner)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"velero_backups": False})

    def test_partial_order_for_option_change_drops_hidden_values(self):
        # An order created elsewhere may carry only the changed options.
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.fixture.offering,
            type=enums.OrderTypes.UPDATE,
            state=enums.OrderStates.EXECUTING,
            attributes={"new_options": {"velero_backups": False}},
        )
        marketplace_utils.process_order(order, self.fixture.owner)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"velero_backups": False})


class OfferingVisibleIfExportImportTest(test.APITestCase):
    def test_rule_survives_export_and_import(self):
        fixture = fixtures.MarketplaceFixture()
        user = fixture.owner
        category = factories.CategoryFactory(title="Visible if category")
        offering = fixture.offering
        offering.category = category
        offering.customer = fixture.customer
        offering.options = copy.deepcopy(BACKUP_OPTIONS)
        offering.resource_options = copy.deepcopy(CHAIN_OPTIONS)
        offering.save()
        fixture.customer.add_user(user, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        offering.add_user(user, OfferingRole.MANAGER)
        self.client.force_authenticate(user)

        response = self.client.post(
            factories.OfferingFactory.get_url(offering, "export_offering"),
            {"include_options": True, "include_resource_options": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        export_data = response.data["export_data"]
        export_data["offering"]["name"] = "Imported visible_if offering"

        response = self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": fixture.customer.uuid.hex,
                "category": category.title,
                "offering_data": yaml.safe_dump(export_data),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        imported = models.Offering.objects.get(name="Imported visible_if offering")
        self.assertEqual(imported.options, BACKUP_OPTIONS)
        self.assertEqual(imported.resource_options, CHAIN_OPTIONS)
