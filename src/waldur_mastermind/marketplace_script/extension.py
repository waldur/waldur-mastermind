from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_SCRIPT = {
            'DOCKER_CLIENT': {
                'base_url': None,
            },
            'DOCKER_RUN_OPTIONS': {
                'mem_limit': '64m',
            },
            'DOCKER_IMAGES': {
                'python:3.7-alpine': 'python',
                'alpine:3.10.0': 'sh',
            },
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_script'

    @staticmethod
    def is_assembly():
        return True
