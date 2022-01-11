import json
import logging
import tempfile

import docker
from django.conf import settings
from docker.errors import DockerException
from rest_framework import serializers as rf_serializers

from . import serializers

logger = logging.getLogger(__name__)


def execute_script(image, command, src, **kwargs):
    with tempfile.NamedTemporaryFile(
        prefix='docker',
        dir=settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_SCRIPT_DIR'],
        mode="w+",
    ) as docker_script:
        docker_script.write(src)
        docker_script.flush()
        client = docker.DockerClient(
            **settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_CLIENT']
        )
        return client.containers.run(
            image=image,
            command=[command, 'script'],
            remove=True,
            working_dir="/work",
            volumes={docker_script.name: {"bind": "/work/script", "mode": "ro",},},
            **settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_RUN_OPTIONS'],
            **kwargs,
        )


class DockerExecutorMixin:
    hook_type = NotImplemented

    def send_request(self, user, resource=None):
        options = self.order_item.offering.secret_options

        serializer = serializers.OrderItemSerializer(instance=self.order_item)
        input_parameters = dict(
            serializer.data
        )  # drop the self-reference to serializer by converting to dict
        environment = {
            key.upper(): input_parameters[key] for key in input_parameters.keys()
        }
        # update environment with offering-specific parameters
        for opt in options.get('environ', []):
            if isinstance(opt, dict):
                environment.update({opt['name']: opt['value']})

        language = options['language']
        image = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(language)
        logger.debug(
            'About to execute marketplace script via Docker. '
            'Hook type is %s. Order item ID is %s.',
            self.hook_type,
            self.order_item.id,
        )

        try:
            environment = {
                key: json.dumps(value)
                if isinstance(value, (dict, list))
                else str(value)
                for key, value in environment.items()
            }

            self.order_item.output = str(
                execute_script(
                    image=image,
                    command=language,
                    src=options[self.hook_type],
                    environment=environment,
                ),
                'utf-8',
            )
            self.order_item.save(update_fields=['output'])
        except DockerException as exc:
            logger.exception(
                'Unable to execute marketplace script via Docker. '
                'Hook type is %s. Order item ID is %s.',
                self.hook_type,
                self.order_item.id,
            )
            raise rf_serializers.ValidationError(str(exc))
        logger.debug(
            'Successfully executed marketplace script via Docker.'
            'Hook type is %s. Order item ID is %s.',
            self.hook_type,
            self.order_item.id,
        )
        return self.order_item.output

    def validate_order_item(self, request):
        options = self.order_item.offering.secret_options

        if self.hook_type not in options:
            raise rf_serializers.ValidationError('Script is not defined.')

        command = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(
            options['language']
        )
        if not command:
            raise rf_serializers.ValidationError('Docker image is not allowed.')
