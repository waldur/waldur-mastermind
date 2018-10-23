from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class SupportInvoicesExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.support_invoices'

    @staticmethod
    def is_assembly():
        return True
