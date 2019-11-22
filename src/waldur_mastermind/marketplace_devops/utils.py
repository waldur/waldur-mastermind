import json

from django.conf import settings
import epicbox
from rest_framework import serializers

from . import serializers


class PythonScriptMixin:
    script_name = NotImplemented

    def send_request(self, user):
        script = self.order_item.offering.plugin_options[self.script_name].encode('utf-8')
        serializer = serializers.OrderItemSerializer(instance=self.order_item)
        stdin = json.dumps(serializer.data).encode('utf-8')

        epicbox.configure(
            profiles=[
                epicbox.Profile('python', settings.WALDUR_MARKETPLACE_DEVOPS['DOCKER_IMAGE_NAME']),
            ],
            docker_url=settings.WALDUR_MARKETPLACE_DEVOPS['DOCKER_URL'],
        )

        result = epicbox.run(
            profile_name='python',
            command='python3 main.py',
            files=[
                {
                    'name': 'main.py',
                    'content': script
                }
            ],
            stdin=stdin,
        )
        if result['exit_code'] != 0:
            raise serializers.ValidationError(result['stderr'])

    def validate_order_item(self, request):
        if self.script_name not in self.order_item.offering.plugin_options:
            raise serializers.ValidationError('Processing script is not defined.')
