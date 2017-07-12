from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class ExpertsExtension(NodeConductorExtension):
    class Settings:
        WALDUR_EXPERTS = {
            'REQUEST_LINK_TEMPLATE': 'https://www.example.com/#/experts/{uuid}/'
        }

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.experts'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def celery_tasks():
        return {}
