from celery import shared_task
from django.conf import settings

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_script import serializers, utils, PLUGIN_NAME


@shared_task(name='waldur_marketplace_script.pull_resources')
def pull_resources():
    for resource in models.Resource.objects.filter(
        offering__type=PLUGIN_NAME,
        offering__plugin_options__has_key='pull',
        state__in=[models.Resource.States.OK, models.Resource.States.ERRED]
    ):
        pull_resource.delay(resource.id)


@shared_task
def pull_resource(resource_id):
    resource = models.Resource.objects.get(id=resource_id)
    options = resource.offering.plugin_options

    serializer = serializers.ResourceSerializer(instance=resource)
    environment = {key.upper(): str(value) for key, value in serializer.data}
    if isinstance(options.get('environ'), dict):
        environment.update(options['environ'])

    language = options['language']
    image = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(language)
    utils.execute_script(
        image=image,
        command=language,
        src=options['pull'],
        environment=environment
    )
