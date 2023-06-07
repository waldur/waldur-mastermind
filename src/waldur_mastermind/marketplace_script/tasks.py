from celery import shared_task
from django.conf import settings
from django.utils import timezone

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_script import PLUGIN_NAME
from waldur_mastermind.marketplace_script import models as marketplace_script_models
from waldur_mastermind.marketplace_script import serializers, utils


@shared_task(name='waldur_marketplace_script.pull_resources')
def pull_resources():
    for resource in models.Resource.objects.filter(
        offering__type=PLUGIN_NAME,
        offering__secret_options__has_key='pull',
        state__in=[models.Resource.States.OK, models.Resource.States.ERRED],
    ):
        pull_resource.delay(resource.id)


@shared_task
def pull_resource(resource_id):
    resource = models.Resource.objects.get(id=resource_id)

    # We use secret_options the same like in ContainerExecutorMixin.send_request
    options = resource.offering.secret_options

    serializer = serializers.ResourceSerializer(instance=resource)
    environment = {key.upper(): str(value) for key, value in serializer.data}
    if isinstance(options.get('environ'), dict):
        environment.update(options['environ'])

    language = options['language']
    image = settings.WALDUR_MARKETPLACE_SCRIPT['DOCKER_IMAGES'].get(language)
    utils.execute_script(
        image=image, command=language, src=options['pull'], environment=environment
    )


@shared_task
def dry_run_executor(dry_run_id):
    dry_run = marketplace_script_models.DryRun.objects.get(id=dry_run_id)
    dry_run.set_state_executing()
    dry_run.save()
    order_item = dry_run.order.items.first()
    executor = utils.ContainerExecutorMixin()
    executor.order_item = order_item
    executor.hook_type = dry_run.order_item_type
    dry_run.output = executor.send_request(dry_run.order.created_by, dry_run=True)
    dry_run.save()
    structure_models.Project.objects.filter(id=dry_run.order.project.id).delete()


@shared_task(name='waldur_marketplace_script.remove_old_dry_runs')
def remove_old_dry_runs():
    marketplace_script_models.DryRun.objects.filter(
        state=marketplace_script_models.DryRun.States.DONE,
        created__lt=timezone.now() - timezone.timedelta(days=1),
    ).delete()
