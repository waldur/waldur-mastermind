from waldur_core.core import WaldurExtension


class MarketplaceVMwareExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_vmware'

    @staticmethod
    def is_assembly():
        return True
