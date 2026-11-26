from types import SimpleNamespace
from unittest import mock

from django.core.management import get_commands
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from waldur_auth_social import const as auth_social_const
from waldur_auth_social import handlers as auth_social_handlers
from waldur_core.core import auth_hooks, enums, user_attributes
from waldur_core.logging import serializers as logging_serializers
from waldur_core.permissions import pat_filtering
from waldur_core.permissions import utils as permission_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import core_hooks as marketplace_core_hooks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models


class KernelExtensionHooksTest(SimpleTestCase):
    def test_auth_social_reexports_core_constants(self):
        self.assertIs(auth_social_const.ProviderChoices, enums.ProviderChoices)
        self.assertIs(
            auth_social_const.WRITABLE_USER_FIELDS,
            user_attributes.WRITABLE_USER_FIELDS,
        )

    def test_auth_social_registers_core_auth_hooks(self):
        self.assertIn(
            auth_social_handlers.forget_oauth_tokens_on_logout,
            auth_hooks._logout_hooks,
        )
        self.assertIs(
            auth_hooks._oidc_logout_url_provider,
            auth_social_handlers.get_oidc_logout_url,
        )
        self.assertIs(
            auth_hooks._default_idp_provider,
            auth_social_handlers.get_default_idp_info,
        )
        self.assertIn(enums.ProviderChoices.EDUTEAMS, auth_hooks._remote_user_pullers)

    def test_marketplace_and_proposal_register_pat_filters(self):
        expected_models = (
            structure_models.Customer,
            structure_models.Project,
            marketplace_models.Offering,
            marketplace_models.Resource,
            marketplace_models.ResourceProject,
            marketplace_models.ServiceProvider,
            marketplace_models.ResourceApiKey,
            proposal_models.CallManagingOrganisation,
            proposal_models.Call,
            proposal_models.Proposal,
        )
        for model in expected_models:
            self.assertIn(model, pat_filtering._FILTER_BUILDERS)

    def test_marketplace_registers_active_account_guard(self):
        self.assertIn(
            marketplace_core_hooks.COURSE_ACCOUNT_GUARD,
            permission_utils.get_active_account_guards(),
        )

    def test_marketplace_registers_offering_access_validator(self):
        self.assertIs(
            logging_serializers._offering_access_validator,
            marketplace_core_hooks.validate_offering_access_for_event_subscription,
        )

    def test_offering_access_validator_reports_unavailable_when_unregistered(self):
        serializer = logging_serializers.EventSubscriptionQueueCreateSerializer(
            context={"request": SimpleNamespace(user=SimpleNamespace(id=1))}
        )
        with mock.patch.object(logging_serializers, "_offering_access_validator", None):
            with self.assertRaisesMessage(
                ValidationError, "Offering access validation is not available"
            ):
                serializer.validate_offering_uuid(
                    "00000000-0000-0000-0000-000000000000"
                )

    def test_probe_broker_latency_command_is_discoverable(self):
        commands = get_commands()
        self.assertEqual(
            commands.get("probe_broker_latency"), "waldur_mastermind.policy"
        )
