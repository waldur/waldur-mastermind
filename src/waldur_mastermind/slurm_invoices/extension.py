from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class SlurmInvoicesExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.slurm_invoices'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True
