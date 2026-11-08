from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    LEGACY_SITE_AGENT_OFFERING,
    SITE_AGENT_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories


def values(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["count"] for v in facet["values"]}


class PublicOfferingFacetsTest(test.APITestCase):
    def setUp(self):
        self.alpha = structure_factories.CustomerFactory(name="Alpha")
        self.beta = structure_factories.CustomerFactory(name="Beta")
        self.compute = factories.CategoryFactory(title="Compute")
        self.storage = factories.CategoryFactory(title="Storage")
        self.gpu = factories.TagFactory(name="gpu")
        self.archive = factories.TagFactory(name="archive")

        self.alpha_compute = self.offering(
            "GPU cluster", self.alpha, self.compute, tags=[self.gpu, self.archive]
        )
        self.alpha_storage = self.offering(
            "Object store", self.alpha, self.storage, tags=[self.archive]
        )
        self.beta_compute = self.offering(
            "Batch nodes", self.beta, self.compute, state=OfferingStates.PAUSED
        )
        # Invisible to a regular user: private and not connected to them.
        self.offering("Hidden", self.beta, self.storage, shared=False)

        self.user = structure_factories.UserFactory()
        self.url = factories.OfferingFactory.get_public_list_url("facets")

    def offering(self, name, customer, category, tags=(), **kwargs):
        offering = factories.OfferingFactory(
            name=name,
            customer=customer,
            category=category,
            shared=kwargs.pop("shared", True),
            **kwargs,
        )
        offering.tags.set(tags)
        return offering

    def get(self, user=None, **params):
        self.client.force_authenticate(user or self.user)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_counts_visible_offerings_per_value(self):
        response = self.get()

        self.assertEqual(
            values(response, "customer"),
            {self.alpha.uuid.hex: 2, self.beta.uuid.hex: 1},
        )
        self.assertEqual(
            values(response, "category"),
            {self.compute.uuid.hex: 2, self.storage.uuid.hex: 1},
        )
        self.assertEqual(values(response, "state"), {"Active": 2, "Paused": 1})
        self.assertEqual(values(response, "shared"), {"true": 3})

    def test_labels(self):
        response = self.get()
        [customer] = [f for f in response.data if f["name"] == "customer"]
        self.assertEqual(
            {v["value"]: v["label"] for v in customer["values"]},
            {self.alpha.uuid.hex: "Alpha", self.beta.uuid.hex: "Beta"},
        )

    def test_other_filters_narrow_a_facet(self):
        response = self.get(customer_uuid=self.alpha.uuid.hex)

        self.assertEqual(
            values(response, "category"),
            {self.compute.uuid.hex: 1, self.storage.uuid.hex: 1},
        )
        self.assertEqual(
            values(response, "tag"), {self.gpu.uuid.hex: 1, self.archive.uuid.hex: 2}
        )

    def test_a_facet_ignores_its_own_filter(self):
        response = self.get(customer_uuid=self.alpha.uuid.hex)

        self.assertEqual(
            values(response, "customer"),
            {self.alpha.uuid.hex: 2, self.beta.uuid.hex: 1},
        )

    def test_every_param_of_a_facet_is_ignored(self):
        response = self.get(tag_name="gpu", facet="tag")
        self.assertEqual(
            values(response, "tag"), {self.gpu.uuid.hex: 1, self.archive.uuid.hex: 2}
        )

    def test_keyword_narrows_facets(self):
        response = self.get(keyword="object")

        self.assertEqual(values(response, "category"), {self.storage.uuid.hex: 1})
        self.assertEqual(values(response, "customer"), {self.alpha.uuid.hex: 1})

    def test_tag_counts_are_distinct(self):
        # The tag filter joins tags; an offering with two tags is still one.
        response = self.get(tag=[self.gpu.uuid.hex, self.archive.uuid.hex])

        self.assertEqual(values(response, "customer"), {self.alpha.uuid.hex: 2})

    def test_state_filter_uses_api_labels(self):
        response = self.get(state="Paused")

        self.assertEqual(values(response, "customer"), {self.beta.uuid.hex: 1})
        self.assertEqual(values(response, "state"), {"Active": 2, "Paused": 1})

    def test_staff_sees_private_offerings(self):
        # Staff see private offerings that have a plan.
        hidden = models.Offering.objects.get(name="Hidden")
        factories.PlanFactory(offering=hidden)

        response = self.get(structure_factories.UserFactory(is_staff=True))

        self.assertEqual(values(response, "shared"), {"true": 3, "false": 1})

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_user(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(values(response, "shared"), {"true": 3})

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_user_sees_nothing_when_catalogue_is_closed(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(values(response, "customer"), {})

    def test_legacy_type_is_counted_as_the_current_one(self):
        self.alpha_compute.type = SITE_AGENT_OFFERING
        self.alpha_compute.save()
        models.Offering.objects.filter(pk=self.alpha_storage.pk).update(
            type=LEGACY_SITE_AGENT_OFFERING
        )

        response = self.get(facet="type")

        self.assertEqual(
            values(response, "type"),
            {SITE_AGENT_OFFERING: 2, self.beta_compute.type: 1},
        )

    def test_selected_facets_only(self):
        response = self.get(facet="state,shared")
        self.assertEqual([f["name"] for f in response.data], ["state", "shared"])

    def test_unknown_facet(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"facet": "nope"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_limit(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"facet_limit": "0"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_limit_keeps_the_largest_values(self):
        response = self.get(facet="customer", facet_limit=1)

        [facet] = response.data
        self.assertTrue(facet["truncated"])
        self.assertEqual([v["value"] for v in facet["values"]], [self.alpha.uuid.hex])

    def test_search_labels(self):
        response = self.get(facet="customer", facet_q="bet")

        [facet] = response.data
        self.assertFalse(facet["truncated"])
        self.assertEqual(values(response, "customer"), {self.beta.uuid.hex: 1})

    def test_search_unlabeled_facet(self):
        response = self.get(facet="state", facet_q="paus")
        self.assertEqual(values(response, "state"), {"Paused": 1})

    def test_ordering_and_paging_params_are_ignored(self):
        response = self.get(o="-total_cost", page=3, page_size=1, field="name")
        self.assertEqual(values(response, "state"), {"Active": 2, "Paused": 1})

    def test_one_query_per_facet(self):
        self.client.force_authenticate(self.user)
        self.client.get(self.url, {"facet": "state"})  # warm up caches

        def queries(facets):
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(self.url, {"facet": facets})
            return len(ctx.captured_queries)

        self.assertEqual(queries("state,shared,customer") - queries("state"), 2)


class ProviderOfferingFacetsTest(test.APITestCase):
    def test_provider_lists_facets_of_own_offerings(self):
        owner = structure_factories.UserFactory()
        customer = structure_factories.CustomerFactory()
        customer.add_user(owner, CustomerRole.OWNER)
        mine = factories.OfferingFactory(customer=customer, shared=False)
        factories.OfferingFactory(shared=False)  # someone else's

        self.client.force_authenticate(owner)
        response = self.client.get(
            factories.OfferingFactory.get_list_url("facets"), {"facet": "customer"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(values(response, "customer"), {mine.customer.uuid.hex: 1})


class ServiceProviderOfferingFacetsTest(test.APITestCase):
    def test_counts_only_the_provider_offerings_the_caller_may_see(self):
        provider = factories.ServiceProviderFactory()
        owner = structure_factories.UserFactory()
        provider.customer.add_user(owner, CustomerRole.OWNER)
        factories.OfferingFactory(
            customer=provider.customer, shared=True, billable=True
        )
        factories.OfferingFactory(
            customer=provider.customer,
            shared=True,
            billable=True,
            state=OfferingStates.DRAFT,
        )
        factories.OfferingFactory(shared=True, billable=True)  # another provider

        url = reverse(
            "service-provider-offerings-facets",
            kwargs={"service_provider_uuid": provider.uuid.hex},
        )
        self.client.force_authenticate(owner)
        response = self.client.get(url, {"facet": "state"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(values(response, "state"), {"Active": 1, "Draft": 1})

        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(url, {"facet": "state"})
        self.assertEqual(values(response, "state"), {})
