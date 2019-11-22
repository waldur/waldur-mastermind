from waldur_core.core import WaldurExtension


class RancherInvoicesExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.rancher_invoices'

    @staticmethod
    def is_assembly():
        return True
