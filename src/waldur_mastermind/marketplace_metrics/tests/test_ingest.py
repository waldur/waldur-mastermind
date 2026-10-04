import datetime
from decimal import Decimal
from unittest import mock

from constance.test import override_config
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status, test
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, ingest, models, views

from . import factories

URL = "/api/marketplace-metric-points/"


def ago(**kwargs):
    return (timezone.now() - datetime.timedelta(**kwargs)).isoformat()


class MetricPointTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )
        self.counter = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(
                key="education.course.completions"
            ),
        )
        self.gauge = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(
                key="support.response_time.median",
                kind=enums.MetricKinds.GAUGE,
                attribute_keys=["priority"],
            ),
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

    def point(self, **extra):
        return {
            "resource": self.resource.uuid.hex,
            "metric": "education.course.completions",
            "timestamp": ago(hours=2),
            "value": 14,
            "attributes": {"course": "intro-to-linux"},
            **extra,
        }

    def report(self, payload, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        return self.client.post(URL, payload, format="json")

    def values(self):
        return list(
            models.MetricPoint.objects.order_by("timestamp").values_list(
                "value", flat=True
            )
        )

    def test_a_point_and_a_batch_are_accepted(self):
        response = self.report(self.point())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"accepted": 1, "rejected": []})

        response = self.report(
            [self.point(timestamp=ago(hours=1)), self.point(timestamp=ago(minutes=5))]
        )
        self.assertEqual(response.data["accepted"], 2)
        self.assertEqual(models.MetricPoint.objects.count(), 3)

    def test_reporting_the_same_point_again_replaces_the_value(self):
        timestamp = ago(hours=2)
        self.report(self.point(timestamp=timestamp))

        self.report(self.point(timestamp=timestamp, value=9))

        self.assertEqual(self.values(), [Decimal("9")])

    def test_a_ratio_keeps_its_precision(self):
        self.report(
            self.point(
                metric="support.response_time.median", value="0.953", attributes={}
            )
        )

        self.assertEqual(self.values(), [Decimal("0.953")])

    def test_a_service_provider_manager_reports(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        manager = structure_factories.UserFactory()
        provider.add_user(manager, ServiceProviderRole.MANAGER)

        response = self.report(self.point(), user=manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["accepted"], 1)

    def test_an_outsider_is_refused_and_learns_nothing(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        response = self.report(
            self.point(metric="unknown"), user=structure_factories.UserFactory()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_batch_with_a_foreign_resource_is_refused_whole(self):
        foreign = marketplace_factories.ResourceFactory(state=ResourceStates.OK)

        response = self.report([self.point(), self.point(resource=foreign.uuid.hex)])

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.MetricPoint.objects.exists())

    def test_invalid_points_are_refused_one_by_one(self):
        self.gauge.state = enums.OfferingMetricStates.PAUSED
        self.gauge.save()
        bad = [
            self.point(metric="not.adopted"),
            self.point(metric="support.response_time.median", attributes={}),
            self.point(attributes={"teacher": "x"}),
            self.point(timestamp=ago(days=-1)),
            self.point(timestamp=ago(days=30)),
            self.point(value=-1),
        ]

        response = self.report([self.point(), *bad])

        self.assertEqual(response.data["accepted"], 1)
        self.assertEqual(
            [r["index"] for r in response.data["rejected"]], [1, 2, 3, 4, 5, 6]
        )
        self.assertEqual(models.MetricPoint.objects.count(), 1)

    def test_a_resource_that_is_not_running_is_refused(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()

        response = self.report(self.point())

        self.assertEqual(len(response.data["rejected"]), 1)

    def test_attribute_values_are_capped_per_resource(self):
        self.counter.definition.max_attribute_values = 2
        self.counter.definition.save()

        response = self.report(
            [
                self.point(attributes={"course": name}, timestamp=ago(hours=i + 1))
                for i, name in enumerate(["a", "b", "c", "a"])
            ]
        )

        self.assertEqual(response.data["accepted"], 3)
        self.assertEqual(models.MetricSeries.objects.count(), 2)

    @override_config(METRICS_MAX_SERIES_PER_RESOURCE_METRIC=1)
    def test_series_are_capped_per_resource_and_metric(self):
        response = self.report(
            [self.point(), self.point(attributes={"course": "other"})]
        )

        self.assertEqual(response.data["accepted"], 1)

    @override_config(METRICS_MAX_POINTS_PER_REQUEST=1)
    def test_an_oversized_request_is_refused(self):
        response = self.report([self.point(), self.point(timestamp=ago(hours=1))])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_running_totals_become_increments_and_survive_a_restart(self):
        start = ago(days=1)
        restart = ago(hours=3)
        totals = [
            (start, ago(hours=6), 10),
            (start, ago(hours=5), 15),
            (start, ago(hours=4), 22),
            (restart, ago(hours=2), 4),  # restarted: counts from zero
            (restart, ago(hours=1), 9),
        ]

        response = self.report(
            [self.point(start_time=s, timestamp=t, value=v) for s, t, v in totals]
        )

        self.assertEqual(response.data["accepted"], 5)
        self.assertEqual(self.values(), [10, 5, 7, 4, 5])

    def test_a_smaller_total_with_the_same_start_is_a_reset(self):
        start = ago(days=1)
        self.report(self.point(start_time=start, timestamp=ago(hours=2), value=10))

        self.report(self.point(start_time=start, timestamp=ago(hours=1), value=3))

        self.assertEqual(self.values(), [10, 3])

    def test_an_older_total_is_refused_but_a_retry_is_harmless(self):
        start = ago(days=1)
        last = ago(hours=1)
        self.report(self.point(start_time=start, timestamp=last, value=10))

        retry = self.report(self.point(start_time=start, timestamp=last, value=10))
        older = self.report(
            self.point(start_time=start, timestamp=ago(hours=2), value=8)
        )

        self.assertEqual(retry.data["accepted"], 1)
        self.assertEqual(len(older.data["rejected"]), 1)
        self.assertEqual(self.values(), [10])

    def test_a_gauge_cannot_be_sent_as_a_running_total(self):
        response = self.report(
            self.point(
                metric="support.response_time.median",
                attributes={},
                start_time=ago(days=1),
            )
        )

        self.assertEqual(len(response.data["rejected"]), 1)

    def test_a_non_finite_or_nul_attribute_refuses_only_that_point(self):
        self.gauge.definition.attribute_keys = ["priority"]
        self.gauge.definition.save()
        points = [
            ingest.Point(
                resource_uuid=self.resource.uuid.hex,
                metric="support.response_time.median",
                timestamp=timezone.now() - datetime.timedelta(hours=1),
                value=Decimal(3),
                attributes={"priority": bad},
            )
            for bad in (float("nan"), float("inf"), "hi\x00gh")
        ] + [
            ingest.Point(
                resource_uuid=self.resource.uuid.hex,
                metric="support.response_time.median",
                timestamp=timezone.now() - datetime.timedelta(hours=1),
                value=Decimal(3),
                attributes={"priority": "high"},
            )
        ]
        request = mock.Mock(user=self.fixture.owner)

        result = ingest.ingest(request, points)

        self.assertEqual(result.accepted, 1)
        self.assertEqual([r["index"] for r in result.rejected], [0, 1, 2])
        self.assertEqual(models.MetricSeries.objects.count(), 1)

    def test_an_uppercase_or_dashed_uuid_is_the_same_resource(self):
        for spelling in (
            self.resource.uuid.hex.upper(),
            str(self.resource.uuid),
            "{%s}" % self.resource.uuid,
        ):
            response = self.report(self.point(resource=spelling))
            self.assertEqual(response.status_code, status.HTTP_200_OK, spelling)

    def test_a_refused_point_creates_no_series(self):
        self.report(self.point(value=-1))

        self.assertFalse(models.MetricSeries.objects.exists())
        self.assertFalse(self.counter.definition.has_data())

    def test_a_long_running_counter_starts_from_a_baseline(self):
        """Its first total covers months nobody reported; only growth counts."""
        long_ago = ago(days=60)
        self.report(self.point(start_time=long_ago, timestamp=ago(hours=2), value=1000))
        self.report(self.point(start_time=long_ago, timestamp=ago(hours=1), value=1012))

        self.assertEqual(self.values(), [12])

    def test_a_counter_that_started_recently_counts_its_first_total(self):
        self.report(self.point(start_time=ago(days=1), timestamp=ago(hours=1), value=7))

        self.assertEqual(self.values(), [7])


class IngestThrottleTest(test.APITestCase):
    """Test settings switch throttling off; these switch it back on."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.fixture = structure_fixtures.ProjectFixture()
        cache.clear()
        for patcher in (
            mock.patch.object(
                views.MetricPointViewSet, "throttle_classes", [ScopedRateThrottle]
            ),
            mock.patch.object(
                views.OtlpMetricsView, "throttle_classes", [ScopedRateThrottle]
            ),
            mock.patch.object(
                ScopedRateThrottle, "THROTTLE_RATES", {"metrics_ingest": "2/min"}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self, user):
        self.client.force_authenticate(user)
        return self.client.post(URL, [], format="json")

    def otlp(self, user):
        self.client.force_authenticate(user)
        return self.client.generic(
            "POST", "/api/otlp/v1/metrics", "{}", content_type="application/json"
        )

    def test_native_and_otlp_share_one_budget_per_user(self):
        owner = self.fixture.owner

        self.assertEqual(self.native(owner).status_code, status.HTTP_200_OK)
        self.assertEqual(self.otlp(owner).status_code, status.HTTP_200_OK)
        response = self.native(owner)

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Retry-After", response.headers)
        self.assertEqual(
            self.otlp(owner).status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(
            self.native(self.fixture.manager).status_code, status.HTTP_200_OK
        )
