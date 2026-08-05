"""
Tests for the outbound half of the plugin.

`RemoteOpenPortalBackend` is what a Waldur that *awards* allocations uses to
push them to a remote portal, as opposed to `OpenPortalBackend`, which receives
them. The two halves are exercised by different offering types
(`Marketplace.OpenPortalRemote` and `Marketplace.OpenPortal`), so an end-to-end
run of one says nothing about the other.

The behaviour pinned here is mostly about *what survives a failure*. The remote
portal is eventually consistent: an allocation that is not fully recorded
locally is retried on the next sync, so every failure path has to leave the
allocation in a state that makes the retry happen — and, just as importantly,
must not mark work as done that was not done.
"""

import datetime
import json
from unittest import mock

import openportal
from django.test import TestCase

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openportal import exceptions, models
from waldur_openportal.remotebackend import RemoteOpenPortalBackend

DESTINATION = "someportal.somebridge.someoffering"
PROJECT = "someportal.someproject"


def award_details(**fields) -> openportal.AwardDetails:
    return openportal.AwardDetails(json.dumps(fields))


class RemoteBackendFixture(TestCase):
    """
    Scaffolding shared by the cases below; holds no tests of its own so that
    subclasses do not re-run each other's.

    Both collaborators are replaced: `client` is the bridge to the remote
    portal, and `remote_project_service` is the local audit trail. Keeping them
    apart matters, because most of what is asserted here is the ordering
    between them — the local record is only written after the remote call has
    actually succeeded.
    """

    def setUp(self):
        self.backend = RemoteOpenPortalBackend(mock.Mock())
        self.client = mock.Mock()
        self.client.destination.return_value = DESTINATION
        self.backend._client = self.client

        service_patcher = mock.patch(
            "waldur_openportal.remotebackend.remote_project_service"
        )
        self.service = service_patcher.start()
        self.addCleanup(service_patcher.stop)

        self.remote_project = mock.Mock()
        self.remote_project.award_details.return_value = award_details()
        self.service.get_or_create_remote_project.return_value = self.remote_project

    def make_allocation(self, details=None, added=True, state=CoreStates.OK):
        allocation = mock.Mock(spec=models.RemoteAllocation)
        allocation.state = state
        allocation.is_added = added
        allocation.node_limit = 100
        allocation.node_usage = 0
        allocation.has_project_identifier.return_value = True
        allocation.get_project_identifier.return_value = PROJECT
        allocation.has_remote_project_identifier.return_value = added
        allocation.get_remote_project_identifier.return_value = PROJECT
        allocation.is_added_to_openportal.return_value = added
        allocation.needs_updating.return_value = True
        allocation.get_version.return_value = 1
        allocation.increment_version.return_value = 2
        allocation.get_project_details.return_value = (
            details if details is not None else award_details(project_id=PROJECT)
        )
        return allocation


class AddAllocatedProjectTest(RemoteBackendFixture):
    """
    `is_added` is the flag that tells every later sync "this allocation is
    already on the remote portal, leave it alone". Setting it when the work did
    not finish strands the allocation; leaving it unset after success makes the
    portal re-add the same project forever. Each case below is one way that
    balance can be got wrong.
    """

    def setUp(self):
        super().setUp()
        # Covered separately by AssertCanAddAllocationTest; stubbed here so
        # these cases are about what happens after the guard has passed.
        self.backend.assert_can_add_allocation = mock.Mock()
        self.client.get_award.return_value = award_details(project_id=PROJECT)

    def test_is_added_is_set_once_the_award_is_recorded(self):
        allocation = self.make_allocation(added=False)

        self.backend._add_allocated_project(allocation)

        self.service.record_award_created.assert_called_once()
        self.assertTrue(allocation.is_added)

    def test_is_added_stays_false_when_the_award_cannot_be_recorded(self):
        """
        The remote call succeeded but the local record did not. Marking the
        allocation as added here would leave Waldur believing in an award it
        has no record of, and no later sync would revisit it.
        """
        allocation = self.make_allocation(added=False)
        self.service.record_award_created.side_effect = RuntimeError("db is down")

        self.backend._add_allocated_project(allocation)

        self.assertFalse(allocation.is_added)

    def test_a_failed_add_is_left_pending_for_the_next_sync(self):
        allocation = self.make_allocation(added=False)
        self.client.add_project.side_effect = RuntimeError("portal unreachable")

        self.backend._add_allocated_project(allocation)

        self.remote_project.record_pending.assert_called_once()
        self.service.record_award_created.assert_not_called()
        self.assertFalse(allocation.is_added)
        allocation.set_erred.assert_not_called()

    def test_a_rejected_award_is_recorded_rather_than_erred(self):
        """
        Rejection is an answer, not a fault: the remote portal has decided.
        The allocation must not be erred, because that is the state reserved
        for "we could not find out".
        """
        allocation = self.make_allocation(added=False)
        self.client.add_project.side_effect = exceptions.ManagedProjectRejectedError(
            "not eligible"
        )

        self.backend._add_allocated_project(allocation)

        self.remote_project.record_rejected.assert_called_once()
        allocation.set_erred.assert_not_called()

    def test_a_rejection_that_cannot_be_recorded_falls_back_to_erred(self):
        allocation = self.make_allocation(added=False)
        self.client.add_project.side_effect = exceptions.ManagedProjectRejectedError(
            "not eligible"
        )
        self.remote_project.record_rejected.side_effect = RuntimeError("db is down")

        self.backend._add_allocated_project(allocation)

        allocation.set_erred.assert_called_once()

    def test_an_older_portal_without_get_award_falls_back_to_the_sent_details(self):
        """
        Regression test for a fallback that could never run: the handler named
        the exception on the `openportal` SDK, which does not define it, so
        reaching this path raised AttributeError while handling the original
        error. The exception now lives in the plugin's own `exceptions` module.
        """
        allocation = self.make_allocation(added=False)
        sent = award_details(project_id=PROJECT, members={"a@example.com": "user"})
        allocation.get_project_details.return_value = sent
        self.client.get_award.side_effect = (
            exceptions.OpenPortalUnsupportedCommandError("unknown command: get_award")
        )

        self.backend._add_allocated_project(allocation)

        self.assertTrue(allocation.is_added)
        _, _, confirmed, _ = self.service.record_award_created.call_args[0]
        self.assertEqual(confirmed, json.loads(sent.to_json()))

    def test_any_other_get_award_failure_is_not_swallowed(self):
        """
        Only the older-portal case may fall back. Treating an arbitrary failure
        as confirmation would record details the remote portal never agreed to.
        """
        allocation = self.make_allocation(added=False)
        self.client.get_award.side_effect = RuntimeError("portal unreachable")

        with self.assertRaises(RuntimeError):
            self.backend._add_allocated_project(allocation)

        self.assertFalse(allocation.is_added)
        self.service.record_award_created.assert_not_called()

    def test_a_brand_new_project_takes_its_identifier_from_the_client(self):
        allocation = self.make_allocation(added=False)
        allocation.has_project_identifier.return_value = False
        self.client.get_project_identifier.return_value = PROJECT

        self.backend._add_allocated_project(allocation)

        allocation.set_mapping.assert_called_once()
        self.assertEqual(allocation.state, CoreStates.OK)
        self.assertTrue(allocation.is_added)


class UpdateAllocatedProjectTest(RemoteBackendFixture):
    def setUp(self):
        super().setUp()
        self.client.get_award.return_value = award_details(project_id=PROJECT)

    def test_an_allocation_in_a_terminal_state_is_refused(self):
        allocation = self.make_allocation(state=CoreStates.ERRED)

        with self.assertRaises(ServiceBackendError):
            self.backend.update_allocated_project(allocation)

        self.client.update_project.assert_not_called()

    def test_an_allocation_missing_from_the_portal_is_added_instead(self):
        allocation = self.make_allocation(added=False)
        self.backend.add_allocated_project = mock.Mock()

        self.backend.update_allocated_project(allocation)

        self.backend.add_allocated_project.assert_called_once_with(allocation)
        self.client.update_project.assert_not_called()

    def test_nothing_is_sent_when_the_allocation_is_already_current(self):
        allocation = self.make_allocation()
        allocation.needs_updating.return_value = False

        self.backend.update_allocated_project(allocation)

        self.client.update_project.assert_not_called()

    def test_an_absent_member_list_is_not_filled_in_from_history(self):
        """
        `members: None` means "this award does not speak about membership",
        which is different from "this award has no members". The stored award
        details are merged in to preserve fields Waldur does not own, and that
        merge would otherwise resurrect the previous member list and re-assert
        it as the current one.
        """
        allocation = self.make_allocation(details=award_details(project_id=PROJECT))
        self.remote_project.award_details.return_value = award_details(
            members={"old@example.com": "user"}
        )

        self.backend.update_allocated_project(allocation)

        sent = self.client.update_project.call_args[0][1]
        self.assertIsNone(sent.members)

    def test_a_member_list_that_is_present_is_sent_as_given(self):
        members = {"new@example.com": "user"}
        allocation = self.make_allocation(
            details=award_details(project_id=PROJECT, members=members)
        )
        self.remote_project.award_details.return_value = award_details(
            members={"old@example.com": "user"}
        )

        self.backend.update_allocated_project(allocation)

        sent = self.client.update_project.call_args[0][1]
        self.assertEqual(sent.members, members)

    def test_a_successful_update_is_confirmed_without_waiting_for_a_notification(self):
        allocation = self.make_allocation()

        self.backend.update_allocated_project(allocation)

        allocation.successfully_updated.assert_called_once_with(2)
        allocation.update_mapping.assert_called_once()
        self.service.record_award_update_confirmed.assert_called_once()

    def test_an_older_portal_without_get_award_confirms_with_the_sent_details(self):
        """
        The sibling of the add-path regression: the same non-existent SDK
        exception was named here, so this fallback could not run either.
        """
        allocation = self.make_allocation()
        self.client.get_award.side_effect = (
            exceptions.OpenPortalUnsupportedCommandError("unknown command: get_award")
        )

        self.backend.update_allocated_project(allocation)

        allocation.successfully_updated.assert_called_once_with(2)
        sent, confirmed = self.service.record_award_update_confirmed.call_args[0][1:3]
        self.assertEqual(confirmed, sent)

    def test_a_rejected_update_is_recorded_rather_than_erred(self):
        allocation = self.make_allocation()
        self.client.update_project.side_effect = exceptions.ManagedProjectRejectedError(
            "no longer eligible"
        )

        self.backend.update_allocated_project(allocation)

        self.remote_project.record_rejected.assert_called_once()
        allocation.set_erred.assert_not_called()

    def test_a_failed_update_stays_updating_so_the_next_sync_retries(self):
        allocation = self.make_allocation()
        self.client.update_project.side_effect = RuntimeError("portal unreachable")

        self.backend.update_allocated_project(allocation)

        self.assertEqual(allocation.state, CoreStates.UPDATING)
        allocation.successfully_updated.assert_not_called()


class CheckAddedAllocationTest(RemoteBackendFixture):
    def setUp(self):
        super().setUp()
        self.backend._add_allocated_project = mock.Mock()

    def test_an_allocation_already_on_the_portal_is_left_alone(self):
        allocation = self.make_allocation()

        self.assertIs(self.backend.check_added_allocation(allocation), allocation)
        self.backend._add_allocated_project.assert_not_called()

    def test_a_deleted_allocation_is_not_re_added(self):
        """
        Without the state guard a terminated allocation would be re-created on
        the remote portal by the next sync, and then terminated again, forever.
        """
        allocation = self.make_allocation(added=False, state=CoreStates.ERRED)

        with self.assertRaises(ServiceBackendError):
            self.backend.check_added_allocation(allocation)

        self.backend._add_allocated_project.assert_not_called()

    def test_an_incomplete_add_is_reported_as_a_failure(self):
        allocation = self.make_allocation(added=False)
        self.backend._add_allocated_project.return_value = allocation

        with self.assertRaises(ServiceBackendError):
            self.backend.check_added_allocation(allocation)


class AssertCanAddAllocationTest(RemoteBackendFixture):
    """
    The guard that runs before anything is sent. It enforces one allocation per
    project per destination, and an optional per-project allow-list of
    destinations.
    """

    def setUp(self):
        super().setUp()
        self.backend.get_allocation_queryset = mock.Mock()
        self.backend.get_allocation_queryset.return_value.filter.return_value = []

        info_patcher = mock.patch(
            "waldur_openportal.remotebackend.models.ProjectInfo.objects"
        )
        self.project_infos = info_patcher.start()
        self.addCleanup(info_patcher.stop)
        self.project_info = mock.Mock(allowed_destinations=None)
        self.project_infos.get_or_create.return_value = (self.project_info, True)

    def allow(self, destinations):
        self.project_info.allowed_destinations = destinations

    def test_a_project_may_hold_only_one_allocation_per_destination(self):
        allocation = self.make_allocation()
        existing = self.make_allocation()
        existing.state = CoreStates.OK
        self.backend.get_allocation_queryset.return_value.filter.return_value = [
            existing
        ]

        with self.assertRaises(ServiceBackendError):
            self.backend.assert_can_add_allocation(allocation)

        allocation.set_erred.assert_called_once()

    def test_an_erred_allocation_does_not_block_a_replacement(self):
        allocation = self.make_allocation()
        erred = self.make_allocation()
        erred.state = CoreStates.ERRED
        self.backend.get_allocation_queryset.return_value.filter.return_value = [erred]

        self.backend.assert_can_add_allocation(allocation)

    def test_no_allow_list_permits_any_destination(self):
        self.allow(None)
        self.backend.assert_can_add_allocation(self.make_allocation())

        self.allow("   ")
        self.backend.assert_can_add_allocation(self.make_allocation())

    def test_an_exact_entry_permits_that_destination(self):
        self.allow(f"someportal.other, {DESTINATION}")
        self.backend.assert_can_add_allocation(self.make_allocation())

    def test_a_wildcard_entry_permits_any_destination(self):
        self.allow("*")
        self.backend.assert_can_add_allocation(self.make_allocation())

    def test_an_entry_is_treated_as_a_regular_expression(self):
        self.allow(r"someportal\..*")
        self.backend.assert_can_add_allocation(self.make_allocation())

    def test_a_destination_outside_the_allow_list_is_refused(self):
        self.allow("otherportal.otherbridge.otheroffering")

        with self.assertRaises(ServiceBackendError):
            self.backend.assert_can_add_allocation(self.make_allocation())

    def test_allow_list_entries_match_a_prefix_rather_than_the_whole_name(self):
        """
        Pinned because it is easy to read the allow-list as a list of names.
        Entries are regular expressions applied with `re.match`, which anchors
        at the start only, so an entry naming one destination also admits every
        destination that extends it. Narrow an entry with an explicit `$` if
        that is not wanted.
        """
        self.allow("someportal.somebridge.some")

        self.backend.assert_can_add_allocation(self.make_allocation())

        self.allow("someportal.somebridge.some$")
        with self.assertRaises(ServiceBackendError):
            self.backend.assert_can_add_allocation(self.make_allocation())


class SyncUsersTest(RemoteBackendFixture):
    def setUp(self):
        super().setUp()
        self.backend.update_allocated_project = mock.Mock()

        associations_patcher = mock.patch(
            "waldur_openportal.remotebackend.models.RemoteAssociation.objects"
        )
        self.associations = associations_patcher.start()
        self.addCleanup(associations_patcher.stop)
        self.associations.filter.return_value = []

        # The signals are intercepted rather than fired: their real receivers
        # provision marketplace offering users against the database, which is
        # a separate concern from whether this method emits them at all.
        signals_patcher = mock.patch("waldur_openportal.remotebackend.signals")
        self.signals = signals_patcher.start()
        self.addCleanup(signals_patcher.stop)

    @property
    def created(self):
        return [
            call.kwargs["user"]
            for call in self.signals.openportal_remote_association_created.send.call_args_list
        ]

    @property
    def deleted(self):
        return [
            call.kwargs["user"]
            for call in self.signals.openportal_remote_association_deleted.send.call_args_list
        ]

    def user(self, pk, is_active=True):
        return mock.Mock(id=pk, is_active=is_active)

    def test_only_active_users_are_associated(self):
        allocation = self.make_allocation()
        active, inactive = self.user(1), self.user(2, is_active=False)
        allocation.project.get_users.return_value = [active, inactive]

        self.backend.sync_users(allocation)

        self.assertEqual(self.created, [active])
        self.associations.create.assert_called_once_with(
            user=active, allocation=allocation
        )

    def test_a_user_who_left_the_project_loses_their_association(self):
        allocation = self.make_allocation()
        gone = self.user(2)
        allocation.project.get_users.return_value = [self.user(1)]
        stale = mock.Mock(user_id=2, user=gone)
        self.associations.filter.return_value = [stale]

        self.backend.sync_users(allocation)

        self.assertEqual(self.deleted, [gone])
        stale.delete.assert_called_once()

    def test_an_existing_association_is_not_recreated(self):
        allocation = self.make_allocation()
        known = self.user(1)
        allocation.project.get_users.return_value = [known]
        self.associations.filter.return_value = [mock.Mock(user_id=1, user=known)]

        self.backend.sync_users(allocation)

        self.associations.create.assert_not_called()
        self.assertEqual(self.created, [])
        self.assertEqual(self.deleted, [])

    def test_the_membership_change_is_pushed_to_the_portal(self):
        """
        Local associations are only bookkeeping. The remote portal applies
        membership itself, from the award, so the sync is not finished until
        the award has been sent.
        """
        allocation = self.make_allocation()
        allocation.project.get_users.return_value = [self.user(1)]

        self.backend.sync_users(allocation)

        self.backend.update_allocated_project.assert_called_once_with(
            allocation, force_update=True
        )


class DeleteAllocationTest(RemoteBackendFixture):
    def test_a_deleted_allocation_is_forgotten_locally(self):
        allocation = self.make_allocation()

        self.backend.delete_allocation(allocation)

        self.client.delete_project.assert_called_once_with(PROJECT)
        self.assertIsNone(allocation.remote_project_identifier)
        self.assertFalse(allocation.is_added)
        self.service.record_resource_deleted.assert_called_once()

    def test_a_failed_delete_leaves_the_allocation_intact(self):
        """
        The project is still on the remote portal, so clearing the identifier
        would lose the only handle Waldur has for deleting it later.
        """
        allocation = self.make_allocation()
        self.client.delete_project.side_effect = RuntimeError("portal unreachable")

        self.backend.delete_allocation(allocation)

        self.assertTrue(allocation.is_added)
        self.service.record_resource_deleted.assert_not_called()

    def test_an_allocation_that_was_never_added_is_still_deleted(self):
        allocation = self.make_allocation()
        allocation.get_project_identifier.side_effect = RuntimeError("no identifier")
        self.client.get_project_identifier.return_value = PROJECT

        self.backend.delete_allocation(allocation)

        self.client.delete_project.assert_called_once_with(PROJECT)


class UsageReportTest(RemoteBackendFixture):
    """
    Usage is written back into the allocation, which is what invoices are
    raised from, so a report that cannot be trusted must not be applied.
    """

    def report(self, dates, hours, is_complete=False):
        return mock.Mock(
            dates=dates,
            is_complete=is_complete,
            total_usage=mock.Mock(hours=hours),
        )

    def test_an_empty_report_changes_nothing(self):
        allocation = self.make_allocation()

        self.backend._update_usage_from_report(allocation, self.report([], 0))

        allocation.save.assert_not_called()

    def test_a_report_spanning_two_months_is_refused(self):
        """
        The value is applied as the total for the month it belongs to, so a
        report covering two months has no single month to be applied to.
        """
        allocation = self.make_allocation()
        report = self.report(
            [datetime.date(2026, 1, 31), datetime.date(2026, 2, 1)], 10
        )

        self.backend._update_usage_from_report(allocation, report)

        allocation.save.assert_not_called()

    def test_a_change_below_the_recorded_precision_is_ignored(self):
        allocation = self.make_allocation()
        allocation.node_usage = 10.0
        report = self.report([datetime.date(2026, 1, 15)], 10.005)

        self.backend._update_usage_from_report(allocation, report)

        allocation.save.assert_not_called()

    def test_a_significant_change_is_applied(self):
        allocation = self.make_allocation()
        allocation.node_usage = 10.0
        report = self.report([datetime.date(2026, 1, 15)], 12.0)

        self.backend._update_usage_from_report(allocation, report)

        self.assertEqual(allocation.node_usage, 12.0)
        allocation.save.assert_called_once_with(update_fields=["node_usage"])

    def test_a_complete_report_is_applied_however_small_the_change(self):
        """
        A complete report is the final word on a closed month, so it is applied
        even when the difference is below the threshold that filters noise out
        of in-progress months.
        """
        allocation = self.make_allocation()
        allocation.node_usage = 10.0
        report = self.report([datetime.date(2026, 1, 15)], 10.001, is_complete=True)

        self.backend._update_usage_from_report(allocation, report)

        self.assertEqual(allocation.node_usage, 10.001)

    def test_a_past_month_does_not_overwrite_the_current_usage(self):
        allocation = self.make_allocation()
        allocation.node_usage = 10.0
        report = self.report([datetime.date(2026, 1, 15)], 99.0, is_complete=True)

        self.backend._update_usage_from_report(allocation, report, update_current=False)

        self.assertEqual(allocation.node_usage, 10.0)
        allocation.save.assert_not_called()


class ResourceLimitsTest(RemoteBackendFixture):
    def setUp(self):
        super().setUp()
        self.backend.check_added_allocation = mock.Mock(side_effect=lambda a: a)

    def test_the_node_limit_is_sent_as_hours(self):
        allocation = self.make_allocation()
        allocation.node_limit = 100
        self.client.set_resource_limits.return_value = openportal.Usage.from_hours(100)

        self.backend.set_resource_limits(allocation)

        project, limit = self.client.set_resource_limits.call_args[0]
        self.assertEqual(project, PROJECT)
        self.assertEqual(limit.hours, 100)

    def test_nothing_is_sent_for_an_allocation_with_no_identifier(self):
        allocation = self.make_allocation()
        allocation.has_project_identifier.return_value = False

        self.backend.set_resource_limits(allocation)

        self.client.set_resource_limits.assert_not_called()

    def test_a_limit_the_portal_would_not_accept_does_not_raise(self):
        """
        The portal is entitled to clamp the limit. Waldur logs the discrepancy
        and carries on, rather than erring an otherwise healthy allocation.
        """
        allocation = self.make_allocation()
        allocation.node_limit = 100
        self.client.set_resource_limits.return_value = openportal.Usage.from_hours(50)

        self.backend.set_resource_limits(allocation)


class PingTest(RemoteBackendFixture):
    def test_a_healthy_portal_answers_true(self):
        self.assertTrue(self.backend.ping())

    def test_an_unreachable_portal_answers_false(self):
        self.client.health.side_effect = exceptions.OpenPortalError("no route")

        self.assertFalse(self.backend.ping())

    def test_an_unreachable_portal_can_raise_instead(self):
        self.client.health.side_effect = exceptions.OpenPortalError("no route")

        with self.assertRaises(ServiceBackendError):
            self.backend.ping(raise_exception=True)


class ReconcileHistoricalUsageTest(RemoteBackendFixture):
    """
    Reconciliation rewrites what a closed month was billed, so these cases run
    against real marketplace rows rather than mocks. What is being pinned is
    which `ResourcePlanPeriod` a `ComponentUsage` ends up attached to, and that
    only means anything with the database's uniqueness constraints in play:
    `unique_with_optional` covers (resource, component, plan_period,
    billing_period), so two rows for one month are permitted as long as they
    name different plan periods.
    """

    MONTH = datetime.date(2026, 1, 1)

    def setUp(self):
        super().setUp()
        self.resource = marketplace_factories.ResourceFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.resource.offering, type="node", name="Node"
        )
        self.backend._adjust_project_credits_for_reconciliation = mock.Mock()

        resource_patcher = mock.patch(
            "waldur_openportal.remotebackend.marketplace_models.Resource.objects.get",
            return_value=self.resource,
        )
        resource_patcher.start()
        self.addCleanup(resource_patcher.stop)

    def plan_period(self, start, end=None):
        return marketplace_factories.ResourcePlanPeriodFactory(
            resource=self.resource,
            plan=self.resource.plan,
            start=start,
            end=end,
        )

    def reconcile(self, hours, is_complete=True):
        report = mock.Mock(is_complete=is_complete, node_usage=hours)
        self.backend._reconcile_historical_usage(
            self.make_allocation(), report, self.MONTH
        )

    def usages(self):
        return marketplace_models.ComponentUsage.objects.filter(resource=self.resource)

    def test_an_incomplete_month_is_not_reconciled(self):
        """
        A month still in progress will be reported again, so correcting its
        billing now would only have to be undone.
        """
        self.plan_period(start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC))

        self.reconcile(50.0, is_complete=False)

        self.assertEqual(self.usages().count(), 0)

    def test_a_month_with_no_usage_is_not_reconciled(self):
        self.plan_period(start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC))

        self.reconcile(0.0)

        self.assertEqual(self.usages().count(), 0)

    def test_a_month_with_no_plan_period_is_skipped(self):
        self.reconcile(50.0)

        self.assertEqual(self.usages().count(), 0)
        self.backend._adjust_project_credits_for_reconciliation.assert_not_called()

    def test_usage_that_was_never_billed_is_recorded_against_that_month(self):
        period = self.plan_period(
            start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC)
        )

        self.reconcile(50.0)

        usage = self.usages().get()
        self.assertEqual(float(usage.usage), 50.0)
        self.assertEqual(usage.billing_period, self.MONTH)
        self.assertEqual(usage.plan_period, period)
        self.backend._adjust_project_credits_for_reconciliation.assert_called_once_with(
            self.resource, self.MONTH, 50.0
        )

    def test_usage_billed_under_a_since_closed_plan_is_corrected_in_place(self):
        """
        The month being reconciled was billed under whichever plan was in force
        at the time. If the plan has changed since, that period is closed and a
        newer one is open — but the usage for the closed month still belongs to
        the old one.

        Resolving the plan period as "whichever is open now" made the lookup for
        the already-billed usage miss, so a *second* row was written for the
        same month under the current period: the month was billed twice, and
        credits were adjusted by the whole usage rather than by the correction.
        """
        billed_under = self.plan_period(
            start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC),
            end=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
        )
        self.plan_period(start=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC))
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            plan_period=billed_under,
            billing_period=core_utils.month_start(self.MONTH),
            usage=40.0,
        )

        self.reconcile(50.0)

        usage = self.usages().get()
        self.assertEqual(usage.plan_period, billed_under)
        self.assertEqual(float(usage.usage), 50.0)
        self.backend._adjust_project_credits_for_reconciliation.assert_called_once_with(
            self.resource, self.MONTH, 10.0
        )

    def test_usage_matching_the_bill_is_left_alone(self):
        period = self.plan_period(
            start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC)
        )
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            plan_period=period,
            billing_period=core_utils.month_start(self.MONTH),
            usage=50.0,
        )

        self.reconcile(50.0)

        self.assertEqual(float(self.usages().get().usage), 50.0)
        self.backend._adjust_project_credits_for_reconciliation.assert_not_called()

    def test_an_offering_without_a_node_component_is_skipped(self):
        self.component.delete()
        self.plan_period(start=datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC))

        self.reconcile(50.0)

        self.assertEqual(self.usages().count(), 0)


class SyncUsageTest(RemoteBackendFixture):
    """
    Usage is collected a month at a time, for last month as well as this one,
    so that nothing is lost across a month boundary. The distinction that runs
    through all of it is complete versus incomplete: a complete month is never
    fetched again, so marking one complete too early freezes whatever partial
    figure happened to be current.
    """

    def setUp(self):
        super().setUp()
        self.backend._reconcile_historical_usage = mock.Mock()

        historical_patcher = mock.patch(
            "waldur_openportal.remotebackend.models.HistoricalRemoteAllocation.objects"
        )
        self.historical = historical_patcher.start()
        self.addCleanup(historical_patcher.stop)
        self.reports = [self.historical_report(), self.historical_report()]
        self.historical.get_or_create.side_effect = [
            (report, False) for report in self.reports
        ]

        cache_patcher = mock.patch(
            "waldur_openportal.remotebackend.models.CachedProjectUsageReport.objects"
        )
        self.cache = cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

        self.client.get_usage_report.return_value = self.usage_report()

    def historical_report(self, is_complete=False):
        return mock.Mock(is_complete=is_complete, node_usage=0)

    def usage_report(self, hours=10.0, is_complete=True):
        return mock.Mock(
            is_complete=is_complete,
            total_usage=mock.Mock(hours=hours, seconds=int(hours * 3600)),
            to_json=mock.Mock(return_value=json.dumps({"total": hours})),
            dates=[],
        )

    def test_both_the_current_and_the_previous_month_are_collected(self):
        self.backend.sync_usage(self.make_allocation())

        self.assertEqual(self.client.get_usage_report.call_count, 2)
        self.assertEqual(self.cache.update_or_create.call_count, 2)

    def test_the_current_month_is_never_recorded_as_complete(self):
        """
        The month is still running, so however complete the remote portal
        believes its own report to be, more usage can still arrive. Recording
        it as complete would stop it ever being fetched again.
        """
        self.client.get_usage_report.return_value = self.usage_report(is_complete=True)

        self.backend.sync_usage(self.make_allocation())

        previous, current = self.reports
        self.assertTrue(previous.is_complete)
        self.assertFalse(current.is_complete)

    def test_a_month_already_complete_is_not_fetched_again(self):
        self.historical.get_or_create.side_effect = [
            (self.historical_report(is_complete=True), False),
            (self.reports[1], False),
        ]

        self.backend.sync_usage(self.make_allocation())

        self.assertEqual(self.client.get_usage_report.call_count, 1)

    def test_a_failed_reconciliation_does_not_stop_the_other_month(self):
        """
        Reconciliation touches billing and is the most likely thing here to
        fail. Collection is what the next run depends on, so it has to finish
        either way.
        """
        self.backend._reconcile_historical_usage.side_effect = RuntimeError("boom")

        self.backend.sync_usage(self.make_allocation())

        self.assertEqual(self.client.get_usage_report.call_count, 2)
        self.assertEqual(self.backend._reconcile_historical_usage.call_count, 2)

    def test_an_allocation_missing_from_the_portal_is_added_first(self):
        allocation = self.make_allocation(added=False)
        self.backend.add_allocated_project = mock.Mock(return_value=allocation)

        self.backend.sync_usage(allocation)

        self.backend.add_allocated_project.assert_called_once_with(allocation)
        self.client.get_usage_report.assert_not_called()

    def test_nothing_is_collected_without_a_project_identifier(self):
        allocation = self.make_allocation()
        allocation.has_project_identifier.return_value = False

        self.backend.sync_usage(allocation)

        self.client.get_usage_report.assert_not_called()

    def test_contact_is_recorded_once_the_portal_has_answered(self):
        allocation = self.make_allocation()

        self.backend.sync_usage(allocation)

        self.service.touch_last_contact.assert_called_once_with(
            allocation.remote_project
        )


class SyncStorageTest(RemoteBackendFixture):
    def setUp(self):
        super().setUp()
        cache_patcher = mock.patch(
            "waldur_openportal.remotebackend.models.CachedProjectStorageReport.objects"
        )
        self.cache = cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

        self.client.get_storage_report.return_value = mock.Mock(
            to_json=mock.Mock(return_value=json.dumps({"bytes": 1024}))
        )

    def test_both_the_current_and_the_previous_month_are_cached(self):
        self.backend.sync_storage(self.make_allocation())

        self.assertEqual(self.cache.update_or_create.call_count, 2)

    def test_a_month_the_portal_cannot_report_is_skipped(self):
        """
        Storage is reported per month and the months are independent, so one
        month the remote portal cannot answer for must not cost us the other.
        """
        self.client.get_storage_report.side_effect = [
            exceptions.OpenPortalError("no data for that month"),
            mock.Mock(to_json=mock.Mock(return_value=json.dumps({"bytes": 1024}))),
        ]

        self.backend.sync_storage(self.make_allocation())

        self.assertEqual(self.cache.update_or_create.call_count, 1)

    def test_contact_is_not_recorded_when_nothing_was_collected(self):
        self.client.get_storage_report.side_effect = exceptions.OpenPortalError("down")

        self.backend.sync_storage(self.make_allocation())

        self.cache.update_or_create.assert_not_called()
        self.service.touch_last_contact.assert_not_called()

    def test_an_allocation_missing_from_the_portal_is_added_first(self):
        allocation = self.make_allocation(added=False)
        self.backend.add_allocated_project = mock.Mock(return_value=allocation)

        self.backend.sync_storage(allocation)

        self.backend.add_allocated_project.assert_called_once_with(allocation)
        self.client.get_storage_report.assert_not_called()
