from waldur_core.core import WaldurExtension


class PackagesExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.packages'

    @staticmethod
    def is_assembly():
        return True
