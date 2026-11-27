from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib import messages
from django.core.management import get_commands
from django.db.models import signals
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import exceptions as rf_exceptions
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from waldur_core.core import mixins as core_mixins
from waldur_core.core import signals as core_signals
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import serializers as structure_serializers
from waldur_freeipa import handlers as freeipa_handlers
from waldur_freeipa import models as freeipa_models
from waldur_mastermind import marketplace_openstack
from waldur_mastermind.analytics import handlers as analytics_handlers
from waldur_mastermind.billing import handlers as billing_handlers
from waldur_mastermind.billing.serializers import NestedPriceEstimateSerializer
from waldur_mastermind.chat.validation import presets as chat_presets
from waldur_mastermind.demo_presets import DemoPresetManager
from waldur_mastermind.demo_presets.manifest import DEFAULT_PERMISSIONS_PATHS
from waldur_mastermind.marketplace import admin as marketplace_admin
from waldur_mastermind.marketplace import agent_configuration
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import hooks as marketplace_hooks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace import views as marketplace_views
from waldur_mastermind.marketplace_openstack import (
    executors as marketplace_openstack_executors,
)
from waldur_mastermind.marketplace_rancher import const as rancher_const
from waldur_mastermind.marketplace_site_agent import config_builder
from waldur_mastermind.marketplace_site_agent import utils as site_agent_utils
from waldur_mastermind.notifications import maintenance_handlers
from waldur_mastermind.notifications import models as notifications_models
from waldur_mastermind.notifications import serializers as notifications_serializers
from waldur_mastermind.policy import handlers as policy_handlers
from waldur_mastermind.policy import rebill_helpers as policy_rebill_helpers
from waldur_mastermind.promotions import handlers as promotions_handlers
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.proposal import filters as proposal_filters
from waldur_mastermind.proposal import managers as proposal_managers
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import serializers as proposal_serializers
from waldur_mastermind.support import handlers as support_handlers
from waldur_pid import mixins as pid_mixins
from waldur_pid import utils as pid_utils


class DemoPresetsExtractionTest(SimpleTestCase):
    def test_management_commands_registered_under_demo_presets(self):
        commands = get_commands()
        for command_name in (
            "demo_presets",
            "export_structure",
            "import_structure",
            "cleanup_structure",
        ):
            self.assertEqual(
                commands.get(command_name),
                "waldur_mastermind.demo_presets",
            )

    def test_demo_preset_urls_preserved(self):
        self.assertEqual(
            reverse("marketplace-demo-preset-list-presets"),
            "/api/marketplace-demo-presets/list/",
        )
        self.assertEqual(
            reverse("demo-preset-status"),
            "/api/demo-preset-status/",
        )

    def test_builtin_presets_and_permissions_paths_resolve(self):
        self.assertTrue(
            (DemoPresetManager.PRESETS_DIR / "minimal_quickstart.json").exists()
        )
        self.assertTrue(any(path.exists() for path in DEFAULT_PERMISSIONS_PATHS))
        self.assertEqual(
            DemoPresetManager.get_preset_path("minimal_quickstart"),
            DemoPresetManager.PRESETS_DIR / "minimal_quickstart.json",
        )

    def test_chat_preset_resolver_delegates_and_respects_patches(self):
        self.assertEqual(
            chat_presets.DemoPresetManager.get_preset_path("minimal_quickstart"),
            DemoPresetManager.PRESETS_DIR / "minimal_quickstart.json",
        )
        sentinel = DemoPresetManager.PRESETS_DIR / "patched.json"
        with mock.patch.object(
            DemoPresetManager, "get_preset_path", return_value=sentinel
        ) as patched:
            self.assertEqual(
                chat_presets.DemoPresetManager.get_preset_path("minimal_quickstart"),
                sentinel,
            )
            patched.assert_called_once_with("minimal_quickstart")

    def test_chat_preset_resolver_returns_none_when_unregistered(self):
        with mock.patch.object(chat_presets, "_preset_path_resolver", None):
            self.assertIsNone(
                chat_presets.DemoPresetManager.get_preset_path("minimal_quickstart")
            )
            self.assertFalse(chat_presets.is_preset_loaded("minimal_quickstart"))

    def test_celery_task_name_preserved(self):
        from waldur_mastermind.demo_presets.tasks import load_demo_preset

        self.assertEqual(
            load_demo_preset.name,
            "waldur_mastermind.marketplace.load_demo_preset",
        )


class MarketplaceHooksWiringTest(SimpleTestCase):
    def test_rancher_deployment_mode_constants_reexported(self):
        self.assertEqual(
            rancher_const.DEPLOYMENT_MODE_MANAGED,
            marketplace_enums.DEPLOYMENT_MODE_MANAGED,
        )
        self.assertEqual(
            rancher_const.DEPLOYMENT_MODE_SELF_MANAGED,
            marketplace_enums.DEPLOYMENT_MODE_SELF_MANAGED,
        )

    def test_support_unresolved_tickets_counter_delegates_and_falls_back(self):
        sentinel_qs = object()
        with mock.patch.object(
            support_handlers, "count_unresolved_tickets_for_resources", return_value=7
        ) as patched:
            self.assertEqual(marketplace_hooks.count_unresolved_tickets(sentinel_qs), 7)
            patched.assert_called_once_with(sentinel_qs)

        with mock.patch.object(marketplace_hooks, "_unresolved_tickets_counter", None):
            self.assertEqual(marketplace_hooks.count_unresolved_tickets(None), 0)

    def test_analytics_quota_history_provider_delegates_and_falls_back(self):
        entries = [(object(), "active_users_count", 3, date(2026, 1, 1))]
        with (
            mock.patch.object(analytics_handlers, "record_daily_quotas") as record_mock,
            mock.patch.object(
                analytics_handlers,
                "get_daily_quota_history",
                return_value=[{"date": date(2026, 1, 1), "usage": 3}],
            ) as history_mock,
        ):
            marketplace_hooks.record_daily_quotas(entries)
            record_mock.assert_called_once_with(entries)
            scope = object()
            self.assertEqual(
                marketplace_hooks.get_daily_quota_history(scope, "active_users_count"),
                [{"date": date(2026, 1, 1), "usage": 3}],
            )
            history_mock.assert_called_once_with(scope, "active_users_count")

        with mock.patch.object(marketplace_hooks, "_quota_history_provider", None):
            marketplace_hooks.record_daily_quotas([])
            self.assertEqual(
                marketplace_hooks.get_daily_quota_history(None, "nc_resource_count"),
                [],
            )

    def test_billing_estimates_updater_and_pre_serializer_fields(self):
        scopes = [object()]
        with mock.patch.object(
            billing_handlers, "update_estimates_for_scopes"
        ) as patched:
            marketplace_hooks.update_estimates_for_scopes(scopes)
            patched.assert_called_once_with(scopes)

        with mock.patch.object(marketplace_hooks, "_estimates_updater", None):
            marketplace_hooks.update_estimates_for_scopes([])

        factory = APIRequestFactory()
        only_estimate_req = Request(factory.get("/?field=billing_price_estimate"))
        only_uuid_req = Request(factory.get("/?field=uuid"))

        for serializer_cls in (
            marketplace_serializers.MarketplaceProviderCustomerProjectSerializer,
            marketplace_serializers.MarketplaceProviderCustomerSerializer,
            marketplace_serializers.ProviderOfferingSerializer,
        ):
            serializer = serializer_cls()
            self.assertIn("billing_price_estimate", serializer.fields)
            annotation = getattr(
                serializer_cls.get_billing_price_estimate,
                "_spectacular_annotation",
                {},
            )
            self.assertIs(annotation.get("field"), NestedPriceEstimateSerializer)

            # RestrictedSerializerMixin filters after AugmentedSerializerMixin adds fields
            self.assertEqual(
                list(serializer_cls(context={"request": only_estimate_req}).fields),
                ["billing_price_estimate"],
            )
            self.assertEqual(
                list(serializer_cls(context={"request": only_uuid_req}).fields),
                ["uuid"],
            )

            sentinel_obj = object()
            sentinel_resources = [object()]
            with (
                mock.patch.object(
                    serializer, "get_resources", return_value=sentinel_resources
                ) as get_resources_mock,
                mock.patch(
                    "waldur_mastermind.invoices.utils.get_billing_price_estimate_for_resources",
                    return_value={"total": 42},
                ) as estimate_mock,
            ):
                self.assertEqual(
                    serializer.get_billing_price_estimate(sentinel_obj), {"total": 42}
                )
                get_resources_mock.assert_called_once_with(sentinel_obj)
                estimate_mock.assert_called_once_with(sentinel_resources)

    def test_promotions_provider_delegates_and_falls_back(self):
        resource = object()
        provider_obj = object()
        today = date(2026, 1, 1)
        with (
            mock.patch.object(
                promotions_handlers, "create_discounted_resource_on_activation"
            ) as activate_mock,
            mock.patch.object(
                promotions_models.Campaign,
                "get_discount_for_resource",
                return_value=("camp", Decimal("5.00")),
            ) as discount_mock,
            mock.patch.object(
                promotions_handlers, "count_active_campaigns", return_value=2
            ) as count_mock,
        ):
            marketplace_hooks.create_discounted_resource_on_activation(resource)
            activate_mock.assert_called_once_with(resource)
            self.assertEqual(
                marketplace_hooks.get_discount_for_resource(
                    resource, 2026, 1, Decimal("10.00")
                ),
                ("camp", Decimal("5.00")),
            )
            discount_mock.assert_called_once_with(resource, 2026, 1, Decimal("10.00"))
            self.assertEqual(
                marketplace_hooks.count_active_campaigns(provider_obj, today), 2
            )
            count_mock.assert_called_once_with(provider_obj, today)

        with mock.patch.object(marketplace_hooks, "_promotions_provider", None):
            marketplace_hooks.create_discounted_resource_on_activation(None)
            self.assertEqual(
                marketplace_hooks.get_discount_for_resource(
                    None, 2026, 1, Decimal("10.00")
                ),
                (None, Decimal("10.00")),
            )
            self.assertEqual(marketplace_hooks.count_active_campaigns(None, today), 0)

    def test_freeipa_username_resolver_delegates_and_signal_connected(self):
        user = object()
        with mock.patch.object(
            freeipa_handlers, "resolve_freeipa_username", return_value="ipa-user"
        ) as patched:
            self.assertEqual(
                marketplace_utils.create_username_from_freeipa_profile(user),
                "ipa-user",
            )
            patched.assert_called_once_with(user)

        with mock.patch.object(marketplace_hooks, "_freeipa_username_resolver", None):
            self.assertEqual(
                marketplace_utils.create_username_from_freeipa_profile(user),
                "",
            )

        receivers = signals.post_save._live_receivers(freeipa_models.Profile)[0]
        self.assertIn(
            freeipa_handlers.update_offering_user_username_after_freeipa_profile_update,
            receivers,
        )

    def test_notifications_maintenance_fk_and_signals(self):
        field = marketplace_models.MaintenanceAnnouncement._meta.get_field(
            "admin_announcement"
        )
        self.assertIs(field.related_model, notifications_models.AdminAnnouncement)

        post_save_receivers = signals.post_save._live_receivers(
            marketplace_models.MaintenanceAnnouncement
        )[0]
        self.assertIn(
            maintenance_handlers.manage_maintenance_admin_announcements,
            post_save_receivers,
        )

        serializer_receivers = core_signals.pre_serializer_fields._live_receivers(
            notifications_serializers.AdminAnnouncementSerializer
        )[0]
        self.assertIn(
            maintenance_handlers.add_maintenance_fields_to_admin_announcement_serializer,
            serializer_receivers,
        )

    def test_pid_mixin_url_and_actions_provider(self):
        self.assertIs(pid_mixins.DataciteMixin, core_mixins.DataciteMixin)
        self.assertTrue(
            issubclass(marketplace_models.Offering, core_mixins.DataciteMixin)
        )
        self.assertEqual(
            reverse("marketplace-offering-referral-list"),
            "/api/marketplace-offering-referrals/",
        )
        provider = marketplace_hooks.get_pid_actions_provider()
        self.assertIsNotNone(provider)

        offering = object()
        with (
            mock.patch.object(pid_utils, "create_doi") as create_mock,
            mock.patch.object(pid_utils, "update_doi") as update_mock,
        ):
            provider.create_doi(offering)
            provider.update_doi(offering)
            create_mock.assert_called_once_with(offering)
            update_mock.assert_called_once_with(offering)

        offering_admin = marketplace_admin.OfferingAdmin(
            marketplace_models.Offering, mock.MagicMock()
        )
        request = mock.MagicMock()
        for action_name in (
            "datacite_registration",
            "datacite_update",
            "link_doi_with_collection",
            "offering_referrals_pull",
        ):
            queryset = mock.MagicMock()
            with (
                mock.patch.object(marketplace_hooks, "_pid_actions_provider", None),
                mock.patch.object(offering_admin, "message_user") as message_user,
            ):
                getattr(offering_admin, action_name)(request, queryset)
                message_user.assert_called_once()
                self.assertEqual(message_user.call_args.args[2], messages.WARNING)
                queryset.filter.assert_not_called()
                queryset.exclude.assert_not_called()

    def test_openstack_constants_mb_components_admin_and_reports(self):
        self.assertEqual(
            marketplace_openstack.CORES_TYPE, marketplace_enums.OPENSTACK_CORES_TYPE
        )
        self.assertEqual(
            marketplace_openstack.RAM_TYPE, marketplace_enums.OPENSTACK_RAM_TYPE
        )
        self.assertEqual(
            marketplace_openstack.STORAGE_TYPE, marketplace_enums.OPENSTACK_STORAGE_TYPE
        )
        self.assertEqual(
            marketplace_openstack.INSTANCE_METRIC_KEYS,
            marketplace_enums.INSTANCE_METRIC_KEYS,
        )
        self.assertEqual(
            marketplace_openstack.VOLUME_METRIC_KEYS,
            marketplace_enums.VOLUME_METRIC_KEYS,
        )
        self.assertEqual(
            marketplace_hooks.get_mb_component_types(),
            marketplace_openstack.get_mb_component_types(),
        )
        with mock.patch.object(marketplace_hooks, "_mb_component_types_provider", None):
            self.assertEqual(marketplace_hooks.get_mb_component_types(), frozenset())

        admin_provider = marketplace_hooks.get_openstack_admin_provider()
        self.assertIsNotNone(admin_provider)
        self.assertIs(
            admin_provider.get_restore_limits_executor(),
            marketplace_openstack_executors.RestoreTenantLimitsExecutor,
        )

        resource_admin = marketplace_admin.ResourceAdmin(
            marketplace_models.Resource, mock.MagicMock()
        )
        request = mock.MagicMock()
        with (
            mock.patch.object(marketplace_hooks, "_openstack_admin_provider", None),
            mock.patch.object(resource_admin, "message_user") as message_user,
        ):
            resource_admin.restore_limits.execute(
                resource_admin, request, mock.MagicMock()
            )
            message_user.assert_called_once()
            self.assertEqual(message_user.call_args.args[2], messages.WARNING)

        with (
            mock.patch.object(marketplace_hooks, "_openstack_admin_provider", None),
            mock.patch.object(resource_admin, "message_user") as message_user,
        ):
            response = resource_admin.create_resources_for_lost_instances_and_volumes(
                request
            )
            message_user.assert_called_once()
            self.assertEqual(message_user.call_args.args[2], messages.WARNING)
            self.assertEqual(response.status_code, 302)

        self.assertIsNotNone(marketplace_hooks.get_openstack_instance_report_provider())
        stats_view = marketplace_views.StatsViewSet()
        factory = APIRequestFactory()
        with mock.patch.object(
            marketplace_hooks, "_openstack_instance_report_provider", None
        ):
            list_resp = stats_view.openstack_instances(Request(factory.get("/")))
            self.assertEqual(list_resp.status_code, 200)
            self.assertEqual(list_resp.data, [])
            agg_resp = stats_view.openstack_instances_aggregate(
                Request(factory.get("/?group_by=flavor_name"))
            )
            self.assertEqual(agg_resp.status_code, 200)
            self.assertEqual(agg_resp.data, [])

    def test_site_agent_configuration_and_provider(self):
        self.assertTrue(agent_configuration.CATALOGUE_PATH.exists())

        sentinel_resource = object()
        with mock.patch.object(
            site_agent_utils, "push_resource_update_message"
        ) as push_mock:
            marketplace_hooks.push_site_agent_resource_update(sentinel_resource)
            push_mock.assert_called_once_with(sentinel_resource)

        provider = marketplace_hooks.get_site_agent_provider()
        self.assertIsNotNone(provider)
        sentinel_offering = mock.MagicMock(type=marketplace_enums.SITE_AGENT_OFFERING)
        with mock.patch.object(
            config_builder, "offering_agent_config", return_value={"offerings": []}
        ) as config_mock:
            self.assertEqual(
                provider.offering_agent_config(sentinel_offering), {"offerings": []}
            )
            config_mock.assert_called_once_with(sentinel_offering)

        offering_view = marketplace_views.ProviderOfferingViewSet()
        with (
            mock.patch.object(marketplace_hooks, "_site_agent_provider", None),
            mock.patch.object(
                offering_view, "get_object", return_value=sentinel_offering
            ),
            self.assertRaises(rf_exceptions.APIException),
        ):
            offering_view.site_agent_config(mock.MagicMock())

    def test_policy_provider_delegates_and_falls_back(self):
        provider = marketplace_hooks.get_policy_provider()
        self.assertIsNotNone(provider)
        sentinel_usage = object()
        with (
            mock.patch.object(
                policy_handlers, "offering_usage_policy_trigger_handler"
            ) as offering_mock,
            mock.patch.object(
                policy_handlers, "slurm_periodic_usage_policy_trigger_handler"
            ) as slurm_mock,
            mock.patch.object(
                policy_handlers, "customer_component_usage_policy_trigger_handler"
            ) as customer_mock,
        ):
            marketplace_hooks.on_component_usage_billed(sentinel_usage, True)
            offering_mock.assert_called_once()
            slurm_mock.assert_called_once()
            customer_mock.assert_called_once()

        sentinel_resource = object()
        with (
            mock.patch.object(
                policy_rebill_helpers,
                "snapshot_estimated_cost_policies",
                return_value=[],
            ) as snap_mock,
            mock.patch.object(
                policy_rebill_helpers,
                "get_policy_scope_label",
                return_value="Project",
            ) as label_mock,
            mock.patch.object(
                policy_rebill_helpers,
                "preview_policy_action_resources",
                return_value=(True, []),
            ) as preview_mock,
        ):
            self.assertEqual(
                provider.snapshot_estimated_cost_policies(sentinel_resource), []
            )
            snap_mock.assert_called_once_with(sentinel_resource)
            self.assertEqual(
                provider.get_policy_scope_label(sentinel_resource), "Project"
            )
            label_mock.assert_called_once_with(sentinel_resource)
            self.assertEqual(
                provider.preview_policy_action_resources(
                    sentinel_resource, "pause", {}, True
                ),
                (True, []),
            )
            preview_mock.assert_called_once_with(sentinel_resource, "pause", {}, True)

        with mock.patch.object(marketplace_hooks, "_policy_provider", None):
            marketplace_hooks.on_component_usage_billed(sentinel_usage, False)
            marketplace_hooks.schedule_slurm_policy_evaluation(1, "uuid")
            self.assertFalse(marketplace_hooks.has_slurm_periodic_policy(object()))

    def test_proposal_provider_and_customer_extensions(self):
        self.assertIsNotNone(marketplace_hooks.get_proposal_provider())
        self.assertTrue(
            any(
                isinstance(
                    backend, proposal_filters.CustomerCallManagingOrganisationFilter
                )
                for backend in structure_filters.ExternalCustomerFilterBackend._filters
            )
        )
        serializer_receivers = core_signals.pre_serializer_fields._live_receivers(
            structure_serializers.CustomerSerializer
        )[0]
        self.assertIn(
            proposal_serializers.add_call_managing_organization_uuid,
            serializer_receivers,
        )

        sentinel_qs = mock.MagicMock()
        with (
            mock.patch.object(
                proposal_managers,
                "annotate_offerings_open_for_proposals",
                return_value=sentinel_qs,
            ) as annotate_mock,
            mock.patch.object(
                proposal_models.RequestedOffering.objects,
                "offering_ids_in_active_calls",
                return_value=[1, 2],
            ) as active_ids_mock,
            mock.patch.object(
                proposal_models.RequestedOffering.objects,
                "offering_ids_open_for_proposals",
                return_value=[3],
            ) as open_ids_mock,
        ):
            self.assertIs(
                marketplace_hooks.annotate_offerings_open_for_proposals(sentinel_qs),
                sentinel_qs,
            )
            annotate_mock.assert_called_once_with(sentinel_qs)
            self.assertEqual(marketplace_hooks.offering_ids_in_active_calls(), [1, 2])
            active_ids_mock.assert_called_once_with()
            self.assertEqual(marketplace_hooks.offering_ids_open_for_proposals(), [3])
            open_ids_mock.assert_called_once_with()

        with mock.patch.object(marketplace_hooks, "_proposal_provider", None):
            self.assertIs(
                marketplace_hooks.annotate_offerings_open_for_proposals(sentinel_qs),
                sentinel_qs,
            )
            self.assertFalse(marketplace_hooks.is_offering_open_for_proposals(object()))
            self.assertEqual(marketplace_hooks.offering_ids_in_active_calls(), ())
            self.assertEqual(marketplace_hooks.offering_ids_open_for_proposals(), ())
            self.assertIs(
                marketplace_hooks.filter_orders_from_award(sentinel_qs, False),
                sentinel_qs,
            )
            marketplace_hooks.filter_orders_from_award(sentinel_qs, True)
            sentinel_qs.none.assert_called_once_with()
