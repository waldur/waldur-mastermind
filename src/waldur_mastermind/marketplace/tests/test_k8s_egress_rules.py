import copy
import importlib

from ddt import data, ddt, unpack
from django.apps import apps
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.serializers import (
    K8sEgressRuleSerializer,
    validate_options,
)
from waldur_mastermind.marketplace import models, serializers, utils
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

migration = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0333_move_k8s_egress_rules"
)

NETWORK_RULE = {
    "name": "Storage",
    "destination_type": "cidr",
    "destination": "10.20.0.0/16",
}
DOMAIN_RULE = {
    "name": "Registry",
    "destination_type": "domain",
    "destination": "*.docker.io",
    "protocol": "TCP",
    "port_range_min": 443,
    "port_range_max": 443,
}
BASELINE_RULE = {
    "name": "DNS",
    "destination_type": "cidr",
    "destination": "10.0.0.53/32",
    "protocol": "UDP",
    "port_range_min": 53,
    "port_range_max": 53,
}


def k8s_options(**default_configs):
    return {
        "order": ["cluster_config"],
        "options": {
            "cluster_config": {
                "type": "single_datacenter_k8s_config",
                "label": "Cluster configuration",
                "default_configs": default_configs,
            }
        },
    }


EGRESS_RESOURCE_OPTIONS = {
    "order": ["egress"],
    "options": {"egress": {"type": "k8s_egress_rules", "label": "Egress rules"}},
}


@ddt
class EgressRuleTest(test.APISimpleTestCase):
    def assert_valid(self, rule):
        serializer = K8sEgressRuleSerializer(data=rule)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def assert_invalid(self, rule, field):
        serializer = K8sEgressRuleSerializer(data=rule)
        self.assertFalse(serializer.is_valid())
        self.assertIn(field, serializer.errors)

    @data(
        ("cidr", "10.0.0.0/8"),
        ("cidr", "192.168.1.10/32"),
        ("cidr", "2001:db8::/32"),
        ("domain", "registry.example.com"),
        ("domain", "*.example.com"),
        ("domain", "Mirror-1.Example.org"),
    )
    @unpack
    def test_valid_destination_is_accepted(self, destination_type, destination):
        self.assert_valid(
            {
                "name": "Rule",
                "destination_type": destination_type,
                "destination": destination,
            }
        )

    @data(
        ("cidr", "10.0.0.0/33"),
        ("cidr", "example.com"),
        # The order form needs an explicit prefix length; so does the server.
        ("cidr", "10.1.2.3"),
        ("cidr", "10.0.0.0/255.0.0.0"),
        ("cidr", "fe80::1%eth0/64"),
        ("domain", "1.2.3.4"),
        ("domain", "10.0.0.0/8"),
        ("domain", "localhost"),
        ("domain", "registry.*.example.com"),
        ("domain", "-bad.example.com"),
        ("domain", "example.com."),
        ("domain", "*"),
    )
    @unpack
    def test_invalid_destination_is_rejected(self, destination_type, destination):
        self.assert_invalid(
            {
                "name": "Rule",
                "destination_type": destination_type,
                "destination": destination,
            },
            "destination",
        )

    def test_protocol_and_ports_are_optional(self):
        self.assert_valid({**NETWORK_RULE, "protocol": "", "port_range_min": None})

    def test_single_port_is_accepted(self):
        self.assert_valid({**NETWORK_RULE, "protocol": "TCP", "port_range_min": 22})

    def test_single_port_closes_its_range(self):
        serializer = K8sEgressRuleSerializer(
            data={**NETWORK_RULE, "protocol": "TCP", "port_range_min": 22}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["port_range_max"], 22)

    @data(
        ({"port_range_min": 0}, "port_range_min"),
        ({"port_range_min": 1, "port_range_max": 65536}, "port_range_max"),
        ({"port_range_min": 100, "port_range_max": 80}, "port_range_max"),
        ({"port_range_max": 80}, "port_range_min"),
        ({"protocol": "ICMP", "port_range_min": 8}, "port_range_min"),
        ({"protocol": "SCTP"}, "protocol"),
    )
    @unpack
    def test_invalid_ports_are_rejected(self, change, field):
        self.assert_invalid({**NETWORK_RULE, **change}, field)

    def test_name_is_required(self):
        self.assert_invalid({**NETWORK_RULE, "name": ""}, "name")


class ClusterConfigEgressRulesTest(test.APISimpleTestCase):
    def validate(self, config, **default_configs):
        return validate_options(
            k8s_options(**default_configs)["options"], {"cluster_config": config}
        )

    def assert_rejected(self, config, **default_configs):
        with self.assertRaises(ValidationError) as cm:
            self.validate(config, **default_configs)
        self.assertIn("cluster_config", cm.exception.detail)
        return cm.exception.detail["cluster_config"]

    def test_rules_are_accepted_when_enabled(self):
        self.validate(
            {"egress_rules": [NETWORK_RULE, DOMAIN_RULE]},
            egress_rules_enabled=True,
            egress_domain_rules_allowed=True,
        )

    def test_rules_are_rejected_when_disabled(self):
        self.assert_rejected({"egress_rules": [NETWORK_RULE]})

    def test_empty_rules_are_accepted_when_disabled(self):
        self.validate({"egress_rules": []})

    def test_domain_rule_is_rejected_unless_allowed(self):
        self.assert_rejected(
            {"egress_rules": [NETWORK_RULE, DOMAIN_RULE]}, egress_rules_enabled=True
        )
        self.validate({"egress_rules": [NETWORK_RULE]}, egress_rules_enabled=True)

    def test_rules_are_stored_cleaned(self):
        values = self.validate(
            {
                "kubernetes_version": "1.30.0",
                "egress_rules": [
                    {
                        **NETWORK_RULE,
                        "uuid": "row-1",
                        "destination": " 10.20.0.0/16 ",
                        "protocol": "TCP",
                        "port_range_min": "443",
                        "junk": {"x": 1},
                    }
                ],
            },
            egress_rules_enabled=True,
        )
        config = values["cluster_config"]
        self.assertEqual(config["kubernetes_version"], "1.30.0")
        self.assertEqual(
            config["egress_rules"],
            [
                {
                    "uuid": "row-1",
                    "name": "Storage",
                    "destination_type": "cidr",
                    "destination": "10.20.0.0/16",
                    "protocol": "TCP",
                    "port_range_min": 443,
                    "port_range_max": 443,
                }
            ],
        )

    def test_invalid_rule_is_reported_under_egress_rules(self):
        detail = self.assert_rejected(
            {"egress_rules": [{**NETWORK_RULE, "destination": "nope"}]},
            egress_rules_enabled=True,
        )
        self.assertIn("egress_rules", detail)

    def test_ingress_rules_without_egress_rows_are_accepted(self):
        self.validate(
            {
                "public_access_rules": [
                    {"name": "HTTP", "cidr": "0.0.0.0/0", "direction": "ingress"}
                ],
                "administrative_access_rules": [{"name": "SSH", "cidr": "10.0.0.0/8"}],
            }
        )

    def test_egress_row_in_ingress_table_is_rejected(self):
        self.assert_rejected(
            {"public_access_rules": [{"name": "Out", "direction": "egress"}]}
        )

    def test_malformed_ingress_rules_are_rejected(self):
        self.assert_rejected({"administrative_access_rules": "10.0.0.0/8"})
        self.assert_rejected({"administrative_access_rules": ["10.0.0.0/8"]})


class EgressPolicyTest(test.APISimpleTestCase):
    def test_policy_is_kept_on_offering_options(self):
        serializer = serializers.OfferingOptionsSerializer(
            data=k8s_options(
                egress_rules_enabled=True,
                egress_domain_rules_allowed=True,
                egress_baseline_rules=[BASELINE_RULE],
            )
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        default_configs = serializer.validated_data["options"]["cluster_config"][
            "default_configs"
        ]
        self.assertTrue(default_configs["egress_rules_enabled"])
        self.assertTrue(default_configs["egress_domain_rules_allowed"])
        self.assertEqual(
            default_configs["egress_baseline_rules"][0]["destination"], "10.0.0.53/32"
        )

    def test_invalid_baseline_rule_is_rejected(self):
        serializer = serializers.OfferingOptionsSerializer(
            data=k8s_options(
                egress_baseline_rules=[{**BASELINE_RULE, "destination": "nope"}]
            )
        )
        self.assertFalse(serializer.is_valid())

    def test_egress_rules_type_is_refused_as_order_option(self):
        options = k8s_options()
        options["options"]["egress"] = {"type": "k8s_egress_rules", "label": "Egress"}
        serializer = serializers.OfferingOptionsSerializer(data=options)
        self.assertFalse(serializer.is_valid())
        self.assertIn("options", serializer.errors)


class EgressResourceOptionPairingTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            options=k8s_options(
                egress_rules_enabled=True,
                egress_baseline_rules=[BASELINE_RULE],
                topology_mode="1-datacenter",
            ),
        )

    def update_resource_options(self, resource_options):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_resource_options"
        )
        return self.client.post(
            url, {"resource_options": resource_options}, format="json"
        )

    def test_resource_option_takes_policy_of_cluster_option(self):
        response = self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        option = self.offering.resource_options["options"]["egress"]
        self.assertEqual(option["k8s_config_option"], "cluster_config")
        self.assertEqual(
            option["default_configs"],
            {"egress_rules_enabled": True, "egress_baseline_rules": [BASELINE_RULE]},
        )

    def test_resource_option_needs_one_cluster_option(self):
        self.offering.options = {"order": [], "options": {}}
        self.offering.save()
        response = self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        options = k8s_options()
        options["options"]["second"] = {
            **options["options"]["cluster_config"],
            "type": "multi_datacenter_k8s_config",
        }
        self.offering.options = options
        self.offering.save()
        response = self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_changed_policy_reaches_resource_option(self):
        self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        url = factories.OfferingFactory.get_url(self.offering, "update_options")
        response = self.client.post(
            url,
            {
                "options": k8s_options(
                    egress_rules_enabled=True, egress_domain_rules_allowed=True
                )
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.resource_options["options"]["egress"]["default_configs"],
            {"egress_rules_enabled": True, "egress_domain_rules_allowed": True},
        )

    def test_new_resource_takes_ordered_egress_rules(self):
        self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.offering.refresh_from_db()
        values = utils.resource_options_from_attributes(
            self.offering,
            {"cluster_config": {"egress_rules": [NETWORK_RULE]}},
        )
        self.assertEqual(values["egress"], [NETWORK_RULE])

        values = utils.resource_options_from_attributes(
            self.offering, {"cluster_config": {"kubernetes_version": "1.30.0"}}
        )
        self.assertEqual(values["egress"], [])

        values = utils.resource_options_from_attributes(self.offering, {})
        self.assertNotIn("egress", values)

    def test_attribute_named_like_the_option_does_not_seed_it(self):
        self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.offering.refresh_from_db()
        values = utils.resource_options_from_attributes(
            self.offering,
            {
                "cluster_config": {"egress_rules": [NETWORK_RULE]},
                "egress": [{"destination": "anything"}],
            },
        )
        self.assertEqual(values["egress"], [NETWORK_RULE])

    def test_existing_resources_get_their_ordered_rules(self):
        ordered = factories.ResourceFactory(
            offering=self.offering,
            attributes={"cluster_config": {"egress_rules": [NETWORK_RULE]}},
        )
        unrestricted = factories.ResourceFactory(
            offering=self.offering,
            attributes={"cluster_config": {"kubernetes_version": "1.30.0"}},
        )
        changed = factories.ResourceFactory(
            offering=self.offering,
            attributes={"cluster_config": {"egress_rules": [NETWORK_RULE]}},
            options={"egress": []},
        )
        response = self.update_resource_options(EGRESS_RESOURCE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        for resource, expected in (
            (ordered, [NETWORK_RULE]),
            (unrestricted, []),
            (changed, []),
        ):
            resource.refresh_from_db()
            self.assertEqual(resource.options["egress"], expected)


class EgressResourceOptionUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        offering = self.fixture.offering
        offering.options = k8s_options(egress_rules_enabled=True)
        offering.resource_options = {
            "order": ["egress"],
            "options": {
                "egress": {
                    "type": "k8s_egress_rules",
                    "label": "Egress rules",
                    "k8s_config_option": "cluster_config",
                    "default_configs": {"egress_rules_enabled": True},
                }
            },
        }
        offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")

    def update(self, rules):
        return self.client.post(self.url, {"options": {"egress": rules}}, format="json")

    def test_customer_changes_egress_rules(self):
        response = self.update([NETWORK_RULE])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["egress"], [NETWORK_RULE])

    def test_rule_outside_policy_is_rejected(self):
        response = self.update([DOMAIN_RULE])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_rule_is_rejected(self):
        response = self.update([{**NETWORK_RULE, "port_range_min": 70000}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EgressRulesOrderTest(BaseOrderCreateTest):
    def create_k8s_order(self, egress_rules, **default_configs):
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=k8s_options(**default_configs),
        )
        return self.create_order(
            self.fixture.staff,
            offering,
            add_payload={
                "attributes": {"cluster_config": {"egress_rules": egress_rules}}
            },
        )

    def test_order_with_egress_rules_is_created(self):
        response = self.create_k8s_order([NETWORK_RULE], egress_rules_enabled=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["attributes"]["cluster_config"]["egress_rules"],
            [NETWORK_RULE],
        )

    def test_resource_of_new_order_holds_ordered_rules(self):
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=k8s_options(egress_rules_enabled=True),
            resource_options={
                "order": ["egress"],
                "options": {
                    "egress": {
                        "type": "k8s_egress_rules",
                        "label": "Egress rules",
                        "k8s_config_option": "cluster_config",
                        "default_configs": {"egress_rules_enabled": True},
                    }
                },
            },
        )
        response = self.create_order(
            self.fixture.staff,
            offering,
            add_payload={
                "attributes": {"cluster_config": {"egress_rules": [NETWORK_RULE]}}
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.options["egress"], [NETWORK_RULE])

    def test_order_with_egress_rules_is_rejected_when_disabled(self):
        response = self.create_k8s_order([NETWORK_RULE])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cluster_config", response.data)


class MoveEgressRowsTest(test.APISimpleTestCase):
    INGRESS = {
        "uuid": "http",
        "name": "HTTP",
        "protocol": "TCP",
        "port_range_min": 80,
        "port_range_max": 80,
        "cidr": "0.0.0.0/0",
        "direction": "ingress",
    }
    EGRESS = {
        "uuid": "out",
        "name": "Mirror",
        "protocol": "TCP",
        "port_range_min": 443,
        "port_range_max": 443,
        "cidr": "192.0.2.0/24",
        "direction": "egress",
        "description": "OS mirror",
    }

    def test_egress_rows_move_to_egress_rules(self):
        config = {
            "kubernetes_version": "1.30.0",
            "public_access_rules": [self.INGRESS, self.EGRESS],
            "administrative_access_rules": [{**self.EGRESS, "uuid": "admin-out"}],
        }
        moved = migration.move_egress_rows(copy.deepcopy(config))
        self.assertEqual(moved["public_access_rules"], [self.INGRESS])
        self.assertEqual(moved["administrative_access_rules"], [])
        self.assertEqual(
            moved["egress_rules"][0],
            {
                "uuid": "out",
                "name": "Mirror",
                "protocol": "TCP",
                "port_range_min": 443,
                "port_range_max": 443,
                "description": "OS mirror",
                "destination_type": "cidr",
                "destination": "192.0.2.0/24",
            },
        )
        self.assertEqual(moved["egress_rules"][1]["uuid"], "admin-out")

    def test_rows_the_old_form_allowed_are_made_valid(self):
        rows = [
            {"name": "", "cidr": "10.0.0.0/8", "direction": "egress"},
            {
                "name": "Cleared",
                "protocol": "TCP",
                "port_range_min": "",
                "port_range_max": "",
                "cidr": "10.0.0.0/8",
                "direction": "egress",
            },
            {
                "name": "Ping",
                "protocol": "ICMP",
                "port_range_min": 80,
                "port_range_max": 80,
                "cidr": "10.0.0.0/8",
                "direction": "egress",
            },
            {
                "name": "Upper only",
                "protocol": "UDP",
                "port_range_max": 53,
                "cidr": "10.0.0.0/8",
                "direction": "egress",
            },
        ]
        moved = migration.move_egress_rows({"public_access_rules": rows})
        names = [rule["name"] for rule in moved["egress_rules"]]
        self.assertEqual(names, ["Egress rule 1", "Cleared", "Ping", "Upper only"])
        for rule in moved["egress_rules"]:
            serializer = K8sEgressRuleSerializer(data=rule)
            self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(moved["egress_rules"][2]["port_range_min"])
        self.assertEqual(moved["egress_rules"][3]["port_range_min"], 53)

    def test_config_without_egress_rows_is_left_alone(self):
        self.assertIsNone(
            migration.move_egress_rows({"public_access_rules": [self.INGRESS]})
        )
        self.assertIsNone(migration.move_egress_rows({"kubernetes_version": "1.30"}))
        self.assertIsNone(migration.move_egress_rows("not a config"))

    def test_reverse_restores_network_rules(self):
        moved = migration.move_egress_rows(
            {"public_access_rules": [self.INGRESS, self.EGRESS]}
        )
        moved["egress_rules"].append(DOMAIN_RULE)
        restored = migration.restore_egress_rows(moved)
        self.assertEqual(restored["public_access_rules"], [self.INGRESS, self.EGRESS])
        self.assertEqual(restored["egress_rules"], [DOMAIN_RULE])


class MoveEgressRowsMigrationTest(test.APITestCase):
    def test_orders_and_resources_are_migrated_and_offering_enabled(self):
        offering = factories.OfferingFactory(options=k8s_options())
        config = {"public_access_rules": [MoveEgressRowsTest.EGRESS]}
        order = factories.OrderFactory(
            offering=offering, attributes={"cluster_config": copy.deepcopy(config)}
        )
        resource = factories.ResourceFactory(
            offering=offering, attributes={"cluster_config": copy.deepcopy(config)}
        )
        untouched = factories.OfferingFactory(options=k8s_options())

        migration.forwards(apps, None)

        for row in (order, resource):
            row.refresh_from_db()
            self.assertEqual(
                row.attributes["cluster_config"]["public_access_rules"], []
            )
            self.assertEqual(
                row.attributes["cluster_config"]["egress_rules"][0]["destination"],
                "192.0.2.0/24",
            )
        offering.refresh_from_db()
        self.assertTrue(
            offering.options["options"]["cluster_config"]["default_configs"][
                "egress_rules_enabled"
            ]
        )
        untouched.refresh_from_db()
        self.assertNotIn(
            "egress_rules_enabled",
            untouched.options["options"]["cluster_config"]["default_configs"],
        )
