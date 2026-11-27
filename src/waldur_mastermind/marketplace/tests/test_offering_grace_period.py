import datetime
from unittest import mock

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.tests import factories, fixtures

END = datetime.date(2020, 1, 1)
IN_GRACE = "2020-01-15"
AFTER_PROJECT_GRACE = "2020-02-01"
AFTER_OFFERING_GRACE = "2020-02-15"


class OfferingGraceResolutionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.end_date = END
        self.project.grace_period_days = 30
        self.project.save()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {}
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.end_date = None
        self.resource.save()

    def test_unset_inherits_the_project_grace_period(self):
        with freeze_time(IN_GRACE):
            self.assertTrue(self.resource.is_in_grace_period)
            self.assertEqual(
                self.resource.effective_end_date, datetime.date(2020, 1, 31)
            )
            self.assertEqual(
                self.resource.grace_period_end_date, datetime.date(2020, 1, 31)
            )
        with freeze_time(AFTER_PROJECT_GRACE):
            self.assertFalse(self.resource.is_in_grace_period)

    def test_unset_resource_end_date_stays_a_hard_stop(self):
        self.resource.end_date = datetime.date(2020, 1, 10)
        self.resource.save()
        with freeze_time("2020-01-11"):
            # The project window would run to the 31st, but the resource end
            # date has already terminated it.
            self.assertFalse(self.resource.is_in_grace_period)
            self.assertEqual(
                self.resource.effective_end_date, datetime.date(2020, 1, 10)
            )

    def test_zero_matches_disable_grace_period(self):
        self.offering.plugin_options = {"grace_period_days": 0}
        self.offering.save()
        with freeze_time(IN_GRACE):
            self.assertFalse(self.resource.is_in_grace_period)
            self.assertEqual(self.resource.effective_end_date, END)
            self.assertIsNone(self.resource.grace_period_end_date)

        self.offering.plugin_options = {"disable_grace_period": True}
        self.offering.save()
        self.resource.refresh_from_db()
        with freeze_time(IN_GRACE):
            self.assertFalse(self.resource.is_in_grace_period)
            self.assertEqual(self.resource.effective_end_date, END)

    def test_offering_length_overrides_the_project_and_applies_to_a_resource_end_date(
        self,
    ):
        self.offering.plugin_options = {"grace_period_days": 40}
        self.offering.save()
        self.resource.end_date = datetime.date(2020, 1, 20)
        self.resource.save()
        # Project clock ends 2020-02-10, resource clock 2020-02-29. The earlier one binds.
        with freeze_time("2020-02-05"):
            self.assertTrue(self.resource.is_in_grace_period)
            self.assertEqual(
                self.resource.effective_end_date, datetime.date(2020, 2, 10)
            )
        with freeze_time(AFTER_OFFERING_GRACE):
            self.assertFalse(self.resource.is_in_grace_period)

    def test_customer_grace_is_inherited_when_the_project_does_not_set_one(self):
        self.project.grace_period_days = None
        self.project.save()
        self.project.customer.grace_period_days = 30
        self.project.customer.save()
        self.resource.refresh_from_db()
        with freeze_time(IN_GRACE):
            self.assertTrue(self.resource.is_in_grace_period)


class OfferingGracePeriodStaffOnlyTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def _update(self, user, plugin_options):
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(url, {"plugin_options": plugin_options})

    def test_staff_can_set_and_clear_grace_period_days(self):
        response = self._update(self.fixture.staff, {"grace_period_days": 12})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options["grace_period_days"], 12)

        response = self._update(self.fixture.staff, {"grace_period_days": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertNotIn("grace_period_days", self.offering.plugin_options)

    def test_clearing_a_migrated_offering_inherits_instead_of_keeping_the_legacy_flag(
        self,
    ):
        # What migration 0339 leaves on an offering that disabled the grace period.
        self.offering.plugin_options = {
            "disable_grace_period": True,
            "grace_period_days": 0,
        }
        self.offering.save()

        response = self._update(self.fixture.staff, {"grace_period_days": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertNotIn("grace_period_days", self.offering.plugin_options)
        self.assertNotIn("disable_grace_period", self.offering.plugin_options)
        self.assertIsNone(self.offering.configured_grace_period_days())

    def test_setting_a_length_drops_the_legacy_flag(self):
        self.offering.plugin_options = {"disable_grace_period": True}
        self.offering.save()

        response = self._update(self.fixture.staff, {"grace_period_days": 30})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options, {"grace_period_days": 30})

    def test_owner_cannot_set_grace_period_days(self):
        response = self._update(self.fixture.owner, {"grace_period_days": 12})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plugin_options", response.data)

    def test_owner_cannot_clear_the_legacy_flag_by_sending_null(self):
        # Stored and sent length are both None, but writing the key drops the flag.
        self.offering.plugin_options = {"disable_grace_period": True}
        self.offering.save()

        response = self._update(self.fixture.owner, {"grace_period_days": None})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.plugin_options["disable_grace_period"])

    def test_owner_may_resend_the_length_the_offering_already_resolves(self):
        self.offering.plugin_options = {"disable_grace_period": True}
        self.offering.save()

        response = self._update(self.fixture.owner, {"grace_period_days": 0})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.configured_grace_period_days(), 0)

    def test_changing_the_length_follows_the_windows_without_the_daily_run(self):
        with (
            mock.patch.object(tasks.follow_offering_grace_windows, "delay") as follow,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._update(self.fixture.staff, {"grace_period_days": 12})
        follow.assert_called_once_with(self.offering.pk)

    def test_an_unrelated_plugin_option_does_not_follow_the_windows(self):
        with (
            mock.patch.object(tasks.follow_offering_grace_windows, "delay") as follow,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._update(self.fixture.staff, {"supports_pausing": True})
        follow.assert_not_called()


@freeze_time(IN_GRACE)
class OfferingGracePeriodTaskTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.end_date = END
        self.project.grace_period_days = 30
        self.project.save()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {
            "supports_pausing": True,
            "grace_period_days": 40,
        }
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

    def test_longer_offering_grace_pauses_and_does_not_terminate_with_the_project(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )

    @freeze_time(AFTER_PROJECT_GRACE)
    def test_longer_offering_grace_outlives_the_project_grace(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )

    @freeze_time(AFTER_OFFERING_GRACE)
    def test_offering_grace_terminates_when_it_ends(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.assertTrue(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )

    def test_grace_signal_is_emitted_once_and_closed_when_the_end_date_is_extended(
        self,
    ):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        started = logging_models.Event.objects.filter(
            event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_STARTED
        )
        self.assertEqual(started.count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            self.project.end_date = datetime.date(2020, 6, 1)
            self.project.save()
        cancelled = logging_models.Event.objects.filter(
            event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_CANCELLED
        )
        self.assertEqual(cancelled.count(), 1)
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_ENDED
            ).exists()
        )
        self.assertNotIn(self.resource.pk, utils.resources_with_open_grace_signal())

    def test_grace_signal_ends_when_the_resource_is_terminated_on_the_last_day(
        self,
    ):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.assertIn(self.resource.pk, utils.resources_with_open_grace_signal())

        # 2020-01-01 plus 40 days: the last day of the window, terminated today.
        with freeze_time("2020-02-10"):
            tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.assertTrue(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )
        ended = logging_models.Event.objects.filter(
            event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_ENDED
        )
        self.assertEqual(ended.count(), 1)
        self.assertNotIn(self.resource.pk, utils.resources_with_open_grace_signal())

    def test_resource_end_date_grace_signal_ends_on_termination(self):
        self.project.end_date = None
        self.project.save()
        self.resource.end_date = END
        self.resource.save()
        tasks.terminate_expired_resources()
        with freeze_time("2020-02-10"):
            tasks.terminate_expired_resources()
        self.assertEqual(
            logging_models.Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_ENDED
            ).count(),
            1,
        )

    def test_setting_a_length_opens_the_window_without_the_daily_run(self):
        # setUp gives the offering 40 days, and no daily run has sent anything.
        tasks.follow_offering_grace_windows(self.offering.pk)

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertEqual(
            logging_models.Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_STARTED
            ).count(),
            1,
        )

    def test_resource_end_date_uses_the_offering_length(self):
        self.project.end_date = None
        self.project.save()
        self.resource.end_date = END
        self.resource.save()
        tasks.terminate_expired_resources()
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )

        with freeze_time("2020-02-11"):
            # end date 2020-01-01 plus 40 days is 2020-02-10, which has passed.
            tasks.terminate_expired_resources()
        self.assertTrue(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.TERMINATE
            ).exists()
        )


class OfferingGracePeriodFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.project.end_date = END
        self.project.grace_period_days = 30
        self.project.save()
        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()
        self.url = factories.ResourceFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def _uuids(self, in_grace):
        response = self.client.get(self.url, {"is_in_grace_period": in_grace})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        return {row["uuid"] for row in rows}

    @freeze_time(IN_GRACE)
    def test_a_length_that_is_not_whole_days_is_read_as_unset(self):
        # Written outside the serializer; the list must not fail on the cast.
        models.Offering.objects.filter(pk=self.fixture.offering.pk).update(
            plugin_options={"grace_period_days": "abc"}
        )
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.offering.configured_grace_period_days())
        # Inherits the project's 30 days, so it is in grace.
        self.assertTrue(self.resource.is_in_grace_period)
        self.assertIn(self.resource.uuid.hex, self._uuids("true"))
        self.assertNotIn(self.resource.uuid.hex, self._uuids("false"))

    @freeze_time(IN_GRACE)
    def test_filter_matches_the_resource_flag(self):
        self.assertTrue(self.resource.is_in_grace_period)
        self.assertIn(self.resource.uuid.hex, self._uuids("true"))
        self.assertNotIn(self.resource.uuid.hex, self._uuids("false"))

    @freeze_time(IN_GRACE)
    def test_filter_excludes_an_offering_that_opts_out(self):
        self.fixture.offering.plugin_options = {"grace_period_days": 0}
        self.fixture.offering.save()
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.is_in_grace_period)
        self.assertNotIn(self.resource.uuid.hex, self._uuids("true"))
        self.assertIn(self.resource.uuid.hex, self._uuids("false"))

    def test_filter_agrees_with_the_flag_for_each_offering_clock(self):
        # (plugin options, resource end date, frozen day, expected flag)
        cases = [
            ({}, None, IN_GRACE, True),
            ({"grace_period_days": 0}, None, IN_GRACE, False),
            ({"grace_period_days": 40}, None, IN_GRACE, True),
            ({"disable_grace_period": True}, None, IN_GRACE, False),
            # The new key wins when the legacy flag is also set.
            (
                {"grace_period_days": 40, "disable_grace_period": True},
                None,
                IN_GRACE,
                True,
            ),
            # A resource end date is a hard stop inside the project window.
            ({}, datetime.date(2020, 1, 10), "2020-01-11", False),
            # The project clock ends 2020-02-10 and wins over the resource clock.
            (
                {"grace_period_days": 40},
                datetime.date(2020, 1, 20),
                "2020-02-15",
                False,
            ),
        ]
        for options, end_date, frozen, in_grace in cases:
            self.fixture.offering.plugin_options = options
            self.fixture.offering.save()
            self.resource.end_date = end_date
            self.resource.save()
            self.resource.refresh_from_db()
            with freeze_time(frozen):
                self.assertIs(
                    self.resource.is_in_grace_period,
                    in_grace,
                    (options, end_date, frozen),
                )
                listed = self._uuids("true")
                excluded = self._uuids("false")
            if in_grace:
                self.assertIn(self.resource.uuid.hex, listed, options)
                self.assertNotIn(self.resource.uuid.hex, excluded, options)
            else:
                self.assertNotIn(self.resource.uuid.hex, listed, options)
                self.assertIn(self.resource.uuid.hex, excluded, options)


class GracePeriodEventGroupTest(test.APITestCase):
    def test_grace_events_are_visible_to_providers_and_on_the_resource(self):
        for event in (
            EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_STARTED,
            EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_ENDED,
            EventType.MARKETPLACE_RESOURCE_GRACE_PERIOD_CANCELLED,
        ):
            self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.PROVIDERS])
            self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.RESOURCES])


class GracePeriodApiFieldTest(test.APITestCase):
    @freeze_time("2020-01-05")
    def test_resource_api_exposes_the_offering_clock(self):
        fixture = fixtures.MarketplaceFixture()
        fixture.project.end_date = END
        fixture.project.grace_period_days = 30
        fixture.project.save()
        fixture.offering.plugin_options = {"grace_period_days": 10}
        fixture.offering.save()
        self.client.force_authenticate(fixture.staff)
        response = self.client.get(factories.ResourceFactory.get_url(fixture.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(response.data["is_in_grace_period"], True)
        self.assertEqual(str(response.data["grace_period_end_date"]), "2020-01-11")
        self.assertEqual(
            str(response.data["resource_effective_end_date"]), "2020-01-11"
        )
        # The project clock is unchanged: 30 days, through the 31st.
        self.assertIs(response.data["project_is_in_grace_period"], True)
