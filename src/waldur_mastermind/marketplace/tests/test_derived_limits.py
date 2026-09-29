import copy
import json
from fractions import Fraction

from ddt import data, ddt, unpack
from django.test import SimpleTestCase
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import derived_limits, models, plugins, processors
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import utils as test_utils
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

STORAGE_COMPONENTS = ("data_primary", "wal_primary", "data_replica", "wal_replica")

# Net database storage entered once; the gross storage layout and a full
# backup covering all of it are derived.
DATABASE_OPTIONS = {
    "order": ["storage", "backup"],
    "options": {
        "storage": {
            "type": "component_formula",
            "label": "Required database storage",
            "required": True,
            "min": 0,
            "max": 5000,
            "component_formula_config": {
                "targets": [
                    {"component_type": "data_primary", "formula": "input * 2"},
                    {"component_type": "wal_primary", "formula": "input * 2 * 0.25"},
                    {"component_type": "data_replica", "formula": "input * 2"},
                    {"component_type": "wal_replica", "formula": "input * 2 * 0.25"},
                ]
            },
        },
        "backup": {
            "type": "component_sum",
            "label": "Daily full backup",
            "component_sum_config": {
                "target_component": "backup",
                "components": list(STORAGE_COMPONENTS),
            },
        },
    },
}


DERIVED_AT_200 = {
    "data_primary": 400,
    "wal_primary": 100,
    "data_replica": 400,
    "wal_replica": 100,
    "backup": 1000,
}


# Lets the customer change the net storage after ordering.
PAIRED_RESOURCE_OPTIONS = {
    "order": ["storage"],
    "options": {
        "storage": {
            "type": "component_formula",
            "label": "Required database storage",
            "min": 0,
            "max": 5000,
        }
    },
}


def database_options(**overrides):
    options = copy.deepcopy(DATABASE_OPTIONS)
    for name, option in overrides.items():
        options["options"][name] = option
    return options


@ddt
class FormulaTest(SimpleTestCase):
    @data(
        ("input * 2", 200, "400"),
        ("input * 2 * 0.25", 200, "100"),
        ("(input + 10) / 4", 10, "5"),
        ("-input + 3", 1, "2"),
        ("2 - -input", 1, "3"),
        ("input * 0.1", 3, "0.3"),
        (".5 * input", 4, "2"),
        ("  input  ", 7, "7"),
        ("1 + 2 * 3", 0, "7"),
        # Exact: Decimal would give 2.0000000000000000000000000001 here.
        ("input * 2 / 3 * 3", 1, "2"),
    )
    @unpack
    def test_valid_formula(self, formula, value, expected):
        node = derived_limits.parse_formula(formula)
        self.assertEqual(
            derived_limits.evaluate_formula(node, Fraction(value)), Fraction(expected)
        )

    @data(
        "",
        "   ",
        "input *",
        "input input",
        "2input",
        "inputs",
        "(input",
        "input)",
        "__import__('os')",
        "input ** 2",
        "min(input, 2)",
        "input % 2",
        "1e3",
        "x",
        "(" * 40 + "input" + ")" * 40,
        "1+" * 200 + "1",
    )
    def test_invalid_formula(self, formula):
        with self.assertRaises(derived_limits.FormulaError):
            derived_limits.parse_formula(formula)

    def test_division_by_zero(self):
        node = derived_limits.parse_formula("10 / input")
        with self.assertRaises(derived_limits.FormulaError):
            derived_limits.evaluate_formula(node, Fraction(0))


class OfferingDerivedOptionsSaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

    def components(self, types=(*STORAGE_COMPONENTS, "backup"), billing_type=None):
        return [
            {
                "type": component_type,
                "name": component_type,
                "measured_unit": "GB",
                "billing_type": billing_type or BillingTypes.LIMIT,
            }
            for component_type in types
        ]

    def create_offering(self, options, components=None, **extra):
        payload = {
            "name": "database",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": SUPPORT_OFFERING,
            "components": self.components() if components is None else components,
            "options": options,
            **extra,
        }
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def assert_rejected(self, options, **kwargs):
        response = self.create_offering(options, **kwargs)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        return response

    def test_derived_options_are_saved(self):
        response = self.create_offering(DATABASE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.options["options"]["storage"]["component_formula_config"],
            DATABASE_OPTIONS["options"]["storage"]["component_formula_config"],
        )

    def test_invalid_formula_is_rejected(self):
        options = database_options()
        targets = options["options"]["storage"]["component_formula_config"]["targets"]
        targets[0]["formula"] = "input ** 2"
        self.assert_rejected(options)

    def test_config_is_required(self):
        options = database_options(
            storage={"type": "component_formula", "label": "Storage"}
        )
        self.assert_rejected(options)

    def test_unknown_component_is_rejected(self):
        response = self.assert_rejected(
            DATABASE_OPTIONS, components=self.components(STORAGE_COMPONENTS)
        )
        self.assertIn("backup", str(response.data))

    def test_usage_component_is_rejected(self):
        components = self.components(STORAGE_COMPONENTS) + self.components(
            ["backup"], billing_type=BillingTypes.USAGE
        )
        self.assert_rejected(DATABASE_OPTIONS, components=components)

    def test_component_derived_twice_is_rejected(self):
        options = database_options()
        options["options"]["backup"]["component_sum_config"]["target_component"] = (
            "data_primary"
        )
        options["options"]["backup"]["component_sum_config"]["components"] = [
            "wal_primary"
        ]
        self.assert_rejected(options)

    def test_sum_into_itself_is_rejected(self):
        options = database_options()
        options["options"]["backup"]["component_sum_config"]["components"].append(
            "backup"
        )
        self.assert_rejected(options)

    def test_cycle_between_sums_is_rejected(self):
        options = {
            "order": ["a", "b"],
            "options": {
                "a": {
                    "type": "component_sum",
                    "label": "a",
                    "component_sum_config": {
                        "target_component": "backup",
                        "components": ["data_primary"],
                    },
                },
                "b": {
                    "type": "component_sum",
                    "label": "b",
                    "component_sum_config": {
                        "target_component": "data_primary",
                        "components": ["backup"],
                    },
                },
            },
        }
        response = self.assert_rejected(options)
        self.assertIn("cycle", str(response.data))

    def test_derived_option_cannot_be_a_resource_option(self):
        self.assert_rejected(
            {"order": [], "options": {}}, resource_options=DATABASE_OPTIONS
        )

    def test_paired_resource_option_takes_formulas_and_bounds_from_order_option(self):
        resource_options = {
            "order": ["storage"],
            "options": {
                "storage": {
                    "type": "component_formula",
                    "label": "Storage",
                    "min": 1,
                    "component_formula_config": {
                        "targets": [{"component_type": "backup", "formula": "input"}]
                    },
                }
            },
        }
        response = self.create_offering(
            DATABASE_OPTIONS, resource_options=resource_options
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.resource_options["options"]["storage"],
            {
                "type": "component_formula",
                "label": "Storage",
                "required": False,
                "min": 0,
                "max": 5000,
            },
        )

    def test_resource_formula_without_order_option_is_rejected(self):
        resource_options = {
            "order": ["other"],
            "options": {"other": {"type": "component_formula", "label": "Other"}},
        }
        response = self.assert_rejected(
            DATABASE_OPTIONS, resource_options=resource_options
        )
        self.assertIn("resource_options", response.data)

    def test_paired_resource_option_follows_order_option_bounds(self):
        offering = factories.OfferingFactory(
            customer=self.customer,
            options=copy.deepcopy(DATABASE_OPTIONS),
            resource_options=copy.deepcopy(PAIRED_RESOURCE_OPTIONS),
        )
        for component_type in (*STORAGE_COMPONENTS, "backup"):
            factories.OfferingComponentFactory(
                offering=offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
            )
        options = copy.deepcopy(DATABASE_OPTIONS)
        options["options"]["storage"]["max"] = 10000
        url = factories.OfferingFactory.get_url(offering, "update_options")
        response = self.client.post(url, {"options": options}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(offering.resource_options["options"]["storage"]["max"], 10000)

    def test_order_option_used_by_resource_option_cannot_be_removed(self):
        offering = factories.OfferingFactory(
            customer=self.customer,
            options=copy.deepcopy(DATABASE_OPTIONS),
            resource_options=copy.deepcopy(PAIRED_RESOURCE_OPTIONS),
        )
        for component_type in (*STORAGE_COMPONENTS, "backup"):
            factories.OfferingComponentFactory(
                offering=offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
            )
        options = copy.deepcopy(DATABASE_OPTIONS)
        del options["options"]["storage"]
        options["order"].remove("storage")
        del options["options"]["backup"]
        options["order"].remove("backup")
        url = factories.OfferingFactory.get_url(offering, "update_options")
        response = self.client.post(url, {"options": options}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_options_checks_components_of_offering(self):
        offering = factories.OfferingFactory(customer=self.customer)
        for component_type in STORAGE_COMPONENTS:
            factories.OfferingComponentFactory(
                offering=offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
            )
        url = factories.OfferingFactory.get_url(offering, "update_options")

        response = self.client.post(url, {"options": DATABASE_OPTIONS}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        factories.OfferingComponentFactory(
            offering=offering, type="backup", billing_type=BillingTypes.LIMIT
        )
        response = self.client.post(url, {"options": DATABASE_OPTIONS}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class DerivedLimitsOrderMixin:
    def build_offering(self, options=None, **component_kwargs):
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            type=SUPPORT_OFFERING,
            options=copy.deepcopy(options or DATABASE_OPTIONS),
        )
        plan = factories.PlanFactory(offering=offering)
        for component_type in (*STORAGE_COMPONENTS, "backup", "extra"):
            component = models.OfferingComponent.objects.create(
                offering=offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
                **component_kwargs.get(component_type, {}),
            )
            factories.PlanComponentFactory(plan=plan, component=component, price=1)
        return offering, plan


class OrderDerivedLimitsTest(DerivedLimitsOrderMixin, BaseOrderCreateTest):
    def order(self, offering, plan, attributes, limits=None):
        payload = {
            "plan": factories.PlanFactory.get_public_url(plan),
            "attributes": attributes,
        }
        if limits is not None:
            payload["limits"] = limits
        return self.create_order(self.fixture.staff, offering, add_payload=payload)

    def stored_limits(self, response):
        return models.Order.objects.get(uuid=response.data["uuid"]).limits

    def test_limits_are_derived_from_input(self):
        offering, plan = self.build_offering()
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.stored_limits(response),
            {
                "data_primary": 400,
                "wal_primary": 100,
                "data_replica": 400,
                "wal_replica": 100,
                "backup": 1000,
            },
        )

    def test_derived_limits_are_priced(self):
        offering, plan = self.build_offering()
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        plain_offering, plain_plan = self.build_offering(
            options={"order": [], "options": {}}
        )
        plain = self.order(plain_offering, plain_plan, {}, limits=dict(order.limits))
        self.assertEqual(plain.status_code, status.HTTP_201_CREATED, plain.data)
        self.assertEqual(
            order.cost, models.Order.objects.get(uuid=plain.data["uuid"]).cost
        )
        self.assertGreater(order.cost, 0)

    def test_client_values_for_derived_limits_are_replaced(self):
        offering, plan = self.build_offering()
        response = self.order(
            offering,
            plan,
            {"storage": 200},
            limits={"data_primary": 1, "backup": 1, "extra": 5},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        limits = self.stored_limits(response)
        self.assertEqual(limits["data_primary"], 400)
        self.assertEqual(limits["backup"], 1000)
        self.assertEqual(limits["extra"], 5)

    def test_value_sent_for_sum_option_is_dropped(self):
        offering, plan = self.build_offering()
        response = self.order(offering, plan, {"storage": 200, "backup": 1})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.attributes, {"storage": 200})

    def test_input_is_bounded_by_option_min_and_max(self):
        offering, plan = self.build_offering()
        response = self.order(offering, plan, {"storage": 5001})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_result_is_rounded_up_to_component_precision(self):
        options = database_options()
        targets = options["options"]["storage"]["component_formula_config"]["targets"]
        targets[1]["formula"] = "input / 3"
        targets[3]["formula"] = "input / 3"
        offering, plan = self.build_offering(
            options, wal_replica={"limit_decimal_places": 1}
        )
        response = self.order(offering, plan, {"storage": 10})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        limits = self.stored_limits(response)
        self.assertEqual(limits["wal_primary"], 4)
        self.assertEqual(limits["wal_replica"], 3.4)
        self.assertEqual(limits["backup"], 20 + 4 + 20 + 4)

    def test_exact_result_is_not_rounded_up(self):
        options = database_options()
        targets = options["options"]["storage"]["component_formula_config"]["targets"]
        targets[0]["formula"] = "input * 2 / 3 * 3"
        offering, plan = self.build_offering(options)
        response = self.order(offering, plan, {"storage": 1})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.stored_limits(response)["data_primary"], 2)

    def test_result_outside_component_bounds_names_component(self):
        offering, plan = self.build_offering(backup={"max_value": 500})
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("1000", str(response.data))

    def test_division_by_zero_is_a_validation_error(self):
        options = database_options()
        targets = options["options"]["storage"]["component_formula_config"]["targets"]
        targets[0]["formula"] = "100 / input"
        offering, plan = self.build_offering(options)
        response = self.order(offering, plan, {"storage": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", response.data)

    def test_ordered_input_is_copied_to_paired_resource_option(self):
        offering, plan = self.build_offering()
        offering.resource_options = copy.deepcopy(PAIRED_RESOURCE_OPTIONS)
        offering.save()
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.options, {"storage": 200})

    def test_option_default_is_used_when_input_is_omitted(self):
        options = database_options()
        options["options"]["storage"].update(required=False, default="50")
        offering, plan = self.build_offering(options)
        response = self.order(offering, plan, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        limits = self.stored_limits(response)
        self.assertEqual(limits["data_primary"], 100)
        self.assertEqual(limits["backup"], 250)

    def test_target_no_longer_limit_billed_refuses_the_order(self):
        offering, plan = self.build_offering()
        models.OfferingComponent.objects.filter(
            offering=offering, type="backup"
        ).update(billing_type=BillingTypes.USAGE)
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("backup", str(response.data))

    def test_sum_includes_components_the_customer_entered(self):
        options = database_options()
        options["options"]["backup"]["component_sum_config"]["components"].append(
            "extra"
        )
        offering, plan = self.build_offering(options)
        response = self.order(offering, plan, {"storage": 200}, limits={"extra": 7})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.stored_limits(response)["backup"], 1007)

    def test_sum_reads_another_sum(self):
        options = database_options()
        options["order"].append("total")
        options["options"]["total"] = {
            "type": "component_sum",
            "label": "Total",
            "component_sum_config": {
                "target_component": "extra",
                "components": ["backup", "data_primary"],
            },
        }
        offering, plan = self.build_offering(options)
        response = self.order(offering, plan, {"storage": 200})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.stored_limits(response)["extra"], 1400)


@ddt
class OrderInputValidationTest(DerivedLimitsOrderMixin, test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        offering, plan = self.build_offering()
        offering.customer = self.fixture.customer
        offering.save()
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=offering,
            plan=plan,
            state=OrderStates.PENDING_CONSUMER,
            attributes={"storage": 200},
            limits=dict(DERIVED_AT_200),
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def patch(self, attributes):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )

    @data("abc", [1], True, {})
    def test_input_that_is_not_a_number_is_refused(self, value):
        response = self.patch({"storage": value})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", response.data)

    def test_input_above_max_is_refused(self):
        response = self.patch({"storage": 5001})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_value_for_sum_option_is_dropped(self):
        response = self.patch({"storage": 100, "backup": 7})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"storage": 100})


class OrderUpdateDerivedLimitsTest(DerivedLimitsOrderMixin, test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        offering, plan = self.build_offering()
        offering.customer = self.fixture.customer
        offering.save()
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=offering,
            plan=plan,
            state=OrderStates.PENDING_CONSUMER,
            attributes={"storage": 200},
            limits={
                "data_primary": 400,
                "wal_primary": 100,
                "data_replica": 400,
                "wal_replica": 100,
                "backup": 1000,
            },
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def patch(self, payload):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order), payload, format="json"
        )

    def test_changing_input_recomputes_limits(self):
        response = self.patch({"attributes": {"storage": 100}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.limits["data_primary"], 200)
        self.assertEqual(self.order.limits["backup"], 500)

    def test_client_values_for_derived_limits_are_replaced(self):
        response = self.patch({"limits": {"data_primary": 1, "backup": 1}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.limits["data_primary"], 400)
        self.assertEqual(self.order.limits["backup"], 1000)


class ResourceDerivedLimitsUpdateTest(DerivedLimitsOrderMixin, test.APITestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=test_utils.TestCreateProcessor,
            update_resource_processor=test_utils.TestUpdateScopedProcessor,
            can_update_limits=True,
        )
        self.fixture = fixtures.MarketplaceFixture()
        # The backup also covers "extra", a component the customer sets.
        options = database_options()
        options["options"]["backup"]["component_sum_config"]["components"].append(
            "extra"
        )
        offering, plan = self.build_offering(options)
        offering.type = "TEST_TYPE"
        offering.save()
        self.resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            attributes={"storage": 200},
            limits={**DERIVED_AT_200, "backup": 1001, "extra": 1},
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)

    def update_limits(self, limits):
        url = factories.ResourceFactory.get_url(self.resource, "update_limits")
        return self.client.post(url, {"limits": limits}, format="json")

    def ordered_limits(self):
        return (
            models.Order.objects.filter(resource=self.resource).latest("created").limits
        )

    def test_sum_follows_a_changed_component(self):
        response = self.update_limits({**self.resource.limits, "extra": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.ordered_limits()["backup"], 1005)

    def test_derived_limit_sent_by_client_is_replaced(self):
        response = self.update_limits(
            {**self.resource.limits, "extra": 5, "backup": 1, "data_primary": 1}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        limits = self.ordered_limits()
        self.assertEqual(limits["data_primary"], 400)
        self.assertEqual(limits["backup"], 1005)

    def test_derived_limit_left_out_is_put_back(self):
        response = self.update_limits({"extra": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.ordered_limits(), {**DERIVED_AT_200, "backup": 1005, "extra": 5}
        )

    def test_changing_only_a_derived_limit_changes_nothing(self):
        response = self.update_limits({**self.resource.limits, "backup": 2000})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly the same", str(response.data))

    def test_resource_without_recorded_input_keeps_its_limits(self):
        # Ordered before the option existed: nothing to derive from.
        self.resource.attributes = {}
        self.resource.save()
        response = self.update_limits({**self.resource.limits, "extra": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        limits = self.ordered_limits()
        self.assertEqual(limits["data_primary"], 400)
        self.assertEqual(limits["backup"], 1005)

    def test_every_update_order_stores_derived_limits(self):
        # Renewals and reallocations create their orders directly; they all
        # pass the update processor's check before being priced and saved.
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.resource.offering,
            plan=self.resource.plan,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            attributes={"old_limits": dict(self.resource.limits)},
            limits={"extra": 5, "backup": 1},
        )
        processors.UpdateScopedResourceProcessor(order).validate_order(None)
        self.assertEqual(order.limits, {**DERIVED_AT_200, "backup": 1005, "extra": 5})

    def test_derived_limit_cannot_be_reallocated(self):
        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_reallocation(
                self.resource,
                {"backup": 10},
                [{"resource_uuid": "0" * 32, "allocated_limits": {"backup": 10}}],
                self.fixture.owner,
            )
        self.assertIn("cannot be reallocated", str(context.exception))


class OfferingComponentGuardTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SUPPORT_OFFERING,
            options=copy.deepcopy(DATABASE_OPTIONS),
        )
        self.components = {
            component_type: factories.OfferingComponentFactory(
                offering=self.offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
            )
            for component_type in (*STORAGE_COMPONENTS, "backup", "extra")
        }
        self.client.force_authenticate(self.fixture.staff)

    def post(self, action, payload):
        url = factories.OfferingFactory.get_url(self.offering, action)
        return self.client.post(url, payload, format="json")

    def test_component_used_by_an_option_cannot_be_removed(self):
        component = self.components["wal_replica"]
        response = self.post("remove_offering_component", {"uuid": component.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("wal_replica", str(response.data))
        self.assertTrue(
            models.OfferingComponent.objects.filter(pk=component.pk).exists()
        )

    def test_component_used_by_an_option_must_stay_limit_billed(self):
        component = self.components["backup"]
        response = self.post(
            "update_offering_component",
            {
                "uuid": component.uuid.hex,
                "type": "backup",
                "name": "Backup",
                "measured_unit": "GB",
                "billing_type": BillingTypes.USAGE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        component.refresh_from_db()
        self.assertEqual(component.billing_type, BillingTypes.LIMIT)

    def test_component_used_by_an_option_cannot_be_renamed(self):
        component = self.components["backup"]
        response = self.post(
            "update_offering_component",
            {
                "uuid": component.uuid.hex,
                "type": "full_backup",
                "name": "Backup",
                "measured_unit": "GB",
                "billing_type": BillingTypes.LIMIT,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_components_are_unaffected(self):
        component = self.components["extra"]
        response = self.post("remove_offering_component", {"uuid": component.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ProviderApprovalDerivedLimitsTest(DerivedLimitsOrderMixin, test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        offering, plan = self.build_offering()
        offering.customer = self.fixture.offering.customer
        offering.save()
        self.order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=OrderStates.PENDING_PROVIDER,
            attributes={"storage": 200},
            limits=dict(DERIVED_AT_200),
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)

    def approve(self, attributes):
        url = factories.OrderFactory.get_url(self.order, "approve_by_provider")
        return self.client.post(url, {"attributes": attributes}, format="json")

    def test_changed_input_reaches_the_resource_options(self):
        self.order.offering.resource_options = copy.deepcopy(PAIRED_RESOURCE_OPTIONS)
        self.order.offering.save()
        self.order.resource.options = {"storage": 200}
        self.order.resource.save()
        response = self.approve({"storage": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.resource.refresh_from_db()
        self.assertEqual(self.order.resource.options, {"storage": 100})

    def test_changed_input_changes_limits_and_cost(self):
        response = self.approve({"storage": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["storage"], 100)
        self.assertEqual(self.order.limits["data_primary"], 200)
        self.assertEqual(self.order.limits["backup"], 500)
        self.assertEqual(self.order.cost, 1000)

    def test_other_attributes_leave_limits_alone(self):
        response = self.approve({"note": "checked"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.limits, DERIVED_AT_200)


class ResourceOptionChangeDerivedLimitsTest(test.APITestCase):
    """Changing the net storage after ordering, through a paired resource option."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.options = copy.deepcopy(DATABASE_OPTIONS)
        self.offering.resource_options = copy.deepcopy(PAIRED_RESOURCE_OPTIONS)
        self.offering.save()
        for component_type in (*STORAGE_COMPONENTS, "backup"):
            component = factories.OfferingComponentFactory(
                offering=self.offering,
                type=component_type,
                billing_type=BillingTypes.LIMIT,
            )
            factories.PlanComponentFactory(
                plan=self.fixture.plan, component=component, price=1
            )
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        # Ordered before the resource option existed: the input is only in
        # the attributes.
        self.resource.attributes = {"storage": 200}
        self.resource.limits = dict(DERIVED_AT_200)
        self.resource.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

    def post(self, action, payload, user=None, url=None):
        self.client.force_authenticate(user or self.fixture.owner)
        url = url or factories.ResourceFactory.get_url(self.resource, action)
        return self.client.post(url, payload, format="json")

    def change_storage(self, value, user=None):
        return self.post("update_options", {"options": {"storage": value}}, user)

    def test_change_is_ordered_with_recalculated_limits_and_price(self):
        response = self.change_storage(100)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.UPDATE)
        self.assertEqual(order.attributes["new_options"], {"storage": 100})
        self.assertEqual(order.attributes["old_limits"], DERIVED_AT_200)
        # The old value came from the order attributes: no resource option yet.
        self.assertEqual(order.attributes["old_options"], {"storage": 200})
        self.assertEqual(
            order.limits,
            {
                "data_primary": 200,
                "wal_primary": 50,
                "data_replica": 200,
                "wal_replica": 50,
                "backup": 500,
            },
        )
        self.assertIsNotNone(order.cost)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, DERIVED_AT_200)

    def test_processing_applies_new_value_and_limits_together(self):
        response = self.change_storage(100)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        order.refresh_from_db()
        if order.state != OrderStates.EXECUTING:
            order.set_state_executing()
            order.save()
        marketplace_utils.process_order(order, self.fixture.owner)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"storage": 100})
        self.assertEqual(self.resource.limits["backup"], 500)

    def test_agent_completion_applies_new_value_and_limits_together(self):
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.resource.project,
            plan=self.resource.plan,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            limits={**DERIVED_AT_200, "data_primary": 200, "backup": 800},
            attributes={
                "old_options": {},
                "new_options": {"storage": 150},
                "old_limits": dict(DERIVED_AT_200),
            },
        )
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OrderFactory.get_url(order, "set_state_done")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"storage": 150})
        self.assertEqual(self.resource.limits["backup"], 800)

    def test_change_with_the_same_limits_is_still_ordered(self):
        # The resource already holds the limits 250 derives, so the change
        # moves only the value: it is ordered all the same, since writing it
        # straight to the resource would bypass approval.
        self.resource.limits = {
            "data_primary": 500,
            "wal_primary": 125,
            "data_replica": 500,
            "wal_replica": 125,
            "backup": 1250,
        }
        self.resource.save()
        response = self.change_storage(250)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("order_uuid", response.data)

    def test_form_encoded_change_needs_order_creation_rights(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        ProjectRole.MANAGER.delete_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "update_options"),
            {"options": json.dumps({"storage": 100})},
            format="multipart",
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

    def test_pending_change_recalculates_from_its_own_value(self):
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.resource.project,
            plan=self.resource.plan,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.manager,
            limits={**DERIVED_AT_200, "data_primary": 200},
            attributes={
                "old_options": {"storage": 200},
                "new_options": {"storage": 100},
                "old_limits": dict(DERIVED_AT_200),
            },
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {
                "attributes": {
                    "old_options": {"storage": 200},
                    "new_options": {"storage": 50},
                    "old_limits": dict(DERIVED_AT_200),
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.limits["data_primary"], 100)
        self.assertEqual(order.limits["backup"], 250)

    def test_unpaired_option_with_the_same_name_does_not_drive_limits(self):
        self.offering.resource_options = {
            "order": ["storage"],
            "options": {"storage": {"type": "integer", "label": "Storage"}},
        }
        self.offering.save()
        self.resource.options = {"storage": 999}
        self.resource.save()
        response = self.post("update_limits", {"limits": {}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly the same", str(response.data))

    def test_unchanged_value_is_not_ordered(self):
        response = self.change_storage(200)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("order_uuid", response.data)

    def test_later_limit_changes_derive_from_the_changed_value(self):
        self.resource.options = {"storage": 300}
        self.resource.save()
        response = self.post("update_limits", {"limits": {}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.filter(resource=self.resource).latest("created")
        self.assertEqual(order.limits["data_primary"], 600)

    def test_provider_cannot_change_value_directly(self):
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "update_options_direct"
        )
        response = self.post(
            None,
            {"options": {"storage": 100}},
            user=self.fixture.staff,
            url=url,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", str(response.data))

    def test_provider_changing_value_at_approval_recalculates(self):
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            project=self.resource.project,
            plan=self.resource.plan,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_PROVIDER,
            limits={**DERIVED_AT_200, "data_primary": 200, "wal_primary": 50},
            attributes={
                "old_options": {},
                "new_options": {"storage": 100},
                "old_limits": dict(DERIVED_AT_200),
            },
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OrderFactory.get_url(order, "approve_by_provider"),
            {"attributes": {"new_options": {"storage": 50}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.limits["data_primary"], 100)
        self.assertEqual(order.limits["backup"], 250)

    def test_change_needs_order_creation_rights(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        # A database built by migrations seeds this; CI's bare roles do not.
        ProjectRole.MANAGER.delete_permission(PermissionEnum.CREATE_ORDER)
        response = self.change_storage(100, user=self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PairedResourceOptionRemovalTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.options = copy.deepcopy(DATABASE_OPTIONS)
        self.offering.resource_options = copy.deepcopy(PAIRED_RESOURCE_OPTIONS)
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.attributes = {"storage": 200}
        self.resource.options = {"storage": 200}
        self.resource.save()
        self.client.force_authenticate(self.fixture.staff)

    def remove_option(self):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_resource_options"
        )
        return self.client.post(
            url,
            {"resource_options": {"order": [], "options": {}}},
            format="json",
        )

    def test_option_can_go_while_values_are_as_ordered(self):
        response = self.remove_option()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_option_stays_while_a_resource_holds_a_changed_value(self):
        self.resource.options = {"storage": 300}
        self.resource.save()
        response = self.remove_option()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", str(response.data))

    def test_terminated_resources_do_not_count(self):
        self.resource.options = {"storage": 300}
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()
        response = self.remove_option()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
