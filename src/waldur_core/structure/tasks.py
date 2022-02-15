import functools
import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.db.utils import DatabaseError
from django.utils import timezone

from waldur_core.core import models as core_models
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure.exceptions import ServiceBackendError

logger = logging.getLogger(__name__)


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, service_settings, *args, **kwargs):
        try:
            return func(self, service_settings, *args, **kwargs)
        except Exception as e:
            raise e.__class__(
                '%s, Service settings: %s, %s'
                % (e, service_settings.name, service_settings.type)
            )

    return wrapped


@shared_task(name='waldur_core.structure.check_expired_permissions')
def check_expired_permissions():
    for cls in structure_models.BasePermission.get_all_models():
        for permission in cls.get_expired():
            permission.revoke()


class BackgroundPullTask(core_tasks.BackgroundTask):
    """Pull information about object from backend. Method "pull" should be implemented.

    Task marks object as ERRED if pull failed and recovers it if pull succeed.
    """

    def run(self, serialized_instance):
        instance = core_utils.deserialize_instance(serialized_instance)
        try:
            self.pull(instance)
        except ServiceBackendError as e:
            self.on_pull_fail(instance, e)
        else:
            self.on_pull_success(instance)

    def is_equal(self, other_task, serialized_instance):
        return self.name == other_task.get(
            'name'
        ) and serialized_instance in other_task.get('args', [])

    def pull(self, instance):
        """Pull instance from backend.

        This method should not handle backend exception.
        """
        raise NotImplementedError('Pull task should implement pull method.')

    def on_pull_fail(self, instance, error):
        error_message = str(error)
        self.log_error_message(instance, error_message)
        try:
            self.set_instance_erred(instance, error_message)
        except DatabaseError as e:
            logger.debug(e, exc_info=True)

    def on_pull_success(self, instance):
        if not isinstance(instance, core_models.StateMixin):
            return
        if instance.state == instance.States.ERRED:
            instance.recover()
            instance.error_message = ''
            instance.save(update_fields=['state', 'error_message'])

    def log_error_message(self, instance, error_message):
        logger_message = 'Failed to pull %s %s (PK: %s). Error: %s' % (
            instance.__class__.__name__,
            instance.name,
            instance.pk,
            error_message,
        )
        if (
            instance.state == instance.States.ERRED
        ):  # report error on debug level if instance already was erred.
            logger.debug(logger_message)
        else:
            logger.error(logger_message, exc_info=True)

    def set_instance_erred(self, instance, error_message):
        """Mark instance as erred and save error message"""
        instance.set_erred()
        instance.error_message = error_message
        instance.save(update_fields=['state', 'error_message'])


class BackgroundListPullTask(core_tasks.BackgroundTask):
    """Schedules pull task for each stable object of the model."""

    model = NotImplemented
    pull_task = NotImplemented

    def is_equal(self, other_task):
        return self.name == other_task.get('name')

    def get_pulled_objects(self):
        States = self.model.States
        return self.model.objects.filter(state__in=[States.ERRED, States.OK]).exclude(
            backend_id=''
        )

    def run(self):
        for instance in self.get_pulled_objects():
            serialized = core_utils.serialize_instance(instance)
            self.pull_task().apply_async(args=(serialized,), kwargs={})


class ServiceListPullTask(BackgroundListPullTask):
    model = structure_models.ServiceSettings

    def get_pulled_objects(self):
        States = self.model.States
        return self.model.objects.filter(
            state__in=[States.ERRED, States.OK], is_active=True
        )


class ServicePropertiesPullTask(BackgroundPullTask):
    def pull(self, service_settings):
        backend = service_settings.get_backend()
        backend.pull_service_properties()


class ServiceResourcesPullTask(BackgroundPullTask):
    @reraise_exceptions
    def pull(self, service_settings):
        backend = service_settings.get_backend()
        backend.pull_resources()


class ServiceSubResourcesPullTask(BackgroundPullTask):
    def pull(self, service_settings):
        backend = service_settings.get_backend()
        backend.pull_subresources()


class ServicePropertiesListPullTask(ServiceListPullTask):
    name = 'waldur_core.structure.ServicePropertiesListPullTask'
    pull_task = ServicePropertiesPullTask


class ServiceResourcesListPullTask(ServiceListPullTask):
    name = 'waldur_core.structure.ServiceResourcesListPullTask'
    pull_task = ServiceResourcesPullTask


class ServiceSubResourcesListPullTask(ServiceListPullTask):
    name = 'waldur_core.structure.ServiceSubResourcesListPullTask'
    pull_task = ServiceSubResourcesPullTask


class RetryUntilAvailableTask(core_tasks.Task):
    max_retries = 300
    default_retry_delay = 5

    def pre_execute(self, instance):
        if not self.is_available(instance):
            self.retry()
        super(RetryUntilAvailableTask, self).pre_execute(instance)

    def is_available(self, instance):
        return True


class BaseThrottleProvisionTask(RetryUntilAvailableTask):
    """
    Before starting resource provisioning, count how many resources
    are already in "creating" state and delay provisioning if there are too many of them.
    """

    DEFAULT_LIMIT = 4

    def is_available(self, resource):
        usage = self.get_usage(resource)
        limit = self.get_limit(resource)
        return usage <= limit

    def get_usage(self, resource):
        service_settings = resource.service_settings
        model_class = resource._meta.model
        return model_class.objects.filter(
            state=core_models.StateMixin.States.CREATING,
            service_settings=service_settings,
        ).count()

    def get_limit(self, resource):
        return self.DEFAULT_LIMIT


class ThrottleProvisionTask(BaseThrottleProvisionTask, core_tasks.BackendMethodTask):
    pass


class ThrottleProvisionStateTask(
    BaseThrottleProvisionTask, core_tasks.StateTransitionTask
):
    pass


class SetErredStuckResources(core_tasks.BackgroundTask):
    """
    This task marks all resources which have been provisioning for more than 3 hours as erred.
    """

    name = 'waldur_core.structure.SetErredStuckResources'

    def is_equal(self, other_task):
        return self.name == other_task.get('name')

    def run(self):
        cutoff = timezone.now() - timedelta(hours=3)
        states = (
            structure_models.BaseResource.States.CREATING,
            structure_models.BaseResource.States.CREATION_SCHEDULED,
        )
        resource_models = (
            structure_models.BaseResource.get_all_models()
            + structure_models.SubResource.get_all_models()
        )
        for model in resource_models:
            for resource in model.objects.filter(modified__lt=cutoff, state__in=states):
                resource.set_erred()
                resource.error_message = 'Provisioning has timed out.'
                resource.save(update_fields=['state', 'error_message'])
                logger.warning(
                    'Switching resource %s to erred state, '
                    'because provisioning has timed out.',
                    core_utils.serialize_instance(resource),
                )


@shared_task
def send_change_email_notification(request_serialized):
    request = core_utils.deserialize_instance(request_serialized)
    link = core_utils.format_homeport_link(
        'user_email_change/{code}/', code=request.uuid.hex
    )
    context = {'request': request, 'link': link}
    core_utils.broadcast_mail(
        'structure', 'change_email_request', context, [request.email]
    )


@shared_task(name='waldur_core.structure.create_customer_permission_reviews')
def create_customer_permission_reviews():
    for customer in structure_models.Customer.objects.all():
        # Skip customers with pending reviews or customers which recently passed permission review
        if structure_models.CustomerPermissionReview.objects.filter(
            Q(customer=customer, is_pending=True)
            | Q(
                customer=customer,
                is_pending=False,
                closed__gte=timezone.now() - timedelta(days=90),
            )
        ).exists():
            continue
        # Skip customers without users
        if not customer.get_users().count():
            continue
        structure_models.CustomerPermissionReview.objects.create(customer=customer)


@shared_task
def send_structure_role_granted_notification(
    permission_serialized, structure_serialized
):
    permission = core_utils.deserialize_instance(permission_serialized)
    structure = core_utils.deserialize_instance(structure_serialized)
    user = permission.user

    if not user.email or not user.notifications_enabled:
        return

    context = {'permission': permission, 'structure': structure}
    core_utils.broadcast_mail(
        'structure', 'structure_role_granted', context, [user.email]
    )
