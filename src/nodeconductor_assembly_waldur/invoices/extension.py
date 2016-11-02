from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class InvoicesExtension(NodeConductorExtension):

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.invoices'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        from celery.schedules import crontab
        return {
            'waldur-create-invoices': {
                'task': 'invoices.create_monthly_invoices_for_openstack_packages',
                'schedule': crontab(minute=0, hour=0, day_of_month='1'),
                'args': (),
            },
        }
