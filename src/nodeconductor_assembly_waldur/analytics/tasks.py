from celery import shared_task
from django.conf import settings
from influxdb import InfluxDBClient

from . import openstack


def get_influxdb_client():
    options = settings.WALDUR_ANALYTICS['INFLUXDB']
    return InfluxDBClient(**options)


@shared_task(name='analytics.push_points')
def push_points():
    if not settings.WALDUR_ANALYTICS['ENABLED']:
        return
    client = get_influxdb_client()
    points = []
    points.extend(openstack.get_tenants())
    points.extend(openstack.get_instances())
    client.write_points(points)
