from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    KpiAggregations,
    KpiCadences,
    KpiDirections,
)
from waldur_mastermind.marketplace.tests import factories


class OfferingKpiTest(test.APITestCase):
    """A KPI is declared by the offering that promises to report it."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)

    def test_kpi_defaults_to_a_neutral_sum(self):
        kpi = factories.OfferingKpiFactory(offering=self.offering)

        self.assertEqual(kpi.aggregation, KpiAggregations.SUM)
        self.assertEqual(kpi.direction, KpiDirections.NEUTRAL)
        self.assertIsNone(kpi.target)

    def test_kpi_declares_how_often_and_by_what_it_is_reported(self):
        """The dashboard needs both to label a figure and group its breakdown."""
        kpi = factories.OfferingKpiFactory(
            offering=self.offering,
            cadence=KpiCadences.DAILY,
            attribute="queue",
        )

        self.assertEqual(kpi.cadence, KpiCadences.DAILY)
        self.assertEqual(kpi.attribute, "queue")

    def test_kpi_without_a_breakdown_leaves_the_attribute_empty(self):
        kpi = factories.OfferingKpiFactory(offering=self.offering, attribute="")

        self.assertEqual(kpi.attribute, "")

    def test_kpi_type_is_unique_per_offering(self):
        factories.OfferingKpiFactory(offering=self.offering, type="tickets_resolved")

        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.OfferingKpiFactory(
                offering=self.offering, type="tickets_resolved"
            )

    def test_two_offerings_may_declare_the_same_kpi_type(self):
        other_offering = factories.OfferingFactory(customer=self.fixture.customer)

        factories.OfferingKpiFactory(offering=self.offering, type="tickets_resolved")
        factories.OfferingKpiFactory(offering=other_offering, type="tickets_resolved")

        self.assertEqual(
            models.OfferingKpi.objects.filter(type="tickets_resolved").count(), 2
        )

    def test_kpi_records_which_way_is_an_improvement(self):
        kpi = factories.OfferingKpiFactory(
            offering=self.offering,
            type="unresolved_tickets",
            direction=KpiDirections.LOWER_IS_BETTER,
            target=Decimal("5.00"),
        )

        self.assertEqual(kpi.direction, KpiDirections.LOWER_IS_BETTER)
        self.assertEqual(kpi.target, Decimal("5.00"))


class ResourceKpiValueTest(test.APITestCase):
    """Datapoints hang off a resource; a project figure is their aggregate."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.kpi = factories.OfferingKpiFactory(offering=self.offering)
        self.resource = factories.ResourceFactory(
            offering=self.offering, project=self.project
        )

    def test_attributes_default_to_an_empty_dict(self):
        value = factories.ResourceKpiValueFactory(resource=self.resource, kpi=self.kpi)

        self.assertEqual(value.attributes, {})

    def test_value_carries_opentelemetry_style_attributes(self):
        value = factories.ResourceKpiValueFactory(
            resource=self.resource,
            kpi=self.kpi,
            attributes={"course": "intro-to-hpc"},
        )

        value.refresh_from_db()
        self.assertEqual(value.attributes, {"course": "intro-to-hpc"})

    def test_saving_a_kpi_from_another_offering_is_refused(self):
        foreign_kpi = factories.OfferingKpiFactory()

        value = models.ResourceKpiValue(
            resource=self.resource,
            kpi=foreign_kpi,
            value=1,
            timestamp=self.resource.created,
        )

        with self.assertRaises(ValidationError):
            value.save()

        self.assertFalse(models.ResourceKpiValue.objects.exists())

    def test_value_from_the_resources_own_offering_is_accepted(self):
        models.ResourceKpiValue(
            resource=self.resource,
            kpi=self.kpi,
            value=1,
            timestamp=self.resource.created,
        ).save()

        self.assertEqual(models.ResourceKpiValue.objects.count(), 1)

    def test_a_repeated_report_does_not_double_count(self):
        moment = self.resource.created

        factories.ResourceKpiValueFactory(
            resource=self.resource, kpi=self.kpi, timestamp=moment, value=3
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            factories.ResourceKpiValueFactory(
                resource=self.resource, kpi=self.kpi, timestamp=moment, value=3
            )

    def test_two_datapoints_may_share_a_timestamp_with_different_attributes(self):
        moment = self.resource.created

        factories.ResourceKpiValueFactory(
            resource=self.resource,
            kpi=self.kpi,
            timestamp=moment,
            attributes={"course": "a"},
        )
        factories.ResourceKpiValueFactory(
            resource=self.resource,
            kpi=self.kpi,
            timestamp=moment,
            attributes={"course": "b"},
        )

        self.assertEqual(self.kpi.values.count(), 2)

    def test_a_resource_can_carry_values_for_several_kpis(self):
        """Each datapoint declares its own KPI, so the factory must not collide."""
        factories.ResourceKpiValueFactory(resource=self.resource)
        factories.ResourceKpiValueFactory(resource=self.resource)

        self.assertEqual(
            models.ResourceKpiValue.objects.filter(resource=self.resource).count(), 2
        )

    def test_clean_without_a_kpi_does_not_raise_a_database_error(self):
        models.ResourceKpiValue(
            resource=self.resource, value=1, timestamp=self.resource.created
        ).clean()

    def test_project_figure_aggregates_over_its_resources(self):
        other_resource = factories.ResourceFactory(
            offering=self.offering, project=self.project
        )
        factories.ResourceKpiValueFactory(resource=self.resource, kpi=self.kpi, value=3)
        factories.ResourceKpiValueFactory(
            resource=other_resource, kpi=self.kpi, value=4
        )

        total = models.ResourceKpiValue.objects.filter(
            resource__project=self.project, kpi=self.kpi
        ).aggregate(total=Sum("value"))["total"]

        self.assertEqual(total, Decimal("7.00"))

    def test_another_projects_values_are_not_counted(self):
        other_project = structure_fixtures.ProjectFixture().project
        other_resource = factories.ResourceFactory(
            offering=self.offering, project=other_project
        )
        factories.ResourceKpiValueFactory(resource=self.resource, kpi=self.kpi, value=3)
        factories.ResourceKpiValueFactory(
            resource=other_resource, kpi=self.kpi, value=99
        )

        total = models.ResourceKpiValue.objects.filter(
            resource__project=self.project, kpi=self.kpi
        ).aggregate(total=Sum("value"))["total"]

        self.assertEqual(total, Decimal("3.00"))
