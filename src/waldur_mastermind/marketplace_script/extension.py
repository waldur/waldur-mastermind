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
            # Key is image name, value is command to execute script.
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
