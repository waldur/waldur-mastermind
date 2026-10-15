import copy

import yaml
from ddt import data, ddt, unpack
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import display_limits, models
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingModes,
    BillingTypes,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.serializers import ExportImportOfferingSerializer
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

# A quota the provider's backend enforces for a component Waldur only meters.
QUOTA_RESOURCE_OPTIONS = {
    "order": ["inode_quota", "note"],
    "options": {
        "inode_quota": {
            "type": "integer",
            "label": "Inode quota",
            "display_limit_config": {"component_type": "inodes"},
        },
        "note": {"type": "string", "label": "Note"},
    },
}


def quota_resource_options(**options):
    result = copy.deepcopy(QUOTA_RESOURCE_OPTIONS)
    result["options"].update(options)
    result["order"] = list(result["options"])
    return result


@ddt
class DisplayLimitValueTest(SimpleTestCase):
    @data(
        (2000000, 2000000),
        ("1500000", 1500000),
        ("1500000.0", 1500000),
        (2.5, 2.5),
        (0, 0),
    )
    @unpack
    def test_number_is_shown(self, value, expected):
        shown = display_limits.get_display_limits(
            QUOTA_RESOURCE_OPTIONS, {"inode_quota": value}
        )
        self.assertEqual(shown, {"inodes": expected})

    @data(None, "", "unlimited", True, -1, "nan", "inf")
    def test_unusable_value_shows_no_limit(self, value):
        shown = display_limits.get_display_limits(
            QUOTA_RESOURCE_OPTIONS, {"inode_quota": value}
        )
        self.assertEqual(shown, {})

    @data(None, {}, {"options": {}}, {"options": {"note": {"type": "string"}}})
    def test_offering_without_the_setting_shows_nothing(self, resource_options):
        shown = display_limits.get_display_limits(resource_options, {"note": 5})
        self.assertEqual(shown, {})

    def test_resource_without_options_shows_nothing(self):
        self.assertEqual(
            display_limits.get_display_limits(QUOTA_RESOURCE_OPTIONS, None), {}
        )


class OfferingDisplayLimitSaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

    def components(self, inodes_billing_type=BillingTypes.USAGE):
        return [
            {
                "type": "storage",
                "name": "Storage",
                "measured_unit": "TB",
                "billing_type": BillingTypes.LIMIT,
            },
            {
                "type": "inodes",
                "name": "Inodes",
                "measured_unit": "",
                "billing_type": inodes_billing_type,
            },
        ]

    def create_offering(self, resource_options=None, components=None, **extra):
        payload = {
            "name": "storage",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": SUPPORT_OFFERING,
            "components": self.components() if components is None else components,
            **extra,
        }
        if resource_options is not None:
            payload["resource_options"] = resource_options
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def assert_rejected(self, **kwargs):
        response = self.create_offering(**kwargs)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        return response

    def test_setting_is_saved(self):
        response = self.create_offering(QUOTA_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.resource_options["options"]["inode_quota"]["display_limit_config"],
            {"component_type": "inodes"},
        )

    def test_unknown_component_is_rejected(self):
        options = quota_resource_options(
            inode_quota={
                "type": "integer",
                "label": "Inode quota",
                "display_limit_config": {"component_type": "files"},
            }
        )
        response = self.assert_rejected(resource_options=options)
        self.assertIn("files", str(response.data))

    def test_limit_based_component_is_rejected(self):
        self.assert_rejected(
            resource_options=QUOTA_RESOURCE_OPTIONS,
            components=self.components(inodes_billing_type=BillingTypes.LIMIT),
        )

    def test_non_integer_option_is_rejected(self):
        options = quota_resource_options(
            inode_quota={
                "type": "string",
                "label": "Inode quota",
                "display_limit_config": {"component_type": "inodes"},
            }
        )
        self.assert_rejected(resource_options=options)

    def test_two_options_for_one_component_are_rejected(self):
        options = quota_resource_options(
            other_quota={
                "type": "integer",
                "label": "Other quota",
                "display_limit_config": {"component_type": "inodes"},
            }
        )
        self.assert_rejected(resource_options=options)

    def test_setting_is_rejected_on_an_order_option(self):
        self.assert_rejected(options=QUOTA_RESOURCE_OPTIONS)

    def test_component_type_is_required(self):
        options = quota_resource_options(
            inode_quota={
                "type": "integer",
                "label": "Inode quota",
                "display_limit_config": {},
            }
        )
        self.assert_rejected(resource_options=options)


class OfferingResourceOptionsUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=SUPPORT_OFFERING
        )
        factories.OfferingComponentFactory(
            offering=self.offering, type="inodes", billing_type=BillingTypes.USAGE
        )
        factories.OfferingComponentFactory(
            offering=self.offering, type="storage", billing_type=BillingTypes.LIMIT
        )
        self.client.force_authenticate(self.fixture.staff)

    def update(self, resource_options):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_resource_options"
        )
        return self.client.post(
            url, {"resource_options": resource_options}, format="json"
        )

    def test_setting_is_saved(self):
        response = self.update(QUOTA_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            display_limits.display_limit_options(self.offering.resource_options),
            {"inodes": "inode_quota"},
        )

    def test_limit_based_component_is_rejected(self):
        options = quota_resource_options(
            inode_quota={
                "type": "integer",
                "label": "Quota",
                "display_limit_config": {"component_type": "storage"},
            }
        )
        response = self.update(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("storage", str(response.data))

    def test_unknown_component_is_rejected(self):
        options = quota_resource_options(
            inode_quota={
                "type": "integer",
                "label": "Quota",
                "display_limit_config": {"component_type": "files"},
            }
        )
        response = self.update(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_setting_is_rejected_on_an_order_option(self):
        url = factories.OfferingFactory.get_url(self.offering, "update_options")
        response = self.client.post(
            url, {"options": QUOTA_RESOURCE_OPTIONS}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingComponentGuardTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SUPPORT_OFFERING,
            resource_options=copy.deepcopy(QUOTA_RESOURCE_OPTIONS),
        )
        self.components = {
            component_type: factories.OfferingComponentFactory(
                offering=self.offering,
                type=component_type,
                billing_type=BillingTypes.USAGE,
            )
            for component_type in ("inodes", "extra")
        }
        self.client.force_authenticate(self.fixture.staff)

    def post(self, action, payload):
        url = factories.OfferingFactory.get_url(self.offering, action)
        return self.client.post(url, payload, format="json")

    def update_component(self, component, **changes):
        payload = {
            "uuid": component.uuid.hex,
            "type": component.type,
            "name": "Inodes",
            "measured_unit": "",
            "billing_type": BillingTypes.USAGE,
            **changes,
        }
        return self.post("update_offering_component", payload)

    def test_component_shown_with_an_option_cannot_be_removed(self):
        component = self.components["inodes"]
        response = self.post("remove_offering_component", {"uuid": component.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("inode_quota", str(response.data))
        self.assertTrue(
            models.OfferingComponent.objects.filter(pk=component.pk).exists()
        )

    def test_component_shown_with_an_option_must_stay_usage_billed(self):
        component = self.components["inodes"]
        response = self.update_component(component, billing_type=BillingTypes.LIMIT)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        component.refresh_from_db()
        self.assertEqual(component.billing_type, BillingTypes.USAGE)

    def test_component_shown_with_an_option_cannot_be_renamed(self):
        response = self.update_component(self.components["inodes"], type="files")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_component_shown_with_an_option_can_change_other_fields(self):
        response = self.update_component(self.components["inodes"], name="Files")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_other_components_are_unaffected(self):
        component = self.components["extra"]
        response = self.post("remove_offering_component", {"uuid": component.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ResourceDisplayLimitTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        factories.OfferingComponentFactory(
            offering=self.offering, type="inodes", billing_type=BillingTypes.USAGE
        )
        self.offering.resource_options = copy.deepcopy(QUOTA_RESOURCE_OPTIONS)
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.limits = {"storage": 10}
        self.resource.options = {"inode_quota": 2000000}
        self.resource.save()
        self.client.force_authenticate(self.fixture.staff)

    def get_resource(self, url=None):
        response = self.client.get(
            url or factories.ResourceFactory.get_url(self.resource)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def get_display_limits(self, url=None):
        return self.get_resource(url)["display_limits"]

    def test_option_is_shown_as_the_component_limit(self):
        self.assertEqual(self.get_display_limits(), {"inodes": 2000000})

    def test_limits_hold_only_stored_limits(self):
        # Clients send limits back in orders and compare them with a backend's,
        # so the shown value never appears there.
        self.assertEqual(self.get_resource()["limits"], {"storage": 10})

    def test_provider_endpoint_shows_it_too(self):
        url = factories.ResourceFactory.get_provider_resource_url(self.resource)
        resource = self.get_resource(url)
        self.assertEqual(resource["display_limits"], {"inodes": 2000000})
        self.assertEqual(resource["limits"], {"storage": 10})

    def test_list_endpoints_show_it(self):
        for url in (
            factories.ResourceFactory.get_list_url(),
            factories.ResourceFactory.get_provider_resource_list_url(),
        ):
            response = self.client.get(url, {"field": ["uuid", "display_limits"]})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            row = next(r for r in response.data if r["uuid"] == self.resource.uuid.hex)
            self.assertEqual(row["display_limits"], {"inodes": 2000000})

    def test_stored_limits_are_not_modified(self):
        self.get_resource()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, {"storage": 10})

    def test_resource_without_a_value_shows_no_limit(self):
        self.resource.options = {}
        self.resource.save()
        self.assertEqual(self.get_display_limits(), {})

    def test_resource_with_null_options_shows_no_limit(self):
        models.Resource.objects.filter(pk=self.resource.pk).update(options=None)
        self.assertEqual(self.get_display_limits(), {})

    def test_offering_without_the_setting_shows_nothing(self):
        self.offering.resource_options = {}
        self.offering.save()
        self.assertEqual(self.get_display_limits(), {})

    def test_changing_the_option_changes_the_limit(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        url = factories.ResourceFactory.get_url(self.resource, "update_options")
        response = self.client.post(
            url, {"options": {"inode_quota": 3000000}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.get_display_limits(), {"inodes": 3000000})

    def test_changing_stored_limits_keeps_the_shown_limit(self):
        self.resource.limits = {"storage": 20}
        self.resource.save()
        self.assertEqual(self.get_display_limits(), {"inodes": 2000000})

    def test_showing_a_limit_bills_nothing(self):
        items = invoice_models.InvoiceItem.objects.count()
        quotas = models.ComponentQuota.objects.count()
        orders = models.Order.objects.count()
        self.get_resource()
        self.assertEqual(invoice_models.InvoiceItem.objects.count(), items)
        self.assertEqual(models.ComponentQuota.objects.count(), quotas)
        self.assertEqual(models.Order.objects.count(), orders)

    def test_stored_limit_is_not_shadowed_by_the_option(self):
        self.resource.limits = {"storage": 10, "inodes": 5}
        self.resource.save()
        resource = self.get_resource()
        self.assertEqual(resource["limits"], {"storage": 10, "inodes": 5})
        self.assertEqual(resource["display_limits"], {})

    def test_component_billed_by_limit_under_the_plan_is_not_shown(self):
        # A plan may bill a component stored as usage-based by limit; what the
        # resource holds for it then is a billed quantity, not a quota.
        models.OfferingComponent.objects.filter(
            offering=self.offering, type="inodes"
        ).update(billed_per_plan=True)
        models.Plan.objects.filter(pk=self.resource.plan.pk).update(
            billing_mode=BillingModes.LIMIT
        )
        self.assertEqual(self.get_display_limits(), {})

    def test_listing_does_not_query_per_resource(self):
        # Resolving an overriding plan has to reuse the prefetched components.
        models.Plan.objects.filter(pk=self.resource.plan.pk).update(
            billing_mode=BillingModes.LIMIT
        )
        url = factories.ResourceFactory.get_list_url()
        params = {"field": ["uuid", "display_limits"]}

        def count_queries():
            with CaptureQueriesContext(connection) as context:
                response = self.client.get(url, params)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertTrue(
                all(
                    row["display_limits"] == {"inodes": 2000000}
                    for row in response.data
                )
            )
            return len(context.captured_queries), len(response.data)

        queries, rows = count_queries()
        for _ in range(2):
            factories.ResourceFactory(
                offering=self.offering,
                plan=self.resource.plan,
                project=self.fixture.project,
                options={"inode_quota": 2000000},
            )
        more_queries, more_rows = count_queries()
        self.assertEqual(more_rows, rows + 2)
        self.assertEqual(more_queries, queries)


class DisplayLimitRoundTripTest(BaseOrderCreateTest):
    """Limits read from a resource can be ordered again as they are."""

    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            type=SUPPORT_OFFERING,
            resource_options=copy.deepcopy(QUOTA_RESOURCE_OPTIONS),
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        for component_type, billing_type in (
            ("storage", BillingTypes.LIMIT),
            ("inodes", BillingTypes.USAGE),
        ):
            component = models.OfferingComponent.objects.create(
                offering=self.offering,
                type=component_type,
                billing_type=billing_type,
            )
            factories.PlanComponentFactory(plan=self.plan, component=component, price=1)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            limits={"storage": 5},
            options={"inode_quota": 2000000},
        )

    def test_limits_as_read_can_be_ordered_again(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ResourceFactory.get_url(self.resource))
        self.assertEqual(response.data["display_limits"], {"inodes": 2000000})
        response = self.create_order(
            self.fixture.staff,
            self.offering,
            add_payload={
                "plan": factories.PlanFactory.get_public_url(self.plan),
                "limits": response.data["limits"],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.limits, {"storage": 5})

    def test_a_displayed_component_is_not_accepted_as_a_limit(self):
        response = self.create_order(
            self.fixture.staff,
            self.offering,
            add_payload={
                "plan": factories.PlanFactory.get_public_url(self.plan),
                "limits": {"storage": 5, "inodes": 2000000},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingImportDisplayLimitTest(test.APITestCase):
    """Importing an offering cannot drop or re-bill a component an option shows."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Quota offering",
            type=SUPPORT_OFFERING,
            resource_options=copy.deepcopy(QUOTA_RESOURCE_OPTIONS),
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="inodes", billing_type=BillingTypes.USAGE
        )

    def import_data(self, **component):
        data = ExportImportOfferingSerializer(self.offering).data
        data["components"] = [
            {**c, "offering_id": self.offering.id}
            for c in data["components"]
            if component or c["type"] != "inodes"
        ]
        for c in data["components"]:
            c.update(component)
        return data

    def test_management_import_cannot_remove_the_component(self):
        serializer = ExportImportOfferingSerializer(
            self.offering, data=self.import_data()
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("inode_quota", str(serializer.errors))

    def test_management_import_cannot_rebill_the_component(self):
        serializer = ExportImportOfferingSerializer(
            self.offering, data=self.import_data(billing_type=BillingTypes.LIMIT)
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("inode_quota", str(serializer.errors))

    def test_management_import_keeping_the_component_is_accepted(self):
        serializer = ExportImportOfferingSerializer(
            self.offering, data=self.import_data(billing_type=BillingTypes.USAGE)
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_api_import_cannot_rebill_the_component(self):
        self.client.force_authenticate(self.fixture.staff)
        import_data = {
            "offering": {"name": self.offering.name, "type": SUPPORT_OFFERING},
            "components": [
                {"type": "inodes", "name": "Inodes", "billing_type": BillingTypes.LIMIT}
            ],
        }
        response = self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.fixture.customer.uuid.hex,
                "category": self.offering.category.title,
                "overwrite_existing": True,
                "offering_data": yaml.safe_dump(import_data),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("inode_quota", str(response.data))
        self.component.refresh_from_db()
        self.assertEqual(self.component.billing_type, BillingTypes.USAGE)
