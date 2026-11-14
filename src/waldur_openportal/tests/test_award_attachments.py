"""An award's credit and usage come from the periods it held each project.

A project can hold one award after another, and an award can be moved from
one project to another. Counting everything a project ever spent, or every
report cached under its identifier, charges an award with what an earlier
award used.
"""

import datetime
import decimal
import importlib
import json
from unittest import mock

import openportal
from django.apps import apps
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_openportal import handlers, models, utils


def aware(*args):
    return timezone.make_aware(datetime.datetime(*args))


def details(start_date=None):
    return mock.Mock(start_date=start_date)


class AwardMixin:
    def make_award(self, identifier, project=None):
        template = models.ProjectTemplate.objects.create(
            name=f"tmpl-{identifier}",
            portal="test-portal",
            allocation_units_mapping={"NHR": 1.0},
        )
        return models.ManagedProject.objects.create(
            destination="test.portal",
            identifier=identifier,
            project=project,
            project_template=template,
        )

    def set_project(self, managed_project, project, start_date=None):
        with (
            mock.patch.object(
                models.ManagedProject, "get_details", return_value=details(start_date)
            ),
            mock.patch.object(models.ManagedProject, "set_details"),
            mock.patch.object(models.ManagedProject, "_get_project_link"),
        ):
            managed_project.set_project(project)


class ManagedProjectAttachmentTest(AwardMixin, TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.other_project = structure_factories.ProjectFactory()
        self.award = self.make_award("award.portal")

    def test_attaching_opens_an_attachment(self):
        self.set_project(self.award, self.project)

        attachment = self.award.attachments.get()
        self.assertEqual(attachment.project, self.project)
        self.assertIsNone(attachment.detached_at)

    def test_attaching_the_same_project_again_changes_nothing(self):
        self.set_project(self.award, self.project)
        self.set_project(self.award, self.project)

        self.assertEqual(self.award.attachments.count(), 1)

    def test_moving_closes_the_old_attachment(self):
        self.set_project(self.award, self.project)
        self.set_project(self.award, self.other_project)

        old = self.award.attachments.get(project=self.project)
        new = self.award.attachments.get(project=self.other_project)
        self.assertIsNotNone(old.detached_at)
        self.assertIsNone(new.detached_at)

    def test_detaching_closes_the_attachment(self):
        self.set_project(self.award, self.project)
        self.set_project(self.award, None)

        self.assertFalse(self.award.attachments.filter(detached_at=None).exists())

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": True})
    def test_deleting_the_project_closes_the_attachment(self):
        self.set_project(self.award, self.project)

        with mock.patch.object(
            models.ManagedProject, "get_details", return_value=mock.Mock(end_date=None)
        ):
            handlers.delete_project(
                sender=structure_models.Project, instance=self.project
            )

        self.assertFalse(self.award.attachments.filter(detached_at=None).exists())


@freeze_time("2025-11-15")
class AwardCreditCountsOnlyItsOwnSpendTest(AwardMixin, TestCase):
    """
    The project spent 800 in September under award 1. Award 1 then left and
    award 2 (500) was attached on 10 October.
    """

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        invoice_models.CustomerCredit.objects.create(
            customer=self.project.customer, value=decimal.Decimal(100000)
        )
        self.add_spend(2025, 9, decimal.Decimal(800))

        self.award_1 = self.make_award("award1.portal")
        with freeze_time("2025-05-01"):
            self.set_project(self.award_1, self.project)
        with freeze_time("2025-10-01"):
            self.set_project(self.award_1, None)

        self.award_2 = self.make_award("award2.portal")
        with freeze_time("2025-10-10"):
            self.set_project(self.award_2, self.project)

    def add_spend(self, year, month, amount):
        invoice = invoice_factories.InvoiceFactory(
            customer=self.project.customer, year=year, month=month
        )
        month_start = aware(year, month, 1)
        invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            unit_price=amount,
            quantity=1,
            start=month_start,
            end=aware(year, month, utils.get_last_day_of_month(month_start).day, 23),
        )

    def set_credits(self, allocation, start_date=None):
        with mock.patch.object(
            models.ManagedProject, "get_details", return_value=details(start_date)
        ):
            utils.set_project_credits(self.project, decimal.Decimal(allocation))
        credit = invoice_models.ProjectCredit.objects.filter(
            project=self.project
        ).first()
        return credit.value if credit else decimal.Decimal(0)

    def test_spend_under_an_earlier_award_does_not_count(self):
        self.assertEqual(self.set_credits(500), decimal.Decimal(500))

    def test_spend_under_the_current_award_counts(self):
        # October straddles the day award 2 arrived: it counts in full.
        self.add_spend(2025, 10, decimal.Decimal(120))

        self.assertEqual(self.set_credits(500), decimal.Decimal(380))

    def test_spend_from_the_award_start_date_counts(self):
        # Award 2 started on 20 September, before it was attached.
        self.assertEqual(
            self.set_credits(1000, start_date=datetime.date(2025, 9, 20)),
            decimal.Decimal(200),
        )

    def test_project_credits_are_this_awards(self):
        self.set_credits(500)

        with mock.patch.object(
            models.ManagedProject, "get_details", return_value=details()
        ):
            self.assertEqual(utils.get_project_credits(self.project), 500)

    def test_a_project_without_award_history_counts_all_spend(self):
        models.ManagedProjectAttachment.objects.all().delete()

        self.assertEqual(self.set_credits(1000), decimal.Decimal(200))


class BackfillAttachmentsMigrationTest(AwardMixin, TestCase):
    def setUp(self):
        self.migration = importlib.import_module(
            "waldur_openportal.migrations.0042_backfill_managedprojectattachment"
        )

    def backfill(self):
        self.migration.backfill_attachments(apps, None)

    def test_an_award_attached_through_the_api_starts_at_the_attach(self):
        project = structure_factories.ProjectFactory()
        award = self.make_award("attached.portal", project=project)
        with freeze_time("2026-03-04"):
            models.ManagedProjectAuditEntry.record(
                award,
                models.ManagedProjectAuditEventType.PROJECT_ATTACHED,
                note=f"Attached project {project.uuid}",
            )

        self.backfill()

        attachment = award.attachments.get()
        self.assertEqual(attachment.project, project)
        self.assertEqual(attachment.attached_at.date(), datetime.date(2026, 3, 4))
        self.assertIsNone(attachment.detached_at)

    def test_an_award_without_an_attach_starts_at_the_project(self):
        project = structure_factories.ProjectFactory()
        award = self.make_award("legacy.portal", project=project)

        self.backfill()

        self.assertEqual(award.attachments.get().attached_at, project.created)

    def test_rerunning_creates_nothing(self):
        project = structure_factories.ProjectFactory()
        award = self.make_award("legacy.portal", project=project)
        self.make_award("unattached.portal")

        self.backfill()
        self.backfill()

        self.assertEqual(models.ManagedProjectAttachment.objects.count(), 1)
        self.assertEqual(award.attachments.count(), 1)


class RemoteProjectTotalUsageTest(test.APITestCase):
    """
    Award R was on project X (5 h in September), then moved to project Y on
    1 October. Y had earlier held award R0 on the same destination (40 h in
    August).
    """

    destination = "remote.portal.offering"

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        customer = self.fixture.customer
        self.x = structure_factories.ProjectFactory(customer=customer)
        self.y = structure_factories.ProjectFactory(customer=customer)
        self.keys = {self.x.id: "projx.here", self.y.id: "projy.here"}

        r0 = models.RemoteProject.objects.create(
            destination=self.destination, identifier="r0.there"
        )
        models.RemoteProjectAttachment.objects.create(remote_project=r0, project=self.y)
        models.RemoteProjectAttachment.objects.filter(remote_project=r0).update(
            attached_at=aware(2026, 7, 1), detached_at=aware(2026, 8, 31)
        )

        self.award = models.RemoteProject.objects.create(
            destination=self.destination, identifier="r.there", current_project=self.y
        )
        on_x = models.RemoteProjectAttachment.objects.create(
            remote_project=self.award, project=self.x
        )
        on_y = models.RemoteProjectAttachment.objects.create(
            remote_project=self.award, project=self.y
        )
        models.RemoteProjectAttachment.objects.filter(pk=on_x.pk).update(
            attached_at=aware(2026, 9, 1), detached_at=aware(2026, 10, 1)
        )
        models.RemoteProjectAttachment.objects.filter(pk=on_y.pk).update(
            attached_at=aware(2026, 10, 1)
        )

        self.cache_usage("projy.here", datetime.date(2026, 8, 10), 40.0)
        self.cache_usage("projx.here", datetime.date(2026, 9, 20), 5.0)

    def cache_usage(self, key, day, hours):
        report = openportal.ProjectUsageReport(openportal.ProjectIdentifier(key))
        daily = openportal.DailyProjectUsageReport()
        daily.add_unattributed_usage(openportal.Usage.from_hours(hours))
        report.set_report(day, daily)
        models.CachedProjectUsageReport.objects.update_or_create(
            year=day.year,
            month=day.month,
            project_identifier=key,
            resource=self.destination,
            defaults={"is_complete": True, "report": json.loads(report.to_json())},
        )

    def total_usage(self):
        self.client.force_authenticate(self.fixture.staff)
        with mock.patch.object(
            utils,
            "get_local_project_identifier",
            side_effect=lambda project: openportal.ProjectIdentifier(
                self.keys[project.id]
            ),
        ):
            response = self.client.get(
                reverse(
                    "openportal-remote-project-total-usage",
                    kwargs={"uuid": self.award.uuid.hex},
                )
            )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["total_hours"]

    @freeze_time("2026-11-14")
    def test_counts_the_awards_usage_on_every_project_it_held(self):
        self.assertEqual(self.total_usage(), 5.0)

    @freeze_time("2026-11-14")
    def test_counts_usage_on_the_current_project_since_the_move(self):
        self.cache_usage("projy.here", datetime.date(2026, 10, 3), 2.0)

        self.assertEqual(self.total_usage(), 7.0)

    @freeze_time("2026-11-14")
    def test_on_the_move_day_the_new_project_claims_the_day(self):
        # Usage on X on 1 October is no longer the award's.
        self.cache_usage("projx.here", datetime.date(2026, 10, 1), 3.0)

        self.assertEqual(self.total_usage(), 5.0)
