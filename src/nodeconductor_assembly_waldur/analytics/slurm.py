from django.db.models import Sum

from waldur_slurm import models, utils


def get_usage():
    if not models.Allocation.objects.exists():
        return []
    qs = models.Allocation.objects.all()
    params = dict(('total_%s' % quota, Sum(quota)) for quota in utils.FIELD_NAMES)
    qs = qs.aggregate(**params)

    points = []
    for quota in utils.FIELD_NAMES:
        points.append({
            'measurement': 'slurm_%s' % quota,
            'fields': {
                'value': qs['total_%s' % quota]
            }
        })

    return points
