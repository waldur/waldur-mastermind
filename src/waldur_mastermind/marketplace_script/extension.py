from waldur_core.core import WaldurExtension


class MarketplaceScriptExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_SCRIPT = {
            # See also: https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient
            'DOCKER_CLIENT': {'base_url': 'unix://var/run/docker.sock',},
            # See also: https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run
            'DOCKER_RUN_OPTIONS': {'mem_limit': '64m',},
            # Path to folder on executor machine where to create temporary submission scripts. If None uses OS-dependent location
            # OS X users, see https://github.com/docker/for-mac/issues/1532
            'DOCKER_SCRIPT_DIR': None,
            # Key is command to execute script, value is image name.
            'DOCKER_IMAGES': {'python': 'python:3.8-alpine', 'shell': 'alpine:3',},
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_script'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-marketplace-script-pull-resources': {
                'task': 'waldur_marketplace_script.pull_resources',
                'schedule': timedelta(hours=1),
                'args': (),
            },
        }
