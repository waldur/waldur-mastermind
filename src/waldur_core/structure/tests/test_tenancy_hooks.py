from types import SimpleNamespace
from unittest import mock

import requests
from django.contrib import admin as django_admin
from django.contrib import messages
from django.test import SimpleTestCase, override_settings
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework.exceptions import ValidationError

from waldur_auth_social import handlers as auth_social_handlers
from waldur_auth_social import utils as auth_social_utils
from waldur_core.core import auth_hooks, enums, user_attributes
from waldur_core.structure import admin as structure_admin
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import hooks as structure_hooks
from waldur_core.structure import models as structure_models
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import utils as structure_utils
from waldur_core.structure import views as structure_views
from waldur_core.users import utils as users_utils
from waldur_core.users.scim import hooks as scim_hooks
from waldur_core.users.scim import tasks as scim_tasks
from waldur_freeipa import handlers as freeipa_handlers
from waldur_freeipa import utils as freeipa_utils
from waldur_mastermind.billing import serializers as billing_serializers
from waldur_mastermind.invoices import handlers as invoice_handlers
from waldur_mastermind.invoices import vat as invoice_vat
from waldur_mastermind.marketplace import core_hooks as marketplace_core_hooks
from waldur_mastermind.marketplace_metrics import (
    handlers as marketplace_metrics_handlers,
)
from waldur_mastermind.marketplace_remote import (
    handlers as marketplace_remote_handlers,
)


class TenancyExtensionHooksTest(SimpleTestCase):
    def test_auth_social_registers_identity_provider_lookup_and_reexports_user_attribute_helpers(
        self,
    ):
        self.assertIs(
            auth_hooks._identity_provider_lookup,
            auth_social_handlers.get_identity_provider,
        )
        self.assertTrue(
            auth_hooks.has_remote_user_puller(enums.ProviderChoices.EDUTEAMS)
        )
        self.assertIs(
            auth_social_utils.update_user_attributes_from_source,
            user_attributes.update_user_attributes_from_source,
        )
        self.assertIs(
            auth_social_utils.remove_user_from_isd,
            user_attributes.remove_user_from_isd,
        )
        self.assertIs(
            auth_social_utils._field_locked,
            user_attributes._field_locked,
        )

    def test_get_identity_provider_caches_hit_and_miss(self):
        sentinel = object()
        cache = {}
        with mock.patch.object(
            auth_hooks, "get_identity_provider", side_effect=[sentinel, None]
        ) as mock_lookup:
            self.assertIs(
                structure_utils._get_identity_provider("keycloak", cache), sentinel
            )
            self.assertIs(
                structure_utils._get_identity_provider("keycloak", cache), sentinel
            )
            self.assertIsNone(structure_utils._get_identity_provider("eduteams", cache))
            self.assertIsNone(structure_utils._get_identity_provider("eduteams", cache))
            self.assertEqual(mock_lookup.call_count, 2)

    @override_settings(WALDUR_AUTH_SOCIAL={"REMOTE_EDUTEAMS_ENABLED": True})
    def test_pull_remote_user_raises_when_puller_not_registered(self):
        viewset = structure_views.UserViewSet()
        fake_user = SimpleNamespace(
            username="alice",
            identity_source=enums.ProviderChoices.EDUTEAMS,
            registration_method=enums.ProviderChoices.EDUTEAMS,
        )
        with (
            mock.patch.object(viewset, "get_object", return_value=fake_user),
            mock.patch.object(auth_hooks, "_remote_user_pullers", {}),
        ):
            with self.assertRaises(ValidationError):
                viewset.pull_remote_user(request=mock.Mock(), uuid="123")

    def test_structure_project_and_customer_bulk_fetchers_registered(self):
        project_fetchers = {
            f.bulk_key: f for f in structure_hooks.get_project_bulk_fetchers()
        }
        self.assertIn("billing_estimates", project_fetchers)
        self.assertIs(
            project_fetchers["billing_estimates"].fetcher,
            billing_serializers.bulk_fetch_project_billing_estimates,
        )
        self.assertIn("project_credits", project_fetchers)
        self.assertIs(
            project_fetchers["project_credits"].fetcher,
            invoice_handlers.bulk_fetch_project_credits,
        )
        self.assertIn("project_has_metrics", project_fetchers)
        self.assertIs(
            project_fetchers["project_has_metrics"].fetcher,
            marketplace_metrics_handlers.bulk_fetch_projects_with_metrics,
        )
        self.assertEqual(
            project_fetchers["project_has_metrics"].default_factory(), set()
        )

        customer_fetchers = {
            f.name: f for f in structure_hooks.get_customer_bulk_fetchers()
        }
        self.assertIn("billing_estimates", customer_fetchers)
        self.assertIs(
            customer_fetchers["billing_estimates"].fetcher,
            billing_serializers.bulk_fetch_customer_billing_estimates,
        )
        self.assertIn("credits", customer_fetchers)
        self.assertIs(
            customer_fetchers["credits"].fetcher,
            invoice_handlers.bulk_fetch_customer_credits,
        )

    def test_project_list_serializer_executes_only_requested_bulk_fetchers(self):
        called = []

        def fetcher_a(request, project_ids):
            called.append(("a", project_ids))
            return {1: "ok"}

        def fetcher_b(request, project_ids):
            called.append(("b", project_ids))
            return {1: "skipped"}

        custom_fetchers = (
            structure_hooks.ProjectBulkFetcher(
                field_name="field_a",
                bulk_key="key_a",
                default_factory=dict,
                fetcher=fetcher_a,
            ),
            structure_hooks.ProjectBulkFetcher(
                field_name="field_b",
                bulk_key="key_b",
                default_factory=set,
                fetcher=fetcher_b,
            ),
        )
        fake_request = SimpleNamespace(
            query_params=mock.Mock(getlist=mock.Mock(return_value=["field_a"])),
            GET=mock.Mock(getlist=mock.Mock(return_value=[])),
            user=None,
        )
        child = mock.Mock()
        child.to_representation.return_value = {"id": 1}
        serializer = structure_serializers.ProjectListSerializer(
            child=child, context={"request": fake_request}
        )
        with (
            mock.patch.object(
                structure_hooks,
                "get_project_bulk_fetchers",
                return_value=custom_fetchers,
            ),
            mock.patch.object(
                structure_serializers.managers,
                "get_read_only_only_project_ids",
                return_value=set(),
            ),
        ):
            serializer.to_representation([SimpleNamespace(id=1)])

        self.assertEqual(called, [("a", [1])])
        self.assertEqual(serializer.context["bulk_data"]["key_a"], {1: "ok"})
        self.assertEqual(serializer.context["bulk_data"]["key_b"], set())

    def test_billing_and_invoices_register_cost_subquery_and_vat_explainer(self):
        self.assertIs(
            structure_hooks._project_estimated_cost_subquery,
            billing_serializers.get_project_estimated_cost_subquery,
        )
        self.assertIs(
            structure_hooks._customer_vat_explainer,
            invoice_vat.explain_customer_vat,
        )

    def test_estimated_cost_filter_noop_when_subquery_provider_unregistered(self):
        backend = structure_filters.ProjectEstimatedCostFilter()
        queryset = mock.Mock()
        request = SimpleNamespace(query_params={"o": "estimated_cost"})
        with mock.patch.object(
            structure_hooks, "get_project_estimated_cost_subquery", return_value=None
        ):
            result = backend.filter_queryset(request, queryset, view=mock.Mock())
        self.assertIs(result, queryset)
        queryset.annotate.assert_not_called()

    def test_customer_serializer_explain_vat_returns_none_when_unregistered(self):
        serializer = structure_serializers.CustomerSerializer(context={})
        with (
            mock.patch.object(
                structure_serializers,
                "config",
                SimpleNamespace(VAT_TAXATION_ENABLED=True),
            ),
            mock.patch.object(structure_hooks, "_customer_vat_explainer", None),
        ):
            self.assertIsNone(serializer._explain_vat(SimpleNamespace(pk=1)))

    def test_marketplace_remote_registers_named_admin_task_handlers(self):
        self.assertIs(
            structure_hooks._remote_project_sync_handler,
            marketplace_remote_handlers.schedule_sync_remote_project,
        )
        self.assertIs(
            structure_hooks._remote_project_clean_handler,
            marketplace_remote_handlers.schedule_clean_remote_projects,
        )

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.sync_remote_project.delay"
        ) as mock_sync:
            self.assertTrue(structure_hooks.schedule_remote_project_sync(["project:1"]))
            mock_sync.assert_called_once_with(["project:1"])

        with mock.patch(
            "waldur_mastermind.marketplace_remote.tasks.clean_remote_projects.delay"
        ) as mock_clean:
            self.assertTrue(structure_hooks.schedule_remote_projects_cleanup())
            mock_clean.assert_called_once_with()

    def test_project_admin_warns_when_remote_handlers_unregistered(self):
        project_admin = structure_admin.ProjectAdmin(
            structure_models.Project, django_admin.site
        )
        request = mock.Mock()
        with (
            mock.patch.object(structure_hooks, "_remote_project_sync_handler", None),
            mock.patch.object(structure_hooks, "_remote_project_clean_handler", None),
            mock.patch.object(project_admin, "message_user") as mock_message,
        ):
            project_admin.sync_remote(request, queryset=[])
            project_admin.clean_remote_projects(request)

        self.assertEqual(mock_message.call_count, 2)
        for call in mock_message.call_args_list:
            self.assertEqual(call.args[2], messages.WARNING)

    def test_marketplace_registers_scim_offering_provider(self):
        self.assertIs(
            scim_hooks.get_scim_offering_provider(),
            marketplace_core_hooks.MARKETPLACE_SCIM_OFFERING_PROVIDER,
        )

    def test_scim_get_user_ssh_login_nodes_logs_warning_on_scim_logger(self):
        fake_user = SimpleNamespace(uuid="user-uuid-1")
        provider_empty_users = scim_hooks.ScimOfferingProvider(
            get_user_offering_usernames=lambda user, pids: {},
            get_reconciliation_user_ids=lambda cutoff: set(),
            get_offering=lambda uuid: None,
            get_offering_eligible_user_uuids=lambda offering: [],
        )
        provider_no_offerings = scim_hooks.ScimOfferingProvider(
            get_user_offering_usernames=lambda user, pids: None,
            get_reconciliation_user_ids=lambda cutoff: set(),
            get_offering=lambda uuid: None,
            get_offering_eligible_user_uuids=lambda offering: [],
        )
        fake_roles = mock.Mock()
        fake_roles.order_by.return_value.values_list.return_value.distinct.return_value = [
            1
        ]
        with (
            mock.patch.object(scim_tasks, "get_project_content_type", return_value=1),
            mock.patch.object(
                scim_tasks.UserRole.objects, "filter", return_value=fake_roles
            ),
        ):
            with (
                mock.patch.object(
                    scim_hooks,
                    "get_scim_offering_provider",
                    return_value=provider_empty_users,
                ),
                self.assertLogs("waldur_core.users.scim.tasks", level="WARNING") as cm,
            ):
                self.assertEqual(scim_tasks.get_user_ssh_login_nodes(fake_user), {})
            self.assertTrue(
                any("no offering users in OK state" in msg for msg in cm.output)
            )

            with (
                mock.patch.object(
                    scim_hooks,
                    "get_scim_offering_provider",
                    return_value=provider_no_offerings,
                ),
                self.assertNoLogs("waldur_core.users.scim.tasks", level="WARNING"),
            ):
                self.assertEqual(scim_tasks.get_user_ssh_login_nodes(fake_user), {})

    def test_freeipa_registers_external_profile_provider(self):
        provider = users_utils._external_profile_provider
        self.assertIsNotNone(provider)
        self.assertIs(
            provider.generate_safe_username,
            freeipa_utils.generate_safe_username,
        )
        self.assertIs(
            provider.get_or_create_profile,
            freeipa_handlers.get_or_create_profile,
        )
        errors = users_utils.get_profile_provider_errors()
        self.assertIn(freeipa_exceptions.FreeIPAError, errors)
        self.assertIn(requests.RequestException, errors)

    def test_external_profile_provider_fallbacks_when_unregistered(self):
        with mock.patch.object(users_utils, "_external_profile_provider", None):
            self.assertEqual(
                users_utils.generate_safe_username("Alice.Smith+test@example.org"),
                "alice_smith_test_example_org",
            )
            with self.assertLogs("waldur_core.users.utils", level="WARNING") as cm:
                self.assertEqual(
                    users_utils.get_or_create_profile(
                        SimpleNamespace(id=42), "alice", "secret"
                    ),
                    (None, False),
                )
            self.assertTrue(
                any(
                    "No external profile provider registered" in msg
                    for msg in cm.output
                )
            )
            self.assertEqual(
                users_utils.get_profile_provider_errors(),
                (requests.RequestException,),
            )
