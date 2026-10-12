"""Coverage for marketplace migration 0306_scim_login_nodes_off_endpoints.

The callable is exercised with the live models, the same way as
marketplace/tests/test_migration_0226_limit_period_backfill.py.
"""

import importlib.util
import pathlib

from django.apps import apps as django_apps
from django.test import TestCase

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "0306_scim_login_nodes_off_endpoints.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "_migration_0306_under_test", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Migration0306ScimLoginNodesTest(TestCase):
    def setUp(self):
        self.migration = _load_migration_module()

    def _run(self):
        self.migration.move_scim_login_nodes_off_endpoints(django_apps, None)

    def _endpoint(self, offering, url, name="Access"):
        return marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=offering, name=name, url=url
        )

    def test_moves_ssh_hostnames_and_keeps_other_endpoints(self):
        offering = marketplace_factories.OfferingFactory(
            plugin_options={
                "enable_scim_entitlements": True,
                "other": "kept",
            }
        )
        self._endpoint(offering, "ssh://Login.Example.ORG:22", "Login")
        self._endpoint(offering, "ssh://user@login.example.org", "Duplicate")
        self._endpoint(offering, "ssh://other.example.org", "Other")
        self._endpoint(offering, "https://ca.example.org", "CA")

        self._run()

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["scim_login_nodes"],
            ["login.example.org", "other.example.org"],
        )
        self.assertIs(offering.plugin_options["enable_scim_entitlements"], True)
        self.assertEqual(offering.plugin_options["other"], "kept")
        self.assertEqual(
            list(offering.endpoints.values_list("url", flat=True)),
            ["https://ca.example.org"],
        )

    def test_leaves_offerings_that_did_not_opt_in(self):
        offering = marketplace_factories.OfferingFactory()
        self._endpoint(offering, "ssh://login.example.org")

        self._run()

        offering.refresh_from_db()
        self.assertNotIn("scim_login_nodes", offering.plugin_options)
        self.assertEqual(offering.endpoints.count(), 1)

    def test_does_not_overwrite_an_existing_list(self):
        offering = marketplace_factories.OfferingFactory(
            plugin_options={
                "enable_scim_entitlements": True,
                "scim_login_nodes": ["kept.example.org"],
            }
        )
        self._endpoint(offering, "ssh://login.example.org")

        self._run()

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["scim_login_nodes"], ["kept.example.org"]
        )
        self.assertEqual(offering.endpoints.count(), 0)

    def test_scim_offering_without_ssh_endpoints_is_unchanged(self):
        offering = marketplace_factories.OfferingFactory(
            plugin_options={"enable_scim_entitlements": True}
        )
        self._endpoint(offering, "https://ca.example.org", "CA")

        self._run()

        offering.refresh_from_db()
        self.assertNotIn("scim_login_nodes", offering.plugin_options)
        self.assertEqual(
            list(offering.endpoints.values_list("url", flat=True)),
            ["https://ca.example.org"],
        )
