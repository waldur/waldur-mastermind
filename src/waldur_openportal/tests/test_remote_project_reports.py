"""An award's usage and storage reports, across every project it held.

An award's usage and storage are cached under the identifier of the local
project that held it, so an award that moved from X to Y has its history
under two keys, and Y's key also holds whatever an earlier award on Y used.
Each project counts only for the days the award was attached there; on the
day it moved, the project it moved to claims the whole day.
"""

import datetime
import json
from unittest import mock

import openportal
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models, utils

DESTINATION = "remote.portal.offering"
X_KEY, Y_KEY = "projx.here", "projy.here"


def aware(*args):
    return timezone.make_aware(datetime.datetime(*args))


def cache_usage(key, day, hours, resource=DESTINATION):
    """Add hours on day to the cached month of key."""
    cached = models.CachedProjectUsageReport.objects.filter(
        year=day.year, month=day.month, project_identifier=key, resource=resource
    ).first()
    if cached:
        report = cached.get_report()
    else:
        report = openportal.ProjectUsageReport(openportal.ProjectIdentifier(key))
    daily = openportal.DailyProjectUsageReport()
    daily.add_unattributed_usage(openportal.Usage.from_hours(hours))
    report.set_report(day, daily)
    models.CachedProjectUsageReport.objects.update_or_create(
        year=day.year,
        month=day.month,
        project_identifier=key,
        resource=resource,
        defaults={"is_complete": True, "report": json.loads(report.to_json())},
    )


def cache_attributed_usage(key, day, users, gpu=None, num_jobs=0, wait=0):
    """
    Set day in the cached month of key to usage by named cluster users
    ({username: hours}), with GPU usage per user, job counts and wait time,
    and each user mapped to an identifier under key.
    """
    cached = models.CachedProjectUsageReport.objects.filter(
        year=day.year, month=day.month, project_identifier=key, resource=DESTINATION
    ).first()
    data = (
        cached.report
        if cached
        else json.loads(
            openportal.ProjectUsageReport(openportal.ProjectIdentifier(key)).to_json()
        )
    )
    data["reports"][day.isoformat()] = {
        "reports": {name: {"seconds": int(h * 3600)} for name, h in users.items()},
        "components": {
            "gpu": {name: {"seconds": int(h * 3600)} for name, h in (gpu or {}).items()}
        },
        "user_job_counts": {name: num_jobs for name in users},
        "user_wait_seconds": {name: wait for name in users},
        "num_jobs": num_jobs * len(users),
        "total_wait_seconds": wait * len(users),
        "is_complete": True,
    }
    for name in [*users, *(gpu or {})]:
        data["users"][f"{name}.{key}"] = name
    report = openportal.ProjectUsageReport.from_json(json.dumps(data))
    models.CachedProjectUsageReport.objects.update_or_create(
        year=day.year,
        month=day.month,
        project_identifier=key,
        resource=DESTINATION,
        defaults={"is_complete": True, "report": json.loads(report.to_json())},
    )


def snapshot(key, day, gb, users=()):
    return openportal.ProjectStorageReport.from_json(
        json.dumps(
            {
                "project": key,
                "generated_at": f"{day.isoformat()}T12:00:00Z",
                "project_quotas": {
                    "home": {"limit": "100.00 GB", "usage": f"{gb}.00 GB"}
                },
                "user_quotas": {
                    f"{name}.{key}": {"home": {"limit": "10.00 GB", "usage": "1.00 GB"}}
                    for name in users
                },
                "users": {f"{name}.{key}": name for name in users},
            }
        )
    )


def cache_storage(key, day, gb, resource=DESTINATION, users=()):
    """Accumulate a snapshot taken on day into the cached month of key."""
    cached = models.CachedProjectStorageReport.objects.filter(
        year=day.year, month=day.month, project_identifier=key, resource=resource
    ).first()
    report = snapshot(key, day, gb, users)
    if cached:
        report = cached.get_report() + report
    models.CachedProjectStorageReport.objects.update_or_create(
        year=day.year,
        month=day.month,
        project_identifier=key,
        resource=resource,
        defaults={"report": json.loads(report.to_json())},
    )


class RemoteProjectReportTestBase(test.APITestCase):
    """
    Award R was on project X from 1 September, then moved to project Y on
    1 October. Y had earlier held award R0 on the same destination, in July
    and August.
    """

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.x = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.y = self.fixture.project
        self.keys = {self.x.id: X_KEY, self.y.id: Y_KEY}

        r0 = models.RemoteProject.objects.create(
            destination=DESTINATION, identifier="r0.there"
        )
        models.RemoteProjectAttachment.objects.create(remote_project=r0, project=self.y)
        models.RemoteProjectAttachment.objects.filter(remote_project=r0).update(
            attached_at=aware(2026, 7, 1), detached_at=aware(2026, 8, 31)
        )

        self.award = models.RemoteProject.objects.create(
            destination=DESTINATION, identifier="r.there", current_project=self.y
        )
        self.on_x = models.RemoteProjectAttachment.objects.create(
            remote_project=self.award, project=self.x
        )
        on_y = models.RemoteProjectAttachment.objects.create(
            remote_project=self.award, project=self.y
        )
        models.RemoteProjectAttachment.objects.filter(pk=self.on_x.pk).update(
            attached_at=aware(2026, 9, 1, 10), detached_at=aware(2026, 10, 1, 9)
        )
        models.RemoteProjectAttachment.objects.filter(pk=on_y.pk).update(
            attached_at=aware(2026, 10, 1, 15)
        )

        self.client.force_authenticate(self.fixture.staff)
        identifier_patch = mock.patch.object(
            utils,
            "get_local_project_identifier",
            side_effect=lambda project: openportal.ProjectIdentifier(
                self.keys[project.id]
            ),
        )
        identifier_patch.start()
        self.addCleanup(identifier_patch.stop)

    def get(self, action, award=None, **params):
        return self.client.get(
            reverse(
                f"openportal-remote-project-{action}",
                kwargs={"uuid": (award or self.award).uuid.hex},
            ),
            params,
        )

    def get_ok(self, action, **params):
        response = self.get(action, **params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.json()


@freeze_time("2026-11-14")
class RemoteProjectUsageReportTest(RemoteProjectReportTestBase):
    def setUp(self):
        super().setUp()
        cache_usage(Y_KEY, datetime.date(2026, 8, 10), 40.0)  # R0's
        cache_usage(X_KEY, datetime.date(2026, 9, 20), 5.0)
        cache_usage(X_KEY, datetime.date(2026, 10, 1), 3.0)  # X after the move
        cache_usage(X_KEY, datetime.date(2026, 10, 5), 4.0)  # X after the move
        cache_usage(Y_KEY, datetime.date(2026, 10, 1), 2.0)
        cache_usage(Y_KEY, datetime.date(2026, 10, 3), 1.0)

    def usage_by_day(self, data):
        report = openportal.ProjectUsageReport.from_json(json.dumps(data["report"]))
        return {
            day: float(report.get_report(day).total_usage.hours) for day in report.dates
        }

    def test_each_project_counts_only_for_its_own_window(self):
        data = self.get_ok("usage-report")

        # X's usage before the move, Y's from the day of the move. Removing
        # the remap before combine() keeps only the first project's usage.
        self.assertEqual(
            self.usage_by_day(data),
            {
                datetime.date(2026, 9, 20): 5.0,
                datetime.date(2026, 10, 1): 2.0,
                datetime.date(2026, 10, 3): 1.0,
            },
        )
        self.assertEqual(data["total_hours"], 8.0)
        self.assertEqual(data["report"]["project"], "r.there")

    def test_on_the_day_of_the_move_the_day_belongs_to_the_new_project(self):
        data = self.get_ok("usage-report", start="2026-10-01", end="2026-10-01")

        self.assertEqual(self.usage_by_day(data), {datetime.date(2026, 10, 1): 2.0})

    def test_an_earlier_award_on_the_same_project_is_not_counted(self):
        data = self.get_ok("usage-report", start="2026-07-01", end="2026-08-31")

        self.assertEqual(self.usage_by_day(data), {})
        self.assertEqual(data["total_hours"], 0.0)

    def test_the_total_equals_total_usage(self):
        data = self.get_ok("usage-report")

        self.assertEqual(data["total_hours"], self.get_ok("total-usage")["total_hours"])
        self.assertEqual(data["start"], "2026-09-01")
        self.assertEqual(data["end"], "2026-11-14")

    def test_the_total_equals_total_usage_without_attachment_history(self):
        models.RemoteProjectAttachment.objects.filter(
            remote_project=self.award
        ).delete()

        data = self.get_ok("usage-report")

        # Everything under the current project's key, as before attachments
        # were tracked: R0's 40 h included.
        self.assertEqual(data["total_hours"], 43.0)
        self.assertEqual(data["total_hours"], self.get_ok("total-usage")["total_hours"])
        self.assertEqual(data["start"], "2026-08-01")
        self.assertEqual(data["windows"][0]["start"], None)

    def test_the_range_is_respected(self):
        data = self.get_ok("usage-report", start="2026-09-21", end="2026-10-02")

        self.assertEqual(self.usage_by_day(data), {datetime.date(2026, 10, 1): 2.0})
        self.assertEqual(data["start"], "2026-09-21")
        self.assertEqual(data["end"], "2026-10-02")

    def test_usage_on_another_destination_is_not_counted(self):
        cache_usage(Y_KEY, datetime.date(2026, 10, 4), 9.0, resource="elsewhere")

        self.assertEqual(self.get_ok("usage-report")["total_hours"], 8.0)

    def test_a_start_after_the_end_is_an_empty_report(self):
        data = self.get_ok("usage-report", start="2026-10-10", end="2026-10-01")

        self.assertEqual(data["total_hours"], 0.0)
        self.assertEqual(data["report"]["reports"], {})
        self.assertEqual(data["report"]["project"], "r.there")

    def test_dates_are_validated(self):
        response = self.get("usage-report", start="yesterday")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_windows_are_listed(self):
        data = self.get_ok("usage-report")

        self.assertEqual(
            data["windows"],
            [
                {
                    "project_uuid": self.x.uuid.hex,
                    "project_name": self.x.name,
                    "start": "2026-09-01",
                    "end": "2026-09-30",
                },
                {
                    "project_uuid": self.y.uuid.hex,
                    "project_name": self.y.name,
                    "start": "2026-10-01",
                    "end": None,
                },
            ],
        )

    def test_a_hard_deleted_project_is_listed_with_no_project_and_counts_nothing(
        self,
    ):
        models.RemoteProjectAttachment.objects.filter(pk=self.on_x.pk).update(
            project=None
        )

        data = self.get_ok("usage-report")

        self.assertEqual(data["total_hours"], 3.0)
        self.assertEqual(data["windows"][0]["project_uuid"], None)
        self.assertEqual(data["windows"][0]["project_name"], None)
        self.assertEqual(data["windows"][0]["start"], "2026-09-01")
        self.assertEqual(data["total_hours"], self.get_ok("total-usage")["total_hours"])

    def test_an_award_with_no_windows_and_no_identifier_has_no_report(self):
        pending = models.RemoteProject.objects.create(destination=DESTINATION)

        response = self.get("usage-report", award=pending)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["report"])
        self.assertEqual(response.data["total_hours"], 0.0)
        self.assertEqual(response.data["windows"], [])

    def test_a_project_the_caller_cannot_see_counts_but_is_not_named(self):
        self.client.force_authenticate(self.fixture.admin)

        data = self.get_ok("usage-report")

        self.assertEqual(data["total_hours"], 8.0)
        self.assertEqual(data["windows"][0]["project_uuid"], None)
        self.assertEqual(data["windows"][0]["project_name"], None)
        self.assertEqual(data["windows"][1]["project_name"], self.y.name)

    def add_attributed_usage(self):
        # Two of X's users, one of them on GPUs, and one of Y's.
        cache_attributed_usage(
            X_KEY,
            datetime.date(2026, 9, 21),
            {"xalice": 3.0, "xbob": 2.0},
            gpu={"xcarol": 1.0},
            num_jobs=2,
            wait=10,
        )
        cache_attributed_usage(Y_KEY, datetime.date(2026, 10, 4), {"ydave": 4.0})

    def test_users_of_a_project_the_caller_cannot_see_are_not_shown(self):
        self.add_attributed_usage()
        staff_data = self.get_ok("usage-report")
        self.client.force_authenticate(self.fixture.admin)

        data = self.get_ok("usage-report")

        body = json.dumps(data["report"])
        for name in ("xalice", "xbob", "xcarol", X_KEY):
            self.assertNotIn(name, body)
        self.assertIn("ydave", body)
        # Folded, not dropped: the same usage per day, GPU usage and jobs.
        self.assertEqual(data["total_hours"], staff_data["total_hours"])
        self.assertEqual(data["total_hours"], 17.0)
        self.assertEqual(self.usage_by_day(data), self.usage_by_day(staff_data))
        report = openportal.ProjectUsageReport.from_json(body)
        self.assertEqual(report.get_component("gpu").total_usage.hours, 1.0)
        self.assertEqual(report.num_jobs, 4)
        self.assertEqual(report.total_wait_seconds, 20)
        self.assertEqual([str(user) for user in report.users], ["ydave.r.there"])

    def test_staff_sees_every_project_s_users(self):
        self.add_attributed_usage()

        data = self.get_ok("usage-report")

        report = openportal.ProjectUsageReport.from_json(json.dumps(data["report"]))
        day = report.get_report(datetime.date(2026, 9, 21)).daily_reports()[0]
        self.assertEqual(day.usage("xalice").hours, 3.0)
        self.assertEqual(day.usage("xbob").hours, 2.0)
        self.assertEqual(day.get_component("gpu").usage("xcarol").hours, 1.0)
        self.assertEqual(
            sorted(str(user) for user in report.users),
            ["xalice.r.there", "xbob.r.there", "xcarol.r.there", "ydave.r.there"],
        )
        self.assertEqual(data["total_hours"], 17.0)

    def test_a_user_who_cannot_see_the_current_project_gets_404(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        for action in ("usage-report", "storage-report"):
            response = self.get(action)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, action)

    def test_support_sees_every_award_and_project(self):
        self.client.force_authenticate(self.fixture.global_support)

        data = self.get_ok("usage-report")

        self.assertEqual(data["windows"][0]["project_name"], self.x.name)
        self.get_ok("storage-report")


@freeze_time("2026-11-14")
class RemoteProjectStorageReportTest(RemoteProjectReportTestBase):
    def setUp(self):
        super().setUp()
        cache_storage(Y_KEY, datetime.date(2026, 8, 10), 77)  # R0's
        cache_storage(X_KEY, datetime.date(2026, 9, 20), 10)
        cache_storage(X_KEY, datetime.date(2026, 10, 1), 98)  # X after the move
        cache_storage(X_KEY, datetime.date(2026, 10, 5), 99)  # X after the move
        cache_storage(Y_KEY, datetime.date(2026, 10, 1), 30)
        cache_storage(Y_KEY, datetime.date(2026, 10, 3), 40)

    def series(self, data):
        report = openportal.ProjectStorageReport.from_json(json.dumps(data["report"]))
        return [
            (
                item.generated_at.date(),
                data_usage(item),
            )
            for item in sorted(report.daily_reports(), key=lambda s: s.generated_at)
        ]

    def test_each_project_contributes_only_snapshots_from_its_own_window(self):
        data = self.get_ok("storage-report")

        self.assertEqual(
            self.series(data),
            [
                (datetime.date(2026, 9, 20), "10.00 GB"),
                (datetime.date(2026, 10, 1), "30.00 GB"),
                (datetime.date(2026, 10, 3), "40.00 GB"),
            ],
        )
        self.assertEqual(data["report"]["project"], "r.there")

    def test_the_latest_is_the_newest_snapshot_in_range(self):
        # X's snapshot of 5 October is later, but X no longer held the award.
        data = self.get_ok("storage-report")

        self.assertEqual(data["latest"][:10], "2026-10-03")
        self.assertEqual(data["report"]["generated_at"][:10], "2026-10-03")
        self.assertEqual(data["report"]["project_quotas"]["home"]["usage"], "40.00 GB")

    def test_the_range_is_respected(self):
        data = self.get_ok("storage-report", start="2026-09-01", end="2026-09-30")

        self.assertEqual(self.series(data), [(datetime.date(2026, 9, 20), "10.00 GB")])
        self.assertEqual(data["latest"][:10], "2026-09-20")

    def test_an_earlier_award_on_the_same_project_is_not_shown(self):
        data = self.get_ok("storage-report", start="2026-07-01", end="2026-08-31")

        self.assertEqual(self.series(data), [])
        self.assertIsNone(data["latest"])

    def test_a_start_after_the_end_is_an_empty_report(self):
        data = self.get_ok("storage-report", start="2026-10-10", end="2026-10-01")

        self.assertIsNone(data["latest"])
        self.assertEqual(data["report"]["project_quotas"], {})

    def test_users_of_a_project_the_caller_cannot_see_are_not_shown(self):
        cache_storage(X_KEY, datetime.date(2026, 9, 22), 11, users=["xalice"])
        cache_storage(Y_KEY, datetime.date(2026, 10, 4), 41, users=["ydave"])
        staff_data = self.get_ok("storage-report")
        self.client.force_authenticate(self.fixture.admin)

        data = self.get_ok("storage-report")

        body = json.dumps(data["report"])
        self.assertNotIn("xalice", body)
        self.assertNotIn(X_KEY, body)
        self.assertIn("ydave", body)
        # X's project-level storage is still shown.
        self.assertEqual(self.series(data), self.series(staff_data))
        self.assertIn((datetime.date(2026, 9, 22), "11.00 GB"), self.series(data))
        self.assertIn("xalice", json.dumps(staff_data["report"]))

    def test_an_award_with_no_windows_and_no_identifier_has_no_report(self):
        pending = models.RemoteProject.objects.create(destination=DESTINATION)

        response = self.get("storage-report", award=pending)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["report"])
        self.assertIsNone(response.data["latest"])


def data_usage(item):
    quotas = {str(volume): quota for volume, quota in item.project_quotas.items()}
    return str(quotas["home"].usage)
