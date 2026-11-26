"""The Kubernetes cluster option type and the migration from the two it replaced."""

import hashlib
import json
from importlib import import_module

from ddt import data, ddt
from django.apps import apps
from django.test import TestCase
from rest_framework import test

from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.tests import factories

# The module name starts with a digit, so it cannot be imported by name.
migration = import_module(
    "waldur_mastermind.marketplace.migrations.0338_merge_k8s_option_types"
)


def k8s_option(field_type, default_configs=None):
    option = {"type": field_type, "label": "Cluster configuration"}
    if default_configs is not None:
        option["default_configs"] = default_configs
    return option


@ddt
class K8sClusterConfigTypeTest(test.APISimpleTestCase):
    def test_cluster_config_without_topology_mode_is_left_without_one(self):
        serializer = serializers.OptionFieldSerializer(
            data=k8s_option("k8s_cluster_config", {"default_lb_vcpus": 2})
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["type"], "k8s_cluster_config")
        self.assertNotIn("topology_mode", serializer.validated_data["default_configs"])

    @data("single_datacenter_k8s_config", "multi_datacenter_k8s_config")
    def test_replaced_type_is_refused(self, field_type):
        serializer = serializers.OptionFieldSerializer(data=k8s_option(field_type))
        self.assertFalse(serializer.is_valid())
        self.assertIn("type", serializer.errors)


class MergeK8sOptionTypesMigrationTest(TestCase):
    def create_offering(self):
        return factories.OfferingFactory(
            options={
                "order": ["single", "multi", "fixed", "name"],
                "options": {
                    "single": k8s_option(
                        "single_datacenter_k8s_config",
                        {"available_kubernetes_versions": "1.30.0"},
                    ),
                    "multi": k8s_option("multi_datacenter_k8s_config"),
                    "fixed": k8s_option(
                        "multi_datacenter_k8s_config",
                        {"topology_mode": "customer_choice"},
                    ),
                    "name": {"type": "string", "label": "Name"},
                },
            },
            resource_options={
                "order": ["cluster"],
                "options": {"cluster": k8s_option("single_datacenter_k8s_config")},
            },
        )

    def test_forwards_converts_legacy_types_and_keeps_their_topology(self):
        offering = self.create_offering()
        migration.forwards(apps, None)
        offering.refresh_from_db()

        options = offering.options["options"]
        self.assertEqual(
            options["single"],
            k8s_option(
                "k8s_cluster_config",
                {
                    "available_kubernetes_versions": "1.30.0",
                    "topology_mode": "1-datacenter",
                },
            ),
        )
        self.assertEqual(
            options["multi"],
            k8s_option("k8s_cluster_config", {"topology_mode": "3-datacenter"}),
        )
        self.assertEqual(
            options["fixed"],
            k8s_option("k8s_cluster_config", {"topology_mode": "customer_choice"}),
        )
        self.assertEqual(options["name"], {"type": "string", "label": "Name"})
        self.assertEqual(
            offering.options["order"], ["single", "multi", "fixed", "name"]
        )
        self.assertEqual(
            offering.resource_options["options"]["cluster"]["type"],
            "k8s_cluster_config",
        )

    def test_backwards_restores_a_type_per_topology(self):
        offering = self.create_offering()
        migration.forwards(apps, None)
        migration.backwards(apps, None)
        offering.refresh_from_db()

        options = offering.options["options"]
        self.assertEqual(options["single"]["type"], "single_datacenter_k8s_config")
        self.assertEqual(options["multi"]["type"], "multi_datacenter_k8s_config")
        # customer_choice has no legacy type of its own; the mode stays set.
        self.assertEqual(options["fixed"]["type"], "single_datacenter_k8s_config")
        self.assertEqual(
            options["fixed"]["default_configs"]["topology_mode"], "customer_choice"
        )

    def test_backwards_spells_out_customer_choice(self):
        offering = factories.OfferingFactory(
            options={
                "order": ["cluster"],
                "options": {"cluster": k8s_option("k8s_cluster_config")},
            }
        )
        migration.backwards(apps, None)
        offering.refresh_from_db()
        self.assertEqual(
            offering.options["options"]["cluster"],
            k8s_option(
                "single_datacenter_k8s_config", {"topology_mode": "customer_choice"}
            ),
        )

    def test_blank_or_malformed_policy_gets_the_implied_topology(self):
        offering = factories.OfferingFactory(
            options={
                "order": ["blank", "missing", "listed"],
                "options": {
                    "blank": k8s_option(
                        "multi_datacenter_k8s_config", {"topology_mode": ""}
                    ),
                    "missing": k8s_option(
                        "multi_datacenter_k8s_config", {"topology_mode": None}
                    ),
                    "listed": {
                        **k8s_option("single_datacenter_k8s_config"),
                        "default_configs": ["not", "a", "dict"],
                    },
                },
            }
        )
        migration.forwards(apps, None)
        offering.refresh_from_db()

        options = offering.options["options"]
        self.assertEqual(
            options["blank"]["default_configs"], {"topology_mode": "3-datacenter"}
        )
        self.assertEqual(
            options["missing"]["default_configs"], {"topology_mode": "3-datacenter"}
        )
        self.assertEqual(
            options["listed"]["default_configs"], {"topology_mode": "1-datacenter"}
        )

    def test_offering_without_k8s_options_is_not_saved(self):
        offering = factories.OfferingFactory(
            options={"order": [], "options": {}}, resource_options={}
        )
        modified = offering.modified
        migration.forwards(apps, None)
        offering.refresh_from_db()
        self.assertEqual(offering.modified, modified)


def template_document(field_type):
    return {
        "schema_version": 1,
        "offering": {
            "name": "Kubernetes",
            "type": "Marketplace.Basic",
            "options": {
                "order": ["cluster"],
                "options": {"cluster": k8s_option(field_type)},
            },
        },
        "resource_options": {
            "order": ["cluster"],
            "options": {"cluster": k8s_option(field_type)},
        },
    }


def document_hash(document):
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MergeK8sOptionTypesTemplateMigrationTest(TestCase):
    def create_template(self, field_type):
        # One version, created by the model's save().
        return models.OfferingTemplate.objects.create(
            name="Kubernetes",
            offering_type="Marketplace.Basic",
            document=template_document(field_type),
            schema_version=1,
        )

    def test_forwards_converts_templates_and_their_versions(self):
        template = self.create_template("multi_datacenter_k8s_config")
        migration.forwards(apps, None)
        template.refresh_from_db()

        for document in (
            template.document,
            template.versions.get().document,
        ):
            self.assertEqual(
                document["offering"]["options"]["options"]["cluster"],
                k8s_option("k8s_cluster_config", {"topology_mode": "3-datacenter"}),
            )
            self.assertEqual(
                document["resource_options"]["options"]["cluster"]["type"],
                "k8s_cluster_config",
            )
        version = template.versions.get()
        # Rewritten in place: no new version, and the hash matches the content.
        self.assertEqual(template.versions.count(), 1)
        self.assertEqual(template.version, version.number)
        self.assertEqual(version.document_hash, document_hash(version.document))
        self.assertEqual(
            version.document_hash,
            models.offering_template_document_hash(version.document),
        )

    def test_template_without_k8s_options_is_left_alone(self):
        template = self.create_template("string")
        version = template.versions.get()
        migration.forwards(apps, None)
        version.refresh_from_db()
        self.assertEqual(version.document, template_document("string"))
        self.assertEqual(version.document_hash, document_hash(version.document))
