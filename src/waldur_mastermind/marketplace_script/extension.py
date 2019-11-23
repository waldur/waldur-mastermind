from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_SCRIPT = {
            # See also: https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient
            'DOCKER_CLIENT': {
                'base_url': None,
            },
            # See also: https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run
            'DOCKER_RUN_OPTIONS': {
                'mem_limit': '64m',
            },
            # Key is command to execute script, value is image name.
            'DOCKER_IMAGES': {
                'python': 'python:3.7-alpine',
                'sh': 'alpine:3.10.0',
            },
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_script'

    @staticmethod
    def is_assembly():
        return True
