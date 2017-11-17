from celery import shared_task

from . import cost_tracking, openstack, slurm, utils


@shared_task(name='analytics.push_points')
def push_points():
    client = utils.get_influxdb_client()
    if not client:
        return
    points = []
    points.extend(openstack.get_tenants())
    points.extend(openstack.get_instances())
    points.extend(cost_tracking.get_total_cost())
    points.extend(slurm.get_usage())
    utils.write_points(client, points)
