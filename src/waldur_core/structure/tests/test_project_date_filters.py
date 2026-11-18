import datetime

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures

TODAY = datetime.date(2026, 6, 15)


def days(n):
    return datetime.timedelta(days=n)


class ProjectDateFilterTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = factories.CustomerFactory(grace_period_days=None)
        self.client.force_authenticate(self.fixture.staff)

    def project(self, **kwargs):
        kwargs.setdefault("customer", self.customer)
        return factories.ProjectFactory(**kwargs)

    def list_uuids(self, **params):
        response = self.client.get(
            factories.ProjectFactory.get_list_url(),
            {"customer": self.customer.uuid.hex, "page_size": 300, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["uuid"] for row in response.data}


@freeze_time(TODAY.isoformat())
class ProjectDateRangeFilterTest(ProjectDateFilterTestBase):
    def test_start_date_bounds_are_inclusive(self):
        early = self.project(start_date=datetime.date(2026, 1, 1))
        on_bound = self.project(start_date=datetime.date(2026, 6, 1))
        late = self.project(start_date=datetime.date(2026, 12, 1))
        self.project(start_date=None)

        self.assertEqual(
            self.list_uuids(start_date_after="2026-06-01"),
            {on_bound.uuid.hex, late.uuid.hex},
        )
        self.assertEqual(
            self.list_uuids(start_date_before="2026-06-01"),
            {early.uuid.hex, on_bound.uuid.hex},
        )

    def test_end_date_bounds_are_inclusive(self):
        early = self.project(end_date=datetime.date(2026, 3, 1))
        on_bound = self.project(end_date=datetime.date(2026, 6, 1))
        late = self.project(end_date=datetime.date(2026, 9, 1))
        self.project(end_date=None)

        self.assertEqual(
            self.list_uuids(end_date_after="2026-06-01"),
            {on_bound.uuid.hex, late.uuid.hex},
        )
        self.assertEqual(
            self.list_uuids(end_date_before="2026-06-01"),
            {early.uuid.hex, on_bound.uuid.hex},
        )


@freeze_time(TODAY.isoformat())
class ProjectStartedEndedFilterTest(ProjectDateFilterTestBase):
    def test_started_treats_missing_start_date_as_started(self):
        past = self.project(start_date=TODAY - days(1))
        today = self.project(start_date=TODAY)
        unset = self.project(start_date=None)
        future = self.project(start_date=TODAY + days(1))

        self.assertEqual(
            self.list_uuids(started="true"),
            {past.uuid.hex, today.uuid.hex, unset.uuid.hex},
        )
        self.assertEqual(self.list_uuids(started="false"), {future.uuid.hex})

    def test_project_has_ended_on_its_end_date(self):
        past = self.project(end_date=TODAY - days(1))
        today = self.project(end_date=TODAY)
        unset = self.project(end_date=None)
        future = self.project(end_date=TODAY + days(1))

        self.assertEqual(self.list_uuids(ended="true"), {past.uuid.hex, today.uuid.hex})
        self.assertEqual(
            self.list_uuids(ended="false"), {unset.uuid.hex, future.uuid.hex}
        )


@freeze_time(TODAY.isoformat())
class ProjectActiveDuringFilterTest(ProjectDateFilterTestBase):
    def created_on(self, project, date):
        created = datetime.datetime.combine(date, datetime.time(12), datetime.UTC)
        models.Project.objects.filter(pk=project.pk).update(created=created)
        return project

    def test_year(self):
        no_start = self.created_on(
            self.project(start_date=None), datetime.date(2024, 5, 1)
        )
        created_after = self.created_on(
            self.project(start_date=None), datetime.date(2026, 1, 1)
        )
        ends_on_first_day = self.project(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2025, 1, 1)
        )
        ends_on_second_day = self.project(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2025, 1, 2)
        )
        starts_on_last_day = self.project(start_date=datetime.date(2025, 12, 31))
        starts_after = self.project(start_date=datetime.date(2026, 1, 1))

        uuids = self.list_uuids(active_during="2025")

        self.assertIn(no_start.uuid.hex, uuids)
        self.assertNotIn(created_after.uuid.hex, uuids)
        self.assertNotIn(ends_on_first_day.uuid.hex, uuids)
        self.assertIn(ends_on_second_day.uuid.hex, uuids)
        self.assertIn(starts_on_last_day.uuid.hex, uuids)
        self.assertNotIn(starts_after.uuid.hex, uuids)

    def test_month(self):
        no_start = self.created_on(
            self.project(start_date=None), datetime.date(2025, 3, 31)
        )
        created_after = self.created_on(
            self.project(start_date=None), datetime.date(2025, 4, 1)
        )
        ends_on_first_day = self.project(
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 3, 1)
        )
        ends_in_month = self.project(
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 3, 10)
        )
        starts_on_last_day = self.project(start_date=datetime.date(2025, 3, 31))
        starts_after = self.project(start_date=datetime.date(2025, 4, 1))

        self.assertEqual(
            self.list_uuids(active_during="2025-03"),
            {
                no_start.uuid.hex,
                ends_in_month.uuid.hex,
                starts_on_last_day.uuid.hex,
            },
        )
        self.assertNotIn(
            created_after.uuid.hex, self.list_uuids(active_during="2025-03")
        )
        self.assertNotIn(
            ends_on_first_day.uuid.hex, self.list_uuids(active_during="2025-03")
        )
        self.assertNotIn(
            starts_after.uuid.hex, self.list_uuids(active_during="2025-03")
        )

    def test_last_day_of_february(self):
        starts_on_leap_day = self.project(start_date=datetime.date(2024, 2, 29))

        self.assertIn(
            starts_on_leap_day.uuid.hex, self.list_uuids(active_during="2024-02")
        )

    def test_malformed_period_is_rejected(self):
        for value in ("2025-13", "abc", "2025-1", "2025-00", "25", "0000"):
            with self.subTest(value=value):
                response = self.client.get(
                    factories.ProjectFactory.get_list_url(), {"active_during": value}
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("active_during", response.data)


@freeze_time(TODAY.isoformat())
class ProjectInGraceFilterTest(ProjectDateFilterTestBase):
    """in_grace is ``end_date <= today < end_date + grace period``.

    Project.is_in_grace_period is ``end_date < today <= effective end date``,
    one day later at both ends. It drives pausing and limit zeroing, so it is
    left as it is; the boundary tests below show where the two disagree.
    """

    def test_project_grace_period(self):
        inside = self.project(end_date=TODAY - days(10), grace_period_days=30)
        outside = self.project(end_date=TODAY - days(10), grace_period_days=5)

        self.assertEqual(self.list_uuids(in_grace="true"), {inside.uuid.hex})
        self.assertEqual(self.list_uuids(in_grace="false"), {outside.uuid.hex})

    def test_falls_back_to_organization_grace_period(self):
        self.customer.grace_period_days = 30
        self.customer.save(update_fields=["grace_period_days"])
        inherited = self.project(end_date=TODAY - days(10), grace_period_days=None)
        # A project-level zero overrides the organization's 30 days.
        overridden = self.project(end_date=TODAY - days(10), grace_period_days=0)

        self.assertEqual(self.list_uuids(in_grace="true"), {inherited.uuid.hex})
        self.assertEqual(self.list_uuids(in_grace="false"), {overridden.uuid.hex})

    def test_no_grace_period_means_never_in_grace(self):
        ends_today = self.project(end_date=TODAY, grace_period_days=None)
        ended = self.project(end_date=TODAY - days(1), grace_period_days=None)

        self.assertEqual(self.list_uuids(in_grace="true"), set())
        self.assertEqual(
            self.list_uuids(in_grace="false"), {ends_today.uuid.hex, ended.uuid.hex}
        )
        ends_today.refresh_from_db()
        self.assertTrue(ends_today.is_expired)

    def test_in_grace_on_end_date(self):
        project = self.project(end_date=TODAY, grace_period_days=30)

        self.assertEqual(self.list_uuids(in_grace="true"), {project.uuid.hex})
        # Project.is_in_grace_period only starts the day after the end date.
        self.assertFalse(project.is_in_grace_period)

    def test_not_in_grace_on_effective_end_date(self):
        project = self.project(end_date=TODAY - days(30), grace_period_days=30)

        self.assertEqual(self.list_uuids(in_grace="true"), set())
        self.assertEqual(self.list_uuids(in_grace="false"), {project.uuid.hex})
        self.assertTrue(project.is_expired)
        # Project.is_in_grace_period still holds on the effective end date.
        self.assertTrue(project.is_in_grace_period)

    def test_last_day_of_grace(self):
        project = self.project(end_date=TODAY - days(29), grace_period_days=30)

        self.assertEqual(self.list_uuids(in_grace="true"), {project.uuid.hex})
        self.assertFalse(project.is_expired)

    def test_running_and_unended_projects_are_not_in_grace(self):
        future = self.project(end_date=TODAY + days(1), grace_period_days=30)
        unset = self.project(end_date=None, grace_period_days=30)

        self.assertEqual(self.list_uuids(in_grace="true"), set())
        self.assertEqual(
            self.list_uuids(in_grace="false"), {future.uuid.hex, unset.uuid.hex}
        )


@freeze_time(TODAY.isoformat())
class ProjectLifecyclePartitionTest(ProjectDateFilterTestBase):
    def test_running_in_grace_and_expired_do_not_overlap(self):
        self.customer.grace_period_days = 7
        self.customer.save(update_fields=["grace_period_days"])
        projects = []
        for offset in (-40, -31, -30, -29, -8, -7, -6, -1, 0, 1, 30):
            for grace in (None, 0, 1, 30):
                projects.append(
                    self.project(end_date=TODAY + days(offset), grace_period_days=grace)
                )
        projects.append(self.project(end_date=None))
        projects.append(self.project(end_date=None, grace_period_days=0))

        running = self.list_uuids(ended="false")
        in_grace = self.list_uuids(in_grace="true")

        for project in projects:
            project.refresh_from_db()
            memberships = [
                project.uuid.hex in running,
                project.uuid.hex in in_grace,
                bool(project.is_expired),
            ]
            self.assertEqual(
                memberships.count(True),
                1,
                f"end_date={project.end_date} grace={project.grace_period_days}: "
                f"running, in_grace, expired = {memberships}",
            )


class ProjectDateFilterTimeZoneTest(ProjectDateFilterTestBase):
    """At 20:00 UTC on 14 June it is already 15 June in Auckland (UTC+12).
    The filters use the deployment's local date, as Project.is_expired does."""

    @override_settings(TIME_ZONE="Pacific/Auckland")
    @freeze_time("2026-06-14 20:00:00")
    def test_filters_use_local_date(self):
        local_today = datetime.date(2026, 6, 15)
        starts_today = self.project(start_date=local_today)
        ends_today = self.project(end_date=local_today, grace_period_days=1)
        grace_ends_today = self.project(
            end_date=local_today - days(1), grace_period_days=1
        )

        self.assertIn(starts_today.uuid.hex, self.list_uuids(started="true"))
        self.assertEqual(self.list_uuids(ended="false"), {starts_today.uuid.hex})
        self.assertEqual(
            self.list_uuids(ended="true"),
            {ends_today.uuid.hex, grace_ends_today.uuid.hex},
        )
        self.assertEqual(self.list_uuids(in_grace="true"), {ends_today.uuid.hex})
        self.assertTrue(grace_ends_today.is_expired)


@freeze_time(TODAY.isoformat())
class ProjectDateFilterVisibilityTest(ProjectDateFilterTestBase):
    def test_filters_only_narrow_visible_projects(self):
        own = self.fixture.project
        own.end_date = TODAY
        own.grace_period_days = 30
        own.save()
        other = factories.ProjectFactory(end_date=TODAY, grace_period_days=30)
        self.client.force_authenticate(self.fixture.owner)

        for params in (
            {"ended": "true"},
            {"in_grace": "true"},
            {"end_date_before": TODAY.isoformat()},
            {"active_during": "2026"},
        ):
            with self.subTest(params=params):
                response = self.client.get(
                    factories.ProjectFactory.get_list_url(), params
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                uuids = {row["uuid"] for row in response.data}
                self.assertIn(own.uuid.hex, uuids)
                self.assertNotIn(other.uuid.hex, uuids)
