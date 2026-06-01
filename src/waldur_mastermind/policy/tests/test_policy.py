from unittest import mock

from ddt import data, ddt
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import enums, policy_actions, structures
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tasks import check_polices
from waldur_mastermind.policy.tests import factories


@override_settings(task_always_eager=True)
@freeze_time("2024-09-01")
class ActionsFunctionsTest(test.APITestCase):
    def setUp(self):
        self.notify_project_team_mock = mock.MagicMock()
        self.notify_project_team_mock.__name__ = "notify_project_team"

        self.block_creation_of_new_resources_mock = mock.MagicMock()
        self.block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        self.restrict_members_mock = mock.MagicMock()
        self.restrict_members_mock.__name__ = "restrict_members"

        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            month=9,
            year=2024,
            tax_percent=0,
        )

    def tearDown(self):
        mock.patch.stopall()

    def create_or_update_invoice_item(self, unit_price):
        if self.invoice.items.first():
            invoice_item = self.invoice.items.first()
            invoice_item.unit_price = unit_price
            invoice_item.save()
        else:
            invoice_item = invoices_factories.InvoiceItemFactory(
                invoice=self.invoice,
                project=self.project,
                quantity=1,
                unit_price=unit_price,
            )
        return invoice_item

    def test_calling_of_immediate_actions(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.THRESHOLD,
                    method=self.block_creation_of_new_resources_mock,
                ),
            ],
        ):
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost + 2)
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.block_creation_of_new_resources_mock.assert_not_called()
            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()

    def test_calling_of_threshold_actions(self):
        # The pre-flight handler reads policy.actions (CharField) directly and
        # would block creation before the post-save threshold runs. Drop the
        # blocking action from the stored string; the mock-patched
        # get_all_actions still injects it for the post-save check.
        self.policy.actions = "notify_project_team"
        self.policy.save()
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.THRESHOLD,
                    method=self.block_creation_of_new_resources_mock,
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.restrict_members_mock,
                    reset_method=policy_actions.reset_member_restriction,
                ),
            ],
        ):
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)

            self.notify_project_team_mock.reset_mock()
            self.block_creation_of_new_resources_mock.reset_mock()
            self.restrict_members_mock.reset_mock()

            order = marketplace_factories.OrderFactory(
                project=self.project,
                offering=self.fixture.offering,
                attributes={"name": "item_name", "description": "Description"},
                plan=self.fixture.plan,
                state=OrderStates.EXECUTING,
            )
            marketplace_utils.process_order(order, self.fixture.staff)

            self.notify_project_team_mock.assert_not_called()
            self.block_creation_of_new_resources_mock.assert_called()
            self.restrict_members_mock.assert_not_called()

    def test_has_fired(self):
        self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        self.create_or_update_invoice_item(self.policy.limit_cost - 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)
        self.assertTrue(self.policy.fired_datetime)

    def test_policy_idempotency_does_not_refire_when_already_fired(self):
        """Test that immediate actions are not called again when policy is already fired."""
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                ),
            ],
        ):
            # First trigger - should fire
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)
            first_fired_datetime = self.policy.fired_datetime

            self.notify_project_team_mock.reset_mock()

            # Second trigger with higher cost - should NOT fire again
            self.create_or_update_invoice_item(self.policy.limit_cost + 100)
            self.notify_project_team_mock.assert_not_called()
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)
            # fired_datetime should remain the same
            self.assertEqual(self.policy.fired_datetime, first_fired_datetime)

    def test_reset_methods_called_when_policy_reset(self):
        """Test that reset methods are called when policy goes from fired to not fired."""
        reset_mock = mock.MagicMock()
        reset_mock.__name__ = "reset_action"

        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                    reset_method=reset_mock,
                ),
            ],
        ):
            # First trigger - should fire
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)
            reset_mock.assert_not_called()

            self.notify_project_team_mock.reset_mock()

            # Now bring cost below limit - should reset and call reset_method
            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.policy.refresh_from_db()
            self.assertFalse(self.policy.has_fired)
            reset_mock.assert_called_once_with(self.policy)

    def test_reset_methods_not_called_for_actions_without_reset_method(self):
        """Test that actions without reset_method don't cause errors on reset."""
        reset_mock = mock.MagicMock()
        reset_mock.__name__ = "reset_action"

        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                    reset_method=None,  # No reset method
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.THRESHOLD,
                    method=self.block_creation_of_new_resources_mock,
                    reset_method=reset_mock,  # Has reset method
                ),
            ],
        ):
            # Trigger policy
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)

            # Reset policy - only actions with reset_method should be called
            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.policy.refresh_from_db()
            self.assertFalse(self.policy.has_fired)
            reset_mock.assert_called_once_with(self.policy)

    def test_policy_can_refire_after_reset(self):
        """Test that policy can fire again after being reset."""
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                ),
            ],
        ):
            # First trigger
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)

            self.notify_project_team_mock.reset_mock()

            # Reset
            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.policy.refresh_from_db()
            self.assertFalse(self.policy.has_fired)
            self.notify_project_team_mock.assert_not_called()

            # Re-trigger - should fire again
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.notify_project_team_mock.assert_called_once()
            self.policy.refresh_from_db()
            self.assertTrue(self.policy.has_fired)

    def test_reset_not_called_when_policy_was_not_fired(self):
        """Test that reset methods are not called when policy was never fired."""
        reset_mock = mock.MagicMock()
        reset_mock.__name__ = "reset_action"

        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                    reset_method=reset_mock,
                ),
            ],
        ):
            # Cost is already below limit, policy never fired
            self.create_or_update_invoice_item(self.policy.limit_cost - 1)
            self.policy.refresh_from_db()
            self.assertFalse(self.policy.has_fired)

            # Update cost but still below limit - reset should not be called
            self.create_or_update_invoice_item(self.policy.limit_cost - 2)
            self.policy.refresh_from_db()
            self.assertFalse(self.policy.has_fired)
            reset_mock.assert_not_called()

    def test_compensation(self):
        self.create_or_update_invoice_item(self.policy.limit_cost - 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        invoice_item = self.invoice.items.first()
        invoice_item.resource = self.fixture.resource
        invoice_item.save()

        invoices_factories.CustomerCreditFactory(
            value=invoice_item.total * 3, customer=self.invoice.customer
        )
        invoices_factories.ProjectCreditFactory(
            value=invoice_item.total * 2, project=self.project
        )
        self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

    def test_several_policies(self):
        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=self.notify_project_team_mock,
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.THRESHOLD,
                    method=self.block_creation_of_new_resources_mock,
                ),
            ],
        ):
            policy_2 = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
            self.create_or_update_invoice_item(self.policy.limit_cost + 1)
            self.policy.refresh_from_db()
            policy_2.refresh_from_db()
            self.assertEqual(self.policy.has_fired, True)
            self.assertEqual(policy_2.has_fired, True)

    def test_policy_period(self):
        # period = 1 month
        invoice_item = self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 7
        invoice_item.invoice.save()
        invoice_item.save()  # for running of a handler
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        self.client.force_authenticate(self.fixture.staff)

        # period = 3 month
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.MONTH_3})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 10
        invoice_item.invoice.year = 2023
        invoice_item.invoice.save()
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = 12 month
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.MONTH_12})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        invoice_item.invoice.month = 9
        invoice_item.invoice.year = 2023
        invoice_item.invoice.save()
        invoice_item.save()
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, False)

        # period = Total
        self.client.patch(url, {"period": ProjectEstimatedCostPolicy.Periods.TOTAL})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

    def test_check_polices_task(self):
        self.create_or_update_invoice_item(self.policy.limit_cost + 1)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.has_fired, True)

        with freeze_time("2024-10-01"):
            check_polices()
            self.policy.refresh_from_db()
            self.assertEqual(self.policy.has_fired, False)


@ddt
class GetPolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    @data("staff", "owner", "customer_support", "admin", "manager")
    def test_user_can_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data("user", "offering_owner")
    def test_user_can_not_get_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class CreatePolicyTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def _create_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,block_modification_of_existing_resources",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
        }
        return self.client.post(self.url, payload)

    @data("staff", "owner")
    def test_user_can_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.has_fired, False)

    @data("admin", "manager", "user", "offering_owner")
    def test_user_can_not_create_policy(self, user):
        response = self._create_policy(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_actions(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,non_existent_method",
            "project": structure_factories.ProjectFactory.get_url(self.project),
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_some_policies_for_one_project(self):
        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self._create_policy("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @freeze_time("2024-09-01")
    def test_policies_should_be_triggered_after_creation_if_cost_limit_has_been_reached(
        self,
    ):
        notify_project_team_mock = mock.MagicMock()
        notify_project_team_mock.__name__ = "notify_project_team"

        block_creation_of_new_resources_mock = mock.MagicMock()
        block_creation_of_new_resources_mock.__name__ = (
            "block_creation_of_new_resources"
        )

        invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            month=9,
            year=2024,
            tax_percent=0,
        )

        with mock.patch.object(
            ProjectEstimatedCostPolicy,
            "get_all_actions",
            return_value=[
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.IMMEDIATE,
                    method=notify_project_team_mock,
                ),
                structures.PolicyAction(
                    action_type=enums.PolicyActionTypes.THRESHOLD,
                    method=block_creation_of_new_resources_mock,
                ),
            ],
        ):
            invoices_factories.InvoiceItemFactory(
                invoice=invoice, project=self.project, quantity=1, unit_price=1000
            )
            response = self._create_policy("staff")
            policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
            notify_project_team_mock.assert_called_once()
            block_creation_of_new_resources_mock.assert_not_called()
            self.assertEqual(policy.has_fired, True)

    def test_validate_options(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_organization_owners,block_modification_of_existing_resources",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
            "options": {"notify_external_user": "test@example.com, user@domain.org"},
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])

        payload = {
            "options": {"notify_external_user": "invalid-email, user@domain.org"}
        }
        url = factories.ProjectEstimatedCostPolicyFactory.get_url(policy)
        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_options_accepts_json_string(self):
        """Test that options field can accept JSON string format"""
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_external_user",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
            "options": '{"notify_external_user": "test@example.com"}',  # JSON string format
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        policy = ProjectEstimatedCostPolicy.objects.get(uuid=response.data["uuid"])
        self.assertEqual(policy.options["notify_external_user"], "test@example.com")

    def test_options_rejects_invalid_json_string(self):
        """Test that invalid JSON string in options field is rejected"""
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "limit_cost": 100,
            "actions": "notify_external_user",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
            "options": '{"notify_external_user": "test@example.com"',  # Invalid JSON (missing closing brace)
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Options must be a valid JSON object", str(response.data))


@ddt
class DeletePolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _delete_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.delete(self.url)

    @data("staff", "owner")
    def test_user_can_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user", "offering_owner")
    def test_user_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("admin", "manager")
    def test_project_member_can_not_delete_policy(self, user):
        response = self._delete_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class UpdatePolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)

    def _update_policy(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"actions": "notify_organization_owners"})

    @data("staff", "owner")
    def test_user_can_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user", "offering_owner")
    def test_user_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("admin", "manager")
    def test_project_member_can_not_update_policy(self, user):
        response = self._update_policy(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProjectCostPolicyQueryFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.name = "Alpha Research Lab"
        self.project.save()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(scope=self.project)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def test_query_filters_by_project_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Alpha"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["scope_name"], "Alpha Research Lab")

    def test_query_excludes_non_matching(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Nonexistent"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_query_is_case_insensitive(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "alpha"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@override_settings(task_always_eager=True)
@freeze_time("2024-09-01")
class AffectedResourcesCountTest(test.APITestCase):
    """WAL-9808: Verify affected_resources_count in cost policy API response."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.policy = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture.project,
            actions="request_pausing",
            limit_cost=10,
        )
        self.resource = self.fixture.resource
        self.resource.offering.plugin_options = {"supports_pausing": True}
        self.resource.offering.save()
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def test_count_is_zero_when_policy_not_fired(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["affected_resources_count"], 0)

    def test_count_reflects_paused_resources(self):
        # Fire the policy and pause the resource
        policy_actions.request_pausing(self.policy)
        self.policy.has_fired = True
        self.policy.save()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["affected_resources_count"], 1)

    def test_count_excludes_resources_without_offering_support(self):
        # Pause resource but disable offering support
        self.resource.paused = True
        self.resource.save()
        self.resource.offering.plugin_options = {"supports_pausing": False}
        self.resource.offering.save()
        self.policy.has_fired = True
        self.policy.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["affected_resources_count"], 0)

    def test_count_filtered_by_user_permissions(self):
        """affected_resources_count applies user permission filtering.

        The count uses filter_queryset_for_user for non-staff users to prevent
        information disclosure. In practice, users who can see a policy already
        have access to its scope resources (enforced by GenericRoleFilter on
        the policy viewset), so this is a defense-in-depth measure.
        """
        # Fire the policy and pause the resource
        policy_actions.request_pausing(self.policy)
        self.policy.has_fired = True
        self.policy.save()

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

        # Owner has access to the project and can see the policy + count
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["affected_resources_count"], 1)

        # A user with no role cannot even see the policy
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
