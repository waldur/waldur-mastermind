from waldur_core.core import WaldurExtension


class AutoprovisioningExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_autoprovisioning"
