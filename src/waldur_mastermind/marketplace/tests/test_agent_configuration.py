import importlib
from unittest import mock

import yaml
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test
from reversion.models import Version

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import agent_configuration
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.serializers import ExportImportOfferingSerializer
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_site_agent import config_builder

SLURM_SETTINGS = {
    "default_account": "root",
    "customer_prefix": "c_",
    "project_prefix": "p_",
    "allocation_prefix": "a_",
    "cluster_name": "alpha",
}
DWDI_SETTINGS = {
    "cscs_dwdi_api_url": "https://dwdi.example.com",
    "cscs_dwdi_client_id": "waldur",
    "cscs_dwdi_oidc_token_url": "https://auth.example.com/token",
}
SLURM_WITH_DWDI = {
    "order_processing_backend": "slurm",
    "membership_sync_backend": "slurm",
    "reporting_backend": "cscs-dwdi-compute",
    "backend_settings": {**SLURM_SETTINGS, **DWDI_SETTINGS},
    "backend_components": {"node": {"unit_factor_reporting": 1}},
}

# A backend whose settings schema allows no other properties.
WALDUR_BACKEND = {
    "order_processing_backend": "waldur",
    "membership_sync_backend": "waldur",
    "reporting_backend": "waldur",
    "backend_settings": {
        "target_api_url": "https://b.example.com/api/",
        "target_offering_uuid": "a" * 32,
        "target_customer_uuid": "b" * 32,
    },
}


class AgentConfigurationTestMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=SITE_AGENT_OFFERING
        )
        factories.OfferingComponentFactory(offering=self.offering, type="node")

    def update(self, agent_configuration, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(
            url, {"agent_configuration": agent_configuration}, format="json"
        )


def configuration_changes(offering):
    """(username, comment) of the revisions recording agent configuration changes."""
    return [
        (version.revision.user.username, version.revision.comment)
        for version in Version.objects.get_for_object(offering).select_related(
            "revision__user"
        )
        if version.revision.comment == agent_configuration.CHANGE_REVISION_COMMENT
    ]


class AgentConfigurationHistoryTest(AgentConfigurationTestMixin, test.APITestCase):
    # Where the agent connects and what it runs come from the configuration,
    # so the offering's history says who changed it and when.

    def test_a_change_is_recorded_with_its_author(self):
        self.update(SLURM_WITH_DWDI)

        self.assertEqual(
            configuration_changes(self.offering),
            [
                (
                    self.fixture.owner.username,
                    agent_configuration.CHANGE_REVISION_COMMENT,
                )
            ],
        )

    def test_saving_the_same_configuration_records_nothing(self):
        self.update(SLURM_WITH_DWDI)
        self.update(SLURM_WITH_DWDI)

        self.assertEqual(len(configuration_changes(self.offering)), 1)

    def test_history_shows_the_change_but_not_the_configuration(self):
        self.update(SLURM_WITH_DWDI)
        # The history endpoint is for staff and support.
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

        response = self.client.get(
            factories.OfferingFactory.get_url(self.offering, "history")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry = next(
            item
            for item in response.data
            if item["revision_comment"] == agent_configuration.CHANGE_REVISION_COMMENT
        )
        self.assertEqual(
            entry["revision_user"]["username"], self.fixture.owner.username
        )
        self.assertNotIn("agent_configuration", entry["serialized_data"])


class AgentConfigurationUpdateTest(AgentConfigurationTestMixin, test.APITestCase):
    def test_owner_sets_configuration(self):
        response = self.update(SLURM_WITH_DWDI)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, SLURM_WITH_DWDI)

    def test_update_replaces_whole_configuration(self):
        self.update(SLURM_WITH_DWDI)

        response = self.update(
            {"order_processing_backend": "slurm", "backend_settings": SLURM_SETTINGS}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertNotIn("reporting_backend", self.offering.agent_configuration)

    def test_other_offering_types_have_no_agent_configuration(self):
        self.offering.type = BASIC_OFFERING
        self.offering.save()

        response = self.update({"order_processing_backend": "slurm"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Refused for its type, not for the SLURM settings it lacks.
        self.assertEqual(
            str(response.data["agent_configuration"]),
            "Only site agent offerings have an agent configuration.",
        )

    def set_a_role(self):
        # With a role already set nothing is filled in for the next one.
        self.offering.agent_configuration = {
            "reporting_backend": "slurm",
            "backend_settings": SLURM_SETTINGS,
        }
        self.offering.save()

    def test_each_missing_setting_is_named_once(self):
        self.set_a_role()
        response = self.update({"order_processing_backend": "slurm"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        for name in ("default_account", "customer_prefix", "project_prefix"):
            self.assertEqual(errors[name], ["slurm: This setting is required."])

    def test_missing_required_setting_is_named(self):
        self.set_a_role()
        settings = dict(SLURM_SETTINGS)
        del settings["default_account"]

        response = self.update(
            {"order_processing_backend": "slurm", "backend_settings": settings}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        self.assertIn("default_account", errors)

    def test_first_role_takes_the_settings_the_generator_served(self):
        # With no role set the offering is generated with SLURM's settings
        # from its plugin options or the generator's defaults; choosing a role
        # keeps them, rather than asking for settings that were never stored.
        self.offering.plugin_options = {"customer_prefix": "c-", "project_prefix": "p-"}
        self.offering.save()
        before = config_builder.offering_agent_config(self.offering)["offering"]

        response = self.update({"order_processing_backend": "slurm"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["backend_settings"],
            {
                "customer_prefix": "c-",
                "project_prefix": "p-",
                "allocation_prefix": "",
                "default_account": "root",
            },
        )
        after = config_builder.offering_agent_config(self.offering)["offering"]
        for key in ("customer_prefix", "project_prefix", "allocation_prefix"):
            self.assertEqual(
                after["backend_settings"][key], before["backend_settings"][key]
            )

    def test_first_role_keeps_the_settings_it_is_given(self):
        response = self.update(
            {
                "order_processing_backend": "slurm",
                "backend_settings": {"customer_prefix": "x-"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        settings = self.offering.agent_configuration["backend_settings"]
        self.assertEqual(settings["customer_prefix"], "x-")
        self.assertEqual(settings["default_account"], "root")

    def test_first_role_without_slurm_fills_nothing(self):
        response = self.update(WALDUR_BACKEND)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, WALDUR_BACKEND)

    def test_secret_setting_is_refused(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_settings": {
                **SLURM_WITH_DWDI["backend_settings"],
                "cscs_dwdi_client_secret": "s3cret",
            },
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        self.assertIn("cscs_dwdi_client_secret", errors)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_nested_secret_setting_is_refused(self):
        response = self.update(
            {
                "username_management_backend": "ldap",
                "backend_settings": {"ldap": {"bind_password": "s3cret"}},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        self.assertIn("ldap.bind_password", errors)

    def test_secret_is_not_required_in_waldur(self):
        # cscs_dwdi_client_secret is required by the plugin, but it is filled
        # in on the agent host, so the configuration is complete without it.
        response = self.update(SLURM_WITH_DWDI)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_unknown_backend_is_refused(self):
        response = self.update({"order_processing_backend": "my-cluster"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("order_processing_backend", response.data["agent_configuration"])

    def test_custom_backend_is_accepted_unvalidated(self):
        response = self.update(
            {
                "order_processing_backend": "my-cluster",
                "custom_backends": ["my-cluster"],
                "backend_settings": {"anything": "goes"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_custom_backend_setting_named_like_a_credential_is_refused(self):
        # Deliberately strict: no catalog plugin declares ssh_key_path as a
        # plain setting, and a custom backend has no schema to declare it, so
        # the name alone decides. It stays in the agent's own file.
        response = self.update(
            {
                "order_processing_backend": "my-cluster",
                "custom_backends": ["my-cluster"],
                "backend_settings": {"ssh_key_path": "/etc/agent/id_ed25519"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "ssh_key_path",
            response.data["agent_configuration"]["backend_settings"],
        )

    def test_offering_manager_sets_configuration(self):
        # As docker/rootfs/etc/waldur/permissions.yaml grants it.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        OfferingRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self.update(SLURM_WITH_DWDI, user=manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, SLURM_WITH_DWDI)

    def test_configuration_is_not_written_on_its_own(self):
        # Stored by the offering's one save in update_integration, not by an
        # extra UPDATE of that column alone.
        with CaptureQueriesContext(connection) as queries:
            response = self.update(SLURM_WITH_DWDI)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [
                query["sql"]
                for query in queries.captured_queries
                if query["sql"].startswith(
                    'UPDATE "marketplace_offering" SET "agent_configuration" ='
                )
            ],
            [],
        )
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, SLURM_WITH_DWDI)

    def test_backend_must_support_the_role(self):
        response = self.update({"order_processing_backend": "cscs-dwdi-compute"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("order_processing_backend", response.data["agent_configuration"])

    def test_unknown_setting_of_a_strict_backend_is_refused(self):
        response = self.update(
            {
                "reporting_backend": "cscs-dwdi-compute",
                "backend_settings": {**DWDI_SETTINGS, "typo_setting": 1},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        self.assertIn("typo_setting", errors)

    def test_component_of_another_offering_is_refused(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {"gpu": {"unit_factor_reporting": 2}},
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gpu", response.data["agent_configuration"]["backend_components"])

    def test_offering_component_fields_are_refused(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {"node": {"unit_factor": 60}},
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_components"]["node"]
        self.assertIn("unit_factor", errors)

    def test_offering_component_names_of_fields_are_refused(self):
        # slurm's component schema is open, so only this check stops a copy
        # of billing_type or name that the agent's accounting_type and label
        # would contradict.
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {
                "node": {
                    "unit_factor_reporting": 1,
                    "billing_type": "usage",
                    "name": "GPU hours",
                    "limit_amount": 10,
                    "backend_id": "gpu",
                }
            },
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_components"]["node"]
        self.assertEqual(
            set(errors), {"billing_type", "name", "limit_amount", "backend_id"}
        )

    def test_reporting_factor_must_be_positive(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {"node": {"unit_factor_reporting": 0}},
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_event_settings_are_stored(self):
        event_settings = {
            "stomp_enabled": True,
            "stomp_membership_sync_enabled": None,
            "stomp_ws_host": "waldur.example.com",
            "stomp_ws_port": 443,
            "stomp_ws_path": "/rmqws-stomp",
            "websocket_use_tls": True,
        }

        response = self.update({**SLURM_WITH_DWDI, "event_settings": event_settings})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["event_settings"], event_settings
        )

    def test_invalid_event_settings_are_refused(self):
        for event_settings, field in (
            ({"stomp_password": "x"}, "stomp_password"),
            ({"stomp_ws_port": 70000}, "stomp_ws_port"),
            ({"stomp_enabled": "maybe"}, "stomp_enabled"),
        ):
            with self.subTest(field=field):
                response = self.update(
                    {**SLURM_WITH_DWDI, "event_settings": event_settings}
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(
                    field, response.data["agent_configuration"]["event_settings"]
                )

    def test_agent_settings_are_stored_and_read_back(self):
        agent_settings = {
            "resource_import_enabled": True,
            "username_reconciliation_enabled": False,
            "preserve_unmanaged_backend_users": True,
            "omit_anomalous_usage_components": False,
        }
        configuration = {**SLURM_WITH_DWDI, "agent_settings": agent_settings}

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["agent_settings"], agent_settings
        )
        self.client.force_authenticate(self.fixture.owner)
        read = self.client.get(factories.OfferingFactory.get_url(self.offering))
        self.assertEqual(read.data["agent_configuration"], configuration)

    def test_unset_agent_settings_are_left_out(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "agent_settings": {"omit_anomalous_usage_components": True},
        }

        self.update(configuration)

        self.client.force_authenticate(self.fixture.owner)
        read = self.client.get(factories.OfferingFactory.get_url(self.offering))
        self.assertEqual(read.data["agent_configuration"], configuration)

    def test_invalid_agent_settings_are_refused(self):
        for agent_settings, field in (
            ({"verify_ssl": False}, "verify_ssl"),
            ({"waldur_api_token": "x"}, "waldur_api_token"),
            ({"resource_import_enabled": "true"}, "resource_import_enabled"),
            ({"resource_import_enabled": 1}, "resource_import_enabled"),
            (
                {"preserve_unmanaged_backend_users": None},
                "preserve_unmanaged_backend_users",
            ),
        ):
            with self.subTest(agent_settings=agent_settings):
                response = self.update(
                    {**SLURM_WITH_DWDI, "agent_settings": agent_settings}
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(
                    field, response.data["agent_configuration"]["agent_settings"]
                )
        self.offering.refresh_from_db()
        self.assertNotIn("agent_settings", self.offering.agent_configuration)

    def test_component_named_like_a_credential_is_accepted(self):
        factories.OfferingComponentFactory(offering=self.offering, type="license_key")
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {"license_key": {"unit_factor_reporting": 1}},
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_secret_in_a_component_is_refused_under_that_component(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {"node": {"api_token": "s3cret"}},
        }

        response = self.update(configuration)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_components"]["node"]
        self.assertIn("api_token", errors)

    def test_component_fields_are_accepted_when_no_backend_is_named(self):
        # As backend_settings are: there is no schema to check them against.
        response = self.update({"backend_components": {"node": {"slurm_tres": "node"}}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_secrets_are_refused_when_no_backend_is_named(self):
        response = self.update({"backend_settings": {"client_secret": "x"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "client_secret", response.data["agent_configuration"]["backend_settings"]
        )

    def test_open_schema_does_not_let_another_plugins_secret_through(self):
        response = self.update(
            {
                "order_processing_backend": "slurm",
                "backend_settings": {**SLURM_SETTINGS, "api_token": "leak"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "api_token", response.data["agent_configuration"]["backend_settings"]
        )

    def test_agent_credentials_are_refused(self):
        response = self.update(
            {
                "order_processing_backend": "slurm",
                "backend_settings": {**SLURM_SETTINGS, "waldur_api_token": "x"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_custom_backend_cannot_shadow_a_catalog_backend(self):
        response = self.update(
            {
                "order_processing_backend": "waldur",
                "custom_backends": ["waldur"],
                "backend_settings": {"target_api_url": "https://b.example.com"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("custom_backends", response.data["agent_configuration"])

    def test_unknown_keys_are_refused(self):
        response = self.update({"order_processing_backend": "slurm", "token": "x"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token", response.data["agent_configuration"])

    def test_reporting_factor_must_be_finite(self):
        serializer = agent_configuration.AgentConfigurationSerializer(
            data={
                "backend_components": {"node": {"unit_factor_reporting": float("inf")}}
            }
        )

        self.assertFalse(serializer.is_valid())

    def test_empty_configuration_clears_one_left_by_a_type_switch(self):
        self.offering.agent_configuration = SLURM_WITH_DWDI
        self.offering.type = BASIC_OFFERING
        self.offering.save()

        response = self.update({})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.agent_configuration, {})

    def test_rejected_value_is_not_echoed_back(self):
        response = self.update(
            {
                "membership_sync_backend": "k8s-ut-namespace",
                "backend_settings": {"keycloak": [{"realm": "S3CRET-VALUE"}]},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("S3CRET-VALUE", str(response.data))

    def test_secret_inside_a_list_is_refused(self):
        response = self.update(
            {
                "order_processing_backend": "slurm",
                "backend_settings": {
                    **SLURM_SETTINGS,
                    "ldap": [{"bind_password": "S3CRET-VALUE"}],
                },
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data["agent_configuration"]["backend_settings"]
        self.assertIn("ldap[0].bind_password", errors)
        self.assertNotIn("S3CRET-VALUE", str(response.data))

    def test_undeclared_credential_like_setting_is_refused(self):
        response = self.update(
            {
                "order_processing_backend": "slurm",
                "backend_settings": {**SLURM_SETTINGS, "site_api_key": "x"},
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "site_api_key", response.data["agent_configuration"]["backend_settings"]
        )

    def test_declared_plain_setting_named_like_a_credential_is_accepted(self):
        # cscs_dwdi_oidc_token_url contains "token" but the plugin declares it
        # as a plain setting; covered by SLURM_WITH_DWDI.
        response = self.update(SLURM_WITH_DWDI)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_without_integration_permission_cannot_set_it(self):
        response = self.update(SLURM_WITH_DWDI, user=self.fixture.user)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class ComponentChangeTest(AgentConfigurationTestMixin, test.APITestCase):
    """backend_components follows the offering's components."""

    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        self.gpu = factories.OfferingComponentFactory(
            offering=self.offering, type="gpu"
        )
        self.offering.agent_configuration = {
            **SLURM_WITH_DWDI,
            "backend_components": {
                "node": {"unit_factor_reporting": 1},
                "gpu": {"unit_factor_reporting": 2},
            },
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)

    @staticmethod
    def configuration_writes(queries):
        """The UPDATEs that write the agent configuration and nothing else."""
        return [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].startswith(
                'UPDATE "marketplace_offering" SET "agent_configuration" ='
            )
        ]

    def test_renamed_component_keeps_its_fields(self):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                url, {"uuid": self.gpu.uuid.hex, "type": "gres_gpu"}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(self.configuration_writes(queries)), 1)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["backend_components"],
            {
                "node": {"unit_factor_reporting": 1},
                "gres_gpu": {"unit_factor_reporting": 2},
            },
        )

    def test_removed_component_leaves_the_configuration(self):
        url = factories.OfferingFactory.get_url(
            self.offering, "remove_offering_component"
        )
        response = self.client.post(url, {"uuid": self.gpu.uuid.hex}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["backend_components"],
            {"node": {"unit_factor_reporting": 1}},
        )
        # The unchanged configuration can be saved again.
        self.assertEqual(
            self.update(self.offering.agent_configuration).status_code,
            status.HTTP_200_OK,
        )

    def test_component_dropped_by_the_import_command_leaves_the_configuration(self):
        # import_offering, the management command, removes the components its
        # file does not list.
        data = ExportImportOfferingSerializer(self.offering).data
        data["components"] = [
            {**component, "offering_id": self.offering.id}
            for component in data["components"]
            if component["type"] != "gpu"
        ]
        serializer = ExportImportOfferingSerializer(self.offering, data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        serializer.save()

        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.agent_configuration["backend_components"],
            {"node": {"unit_factor_reporting": 1}},
        )

    def test_components_dropped_together_are_written_once(self):
        factories.OfferingComponentFactory(offering=self.offering, type="cpu")
        data = ExportImportOfferingSerializer(self.offering).data
        data["components"] = [
            {**component, "offering_id": self.offering.id}
            for component in data["components"]
            if component["type"] == "cpu"
        ]
        serializer = ExportImportOfferingSerializer(self.offering, data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        with CaptureQueriesContext(connection) as queries:
            serializer.save()

        self.offering.refresh_from_db()
        self.assertNotIn("backend_components", self.offering.agent_configuration)
        self.assertEqual(len(self.configuration_writes(queries)), 1)

    def test_component_without_configuration_writes_nothing(self):
        self.offering.agent_configuration = {}
        self.offering.save()
        url = factories.OfferingFactory.get_url(
            self.offering, "remove_offering_component"
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(url, {"uuid": self.gpu.uuid.hex}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.configuration_writes(queries), [])


class AgentConfigurationVisibilityTest(AgentConfigurationTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.offering.agent_configuration = SLURM_WITH_DWDI
        self.offering.shared = True
        self.offering.save()

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(factories.OfferingFactory.get_url(self.offering))

    def test_provider_sees_configuration(self):
        response = self.get(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["agent_configuration"], SLURM_WITH_DWDI)

    def test_offering_manager_sees_configuration(self):
        # As docker/rootfs/etc/waldur/permissions.yaml grants it.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self.get(manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["agent_configuration"], SLURM_WITH_DWDI)

    def test_other_users_do_not(self):
        response = self.get(self.fixture.user)

        if response.status_code == status.HTTP_200_OK:
            self.assertNotIn("agent_configuration", response.data)

    def test_public_offering_never_has_configuration(self):
        # Not even for the provider: the public view leaves it out like
        # secret_options, rather than relying on the provider-only filter.
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.offering)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("agent_configuration", response.data)


class AgentConfigurationCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.category = factories.CategoryFactory()

    def create(self, offering_type, agent_configuration):
        self.client.force_authenticate(self.fixture.owner)
        return self.client.post(
            factories.OfferingFactory.get_list_url(),
            {
                "name": "Cluster",
                "category": factories.CategoryFactory.get_url(self.category),
                "customer": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
                "type": offering_type,
                "components": [
                    {
                        "type": "node",
                        "name": "Node hours",
                        "measured_unit": "node-hours",
                        "billing_type": "limit",
                    }
                ],
                "plans": [{"name": "Default", "unit": "month"}],
                "agent_configuration": agent_configuration,
            },
            format="json",
        )

    def test_create_with_configuration(self):
        response = self.create(SITE_AGENT_OFFERING, SLURM_WITH_DWDI)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.agent_configuration, SLURM_WITH_DWDI)

    def test_create_other_type_with_configuration_is_refused(self):
        response = self.create(BASIC_OFFERING, {"order_processing_backend": "slurm"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.data["agent_configuration"]),
            "Only site agent offerings have an agent configuration.",
        )


class AgentPluginListTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = reverse("marketplace-site-agent-plugins")

    def test_lists_catalogue(self):
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {plugin["name"] for plugin in response.data}
        self.assertIn("slurm", names)
        self.assertIn("cscs-dwdi-compute", names)

    def test_filters_by_role(self):
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(self.url, {"role": "order_processing"})

        names = {plugin["name"] for plugin in response.data}
        self.assertIn("slurm", names)
        self.assertNotIn("cscs-dwdi-compute", names)

    def test_unknown_role_is_refused(self):
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(self.url, {"role": "everything"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_is_refused(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class LegacySettingsTest(test.APITestCase):
    LEGACY = {
        "default_account": "acc",
        "customer_prefix": "c_",
        "hostname": "slurm.example.com",
        "service_provider_can_create_offering_user": True,
    }

    def test_move_keeps_existing_backend_settings(self):
        plugin_options, configuration = agent_configuration.move_legacy_plugin_options(
            self.LEGACY, {"backend_settings": {"default_account": "kept"}}
        )

        self.assertEqual(
            plugin_options, {"service_provider_can_create_offering_user": True}
        )
        self.assertEqual(
            configuration["backend_settings"],
            {
                "default_account": "kept",
                "customer_prefix": "c_",
                "hostname": "slurm.example.com",
            },
        )

    def test_data_migration_moves_site_agent_offerings_only(self):
        migration = importlib.import_module(
            "waldur_mastermind.marketplace.migrations."
            "0328_move_site_agent_settings_to_agent_configuration"
        )
        slurm = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, plugin_options=dict(self.LEGACY)
        )
        basic = factories.OfferingFactory(
            type=BASIC_OFFERING, plugin_options=dict(self.LEGACY)
        )

        migration.move_to_agent_configuration(apps, None)

        slurm.refresh_from_db()
        basic.refresh_from_db()
        self.assertEqual(
            slurm.plugin_options, {"service_provider_can_create_offering_user": True}
        )
        self.assertEqual(
            slurm.agent_configuration["backend_settings"]["default_account"], "acc"
        )
        self.assertEqual(basic.plugin_options, self.LEGACY)

        migration.move_back_to_plugin_options(apps, None)

        slurm.refresh_from_db()
        self.assertEqual(slurm.plugin_options["hostname"], "slurm.example.com")

    def test_data_migration_leaves_plugin_options_that_are_not_a_mapping(self):
        # import_offering used to store plugin_options unvalidated, so a
        # database can hold a string there.
        migration = importlib.import_module(
            "waldur_mastermind.marketplace.migrations."
            "0328_move_site_agent_settings_to_agent_configuration"
        )
        offering = factories.OfferingFactory(type=SITE_AGENT_OFFERING)
        marketplace_models.Offering.objects.filter(pk=offering.pk).update(
            plugin_options="abc"
        )

        migration.move_to_agent_configuration(apps, None)
        migration.move_back_to_plugin_options(apps, None)

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options, "abc")
        self.assertEqual(offering.agent_configuration, {})


class ImportValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        # Imports create offerings for service providers only.
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION
        )
        self.category = factories.CategoryFactory(title="HPC")

    def import_offering(self, document, import_components=True):
        self.client.force_authenticate(self.fixture.owner)
        return self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.fixture.customer.uuid.hex,
                "category": self.category.title,
                "import_components": import_components,
                "offering_data": yaml.safe_dump(document),
            },
        )

    def document(self, **sections):
        return {
            "offering": {
                "name": "Imported cluster",
                "type": SITE_AGENT_OFFERING,
                "category_name": self.category.title,
            },
            "components": [
                {
                    "type": "node",
                    "name": "Node hours",
                    "billing_type": "limit",
                    "measured_unit": "node-hours",
                }
            ],
            **sections,
        }

    def test_imported_configuration_is_recorded_with_the_importer(self):
        response = self.import_offering(
            self.document(agent_configuration=SLURM_WITH_DWDI)
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(
            configuration_changes(offering),
            [
                (
                    self.fixture.owner.username,
                    agent_configuration.CHANGE_REVISION_COMMENT,
                )
            ],
        )

    def test_invalid_plugin_options_are_refused(self):
        response = self.import_offering(
            self.document(plugin_options={"username_generation_policy": "nonsense"})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            marketplace_models.Offering.objects.filter(name="Imported cluster").exists()
        )

    def test_options_that_are_not_mappings_are_refused(self):
        for section, value in (
            ("plugin_options", "abc"),
            ("plugin_options", ["default_account"]),
            ("agent_configuration", [1]),
        ):
            with self.subTest(section=section, value=value):
                # Legacy settings in plugin_options are what used to reach the
                # mapping-only code before the check.
                sections = {"plugin_options": {"default_account": "acc"}}
                sections[section] = value
                response = self.import_offering(self.document(**sections))

                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn(section, response.data)

    def test_components_left_out_of_the_import_do_not_count(self):
        response = self.import_offering(
            self.document(
                agent_configuration={
                    "order_processing_backend": "slurm",
                    "backend_settings": SLURM_SETTINGS,
                    "backend_components": {"node": {"unit_factor_reporting": 1}},
                }
            ),
            import_components=False,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "node", response.data["agent_configuration"]["backend_components"]
        )

    def test_legacy_settings_move_to_agent_configuration(self):
        response = self.import_offering(
            self.document(plugin_options={"default_account": "acc", "hostname": "h"})
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertNotIn("default_account", offering.plugin_options)
        self.assertEqual(
            offering.agent_configuration["backend_settings"]["default_account"], "acc"
        )

    def test_old_export_over_another_backend_drops_the_legacy_settings(self):
        # waldur's settings schema allows no other properties, so the SLURM
        # settings of an export made before the move would fail validation.
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        offering.agent_configuration = WALDUR_BACKEND
        offering.save()

        response = self.overwrite(
            self.document(plugin_options={"default_account": "acc", "hostname": "h"})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(offering.agent_configuration, WALDUR_BACKEND)
        self.assertNotIn("default_account", offering.plugin_options)
        self.assertTrue(
            any(
                "SLURM settings" in str(warning) and "hostname" in str(warning)
                for warning in response.data["warnings"]
            )
        )

    def test_legacy_settings_next_to_another_backend_are_dropped(self):
        response = self.import_offering(
            self.document(
                plugin_options={"default_account": "acc"},
                agent_configuration=WALDUR_BACKEND,
            )
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(offering.agent_configuration, WALDUR_BACKEND)
        self.assertNotIn("default_account", offering.plugin_options)

    def test_api_key_options_need_key_provisioning(self):
        response = self.import_offering(
            self.document(plugin_options={"api_key_daily_usage": True})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("enable_api_key_provisioning", str(response.data))
        self.assertFalse(
            marketplace_models.Offering.objects.filter(name="Imported cluster").exists()
        )

    def test_agent_configuration_is_imported_and_validated(self):
        response = self.import_offering(
            self.document(agent_configuration=SLURM_WITH_DWDI)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        bad = {
            **SLURM_WITH_DWDI,
            "backend_settings": {**SLURM_SETTINGS, "cscs_dwdi_client_secret": "x"},
        }
        document = self.document(agent_configuration=bad)
        document["offering"]["name"] = "Second cluster"
        response = self.import_offering(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_settings_are_imported_and_validated(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "agent_settings": {"resource_import_enabled": True},
        }
        response = self.import_offering(
            self.document(agent_configuration=configuration)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(offering.agent_configuration, configuration)

        document = self.document(
            agent_configuration={
                **SLURM_WITH_DWDI,
                "agent_settings": {"verify_ssl": False},
            }
        )
        document["offering"]["name"] = "Second cluster"
        response = self.import_offering(document)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_configuration_of_another_offering_type_is_skipped(self):
        document = self.document(agent_configuration=SLURM_WITH_DWDI)
        document["offering"]["type"] = BASIC_OFFERING

        response = self.import_offering(document)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(offering.agent_configuration, {})
        self.assertTrue(
            any("agent configuration" in str(w) for w in response.data["warnings"])
        )

    def test_old_export_merges_into_the_current_configuration(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document(agent_configuration=SLURM_WITH_DWDI))
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.fixture.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": True,
                "offering_data": yaml.safe_dump(
                    self.document(plugin_options={"default_account": "legacy"})
                ),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(
            offering.agent_configuration["reporting_backend"], "cscs-dwdi-compute"
        )
        # The current value wins over the one in the old export.
        self.assertEqual(
            offering.agent_configuration["backend_settings"]["default_account"], "root"
        )

    def test_script_secret_options_survive_an_import(self):
        document = self.document()
        document["offering"]["type"] = BASIC_OFFERING
        document["secret_options"] = {
            "language": "python",
            "resource_options_handler": "print(1)",
            "environ": [{"name": "EMPTY", "value": ""}],
        }
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.fixture.customer.uuid.hex,
                "category": self.category.title,
                "import_secret_options": True,
                "offering_data": yaml.safe_dump(document),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertEqual(
            offering.secret_options["resource_options_handler"], "print(1)"
        )
        self.assertEqual(offering.secret_options["environ"][0]["value"], "")

    def overwrite(self, document, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        return self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.fixture.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": True,
                "offering_data": yaml.safe_dump(document),
            },
        )

    def test_overwriting_options_needs_the_integration_permission(self):
        self.import_offering(self.document())

        response = self.overwrite(
            self.document(plugin_options={"auto_approve_remote_orders": True})
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_overwriting_disabled_actions_is_staff_only(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())

        response = self.overwrite(
            self.document(plugin_options={"disabled_resource_actions": ["terminate"]})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertNotIn("disabled_resource_actions", offering.plugin_options)

    def test_overwriting_expiration_reminders_is_staff_only(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())

        response = self.overwrite(
            self.document(plugin_options={"resource_expiration_reminders": [3]})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertNotIn("resource_expiration_reminders", offering.plugin_options)

    def test_overwriting_keeps_expiration_reminders_left_out(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        offering.plugin_options = {"resource_expiration_reminders": [14, 3]}
        offering.save()

        response = self.overwrite(
            self.document(plugin_options={"auto_approve_remote_orders": True})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["resource_expiration_reminders"], [14, 3]
        )

    def test_staff_can_import_staff_only_options(self):
        self.import_offering(self.document())
        staff = structure_factories.UserFactory(is_staff=True)

        response = self.overwrite(
            self.document(
                plugin_options={
                    "disable_grace_period": True,
                    "disabled_resource_actions": ["terminate"],
                }
            ),
            user=staff,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertIs(offering.plugin_options["disable_grace_period"], True)
        self.assertEqual(
            offering.plugin_options["disabled_resource_actions"], ["terminate"]
        )

    def test_overwriting_keeps_staff_only_options_left_out(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        offering.plugin_options = {"disabled_resource_actions": ["terminate"]}
        offering.save()

        response = self.overwrite(
            self.document(plugin_options={"auto_approve_remote_orders": True})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["disabled_resource_actions"], ["terminate"]
        )
        self.assertTrue(offering.plugin_options["auto_approve_remote_orders"])

    def test_overwriting_with_the_integration_permission_works(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.import_offering(self.document())

        response = self.overwrite(
            self.document(plugin_options={"auto_approve_remote_orders": True})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_unknown_plugin_options_are_dropped_with_a_warning(self):
        response = self.import_offering(self.document(plugin_options={"no_such": 1}))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(name="Imported cluster")
        self.assertNotIn("no_such", offering.plugin_options)
        self.assertTrue(
            any("no_such" in str(warning) for warning in response.data["warnings"])
        )


class AgentConfigurationExportTest(AgentConfigurationTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.offering.agent_configuration = SLURM_WITH_DWDI
        self.offering.save()

    def export(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering, "export_offering")
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["export_data"]

    def test_exported_to_who_may_see_the_integration(self):
        self.assertEqual(self.export()["agent_configuration"], SLURM_WITH_DWDI)

    def test_agent_settings_are_exported(self):
        configuration = {
            **SLURM_WITH_DWDI,
            "agent_settings": {"preserve_unmanaged_backend_users": True},
        }
        self.offering.agent_configuration = configuration
        self.offering.save()

        self.assertEqual(self.export()["agent_configuration"], configuration)

    def test_not_exported_without_the_integration_permission(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

        self.assertNotIn("agent_configuration", self.export())


class SecretRequirementTest(test.APITestCase):
    SCHEMA = {
        "$defs": {
            "Ldap": {
                "properties": {"bind_password": {"secret": True}},
                "required": ["bind_password"],
            }
        },
        "properties": {
            "token": {"secret": True},
            "optional_token": {"secret": True},
            "ldap": {"anyOf": [{"$ref": "#/$defs/Ldap"}, {"type": "null"}]},
            "required_ldap": {"$ref": "#/$defs/Ldap"},
        },
        "required": ["token", "required_ldap"],
    }

    def test_requirements(self):
        requirement = agent_configuration.secret_requirement
        self.assertEqual(
            requirement(self.SCHEMA, "token"), agent_configuration.REQUIRED
        )
        self.assertEqual(
            requirement(self.SCHEMA, "optional_token"), agent_configuration.OPTIONAL
        )
        self.assertEqual(
            requirement(self.SCHEMA, "ldap.bind_password"),
            agent_configuration.REQUIRED_WITH_PARENT,
        )
        self.assertEqual(
            requirement(self.SCHEMA, "required_ldap.bind_password"),
            agent_configuration.REQUIRED,
        )

    def test_wrapped_ref_is_followed(self):
        # pydantic puts a $ref with a description of its own under allOf.
        schema = {
            **self.SCHEMA,
            "properties": {"ldap": {"allOf": [{"$ref": "#/$defs/Ldap"}]}},
            "required": ["ldap"],
        }

        self.assertEqual(
            agent_configuration.secret_requirement(schema, "ldap.bind_password"),
            agent_configuration.REQUIRED,
        )

    def _secret_settings(self, plugins):
        with mock.patch.object(
            agent_configuration, "get_plugin", side_effect=plugins.get
        ):
            return agent_configuration.secret_settings(list(plugins))

    def test_backend_that_needs_a_shared_secret_most_decides(self):
        optional_ldap = {
            "settings_schema": {**self.SCHEMA, "required": ["token"]},
            "secret_settings": ["required_ldap.bind_password"],
        }
        required_ldap = {
            "settings_schema": self.SCHEMA,
            "secret_settings": ["required_ldap.bind_password"],
        }

        found = self._secret_settings(
            {"ldap-roles": optional_ldap, "ldap": required_ldap}
        )

        self.assertEqual(
            found,
            [
                (
                    "ldap-roles",
                    "required_ldap.bind_password",
                    agent_configuration.REQUIRED,
                )
            ],
        )

    def test_plugin_with_null_secret_settings_has_none(self):
        self.assertEqual(
            self._secret_settings({"plain": {"secret_settings": None}}), []
        )

    def test_without_secrets_handles_any_key_and_nesting(self):
        settings = {
            "hosts": [[{"password": "a", "name": "h1"}]],
            "a.b": {"token": "b", "x": 1},
            "list[0]": "kept",
        }

        self.assertEqual(
            agent_configuration.without_secrets(settings),
            {"hosts": [[{"name": "h1"}]], "a.b": {"x": 1}, "list[0]": "kept"},
        )

    def test_without_secrets_strips_secrets_in_any_shape(self):
        settings = {
            "ldap": [{"bind_password": "a", "uri": "ldap://x"}, {"bind_password": "b"}],
            "keycloak": {"keycloak_password": "c", "keycloak_url": "https://k"},
            "site_api_key": "d",
            "cluster_name": "alpha",
        }

        cleaned = agent_configuration.without_secrets(settings)

        self.assertEqual(
            cleaned,
            {
                "ldap": [{"uri": "ldap://x"}, {}],
                "keycloak": {"keycloak_url": "https://k"},
                "cluster_name": "alpha",
            },
        )
