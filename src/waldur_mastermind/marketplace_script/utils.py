import tempfile

import docker
from django.conf import settings
from docker.errors import DockerException
from rest_framework import serializers as rf_serializers

from . import serializers


def execute_script(image, command, src, **kwargs):
    with tempfile.NamedTemporaryFile(prefix='docker', mode="w+") as docker_script:
        docker_script.write(src)
        docker_script.flush()
        client = docker.DockerClient(**settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_CLIENT'])
        return client.containers.run(
            image=image,
            command=[command, 'script'],
            auto_remove=True,
            working_dir="/work",
            volumes={
                docker_script.name: {
                    "bind": "/work/script",
                    "mode": "ro",
                },
            },
            **settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_RUN_OPTIONS'],
            **kwargs,
        )


class DockerExecutorMixin:
    script_name = NotImplemented

    def send_request(self, user):
        options = self.order_item.offering.plugin_options[self.script_name]
        serializer = serializers.OrderItemSerializer(instance=self.order_item)
        environment = {key.upper(): str(value) for key, value in serializer.data}

        image = options['image']
        command = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(image)

        try:
            execute_script(
                image=image,
                command=command,
                src=options['script'],
                environment=environment
            )
        except DockerException as exc:
            raise rf_serializers.ValidationError(str(exc))

    def validate_order_item(self, request):
        options = self.order_item.offering.plugin_options.get(self.script_name)
        if not options:
            raise rf_serializers.ValidationError('Script options are not defined.')

        command = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(options['image'])
        if not command:
            raise rf_serializers.ValidationError('Docker image is not allowed.')
