from waldur_core.core import WaldurExtension


class PackagesExtension(WaldurExtension):

    class Settings:
        WALDUR_PACKAGES = {
            'BILLING_ENABLED': False,
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.packages'

    @staticmethod
    def is_assembly():
        return True
