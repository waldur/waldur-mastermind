import datetime
import gzip

from constance.test import override_config
from django.utils import timezone
from google.protobuf import json_format
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, models

from . import factories

URL = "/api/otlp/v1/metrics"


def nanos(**ago):
    moment = timezone.now() - datetime.timedelta(**ago)
    return int(moment.timestamp() * 1e9)


class OtlpTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.fixture = structure_fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)
        factories.OfferingMetricFactory(
            offering=offering,
            definition=factories.MetricDefinitionFactory(
                key="education.course.completions"
            ),
        )
        factories.OfferingMetricFactory(
            offering=offering,
            definition=factories.MetricDefinitionFactory(
                key="education.learners.active",
                kind=enums.MetricKinds.GAUGE,
                attribute_keys=[],
            ),
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=offering, project=self.fixture.project, state=ResourceStates.OK
        )

    def request_message(self, metrics, resource_uuid=None):
        message = metrics_service_pb2.ExportMetricsServiceRequest()
        resource_metrics = message.resource_metrics.add()
        uuid = resource_uuid if resource_uuid is not None else self.resource.uuid.hex
        if uuid:
            attribute = resource_metrics.resource.attributes.add()
            attribute.key = "waldur.resource.uuid"
            attribute.value.string_value = uuid
        resource_metrics.scope_metrics.add().metrics.extend(metrics)
        return message

    def gauge(self, value=42, name="education.learners.active"):
        metric = metrics_pb2.Metric(name=name)
        metric.gauge.data_points.add(time_unix_nano=nanos(hours=1), as_int=value)
        return metric

    def cumulative(self, totals):
        metric = metrics_pb2.Metric(name="education.course.completions")
        metric.sum.is_monotonic = True
        metric.sum.aggregation_temporality = (
            metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE
        )
        start = nanos(days=1)
        for hours, total in totals:
            point = metric.sum.data_points.add(
                start_time_unix_nano=start, time_unix_nano=nanos(hours=hours)
            )
            point.as_double = total
            attribute = point.attributes.add()
            attribute.key = "course"
            attribute.value.string_value = "linux"
        return metric

    def histogram(self):
        metric = metrics_pb2.Metric(name="education.course.duration")
        metric.histogram.data_points.add(time_unix_nano=nanos(hours=1), count=3)
        return metric

    def post_json(self, message, user=None):
        self.client.force_authenticate(user or self.fixture.owner)
        return self.client.generic(
            "POST",
            URL,
            json_format.MessageToJson(message),
            content_type="application/json",
        )

    def post_protobuf(self, message, gzipped=False):
        self.client.force_authenticate(self.fixture.owner)
        body = message.SerializeToString()
        headers = {}
        if gzipped:
            body = gzip.compress(body)
            headers["HTTP_CONTENT_ENCODING"] = "gzip"
        return self.client.generic(
            "POST", URL, body, content_type="application/x-protobuf", **headers
        )

    def test_json_gauge_is_recorded(self):
        response = self.post_json(self.request_message([self.gauge()]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {})
        self.assertEqual(models.MetricPoint.objects.get().value, 42)

    def test_protobuf_cumulative_sum_becomes_increments(self):
        response = self.post_protobuf(
            self.request_message([self.cumulative([(3, 10), (2, 25), (1, 31)])]),
            gzipped=True,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reply = metrics_service_pb2.ExportMetricsServiceResponse.FromString(
            response.content
        )
        self.assertEqual(reply.partial_success.rejected_data_points, 0)
        values = models.MetricPoint.objects.order_by("timestamp").values_list(
            "value", flat=True
        )
        self.assertEqual(list(values), [10, 15, 6])

    def test_unsupported_types_and_mismatches_are_partial_success(self):
        mismatched = self.gauge(name="education.course.completions")

        response = self.post_json(
            self.request_message([self.gauge(), self.histogram(), mismatched])
        )

        body = response.json()
        self.assertEqual(body["partialSuccess"]["rejectedDataPoints"], "2")
        self.assertIn("histogram", body["partialSuccess"]["errorMessage"])
        self.assertEqual(models.MetricPoint.objects.count(), 1)

    def test_a_resource_without_the_uuid_attribute_is_refused(self):
        response = self.post_json(
            self.request_message([self.gauge()], resource_uuid="")
        )

        self.assertEqual(response.json()["partialSuccess"]["rejectedDataPoints"], "1")

    def test_an_outsider_is_refused(self):
        response = self.post_json(
            self.request_message([self.gauge()]), user=structure_factories.UserFactory()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_refused(self):
        response = self.client.generic(
            "POST", URL, "{}", content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_malformed_body_is_a_bad_request(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.generic(
            "POST", URL, "not json", content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(METRICS_MAX_OTLP_BODY_BYTES=1000)
    def test_a_body_that_inflates_past_the_cap_is_refused(self):
        self.client.force_authenticate(self.fixture.owner)
        bomb = gzip.compress(b"\x00" * 50_000)

        response = self.client.generic(
            "POST",
            URL,
            bomb,
            content_type="application/x-protobuf",
            HTTP_CONTENT_ENCODING="gzip",
        )

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    def test_a_point_without_a_value_is_refused_not_stored_as_zero(self):
        metric = metrics_pb2.Metric(name="education.learners.active")
        metric.gauge.data_points.add(time_unix_nano=nanos(hours=1))
        flagged = metric.gauge.data_points.add(time_unix_nano=nanos(hours=1), as_int=5)
        flagged.flags = metrics_pb2.DATA_POINT_FLAGS_NO_RECORDED_VALUE_MASK

        response = self.post_json(self.request_message([metric]))

        self.assertEqual(response.json()["partialSuccess"]["rejectedDataPoints"], "2")
        self.assertFalse(models.MetricPoint.objects.exists())

    def test_a_nan_attribute_is_refused_not_a_server_error(self):
        metric = metrics_pb2.Metric(name="education.course.completions")
        metric.sum.is_monotonic = True
        metric.sum.aggregation_temporality = metrics_pb2.AGGREGATION_TEMPORALITY_DELTA
        point = metric.sum.data_points.add(time_unix_nano=nanos(hours=1), as_int=2)
        attribute = point.attributes.add()
        attribute.key = "course"
        attribute.value.double_value = float("nan")

        response = self.post_json(self.request_message([metric]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["partialSuccess"]["rejectedDataPoints"], "1")
