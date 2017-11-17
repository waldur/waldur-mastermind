import logging

from django.conf import settings
from influxdb import InfluxDBClient, exceptions


logger = logging.getLogger(__name__)


def get_influxdb_client():
    if settings.WALDUR_ANALYTICS['ENABLED']:
        options = settings.WALDUR_ANALYTICS['INFLUXDB']
        return InfluxDBClient(**options)


def write_points(client, points):
    try:
        client.write_points(points)
    except (exceptions.InfluxDBClientError, exceptions.InfluxDBServerError) as e:
        logger.warning('Unable to write to InfluxDB %s', e)
