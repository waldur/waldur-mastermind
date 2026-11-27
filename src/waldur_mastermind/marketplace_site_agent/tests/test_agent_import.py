import textwrap
from unittest import mock

import yaml
from django.test import SimpleTestCase, override_settings
from rest_framework import serializers, status, test
from reversion.models import Version

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import agent_configuration
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SITE_AGENT_OFFERING,
    BillingTypes,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_site_agent import (
    agent_import,
    agent_import_options,
)
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory

# Values that must never be stored or echoed. None of them is real.
API_TOKEN = "tok-0123456789abcdef-not-real"
SENTRY_DSN = "https://publickey@sentry.invalid/1"


# What the file leaves to the agent host's environment without being secret.
VALUES = {
    "backend_settings.cscs_dwdi_api_url": "https://dwdi.example.com",
    "backend_settings.cscs_dwdi_client_id": "waldur-agent",
}


def cscs_file(offering_uuid, unit_factor=60, extra_offerings=""):
    """A sanitized CSCS-style agent configuration with one offering block."""
    return textwrap.dedent(
        f"""\
        sentry_dsn: "{SENTRY_DSN}"
        timezone: Europe/Zurich
        offerings:
          - name: CSCS Alps
            waldur_api_url: https://waldur.example.com/api/
            waldur_api_token: "{API_TOKEN}"
            waldur_offering_uuid: "{offering_uuid}"
            backend_type: slurm
            order_processing_backend: slurm
            membership_sync_backend: slurm
            reporting_backend: cscs-dwdi-compute
            stomp_enabled: true
            websocket_use_tls: true
            verify_ssl: true
            backend_settings:
              default_account: root
              customer_prefix: c-
              project_prefix: p-
              allocation_prefix: a-
              qos_downscaled: slowdown
              qos_paused: stop
              qos_default: normal
              cscs_dwdi_api_url: "${{dwdi_api_url}}"
              cscs_dwdi_client_id: "${{dwdi_client_id}}"
              cscs_dwdi_client_secret: "${{dwdi_client_secret}}"
              cscs_dwdi_oidc_token_url: https://auth.example.com/token
              periodic_limits:
                enabled: true
                limit_type: GrpTRESMins
            backend_components:
              node:
                measured_unit: node-hours
                unit_factor: {unit_factor}
                unit_factor_reporting: 1
                accounting_type: limit
                label: Node
            policy_settings:
              limit_type: GrpTRESMins
              tres_billing_enabled: true
              carryover_factor: 25
              grace_ratio: 0.1
              qos_strategy: threshold
              unknown_policy_knob: 1
        """
    ) + textwrap.indent(textwrap.dedent(extra_offerings), "  ")


class ImportTestMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        # Test roles do not load permissions.yaml.
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=SITE_AGENT_OFFERING
        )
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node",
            measured_unit="node-hours",
            unit_factor=1,
        )

    def post(self, configuration, user=None, **params):
        self.client.force_authenticate(user or self.fixture.owner)
        url = factories.OfferingFactory.get_url(
            self.offering, "import_agent_configuration"
        )
        return self.client.post(
            url, {"configuration": configuration, **params}, format="json"
        )


class DryRunTest(ImportTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.offering.agent_configuration = {
            "order_processing_backend": "slurm",
            "membership_sync_backend": "slurm",
            "reporting_backend": "slurm",
            "backend_settings": {
                "default_account": "root",
                "customer_prefix": "hpc_",
                "project_prefix": "hpc_",
                "allocation_prefix": "hpc_",
            },
        }
        self.offering.save()
        self.response = self.post(cscs_file(self.offering.uuid), values=VALUES)
        self.assertEqual(
            self.response.status_code, status.HTTP_200_OK, self.response.data
        )
        self.data = self.response.data

    def test_roles_and_settings_are_proposed(self):
        proposed = self.data["agent_configuration"]
        self.assertEqual(proposed["order_processing_backend"], "slurm")
        self.assertEqual(proposed["membership_sync_backend"], "slurm")
        self.assertEqual(proposed["reporting_backend"], "cscs-dwdi-compute")
        self.assertEqual(
            proposed["event_settings"],
            {"stomp_enabled": True, "websocket_use_tls": True},
        )
        settings = proposed["backend_settings"]
        self.assertEqual(settings["customer_prefix"], "c-")
        self.assertEqual(settings["project_prefix"], "p-")
        self.assertEqual(settings["qos_downscaled"], "slowdown")
        self.assertEqual(settings["qos_paused"], "stop")
        self.assertEqual(
            settings["periodic_limits"], {"enabled": True, "limit_type": "GrpTRESMins"}
        )
        self.assertEqual(settings["cscs_dwdi_client_id"], "waldur-agent")

    def test_only_agent_only_component_fields_are_proposed(self):
        self.assertEqual(
            self.data["agent_configuration"]["backend_components"],
            {"node": {"unit_factor_reporting": 1}},
        )

    def test_secrets_are_dropped_and_listed(self):
        self.assertEqual(
            sorted(self.data["dropped_secrets"]),
            ["backend_settings.cscs_dwdi_client_secret", "waldur_api_token"],
        )
        self.assertNotIn(
            "cscs_dwdi_client_secret",
            self.data["agent_configuration"]["backend_settings"],
        )

    def test_secret_values_are_not_echoed(self):
        body = self.response.content.decode()
        self.assertNotIn(API_TOKEN, body)
        self.assertNotIn("dwdi_client_secret}", body)
        self.assertNotIn(SENTRY_DSN, body)

    def test_bootstrap_and_unheld_keys_are_ignored(self):
        ignored = {item["path"]: item["reason"] for item in self.data["ignored"]}
        for key in (
            "name",
            "waldur_api_url",
            "waldur_offering_uuid",
            "verify_ssl",
            "backend_type",
            "sentry_dsn",
            "timezone",
        ):
            self.assertIn(key, ignored)
        self.assertIn("reaches Waldur", ignored["waldur_api_url"])

    def test_component_fields_are_compared_with_the_offering(self):
        self.assertEqual(
            self.data["component_differences"],
            [
                {
                    "component": "node",
                    "field": "unit_factor",
                    "offering": 1,
                    "imported": 60,
                    "applicable": True,
                    "reason": None,
                }
            ],
        )

    def test_changes_list_current_and_imported_values(self):
        changes = {item["path"]: item for item in self.data["changes"]}
        self.assertEqual(
            changes["backend_settings.customer_prefix"],
            {
                "path": "backend_settings.customer_prefix",
                "current": "hpc_",
                "imported": "c-",
            },
        )
        self.assertEqual(changes["reporting_backend"]["imported"], "cscs-dwdi-compute")
        self.assertIsNone(changes["event_settings.stomp_enabled"]["current"])
        self.assertNotIn("backend_settings.default_account", changes)
        self.assertEqual(
            self.data["current"],
            agent_import.sanitized(self.offering.agent_configuration),
        )

    def test_blocks_are_described(self):
        self.assertEqual(self.data["selected_index"], 0)
        self.assertEqual(
            self.data["offerings"],
            [
                {
                    "index": 0,
                    "name": "CSCS Alps",
                    "waldur_offering_uuid": str(self.offering.uuid),
                    "matches_offering": True,
                }
            ],
        )

    def test_nothing_is_saved(self):
        stored = self.offering.agent_configuration
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, stored)
        self.assertFalse(self.data["applied"])
        self.assertFalse(
            Version.objects.get_for_object(self.offering)
            .filter(revision__comment=agent_configuration.CHANGE_REVISION_COMMENT)
            .exists()
        )


class ApplyTest(ImportTestMixin, test.APITestCase):
    def test_stores_exactly_the_proposed_configuration_and_records_the_change(self):
        response = self.post(
            cscs_file(self.offering.uuid), values=VALUES, dry_run=False
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["applied"])
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration, response.data["agent_configuration"]
        )
        self.assertEqual(
            [
                version.revision.user
                for version in Version.objects.get_for_object(self.offering)
                if version.revision.comment
                == agent_configuration.CHANGE_REVISION_COMMENT
            ],
            [self.fixture.owner],
        )

    def test_secrets_are_never_stored(self):
        self.post(cscs_file(self.offering.uuid), values=VALUES, dry_run=False)

        self.offering.refresh_from_db()
        stored = yaml.safe_dump(self.offering.agent_configuration)
        self.assertNotIn(API_TOKEN, stored)
        self.assertNotIn("dwdi_client_secret", stored)
        self.assertEqual(
            agent_configuration.configuration_secret_paths(
                self.offering.agent_configuration
            ),
            [],
        )

    def test_dry_run_then_apply_give_the_same_proposal(self):
        preview = self.post(cscs_file(self.offering.uuid), values=VALUES)
        applied = self.post(cscs_file(self.offering.uuid), values=VALUES, dry_run=False)

        self.assertEqual(
            preview.data["agent_configuration"], applied.data["agent_configuration"]
        )
        self.assertEqual(preview.data["changes"], applied.data["changes"])


class SelectionTest(ImportTestMixin, test.APITestCase):
    OTHER = """\
        - name: Other cluster
          waldur_offering_uuid: 00000000-0000-0000-0000-000000000001
          backend_type: slurm
          backend_settings:
            default_account: root
            customer_prefix: x-
            project_prefix: x-
            allocation_prefix: x-
        """

    def test_block_matching_the_offering_is_selected(self):
        dashed = str(self.offering.uuid)
        text = cscs_file(dashed, extra_offerings=self.OTHER)
        # The other block comes first, so the match is not the default.
        data = yaml.safe_load(text)
        data["offerings"].reverse()

        response = self.post(yaml.safe_dump(data))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["selected_index"], 1)
        self.assertEqual(
            [block["matches_offering"] for block in response.data["offerings"]],
            [False, True],
        )

    def test_uuid_is_matched_without_dashes(self):
        response = self.post(cscs_file(self.offering.uuid.hex))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["offerings"][0]["matches_offering"])

    def test_block_is_selected_by_index(self):
        text = cscs_file(self.offering.uuid, extra_offerings=self.OTHER)

        response = self.post(text, offering_index=1)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["selected_index"], 1)
        self.assertEqual(
            response.data["agent_configuration"]["backend_settings"]["customer_prefix"],
            "x-",
        )
        # backend_type alone fills the three agent roles.
        proposed = response.data["agent_configuration"]
        for field in (
            "order_processing_backend",
            "membership_sync_backend",
            "reporting_backend",
        ):
            self.assertEqual(proposed[field], "slurm")
        self.assertNotIn("username_management_backend", proposed)

    def test_ambiguous_selection_names_the_blocks(self):
        text = cscs_file(
            "00000000-0000-0000-0000-000000000002", extra_offerings=self.OTHER
        )

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data["offering_index"][0])
        self.assertIn("CSCS Alps", message)
        self.assertIn("Other cluster", message)
        self.assertEqual(
            [block["index"] for block in response.data["offerings"]], [0, 1]
        )
        self.assertEqual(
            [block["matches_offering"] for block in response.data["offerings"]],
            [False, False],
        )

    def test_only_block_is_selected_whatever_its_uuid(self):
        response = self.post(cscs_file("00000000-0000-0000-0000-000000000002"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["offerings"][0]["matches_offering"])

    def test_index_out_of_range_is_refused(self):
        response = self.post(cscs_file(self.offering.uuid), offering_index=3)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_index", response.data)
        self.assertEqual(len(response.data["offerings"]), 1)


class InvalidFileTest(ImportTestMixin, test.APITestCase):
    def assert_refused(self, text):
        response = self.post(text)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("configuration", response.data)
        return response

    def test_invalid_yaml_is_refused_without_its_content(self):
        response = self.assert_refused(f'offerings: [{{"token": "{API_TOKEN}"')
        self.assertNotIn(API_TOKEN, response.content.decode())

    def test_non_mapping_is_refused(self):
        self.assert_refused("- just\n- a list\n")

    def test_missing_offerings_is_refused(self):
        self.assert_refused("timezone: UTC\n")

    def test_binary_values_are_refused(self):
        self.assert_refused("offerings: [{name: a, blob: !!binary aGVsbG8=}]\n")

    def test_dates_are_kept_as_text(self):
        text = cscs_file(self.offering.uuid).replace(
            "qos_default: normal", "qos_default: 2024-01-01"
        )

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["agent_configuration"]["backend_settings"]["qos_default"],
            "2024-01-01",
        )

    def test_python_tags_are_refused(self):
        self.assert_refused("offerings: !!python/object/apply:os.system ['true']\n")

    def test_non_finite_numbers_are_refused(self):
        for value in (".nan", ".inf", "-.inf"):
            text = cscs_file(self.offering.uuid).replace(
                "qos_default: normal", f"qos_default: {value}"
            )
            for dry_run in (True, False):
                response = self.post(text, values=VALUES, dry_run=dry_run)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, value
                )
                self.assertIn("not finite", str(response.data["configuration"][0]))
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_deep_nesting_is_refused(self):
        response = self.assert_refused("a: " + "[" * 5000 + "]" * 5000 + "\n")
        self.assertIn("nests too deeply", str(response.data["configuration"][0]))

    def test_moderate_nesting_past_the_limit_is_refused(self):
        response = self.assert_refused(
            "offerings: [{name: deep, x: " + "[" * 60 + "]" * 60 + "}]\n"
        )
        self.assertIn("nests too deeply", str(response.data["configuration"][0]))

    def test_self_referencing_value_is_refused(self):
        response = self.assert_refused("offerings: &a [*a]\n")
        self.assertIn("refers to itself", str(response.data["configuration"][0]))

    def test_alias_used_twice_is_accepted(self):
        text = cscs_file(self.offering.uuid).replace(
            "timezone: Europe/Zurich", "timezone: &tz Europe/Zurich\nzone: *tz"
        )
        response = self.post(text, values=VALUES)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_alias_expansion_is_bounded(self):
        lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
        for i in range(1, 12):
            previous = f"*a{i - 1}"
            lines.append(f"a{i}: &a{i} [{', '.join([previous] * 10)}]")
        lines.append("offerings: [{name: bomb, nested: *a11}]")
        response = self.assert_refused("\n".join(lines))
        self.assertIn("too many", str(response.data["configuration"][0]))


class ValidationTest(ImportTestMixin, test.APITestCase):
    def test_dry_run_previews_a_configuration_with_errors(self):
        response = self.post(cscs_file(self.offering.uuid))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(response.data["errors"]["backend_settings"]),
            ["cscs_dwdi_api_url", "cscs_dwdi_client_id"],
        )
        self.assertEqual(
            response.data["unresolved"],
            [
                {
                    "path": "backend_settings.cscs_dwdi_api_url",
                    "placeholder": "dwdi_api_url",
                },
                {
                    "path": "backend_settings.cscs_dwdi_client_id",
                    "placeholder": "dwdi_client_id",
                },
            ],
        )
        # Placeholders of settings that are not secret are not secrets.
        self.assertEqual(
            sorted(response.data["dropped_secrets"]),
            ["backend_settings.cscs_dwdi_client_secret", "waldur_api_token"],
        )
        settings = response.data["agent_configuration"]["backend_settings"]
        self.assertNotIn("cscs_dwdi_api_url", settings)
        self.assertEqual(settings["customer_prefix"], "c-")
        self.assertIn(
            "backend_settings.customer_prefix",
            [change["path"] for change in response.data["changes"]],
        )

    def test_values_resolve_the_placeholders(self):
        response = self.post(cscs_file(self.offering.uuid), values=VALUES)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["errors"], {})
        self.assertEqual(response.data["unresolved"], [])
        changes = {change["path"]: change for change in response.data["changes"]}
        self.assertEqual(
            changes["backend_settings.cscs_dwdi_api_url"]["imported"],
            "https://dwdi.example.com",
        )
        self.assertEqual(
            changes["backend_settings.cscs_dwdi_client_id"]["imported"],
            "waldur-agent",
        )

    def test_apply_with_errors_is_refused_as_update_integration_refuses_it(self):
        response = self.post(cscs_file(self.offering.uuid), dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "cscs_dwdi_api_url",
            response.data["agent_configuration"]["backend_settings"],
        )
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_apply_with_values_stores_them(self):
        response = self.post(
            cscs_file(self.offering.uuid), values=VALUES, dry_run=False
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        settings = self.offering.agent_configuration["backend_settings"]
        self.assertEqual(settings["cscs_dwdi_api_url"], "https://dwdi.example.com")
        self.assertEqual(settings["cscs_dwdi_client_id"], "waldur-agent")

    def test_value_for_an_absent_setting_is_accepted(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values={**VALUES, "backend_settings.cluster_name": "alps"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["agent_configuration"]["backend_settings"]["cluster_name"],
            "alps",
        )

    def assert_value_refused(self, path, value="x"):
        response = self.post(
            cscs_file(self.offering.uuid), values={**VALUES, path: value}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.data["values"]), [path])
        self.assertNotIn("not-a-real-secret", response.content.decode())

    def test_value_for_a_secret_is_refused(self):
        self.assert_value_refused(
            "backend_settings.cscs_dwdi_client_secret", "not-a-real-secret"
        )
        self.assert_value_refused("backend_settings.ldap.bind_password")
        self.assert_value_refused("waldur_api_token")

    def test_value_holding_a_secret_is_refused(self):
        self.assert_value_refused(
            "backend_settings.ldap", {"bind_password": "not-a-real-secret"}
        )

    def test_value_outside_the_settings_is_refused(self):
        self.assert_value_refused("order_processing_backend", "slurm")
        self.assert_value_refused("backend_components.node.unit_factor", 2)
        self.assert_value_refused("event_settings.unknown", True)

    def test_value_for_a_setting_the_file_sets_is_refused(self):
        self.assert_value_refused("backend_settings.customer_prefix", "x-")

    def test_placeholder_as_a_value_is_refused(self):
        self.assert_value_refused("backend_settings.cluster_name", "${CLUSTER}")

    def list_file(self):
        return cscs_file(self.offering.uuid).replace(
            "      qos_default: normal\n",
            "      qos_default: normal\n"
            '      extra_partitions: [normal, "${EXTRA_PARTITION}"]\n',
        )

    def test_placeholder_in_a_list_leaves_the_list_unresolved(self):
        response = self.post(self.list_file(), values=VALUES)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(
            {
                "path": "backend_settings.extra_partitions",
                "placeholder": "EXTRA_PARTITION",
            },
            response.data["unresolved"],
        )
        self.assertNotIn(
            "extra_partitions",
            response.data["agent_configuration"]["backend_settings"],
        )
        self.assertNotIn("${", str(response.data["agent_configuration"]))

    def test_values_give_an_unresolved_list(self):
        response = self.post(
            self.list_file(),
            values={
                **VALUES,
                "backend_settings.extra_partitions": ["normal", "debug"],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["agent_configuration"]["backend_settings"][
                "extra_partitions"
            ],
            ["normal", "debug"],
        )
        self.assertEqual(response.data["unresolved"], [])

    def test_event_setting_placeholder_is_unresolved(self):
        text = cscs_file(self.offering.uuid).replace(
            "    stomp_enabled: true\n",
            "    stomp_enabled: true\n    stomp_ws_host: ${STOMP_HOST}\n",
        )

        response = self.post(
            text, values={**VALUES, "event_settings.stomp_ws_host": "mq.example.com"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["agent_configuration"]["event_settings"]["stomp_ws_host"],
            "mq.example.com",
        )

        response = self.post(text, values=VALUES)

        self.assertEqual(
            response.data["unresolved"],
            [{"path": "event_settings.stomp_ws_host", "placeholder": "STOMP_HOST"}],
        )

    def test_component_the_offering_lacks_is_reported_not_imported(self):
        text = cscs_file(self.offering.uuid).replace("    node:\n", "    gpu:\n")

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn("backend_components", response.data["agent_configuration"])
        differences = {
            item["field"]: item
            for item in response.data["component_differences"]
            if item["component"] == "gpu"
        }
        self.assertEqual(
            set(differences),
            {"measured_unit", "unit_factor", "accounting_type", "label"},
        )
        self.assertIsNone(differences["unit_factor"]["offering"])
        self.assertIn(
            "backend_components.gpu",
            [item["path"] for item in response.data["ignored"]],
        )

    def test_non_site_agent_offering_is_refused(self):
        self.offering.type = BASIC_OFFERING
        self.offering.save()

        response = self.post(cscs_file(self.offering.uuid))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PermissionTest(ImportTestMixin, test.APITestCase):
    def test_offering_manager_with_both_permissions_imports(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self.post(
            cscs_file(self.offering.uuid), user=manager, values=VALUES, dry_run=False
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_without_agent_configuration_permission_import_is_refused(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        preview = self.post(cscs_file(self.offering.uuid), user=manager, values=VALUES)

        # Previewed, with the refusal among the errors.
        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        self.assertIn("permission", preview.data["errors"]["non_field_errors"][0])
        self.assertEqual(
            preview.data["agent_configuration"]["reporting_backend"],
            "cscs-dwdi-compute",
        )

        response = self.post(
            cscs_file(self.offering.uuid), user=manager, values=VALUES, dry_run=False
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_without_integration_permission_import_is_refused(self):
        user = self.fixture.admin

        response = self.post(cscs_file(self.offering.uuid), user=user)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class MappingTest(SimpleTestCase):
    def test_placeholder_is_a_whole_value(self):
        self.assertTrue(agent_import.is_placeholder("${DWDI_SECRET}"))
        self.assertFalse(agent_import.is_placeholder("https://${HOST}/api"))
        self.assertFalse(agent_import.is_placeholder(5))

    def result(self):
        return agent_import.ImportedConfiguration(
            offerings=[], selected_index=0, agent_configuration={}
        )

    def test_nested_declared_secret_is_dropped(self):
        result = self.result()
        kept = agent_import._without_secrets(
            {
                "ldap": {
                    "uri": "ldaps://ldap.example.com",
                    "bind_password": "${LDAP_PASSWORD}",
                }
            },
            "backend_settings",
            result,
        )
        self.assertEqual(kept, {"ldap": {"uri": "ldaps://ldap.example.com"}})
        self.assertEqual(
            result.dropped_secrets, ["backend_settings.ldap.bind_password"]
        )
        self.assertEqual(result.unresolved, [])

    def test_placeholder_under_a_plain_name_is_unresolved(self):
        result = self.result()
        kept = agent_import._without_secrets(
            {"hostname": "${SLURM_HOST}", "cluster_name": "alps"},
            "backend_settings",
            result,
        )
        self.assertEqual(kept, {"cluster_name": "alps"})
        self.assertEqual(result.dropped_secrets, [])
        self.assertEqual(
            result.unresolved,
            [{"path": "backend_settings.hostname", "placeholder": "SLURM_HOST"}],
        )

    def test_changes_compare_leaves(self):
        changes = agent_import.configuration_changes(
            {"backend_settings": {"a": 1, "b": 2}},
            {"backend_settings": {"a": 1, "b": 3, "c": [1]}},
        )
        self.assertEqual(
            changes,
            [
                {"path": "backend_settings.b", "current": 2, "imported": 3},
                {"path": "backend_settings.c", "current": None, "imported": [1]},
            ],
        )

    def test_selection_error_is_a_validation_error(self):
        with self.assertRaises(serializers.ValidationError):
            agent_import.select_block(
                [
                    {
                        "index": 0,
                        "name": "a",
                        "waldur_offering_uuid": None,
                        "matches_offering": False,
                    },
                    {
                        "index": 1,
                        "name": "b",
                        "waldur_offering_uuid": None,
                        "matches_offering": False,
                    },
                ],
                None,
            )


NODE_UNIT_FACTOR = [{"component": "node", "field": "unit_factor"}]
ACTIONS = ["notify_organization_owners", "request_slurm_resource_downscaling"]


class ComponentOptInTest(ImportTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        self.component = self.offering.components.get(type="node")

    def differences(self, response):
        return {
            (row["component"], row["field"]): row
            for row in response.data["component_differences"]
        }

    def test_difference_says_it_is_applicable(self):
        response = self.post(cscs_file(self.offering.uuid), values=VALUES)

        row = self.differences(response)[("node", "unit_factor")]
        self.assertTrue(row["applicable"])
        self.assertIsNone(row["reason"])

    def test_opting_into_unit_factor_applies_it_and_keeps_the_agent_fields(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_components=NODE_UNIT_FACTOR,
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.component.refresh_from_db()
        self.assertEqual(self.component.unit_factor, 60)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["backend_components"],
            {"node": {"unit_factor_reporting": 1}},
        )

    def test_differences_are_not_applied_unless_chosen(self):
        self.post(cscs_file(self.offering.uuid), values=VALUES, dry_run=False)

        self.component.refresh_from_db()
        self.assertEqual(self.component.unit_factor, 1)

    def test_dry_run_with_opt_ins_saves_nothing(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_components=NODE_UNIT_FACTOR,
            apply_policy=True,
            policy_actions=ACTIONS,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["errors"], {})
        self.assertFalse(response.data["applied"])
        self.component.refresh_from_db()
        self.assertEqual(self.component.unit_factor, 1)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})
        self.assertFalse(
            SlurmPeriodicUsagePolicy.objects.filter(scope=self.offering).exists()
        )

    def usage_file(self):
        return cscs_file(self.offering.uuid).replace(
            "accounting_type: limit", "accounting_type: usage"
        )

    def test_billing_type_change_is_applied_while_nothing_uses_the_component(self):
        response = self.post(
            self.usage_file(),
            values=VALUES,
            apply_components=[{"component": "node", "field": "accounting_type"}],
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.component.refresh_from_db()
        self.assertEqual(self.component.billing_type, BillingTypes.USAGE)

    def test_billing_type_change_is_refused_while_resources_exist(self):
        factories.ResourceFactory(offering=self.offering)
        chosen = [{"component": "node", "field": "accounting_type"}]

        preview = self.post(self.usage_file(), values=VALUES, apply_components=chosen)

        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        row = self.differences(preview)[("node", "accounting_type")]
        self.assertFalse(row["applicable"])
        self.assertIn("resources", row["reason"])
        self.assertIn(
            "node.accounting_type", preview.data["errors"]["apply_components"]
        )

        response = self.post(
            self.usage_file(), values=VALUES, apply_components=chosen, dry_run=False
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("node.accounting_type", response.data["apply_components"])
        self.component.refresh_from_db()
        self.assertEqual(self.component.billing_type, BillingTypes.FIXED)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_billing_type_change_is_refused_while_a_plan_prices_the_component(self):
        plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(plan=plan, component=self.component, price=5)

        response = self.post(self.usage_file(), values=VALUES)

        row = self.differences(response)[("node", "accounting_type")]
        self.assertFalse(row["applicable"])

    def test_missing_component_is_not_applicable(self):
        text = cscs_file(self.offering.uuid).replace("    node:\n", "    gpu:\n")

        response = self.post(
            text,
            values=VALUES,
            apply_components=[{"component": "gpu", "field": "unit_factor"}],
        )

        row = self.differences(response)[("gpu", "unit_factor")]
        self.assertFalse(row["applicable"])
        self.assertIn("no gpu component", row["reason"])
        self.assertIn("gpu.unit_factor", response.data["errors"]["apply_components"])

    def test_a_choice_that_is_no_difference_is_refused(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_components=[{"component": "node", "field": "label"}],
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("node.label", response.data["apply_components"])

    def test_without_component_permission_only_opting_in_is_refused(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

        response = self.post(cscs_file(self.offering.uuid), values=VALUES)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        preview = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_components=NODE_UNIT_FACTOR,
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        self.assertIn(
            "components",
            preview.data["errors"]["apply_components"]["non_field_errors"][0],
        )

        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_components=NODE_UNIT_FACTOR,
            dry_run=False,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("components", str(response.data["detail"]))
        self.component.refresh_from_db()
        self.assertEqual(self.component.unit_factor, 1)

    def test_a_failing_part_undoes_the_others(self):
        with mock.patch.object(
            agent_import_options.PolicyImport,
            "apply",
            side_effect=serializers.ValidationError({"policy": ["refused"]}),
        ):
            response = self.post(
                cscs_file(self.offering.uuid),
                values=VALUES,
                apply_components=NODE_UNIT_FACTOR,
                apply_policy=True,
                policy_actions=ACTIONS,
                dry_run=False,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"policy": ["refused"]})
        self.component.refresh_from_db()
        self.assertEqual(self.component.unit_factor, 1)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})


class PolicyOptInTest(ImportTestMixin, test.APITestCase):
    def test_policy_is_described_not_ignored(self):
        response = self.post(cscs_file(self.offering.uuid), values=VALUES)

        policy = response.data["policy"]
        self.assertFalse(policy["exists"])
        self.assertTrue(policy["needs_actions"])
        self.assertIn(
            {
                "value": "request_slurm_resource_pausing",
                "label": "Request SLURM resource pausing (blocked QoS)",
            },
            policy["action_choices"],
        )
        self.assertEqual(
            {choice["value"] for choice in policy["action_choices"]},
            SlurmPeriodicUsagePolicy.available_actions,
        )
        self.assertTrue(policy["applicable"])
        self.assertIsNone(policy["reason"])
        changes = {change["field"]: change for change in policy["changes"]}
        self.assertEqual(
            changes["carryover_factor"],
            {"field": "carryover_factor", "current": None, "imported": 25},
        )
        ignored = [item["path"] for item in response.data["ignored"]]
        self.assertNotIn("policy_settings", ignored)
        self.assertIn("policy_settings.unknown_policy_knob", ignored)

    def test_no_policy_without_policy_settings(self):
        text = cscs_file(self.offering.uuid).split("    policy_settings:")[0]

        response = self.post(text, values=VALUES)

        self.assertIsNone(response.data["policy"])

    def test_policy_is_not_created_unless_chosen(self):
        self.post(cscs_file(self.offering.uuid), values=VALUES, dry_run=False)

        self.assertFalse(
            SlurmPeriodicUsagePolicy.objects.filter(scope=self.offering).exists()
        )

    def test_policy_is_created_from_policy_settings(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_policy=True,
            policy_actions=ACTIONS,
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        policy = SlurmPeriodicUsagePolicy.objects.get(scope=self.offering)
        self.assertEqual(set(policy.actions.split(",")), set(ACTIONS))
        self.assertEqual(policy.carryover_factor, 25)
        self.assertEqual(policy.grace_ratio, 0.1)
        self.assertTrue(policy.apply_to_all)
        self.assertEqual(policy.created_by, self.fixture.owner)

    def test_existing_policy_is_updated(self):
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering, carryover_factor=50
        )

        preview = self.post(cscs_file(self.offering.uuid), values=VALUES)
        self.assertTrue(preview.data["policy"]["exists"])
        self.assertFalse(preview.data["policy"]["needs_actions"])
        changes = {c["field"]: c for c in preview.data["policy"]["changes"]}
        self.assertEqual(changes["carryover_factor"]["current"], 50)

        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_policy=True,
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        policy.refresh_from_db()
        self.assertEqual(policy.carryover_factor, 25)
        self.assertEqual(
            SlurmPeriodicUsagePolicy.objects.filter(scope=self.offering).count(), 1
        )

    def test_policy_is_not_applicable_when_the_roles_do_not_use_slurm(self):
        text = (
            cscs_file(self.offering.uuid)
            .replace("order_processing_backend: slurm", "order_processing_backend: mup")
            .replace("membership_sync_backend: slurm", "membership_sync_backend: mup")
        )

        response = self.post(text, values=VALUES, apply_policy=True)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["policy"]["applicable"])
        self.assertIn("SLURM", response.data["policy"]["reason"])
        self.assertIn("policy", response.data["errors"])

    def test_policy_needs_who_may_configure_it(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self.post(cscs_file(self.offering.uuid), user=manager, values=VALUES)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["policy"]["applicable"])

        preview = self.post(
            cscs_file(self.offering.uuid),
            user=manager,
            values=VALUES,
            apply_policy=True,
            policy_actions=ACTIONS,
        )
        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        self.assertIn(
            "may not configure",
            preview.data["errors"]["policy"]["non_field_errors"][0],
        )

        response = self.post(
            cscs_file(self.offering.uuid),
            user=manager,
            values=VALUES,
            apply_policy=True,
            policy_actions=ACTIONS,
            dry_run=False,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("may not configure", str(response.data["detail"]))
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_permission_is_the_reason_given_whatever_the_roles(self):
        # The roles do not use SLURM either; the 403 still names the permission.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        text = (
            cscs_file(self.offering.uuid)
            .replace("order_processing_backend: slurm", "order_processing_backend: mup")
            .replace("membership_sync_backend: slurm", "membership_sync_backend: mup")
        )

        response = self.post(
            text, user=manager, values=VALUES, apply_policy=True, dry_run=False
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("may not configure", str(response.data["detail"]))

    def test_file_level_keys_apply_to_the_agent_process(self):
        response = self.post(cscs_file(self.offering.uuid), values=VALUES)

        reasons = {item["path"]: item["reason"] for item in response.data["ignored"]}
        self.assertIn("whole agent process", reasons["timezone"])


@override_settings(task_always_eager=True)
class PolicyEventTest(ImportTestMixin, test.APITestCase):
    def test_creation_is_logged(self):
        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_policy=True,
            policy_actions=ACTIONS,
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        policy = SlurmPeriodicUsagePolicy.objects.get(scope=self.offering)
        event = Event.objects.get(event_type=EventType.POLICY_CREATED)
        self.assertEqual(event.context["policy_uuid"], policy.uuid.hex)
        self.assertEqual(event.context["user_uuid"], self.fixture.owner.uuid.hex)

    def test_update_logs_the_changed_fields(self):
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering, carryover_factor=50
        )

        response = self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_policy=True,
            dry_run=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        event = Event.objects.get(event_type=EventType.POLICY_UPDATED)
        self.assertEqual(event.context["policy_uuid"], policy.uuid.hex)
        self.assertIn("carryover_factor: 50 -> 25", event.context["policy_changes"])
        self.assertNotIn("limit_type", event.context["policy_changes"])

    def test_dry_run_logs_nothing(self):
        self.post(
            cscs_file(self.offering.uuid),
            values=VALUES,
            apply_policy=True,
            policy_actions=ACTIONS,
        )

        self.assertFalse(
            Event.objects.filter(
                event_type__in=[EventType.POLICY_CREATED, EventType.POLICY_UPDATED]
            ).exists()
        )


class PolicyActionsTest(ImportTestMixin, test.APITestCase):
    def apply(self, **params):
        return self.post(
            cscs_file(self.offering.uuid), values=VALUES, apply_policy=True, **params
        )

    def test_creating_needs_actions(self):
        preview = self.apply()

        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        self.assertIn("actions", preview.data["errors"]["policy"])

        response = self.apply(dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("actions", response.data["policy"])
        self.assertFalse(
            SlurmPeriodicUsagePolicy.objects.filter(scope=self.offering).exists()
        )
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_chosen_actions_are_listed_for_a_new_policy(self):
        response = self.apply(policy_actions=ACTIONS)

        self.assertEqual(response.data["errors"], {})
        changes = {c["field"]: c for c in response.data["policy"]["changes"]}
        self.assertEqual(
            changes["actions"],
            {"field": "actions", "current": None, "imported": ACTIONS},
        )

    def test_update_without_actions_keeps_them(self):
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering, actions="block_creation_of_new_resources"
        )

        response = self.apply(dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn(
            "actions", [c["field"] for c in response.data["policy"]["changes"]]
        )
        policy.refresh_from_db()
        self.assertEqual(policy.actions, "block_creation_of_new_resources")
        self.assertEqual(policy.carryover_factor, 25)

    def test_update_with_actions_changes_them(self):
        policy = SlurmPeriodicUsagePolicyFactory(
            scope=self.offering, actions="block_creation_of_new_resources"
        )

        response = self.apply(policy_actions=ACTIONS, dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        changes = {c["field"]: c for c in response.data["policy"]["changes"]}
        self.assertEqual(
            changes["actions"]["current"], ["block_creation_of_new_resources"]
        )
        self.assertEqual(changes["actions"]["imported"], ACTIONS)
        policy.refresh_from_db()
        self.assertEqual(set(policy.actions.split(",")), set(ACTIONS))

    def test_unknown_action_is_refused(self):
        preview = self.apply(policy_actions=["notify_organization_owners", "explode"])

        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        self.assertIn("actions", preview.data["errors"]["policy"])

        response = self.apply(policy_actions=["explode"], dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("actions", response.data["policy"])
        self.assertFalse(
            SlurmPeriodicUsagePolicy.objects.filter(scope=self.offering).exists()
        )
