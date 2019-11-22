from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_DEVOPS = {
            'DOCKER_URL': None,
            'DOCKER_IMAGE_NAME': 'python:3.7-alpine',
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_script'

    @staticmethod
    def is_assembly():
        return True
