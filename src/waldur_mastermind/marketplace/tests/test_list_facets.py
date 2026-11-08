from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


def values(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["count"] for v in facet["values"]}


def labels(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["label"] for v in facet["values"]}


class FacetsTestCase(test.APITestCase):
    url_name = None

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def get(self, user=None, **params):
        self.client.force_authenticate(user or self.staff)
        response = self.client.get(reverse(self.url_name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response


class ResourceFacetsTest(FacetsTestCase):
    url_name = "marketplace-resource-facets"

    def setUp(self):
        super().setUp()
        self.alpha = structure_factories.ProjectFactory(name="Alpha project")
        self.beta = structure_factories.ProjectFactory(name="Beta project")
        self.offering = factories.OfferingFactory(name="VMs")
        self.ok = factories.ResourceFactory(
            project=self.alpha,
            offering=self.offering,
            state=ResourceStates.OK,
            backend_metadata={"runtime_state": "ACTIVE"},
        )
        factories.ResourceFactory(
            project=self.alpha,
            state=ResourceStates.ERRED,
            backend_metadata={"runtime_state": "ERROR"},
        )
        factories.ResourceFactory(
            project=self.beta,
            offering=self.offering,
            state=ResourceStates.OK,
            backend_metadata={"runtime_state": "ACTIVE"},
        )

    def test_counts_resources_per_value(self):
        response = self.get()

        self.assertEqual(values(response, "state"), {"OK": 2, "Erred": 1})
        self.assertEqual(values(response, "runtime_state"), {"ACTIVE": 2, "ERROR": 1})
        self.assertEqual(
            values(response, "project"),
            {self.alpha.uuid.hex: 2, self.beta.uuid.hex: 1},
        )
        self.assertEqual(
            labels(response, "project")[self.alpha.uuid.hex], "Alpha project"
        )
        self.assertEqual(
            values(response, "customer"),
            {self.alpha.customer.uuid.hex: 2, self.beta.customer.uuid.hex: 1},
        )
        self.assertEqual(values(response, "offering")[self.offering.uuid.hex], 2)

    def test_other_filters_narrow_and_own_filter_is_ignored(self):
        response = self.get(state="Erred", project_uuid=self.alpha.uuid.hex)

        self.assertEqual(values(response, "runtime_state"), {"ERROR": 1})
        # The state facet ignores state, the project facet ignores project.
        self.assertEqual(values(response, "state"), {"OK": 1, "Erred": 1})
        self.assertEqual(
            values(response, "project"),
            {self.alpha.uuid.hex: 1},
        )

    def test_blank_values_are_not_offered(self):
        # A missing or empty runtime state cannot be sent back as a filter.
        factories.ResourceFactory(project=self.alpha, backend_metadata={})
        factories.ResourceFactory(
            project=self.alpha, backend_metadata={"runtime_state": None}
        )
        factories.ResourceFactory(
            project=self.alpha, backend_metadata={"runtime_state": ""}
        )

        response = self.get(facet="runtime_state")

        self.assertEqual(values(response, "runtime_state"), {"ACTIVE": 2, "ERROR": 1})

    def test_regular_user_counts_what_list_shows_them(self):
        # permissions.yaml grants this to owners; tests do not import it.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        owner = structure_factories.UserFactory()
        self.alpha.customer.add_user(owner, CustomerRole.OWNER)
        self.client.force_authenticate(owner)
        listed = self.client.get(reverse("marketplace-resource-list")).data

        response = self.get(owner)

        self.assertEqual(len(listed), 2)
        self.assertEqual(values(response, "project"), {self.alpha.uuid.hex: 2})

    def test_provider_resources(self):
        self.url_name = "marketplace-provider-resource-facets"
        response = self.get(facet="offering")
        self.assertEqual(values(response, "offering")[self.offering.uuid.hex], 2)


class OrderFacetsTest(FacetsTestCase):
    url_name = "marketplace-order-facets"

    def test_state_and_type_are_api_values(self):
        offering = factories.OfferingFactory()
        factories.OrderFactory(offering=offering, state=OrderStates.EXECUTING)
        factories.OrderFactory(
            offering=offering, state=OrderStates.DONE, type=OrderTypes.TERMINATE
        )
        factories.OrderFactory(state=OrderStates.DONE)

        response = self.get()

        self.assertEqual(values(response, "state"), {"done": 2, "executing": 1})
        self.assertEqual(values(response, "type"), {"Create": 2, "Terminate": 1})

        response = self.get(offering_uuid=offering.uuid.hex)
        self.assertEqual(values(response, "state"), {"done": 1, "executing": 1})
        self.assertEqual(len(values(response, "offering")), 2)


class OfferingUserFacetsTest(FacetsTestCase):
    url_name = "marketplace-offering-user-facets"

    def test_counts_states_per_offering(self):
        offering = factories.OfferingFactory()
        factories.OfferingUserFactory(offering=offering, state=OfferingUserStates.OK)
        requested = factories.OfferingUserFactory(offering=offering)
        # Saving a user with a username moves it to OK; set the state as is.
        models.OfferingUser.objects.filter(pk=requested.pk).update(
            state=OfferingUserStates.CREATION_REQUESTED
        )
        factories.OfferingUserFactory(state=OfferingUserStates.OK)

        response = self.get(offering_uuid=offering.uuid.hex)

        self.assertEqual(values(response, "state"), {"OK": 1, "Requested": 1})
        self.assertEqual(len(values(response, "offering")), 2)


class OfferingUserConsentFacetsTest(FacetsTestCase):
    url_name = "marketplace-offering-user-facets"

    @override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
    def test_counts_only_users_the_list_shows(self):
        # The list hides a user without consent to the offering's terms from
        # its provider; the facets count only what the list shows.
        fixture = structure_fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            customer=fixture.customer,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        models.OfferingTermsOfService.objects.create(
            offering=offering, terms_of_service="ToS", version="1.0", is_active=True
        )
        consented = factories.OfferingUserFactory(offering=offering)
        factories.OfferingUserFactory(offering=offering)
        models.UserOfferingConsent.objects.create(
            user=consented.user, offering=offering, version="1.0"
        )

        self.client.force_authenticate(fixture.owner)
        listed = self.client.get(reverse("marketplace-offering-user-list")).data
        response = self.get(fixture.owner, facet="offering")

        self.assertEqual([row["uuid"] for row in listed], [consented.uuid.hex])
        self.assertEqual(values(response, "offering"), {offering.uuid.hex: 1})


class SoftwarePackageFacetsTest(FacetsTestCase):
    url_name = "marketplace-software-package-facets"

    def setUp(self):
        super().setUp()
        self.eessi = factories.SoftwareCatalogFactory(
            name="EESSI", catalog_type="binary_runtime"
        )
        self.spack = factories.SoftwareCatalogFactory(
            name="Spack", catalog_type="source_package"
        )
        factories.SoftwarePackageFactory(
            catalog=self.eessi, categories=["bio", "chem"], licenses=["MIT"]
        )
        factories.SoftwarePackageFactory(
            catalog=self.eessi, categories=["bio"], licenses=["GPL"]
        )
        factories.SoftwarePackageFactory(
            catalog=self.spack, categories=["math"], licenses=["MIT"]
        )

    def test_array_elements_are_counted(self):
        response = self.get()

        self.assertEqual(values(response, "category"), {"bio": 2, "chem": 1, "math": 1})
        self.assertEqual(values(response, "license"), {"MIT": 2, "GPL": 1})
        self.assertEqual(
            labels(response, "catalog_type"),
            {
                "binary_runtime": "Binary Runtime (EESSI)",
                "source_package": "Source Package (Spack)",
            },
        )

    def test_array_facet_ignores_its_own_filter_and_honours_others(self):
        response = self.get(category="chem", catalog_uuid=self.eessi.uuid.hex)

        self.assertEqual(values(response, "category"), {"bio": 2, "chem": 1})
        self.assertEqual(values(response, "license"), {"MIT": 1})
        self.assertEqual(values(response, "catalog_name"), {"EESSI": 1})

    def test_array_through_relations(self):
        # GPU architectures live on the targets of a package's versions; a
        # package counts once however many of its targets name one.
        package = models.SoftwarePackage.objects.filter(catalog=self.spack).first()
        version = factories.SoftwareVersionFactory(package=package)
        factories.SoftwareTargetFactory(
            version=version, gpu_architectures=["nvidia/cc90", "nvidia/cc80"]
        )
        factories.SoftwareTargetFactory(
            version=version, gpu_architectures=["nvidia/cc90"]
        )
        other = factories.SoftwareVersionFactory(
            package=factories.SoftwarePackageFactory(catalog=self.eessi)
        )
        factories.SoftwareTargetFactory(
            version=other, gpu_architectures=["nvidia/cc90"]
        )

        response = self.get(facet="gpu_arch")
        self.assertEqual(
            values(response, "gpu_arch"), {"nvidia/cc90": 2, "nvidia/cc80": 1}
        )

        response = self.get(facet="gpu_arch", catalog_name="Spack")
        self.assertEqual(
            values(response, "gpu_arch"), {"nvidia/cc90": 1, "nvidia/cc80": 1}
        )

        response = self.get(facet="gpu_arch", facet_q="80")
        self.assertEqual(values(response, "gpu_arch"), {"nvidia/cc80": 1})

    def test_relation_facets_count_packages_with_some_versions_lacking_it(self):
        # Most packages have several versions; one without a toolchain or a
        # GPU target must not hide the package from these facets.
        package = factories.SoftwarePackageFactory(catalog=self.spack)
        with_both = factories.SoftwareVersionFactory(
            package=package, metadata={"toolchain": {"name": "foss"}}
        )
        factories.SoftwareTargetFactory(
            version=with_both, gpu_architectures=["nvidia/cc90"]
        )
        factories.SoftwareVersionFactory(package=package, metadata={})

        response = self.get(facet="toolchain,gpu_arch")

        self.assertEqual(values(response, "toolchain"), {"foss": 1})
        self.assertEqual(values(response, "gpu_arch"), {"nvidia/cc90": 1})

    def test_catalog_values_select_exactly_what_they_count(self):
        newer = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        older = factories.SoftwareCatalogFactory(name="EESSI", version="2023")
        factories.SoftwarePackageFactory(catalog=newer)
        factories.SoftwarePackageFactory(catalog=older)

        counts = values(self.get(facet="catalog_version"), "catalog_version")
        self.client.force_authenticate(self.staff)
        listed = self.client.get(
            reverse("marketplace-software-package-list"),
            {"catalog_version_exact": "2023"},
        )

        self.assertEqual(counts["2023"], 1)
        self.assertEqual(listed["X-Result-Count"], "1")

    def test_long_search_is_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            reverse(self.url_name), {"facet": "license", "facet_q": "x" * 101}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_array_facet_search_and_limit(self):
        response = self.get(facet="category", facet_q="B")
        self.assertEqual(values(response, "category"), {"bio": 2})

        response = self.get(facet="category", facet_limit=1)
        [facet] = response.data
        self.assertTrue(facet["truncated"])
        self.assertEqual(values(response, "category"), {"bio": 2})

    def test_anonymous_user_when_catalogue_is_public(self):
        with override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True):
            response = self.client.get(reverse(self.url_name), {"facet": "license"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(values(response, "license"), {"MIT": 2, "GPL": 1})
