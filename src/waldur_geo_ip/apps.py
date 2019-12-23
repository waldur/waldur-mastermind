from django.apps import AppConfig
from django_fsm import signals as fsm_signals


class GeoIPConfig(AppConfig):
    name = 'waldur_geo_ip'

    def ready(self):
        from waldur_geo_ip.mixins import IPCoordinatesMixin
        from . import handlers

        for index, model in enumerate(IPCoordinatesMixin.get_all_models()):
            fsm_signals.post_transition.connect(
                handlers.detect_vm_coordinates,
                sender=model,
                dispatch_uid='waldur_geo_ip.handlers.detect_vm_coordinates_{}_{}'.format(
                    model.__name__, index),
            )
