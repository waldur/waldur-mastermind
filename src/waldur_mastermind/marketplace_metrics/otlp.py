"""OTLP/HTTP metrics, translated into ingest points.

Only what a business metric needs is accepted: Gauge, and Sum as either
increments (delta) or running totals (cumulative). Histograms and summaries
are counted as rejected data points, which is how OTLP reports partial
success, so a collector exporting a mix keeps working.
"""

import datetime
import decimal
import zlib

from constance import config
from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2

from . import ingest

RESOURCE_ATTRIBUTE = "waldur.resource.uuid"
JSON = "application/json"
PROTOBUF = "application/x-protobuf"


class InvalidPayload(Exception):
    pass


class PayloadTooLarge(Exception):
    pass


# Bit set on a data point that carries no value, per the OTLP data model.
NO_RECORDED_VALUE = metrics_pb2.DATA_POINT_FLAGS_NO_RECORDED_VALUE_MASK


def _time(nanos):
    return datetime.datetime.fromtimestamp(nanos / 1e9, tz=datetime.UTC)


def _any_value(value):
    kind = value.WhichOneof("value")
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(value, kind)
    raise ValueError(kind)


def _attributes(key_values):
    return {kv.key: _any_value(kv.value) for kv in key_values}


def _number(point):
    if point.WhichOneof("value") == "as_int":
        return decimal.Decimal(point.as_int)
    return decimal.Decimal(repr(point.as_double))


def _gunzip(body, limit):
    # Decompress in bounded steps: a few megabytes of gzip can expand to
    # gigabytes, and the size check has to happen before that, not after.
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = decompressor.decompress(body, limit + 1)
    except zlib.error:
        raise InvalidPayload("Body is not valid gzip.")
    if len(data) > limit or decompressor.unconsumed_tail:
        raise PayloadTooLarge(f"Decompressed body exceeds {limit} bytes.")
    return data


def parse(body, content_type, content_encoding=""):
    limit = config.METRICS_MAX_OTLP_BODY_BYTES
    if "gzip" in (content_encoding or ""):
        body = _gunzip(body, limit)
    elif len(body) > limit:
        raise PayloadTooLarge(f"Body exceeds {limit} bytes.")
    message = metrics_service_pb2.ExportMetricsServiceRequest()
    try:
        if content_type.startswith(JSON):
            json_format.Parse(body.decode(), message, ignore_unknown_fields=True)
        elif content_type.startswith(PROTOBUF):
            message.ParseFromString(body)
        else:
            raise InvalidPayload(f"Unsupported content type {content_type}.")
    except (json_format.ParseError, DecodeError, UnicodeDecodeError) as e:
        raise InvalidPayload(str(e))
    return message


def to_points(message):
    """Returns the points to ingest and how many data points were refused here."""
    points = []
    refused = []
    for resource_metrics in message.resource_metrics:
        try:
            resource_attributes = _attributes(resource_metrics.resource.attributes)
        except ValueError:
            resource_attributes = {}
        resource_uuid = resource_attributes.get(RESOURCE_ATTRIBUTE)
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                data = metric.WhichOneof("data")
                if data == "gauge":
                    data_points = metric.gauge.data_points
                    kind, cumulative = ingest.GAUGE, False
                elif data == "sum":
                    data_points = metric.sum.data_points
                    cumulative = (
                        metric.sum.aggregation_temporality
                        == metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE
                    )
                    # A sum that can go down is a level, whatever it is called.
                    kind = ingest.COUNTER if metric.sum.is_monotonic else ingest.GAUGE
                    cumulative = cumulative and kind == ingest.COUNTER
                else:
                    count = len(getattr(metric, data).data_points) if data else 0
                    refused.append((count, f"{metric.name}: {data} is not supported"))
                    continue
                if not resource_uuid:
                    refused.append(
                        (
                            len(data_points),
                            f"{RESOURCE_ATTRIBUTE} resource attribute is missing",
                        )
                    )
                    continue
                for data_point in data_points:
                    if (
                        data_point.WhichOneof("value") is None
                        or data_point.flags & NO_RECORDED_VALUE
                    ):
                        # Recording 0 would be a wrong level, and for a
                        # running total a false restart.
                        refused.append((1, f"{metric.name}: data point has no value"))
                        continue
                    try:
                        attributes = _attributes(data_point.attributes)
                    except ValueError:
                        refused.append(
                            (1, f"{metric.name}: unsupported attribute value")
                        )
                        continue
                    points.append(
                        ingest.Point(
                            resource_uuid=str(resource_uuid),
                            metric=metric.name,
                            timestamp=_time(data_point.time_unix_nano),
                            value=_number(data_point),
                            attributes=attributes,
                            start_time=(
                                _time(data_point.start_time_unix_nano)
                                if cumulative
                                else None
                            ),
                            reported_kind=kind,
                        )
                    )
    return points, refused


def response(rejected_count, error_message, content_type):
    message = metrics_service_pb2.ExportMetricsServiceResponse()
    if rejected_count:
        message.partial_success.rejected_data_points = rejected_count
        message.partial_success.error_message = error_message
    if content_type.startswith(PROTOBUF):
        return message.SerializeToString(), PROTOBUF
    return json_format.MessageToJson(message).encode(), JSON
