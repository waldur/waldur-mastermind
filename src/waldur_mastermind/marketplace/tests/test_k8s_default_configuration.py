from ddt import data, ddt, unpack
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest


class K8sLoadBalancerModeTest(test.APISimpleTestCase):
    def build_option(self, default_configs):
        return {
            "type": "single_datacenter_k8s_config",
            "label": "Cluster configuration",
            "default_configs": default_configs,
        }

    def test_load_balancer_mode_accepts_known_values(self):
        for mode in ("required", "optional", "disabled"):
            serializer = serializers.OptionFieldSerializer(
                data=self.build_option({"load_balancer_mode": mode})
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(
                serializer.validated_data["default_configs"]["load_balancer_mode"],
                mode,
            )

    def test_load_balancer_mode_rejects_unknown_value(self):
        serializer = serializers.OptionFieldSerializer(
            data=self.build_option({"load_balancer_mode": "sometimes"})
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("load_balancer_mode", serializer.errors["default_configs"])

    def test_load_balancer_mode_may_be_omitted(self):
        serializer = serializers.OptionFieldSerializer(
            data=self.build_option({"default_lb_vcpus": 2})
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn(
            "load_balancer_mode", serializer.validated_data["default_configs"]
        )

    def test_load_balancer_mode_is_kept_on_offering_options(self):
        serializer = serializers.OfferingOptionsSerializer(
            data={
                "order": ["cluster_config"],
                "options": {
                    "cluster_config": {
                        "type": "multi_datacenter_k8s_config",
                        "label": "Cluster configuration",
                        "default_configs": {"load_balancer_mode": "optional"},
                    }
                },
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["options"]["cluster_config"]["default_configs"][
                "load_balancer_mode"
            ],
            "optional",
        )


class K8sTopologyModeTest(test.APISimpleTestCase):
    def build_option(self, default_configs, field_type="single_datacenter_k8s_config"):
        return {
            "type": field_type,
            "label": "Cluster configuration",
            "default_configs": default_configs,
        }

    def test_topology_mode_accepts_known_values(self):
        for field_type in (
            "single_datacenter_k8s_config",
            "multi_datacenter_k8s_config",
        ):
            for mode in ("1-datacenter", "3-datacenter", "customer_choice"):
                serializer = serializers.OptionFieldSerializer(
                    data=self.build_option({"topology_mode": mode}, field_type)
                )
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(
                    serializer.validated_data["default_configs"]["topology_mode"],
                    mode,
                )

    def test_topology_mode_rejects_unknown_value(self):
        serializer = serializers.OptionFieldSerializer(
            data=self.build_option({"topology_mode": "2-datacenter"})
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("topology_mode", serializer.errors["default_configs"])

    def test_topology_mode_may_be_omitted(self):
        serializer = serializers.OptionFieldSerializer(
            data=self.build_option({"default_lb_vcpus": 2})
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("topology_mode", serializer.validated_data["default_configs"])


def build_cluster_config(topology, datacenter_count):
    config = {
        "kubernetes_version": "1.30.0",
        "datacenters": [
            {"id": f"datacenter-{i + 1}", "name": f"Datacenter {i + 1}"}
            for i in range(datacenter_count)
        ],
    }
    if topology is not None:
        config["topology"] = topology
    return config


@ddt
class K8sClusterConfigValidationTest(test.APISimpleTestCase):
    def build_options(self, field_type, topology_mode=None):
        default_configs = {"available_kubernetes_versions": "1.30.0"}
        if topology_mode:
            default_configs["topology_mode"] = topology_mode
        return {
            "cluster_config": {
                "type": field_type,
                "label": "Cluster configuration",
                "default_configs": default_configs,
            }
        }

    @data(
        # No topology_mode: the option type decides.
        ("single_datacenter_k8s_config", None, "1-datacenter", 1),
        ("multi_datacenter_k8s_config", None, "3-datacenter", 3),
        # A fixed mode overrides the type.
        ("single_datacenter_k8s_config", "3-datacenter", "3-datacenter", 3),
        ("multi_datacenter_k8s_config", "1-datacenter", "1-datacenter", 1),
        # The customer picks.
        ("single_datacenter_k8s_config", "customer_choice", "1-datacenter", 1),
        ("single_datacenter_k8s_config", "customer_choice", "3-datacenter", 3),
        ("multi_datacenter_k8s_config", "customer_choice", "1-datacenter", 1),
        # Values without a topology key are checked against the fixed topology.
        ("single_datacenter_k8s_config", None, None, 1),
        ("multi_datacenter_k8s_config", "customer_choice", None, 3),
    )
    @unpack
    def test_allowed_config_is_accepted(
        self, field_type, topology_mode, topology, datacenter_count
    ):
        validate_options(
            self.build_options(field_type, topology_mode),
            {"cluster_config": build_cluster_config(topology, datacenter_count)},
        )

    @data(
        ("single_datacenter_k8s_config", None),
        ("multi_datacenter_k8s_config", "customer_choice"),
    )
    @unpack
    def test_non_string_topology_is_rejected(self, field_type, topology_mode):
        for topology in (["1-datacenter"], {"name": "1-datacenter"}, 1):
            config = build_cluster_config(None, 1)
            config["topology"] = topology
            with self.assertRaises(ValidationError) as cm:
                validate_options(
                    self.build_options(field_type, topology_mode),
                    {"cluster_config": config},
                )
            self.assertIn("cluster_config", cm.exception.detail)

    @data(0, 2, 4)
    def test_customer_choice_without_topology_needs_one_or_three_datacenters(
        self, datacenter_count
    ):
        with self.assertRaises(ValidationError) as cm:
            validate_options(
                self.build_options("single_datacenter_k8s_config", "customer_choice"),
                {"cluster_config": build_cluster_config(None, datacenter_count)},
            )
        self.assertIn("cluster_config", cm.exception.detail)

    def test_customer_choice_value_without_topology_or_datacenters_is_accepted(self):
        validate_options(
            self.build_options("single_datacenter_k8s_config", "customer_choice"),
            {"cluster_config": {"kubernetes_version": "1.30.0"}},
        )

    def test_value_without_topology_or_datacenters_is_accepted(self):
        validate_options(
            self.build_options("multi_datacenter_k8s_config"),
            {"cluster_config": {"kubernetes_version": "1.30.0"}},
        )

    @data(
        ("single_datacenter_k8s_config", None, "3-datacenter", 3),
        ("multi_datacenter_k8s_config", None, "1-datacenter", 1),
        ("single_datacenter_k8s_config", "3-datacenter", "1-datacenter", 1),
        ("multi_datacenter_k8s_config", "customer_choice", "2-datacenter", 2),
    )
    @unpack
    def test_disallowed_topology_is_rejected(
        self, field_type, topology_mode, topology, datacenter_count
    ):
        with self.assertRaises(ValidationError) as cm:
            validate_options(
                self.build_options(field_type, topology_mode),
                {"cluster_config": build_cluster_config(topology, datacenter_count)},
            )
        self.assertIn("cluster_config", cm.exception.detail)

    @data(
        ("single_datacenter_k8s_config", None, "1-datacenter", 3),
        ("multi_datacenter_k8s_config", None, "3-datacenter", 1),
        ("single_datacenter_k8s_config", "customer_choice", "3-datacenter", 2),
        ("multi_datacenter_k8s_config", None, None, 1),
    )
    @unpack
    def test_datacenter_count_mismatch_is_rejected(
        self, field_type, topology_mode, topology, datacenter_count
    ):
        with self.assertRaises(ValidationError) as cm:
            validate_options(
                self.build_options(field_type, topology_mode),
                {"cluster_config": build_cluster_config(topology, datacenter_count)},
            )
        self.assertIn("cluster_config", cm.exception.detail)


class K8sTopologyOrderTest(BaseOrderCreateTest):
    def create_k8s_offering(self, topology_mode):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options={
                "order": ["cluster_config"],
                "options": {
                    "cluster_config": {
                        "type": "single_datacenter_k8s_config",
                        "label": "Cluster configuration",
                        "default_configs": {"topology_mode": topology_mode},
                    }
                },
            },
        )

    def create_k8s_order(self, topology_mode, topology, datacenter_count):
        return self.create_order(
            self.fixture.staff,
            self.create_k8s_offering(topology_mode),
            add_payload={
                "attributes": {
                    "cluster_config": build_cluster_config(topology, datacenter_count)
                }
            },
        )

    def test_order_with_allowed_topology_is_created(self):
        response = self.create_k8s_order("3-datacenter", "3-datacenter", 3)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["attributes"]["cluster_config"]["topology"], "3-datacenter"
        )

    def test_order_with_disallowed_topology_is_rejected(self):
        response = self.create_k8s_order("3-datacenter", "1-datacenter", 1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cluster_config", response.data)

    def test_order_with_non_string_topology_is_rejected(self):
        response = self.create_order(
            self.fixture.staff,
            self.create_k8s_offering("customer_choice"),
            add_payload={
                "attributes": {
                    "cluster_config": {
                        "topology": ["1-datacenter"],
                        "datacenters": [{"id": "datacenter-1"}],
                    }
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cluster_config", response.data)

    def test_order_with_mismatched_datacenters_is_rejected(self):
        response = self.create_k8s_order("customer_choice", "1-datacenter", 3)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cluster_config", response.data)
