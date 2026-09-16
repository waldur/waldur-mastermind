from rest_framework import test

from waldur_mastermind.marketplace import serializers


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
