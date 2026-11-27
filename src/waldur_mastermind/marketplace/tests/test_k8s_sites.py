"""Kubernetes clusters on OpenStack sites the provider fixed."""

import copy

from ddt import data, ddt
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.marketplace import k8s_sites, serializers
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_openstack.tests import factories as openstack_factories


class SitesMixin:
    """Three OpenStack tenant offerings with overlapping flavors.

    m1.small and m1.large exist on every site with the same size; m1.odd
    exists everywhere but is bigger on the third site; only-first exists on
    the first site alone.
    """

    def create_sites(self):
        # The provider's own active tenant offerings.
        self.provider = structure_factories.CustomerFactory()
        self.sites = []
        self.flavors = []
        for index in range(3):
            settings = openstack_factories.SettingsFactory()
            site = factories.OfferingFactory(
                type=OPENSTACK_TENANT_OFFERING,
                scope=settings,
                name=f"Site {index}",
                customer=self.provider,
                state=OfferingStates.ACTIVE,
            )
            flavors = {
                "m1.small": openstack_factories.FlavorFactory(
                    settings=settings, name="m1.small", cores=2, ram=2048
                ),
                "m1.large": openstack_factories.FlavorFactory(
                    settings=settings, name="m1.large", cores=4, ram=8192
                ),
                "m1.odd": openstack_factories.FlavorFactory(
                    settings=settings,
                    name="m1.odd",
                    cores=8 if index == 2 else 4,
                    ram=4096,
                ),
            }
            if index == 0:
                flavors["only-first"] = openstack_factories.FlavorFactory(
                    settings=settings, name="only-first", cores=4, ram=8192
                )
            self.sites.append(site)
            self.flavors.append(flavors)

    def policy(self, **overrides):
        return {
            "infrastructure_mode": "provider_fixed",
            "fixed_openstack_offerings": {
                "1-datacenter": [self.sites[0].uuid.hex],
                "3-datacenter": [site.uuid.hex for site in self.sites],
            },
            "minimal_worker_vcpus": 2,
            "minimal_worker_ram_gb": 2,
            **overrides,
        }

    def cluster(self, topology="3-datacenter", flavor="m1.large", node_count=3):
        count = 3 if topology == "3-datacenter" else 1
        return {
            "kubernetes_version": "1.30.0",
            "topology": topology,
            "datacenters": [
                {
                    "id": f"datacenter-{index + 1}",
                    "name": f"Datacenter {index + 1}",
                    "openstack_infrastructure": {
                        "uuid": self.sites[index].uuid.hex,
                        "name": self.sites[index].name,
                    },
                    "node_groups": [
                        {
                            "id": f"dc{index + 1}-worker-1",
                            "type": "worker",
                            "node_count": node_count,
                            "openstack_flavor": {
                                "uuid": self.flavors[index][flavor].uuid.hex,
                                "name": flavor,
                            },
                            "disk_config": {"data_disk_size_gb": 100},
                        }
                    ],
                }
                for index in range(count)
            ],
        }


@ddt
class FixedSitesPolicyTest(SitesMixin, test.APITestCase):
    def setUp(self):
        self.create_sites()

    def validate(self, default_configs):
        serializer = serializers.OptionFieldSerializer(
            data={
                "type": "k8s_cluster_config",
                "label": "Cluster",
                "default_configs": default_configs,
            }
        )
        valid = serializer.is_valid()
        return serializer, valid

    def test_sites_for_every_topology_are_saved(self):
        serializer, valid = self.validate(self.policy())
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(
            serializer.validated_data["default_configs"]["fixed_openstack_offerings"],
            {
                "1-datacenter": [self.sites[0].uuid.hex],
                "3-datacenter": [site.uuid.hex for site in self.sites],
            },
        )

    def test_dashed_uuids_are_stored_in_hex(self):
        policy = self.policy(topology_mode="1-datacenter")
        policy["fixed_openstack_offerings"] = {
            "1-datacenter": [str(self.sites[0].uuid)]
        }
        serializer, valid = self.validate(policy)
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(
            serializer.validated_data["default_configs"]["fixed_openstack_offerings"],
            {"1-datacenter": [self.sites[0].uuid.hex]},
        )

    def test_customer_choice_needs_both_topologies(self):
        policy = self.policy()
        del policy["fixed_openstack_offerings"]["3-datacenter"]
        serializer, valid = self.validate(policy)
        self.assertFalse(valid)
        self.assertIn("fixed_openstack_offerings", serializer.errors["default_configs"])

    def test_fixed_topology_needs_only_its_own_list(self):
        policy = self.policy(topology_mode="1-datacenter")
        del policy["fixed_openstack_offerings"]["3-datacenter"]
        serializer, valid = self.validate(policy)
        self.assertTrue(valid, serializer.errors)

    @data(
        lambda sites: [sites[0].uuid.hex, sites[1].uuid.hex],
        lambda sites: [sites[0].uuid.hex] * 3,
    )
    def test_three_different_sites_are_needed(self, make_list):
        policy = self.policy()
        policy["fixed_openstack_offerings"]["3-datacenter"] = make_list(self.sites)
        serializer, valid = self.validate(policy)
        self.assertFalse(valid)

    def test_sites_must_be_uuids(self):
        policy = self.policy()
        policy["fixed_openstack_offerings"]["1-datacenter"] = ["not-a-uuid"]
        serializer, valid = self.validate(policy)
        self.assertFalse(valid)
        self.assertIn("fixed_openstack_offerings", serializer.errors["default_configs"])

    def test_list_of_a_topology_not_offered_is_kept(self):
        policy = self.policy(topology_mode="1-datacenter")
        serializer, valid = self.validate(policy)
        self.assertTrue(valid, serializer.errors)
        self.assertIn(
            "3-datacenter",
            serializer.validated_data["default_configs"]["fixed_openstack_offerings"],
        )

    def test_customer_choice_mode_does_not_check_the_lists(self):
        serializer, valid = self.validate({"infrastructure_mode": "customer_choice"})
        self.assertTrue(valid, serializer.errors)


class CommonFlavorsTest(SitesMixin, test.APITestCase):
    def setUp(self):
        self.create_sites()

    def test_only_flavors_of_the_same_name_and_size_everywhere(self):
        flavors = k8s_sites.common_flavors(self.sites)
        self.assertEqual([f["name"] for f in flavors], ["m1.small", "m1.large"])
        self.assertEqual(
            flavors[1]["sites"],
            [
                {
                    "offering_uuid": site.uuid.hex,
                    "flavor_uuid": site_flavors["m1.large"].uuid.hex,
                }
                for site, site_flavors in zip(self.sites, self.flavors)
            ],
        )

    def test_a_single_site_offers_all_its_flavors(self):
        names = [f["name"] for f in k8s_sites.common_flavors(self.sites[:1])]
        self.assertEqual(names, ["m1.small", "m1.odd", "m1.large", "only-first"])

    def test_a_name_used_twice_on_a_site_is_left_out(self):
        openstack_factories.FlavorFactory(
            settings=self.sites[1].scope, name="m1.large", cores=4, ram=8192
        )
        names = [f["name"] for f in k8s_sites.common_flavors(self.sites)]
        self.assertEqual(names, ["m1.small"])


class K8sSitesEndpointTest(SitesMixin, test.APITestCase):
    def setUp(self):
        self.create_sites()
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            customer=self.provider,
            options={
                "order": ["cluster"],
                "options": {
                    "cluster": {
                        "type": "k8s_cluster_config",
                        "label": "Cluster",
                        "default_configs": self.policy(),
                    }
                },
            },
        )
        self.client.force_authenticate(self.fixture.user)

    def get(self, **params):
        return self.client.get(
            factories.OfferingFactory.get_public_url(self.offering, "k8s_sites"),
            params,
        )

    def test_lists_sites_and_common_flavors(self):
        response = self.get(option="cluster", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["sites"],
            [{"uuid": site.uuid.hex, "name": site.name} for site in self.sites],
        )
        self.assertEqual(
            [f["name"] for f in response.data["flavors"]], ["m1.small", "m1.large"]
        )

    def test_single_site_topology(self):
        response = self.get(option="cluster", topology="1-datacenter")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["sites"]), 1)
        self.assertIn("only-first", [f["name"] for f in response.data["flavors"]])

    def test_resource_option_is_served_too(self):
        self.offering.resource_options = {
            "order": ["cluster_change"],
            "options": {"cluster_change": self.offering.options["options"]["cluster"]},
        }
        self.offering.save()
        response = self.get(option="cluster_change", topology="1-datacenter")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_unknown_option_is_refused(self):
        response = self.get(option="missing", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_option_where_the_customer_picks_is_refused(self):
        options = copy.deepcopy(self.offering.options)
        options["options"]["cluster"]["default_configs"]["infrastructure_mode"] = (
            "customer_choice"
        )
        self.offering.options = options
        self.offering.save()
        response = self.get(option="cluster", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_site_taken_down_by_its_owner_is_not_served(self):
        self.sites[1].state = OfferingStates.ARCHIVED
        self.sites[1].save()
        response = self.get(option="cluster", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_private_site_of_another_organisation_is_not_served(self):
        self.sites[1].customer = structure_factories.CustomerFactory()
        self.sites[1].shared = False
        self.sites[1].save()
        response = self.get(option="cluster", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("Site 1", str(response.data))

    def test_topology_the_option_does_not_offer_is_refused(self):
        options = copy.deepcopy(self.offering.options)
        options["options"]["cluster"]["default_configs"]["topology_mode"] = (
            "1-datacenter"
        )
        self.offering.options = options
        self.offering.save()
        response = self.get(option="cluster", topology="3-datacenter")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class FixedSitesOrderValidationTest(SitesMixin, test.APITestCase):
    def setUp(self):
        self.create_sites()

    def validate(self, value, policy=None):
        return validate_options(
            {
                "cluster": {
                    "type": "k8s_cluster_config",
                    "label": "Cluster",
                    "default_configs": policy or self.policy(),
                }
            },
            {"cluster": value},
        )

    def assertRefused(self, value, policy=None):
        with self.assertRaises(ValidationError) as cm:
            self.validate(value, policy)
        self.assertIn("cluster", cm.exception.detail)

    def test_cluster_on_the_provider_sites_is_accepted(self):
        self.validate(self.cluster())
        self.validate(self.cluster(topology="1-datacenter", flavor="only-first"))

    def test_other_site_is_refused(self):
        value = self.cluster()
        value["datacenters"][1]["openstack_infrastructure"]["uuid"] = (
            factories.OfferingFactory(type=OPENSTACK_TENANT_OFFERING).uuid.hex
        )
        self.assertRefused(value)

    def test_sites_in_another_order_are_refused(self):
        value = self.cluster()
        value["datacenters"].reverse()
        self.assertRefused(value)

    def test_flavor_of_another_site_is_refused(self):
        value = self.cluster()
        value["datacenters"][1]["node_groups"][0]["openstack_flavor"]["uuid"] = (
            self.flavors[0]["m1.large"].uuid.hex
        )
        self.assertRefused(value)

    def test_flavor_missing_on_a_site_is_refused(self):
        value = self.cluster()
        for index, datacenter in enumerate(value["datacenters"]):
            flavor = (
                self.flavors[index].get("only-first") or self.flavors[index]["m1.large"]
            )
            datacenter["node_groups"][0]["openstack_flavor"] = {
                "uuid": flavor.uuid.hex,
                "name": "only-first",
            }
        self.assertRefused(value)

    def test_flavor_sized_differently_between_sites_is_refused(self):
        self.assertRefused(self.cluster(flavor="m1.odd"))

    def test_different_node_groups_between_sites_are_refused(self):
        value = self.cluster()
        value["datacenters"][2]["node_groups"][0]["node_count"] = 5
        self.assertRefused(value)

    def test_worker_flavor_below_the_minimum_is_refused(self):
        self.assertRefused(
            self.cluster(flavor="m1.small"),
            self.policy(minimal_worker_vcpus=4),
        )

    def test_value_without_datacenters_is_refused(self):
        self.assertRefused({"kubernetes_version": "1.30.0"})
        self.assertRefused({"kubernetes_version": "1.30.0", "datacenters": {}})

    @data(
        lambda v: v["datacenters"].__setitem__(0, "x"),
        lambda v: v["datacenters"][0].__setitem__("openstack_infrastructure", "x"),
        lambda v: v["datacenters"][0].__setitem__("node_groups", {"a": 1}),
        lambda v: v["datacenters"][0].__setitem__("node_groups", ["x"]),
        lambda v: v["datacenters"][0]["node_groups"][0].__setitem__(
            "openstack_flavor", "small"
        ),
        lambda v: v["datacenters"][0]["node_groups"][0]["openstack_flavor"].__setitem__(
            "name", ["a"]
        ),
        lambda v: [
            dc["node_groups"][0].__setitem__("type", "WORKER")
            for dc in v["datacenters"]
        ],
    )
    def test_malformed_cluster_is_refused_not_crashed(self, mutate):
        value = self.cluster()
        mutate(value)
        self.assertRefused(value)

    def test_customer_choice_mode_is_not_checked(self):
        value = self.cluster()
        value["datacenters"].reverse()
        self.validate(value, {"infrastructure_mode": "customer_choice"})


class OfferingSitesOwnershipTest(SitesMixin, test.APITestCase):
    def setUp(self):
        self.create_sites()

    def options(self, policy=None):
        return {
            "order": ["cluster"],
            "options": {
                "cluster": {
                    "type": "k8s_cluster_config",
                    "label": "Cluster",
                    "default_configs": policy or self.policy(),
                }
            },
        }

    def test_own_active_sites_are_accepted(self):
        k8s_sites.check_offering_sites(self.options(), self.provider)

    def test_private_site_of_another_organisation_is_refused(self):
        foreign = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE, shared=False
        )
        policy = self.policy()
        policy["fixed_openstack_offerings"]["1-datacenter"] = [foreign.uuid.hex]
        with self.assertRaises(ValidationError) as cm:
            k8s_sites.check_offering_sites(self.options(policy), self.provider)
        self.assertIn(foreign.uuid.hex, str(cm.exception.detail))

    def test_shared_site_of_another_organisation_is_accepted(self):
        shared = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE, shared=True
        )
        policy = self.policy()
        policy["fixed_openstack_offerings"]["1-datacenter"] = [shared.uuid.hex]
        k8s_sites.check_offering_sites(self.options(policy), self.provider)

    def test_site_shared_only_with_other_organisation_groups_is_refused(self):
        restricted = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE, shared=True
        )
        restricted.organization_groups.add(
            structure_factories.OrganizationGroupFactory()
        )
        policy = self.policy()
        policy["fixed_openstack_offerings"]["1-datacenter"] = [restricted.uuid.hex]
        with self.assertRaises(ValidationError):
            k8s_sites.check_offering_sites(self.options(policy), self.provider)

    def test_archived_site_is_refused(self):
        self.sites[2].state = OfferingStates.ARCHIVED
        self.sites[2].save()
        with self.assertRaises(ValidationError):
            k8s_sites.check_offering_sites(self.options(), self.provider)

    def test_customer_choice_options_are_not_checked(self):
        k8s_sites.check_offering_sites(
            self.options({"infrastructure_mode": "customer_choice"}), self.provider
        )

    def test_update_options_refuses_another_organisations_private_site(self):
        offering = factories.OfferingFactory(customer=self.provider)
        foreign = factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE, shared=False
        )
        policy = self.policy()
        policy["fixed_openstack_offerings"]["1-datacenter"] = [foreign.uuid.hex]
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        url = factories.OfferingFactory.get_url(offering, "update_options")
        response = self.client.post(
            url, {"options": self.options(policy)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(foreign.uuid.hex, str(response.data))

        response = self.client.post(url, {"options": self.options()}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class SharedSiteEndpointTest(SitesMixin, test.APITestCase):
    def test_shared_site_of_another_organisation_is_served(self):
        self.create_sites()
        self.sites[2].customer = structure_factories.CustomerFactory()
        self.sites[2].shared = True
        self.sites[2].save()
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            customer=self.provider,
            options={
                "order": ["cluster"],
                "options": {
                    "cluster": {
                        "type": "k8s_cluster_config",
                        "label": "Cluster",
                        "default_configs": self.policy(),
                    }
                },
            },
        )
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(
            factories.OfferingFactory.get_public_url(offering, "k8s_sites"),
            {"option": "cluster", "topology": "3-datacenter"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["sites"]), 3)
