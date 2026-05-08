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
from waldur_core.core.enums import CoreStates
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure import models as structure_models
from waldur_core.structure.exceptions import (
    ServiceBackendError,
    ServiceBackendNotImplemented,
)
from waldur_core.structure.managers import count_customer_users
from waldur_core.structure.models import ProjectEndDateChangeRequest
from waldur_core.structure.registry import SupportedServices

logger = logging.getLogger(__name__)


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, service_settings, *args, **kwargs):
        try:
            return func(self, service_settings, *args, **kwargs)
        except Exception as e:
            raise e.__class__(
                f"{e}, Service settings: {service_settings.name}, {service_settings.type}"
            )

    return wrapped


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

    def pull(self, instance):
        """Pull instance from backend.

        This method should not handle backend exception.
        """
        raise NotImplementedError("Pull task should implement pull method.")

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
        if instance.state == CoreStates.ERRED:
            instance.recover()
            instance.error_message = ""
            instance.save(update_fields=["state", "error_message"])

    def log_error_message(self, instance, error_message):
        logger_message = f"Failed to pull {instance.__class__.__name__} {instance.name} (PK: {instance.pk}). Error: {error_message}"
        if (
            instance.state == CoreStates.ERRED
        ):  # report error on debug level if instance already was erred.
            logger.debug(logger_message)
        else:
            logger.error(logger_message, exc_info=True)

    def set_instance_erred(self, instance, error_message):
        """Mark instance as erred and save error message"""
        instance.set_erred()
        instance.error_message = error_message
        instance.save(update_fields=["state", "error_message"])


class BackgroundListPullTask(core_tasks.BackgroundTask):
    """Schedules pull task for each stable object of the model."""

    model = NotImplemented
    pull_task = NotImplemented

    def get_pulled_objects(self):
        return self.model.objects.filter(
            state__in=[CoreStates.ERRED, CoreStates.OK]
        ).exclude(backend_id="")

    def run(self):
        # Client-side chunked iteration: avoids server-side cursors which
        # break with PgBouncer transaction pooling / load-balanced PG.
        for instance in core_utils.chunked_queryset(
            self.get_pulled_objects(), chunk_size=50, max_records=50_000
        ):
            serialized = core_utils.serialize_instance(instance)
            self.pull_task().apply_async(args=(serialized,), kwargs={})


class ServiceListPullTask(BackgroundListPullTask):
    model = structure_models.ServiceSettings

    def get_pulled_objects(self):
        return self.model.objects.filter(
            state__in=[CoreStates.ERRED, CoreStates.OK],
            is_active=True,
            type__in=[k[0] for k in SupportedServices.get_choices()],
        )


class ServicePropertiesPullTask(BackgroundPullTask):
    def pull(self, service_settings):
        backend = service_settings.get_backend()
        backend.pull_service_properties()


class ServiceResourcesPullTask(BackgroundPullTask):
    @reraise_exceptions
    def pull(self, service_settings):
        try:
            backend = service_settings.get_backend()
        except ServiceBackendNotImplemented:
            logger.info(
                "Service % has not backend so ServiceResourcePullTask cannot execute.",
                service_settings,
            )
        else:
            backend.pull_resources()


class RetryUntilAvailableTask(core_tasks.Task):
    max_retries = 300
    default_retry_delay = 5

    def pre_execute(self, instance):
        if not self.is_available(instance):
            self.retry()
        super().pre_execute(instance)

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
            state=CoreStates.CREATING,
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

    name = "waldur_core.structure.SetErredStuckResources"

    def run(self):
        cutoff = timezone.now() - timedelta(hours=3)
        states = (
            CoreStates.CREATING,
            CoreStates.CREATION_SCHEDULED,
        )
        for model in structure_models.BaseResource.get_all_models():
            for resource in model.objects.filter(modified__lt=cutoff, state__in=states):
                resource.set_erred()
                resource.error_message = "Provisioning has timed out."
                resource.save(update_fields=["state", "error_message"])
                logger.warning(
                    "Switching resource %s to erred state, "
                    "because provisioning has timed out.",
                    core_utils.serialize_instance(resource),
                )


@shared_task
def send_change_email_notification(request_serialized):
    request = core_utils.deserialize_instance(request_serialized)
    link = core_utils.format_homeport_link(
        "user_email_change/{code}/", code=request.uuid.hex
    )
    context = {"request": request, "link": link}
    core_utils.broadcast_mail(
        "structure", "change_email_request", context, [request.email]
    )


@shared_task(name="waldur_core.structure.create_customer_permission_reviews")
def create_customer_permission_reviews():
    """Create customer permission reviews for customers that need periodic review of user permissions."""
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
        if not count_customer_users(customer):
            continue
        review = structure_models.CustomerPermissionReview.objects.create(
            customer=customer
        )
        event_logger.emit(
            "Customer permission review has been created for organization %s."
            % customer.name,
            event_type=EventType.CUSTOMER_PERMISSION_REVIEW_CREATED,
            event_context={"customer_permission_review": review},
            scopes=[customer],
        )


@shared_task(name="waldur_core.structure.create_project_permission_reviews")
def create_project_permission_reviews():
    """Create project permission reviews for projects that need periodic review of user permissions."""
    for project in structure_models.Project.available_objects.all():
        if structure_models.ProjectPermissionReview.objects.filter(
            Q(project=project, is_pending=True)
            | Q(
                project=project,
                is_pending=False,
                closed__gte=timezone.now() - timedelta(days=90),
            )
        ).exists():
            continue
        if project.get_users().count() < 1:
            continue
        review = structure_models.ProjectPermissionReview.objects.create(
            project=project
        )
        logger.info(
            f"Project permission review has been created for project {project.name} | {project.uuid}."
        )
        event_logger.emit(
            "Project permission review has been created for project %s." % project.name,
            event_type=EventType.PROJECT_PERMISSION_REVIEW_CREATED,
            event_context={"project_permission_review": review},
            scopes=[project],
        )


@shared_task
def send_structure_role_granted_notification(
    permission_serialized, structure_serialized
):
    permission = core_utils.deserialize_instance(permission_serialized)
    structure = core_utils.deserialize_instance(structure_serialized)
    user = permission.user

    if not user.email or not user.notifications_enabled:
        return

    context = {"permission": permission, "structure": structure}
    core_utils.broadcast_mail(
        "structure", "structure_role_granted", context, [user.email]
    )


@shared_task(
    name="waldur_core.structure.send_project_end_date_change_request_notification"
)
def send_project_end_date_change_request_notification(request_uuid):
    """Notify organization owners when a project end date change request is created."""

    try:
        request = ProjectEndDateChangeRequest.objects.get(uuid=request_uuid)
    except ProjectEndDateChangeRequest.DoesNotExist:
        logger.warning(
            "Project end date change request %s not found, skipping notification",
            request_uuid,
        )
        return

    mails = request.project.customer.get_owner_mails()
    if not mails:
        logger.info(
            "No owner emails for customer %s, skipping project end date change request notification",
            request.project.customer.uuid,
        )
        return

    logger.info(
        "Sending project end date change request notification for request %s to %d recipient(s)",
        request_uuid,
        len(mails),
    )
    project_url = core_utils.format_homeport_link(
        "projects/{project_uuid}/manage/?tab=end-date-change-requests",
        project_uuid=request.project.uuid.hex,
    )
    context = {
        "project_end_date_change_request": request,
        "project_url": project_url,
    }
    core_utils.broadcast_mail(
        "structure",
        "notification_project_end_date_change_request_created",
        context,
        mails,
    )


@shared_task(
    name="waldur_core.structure.send_project_end_date_change_request_approved_notification"
)
def send_project_end_date_change_request_approved_notification(request_uuid):
    """Notify the requester when their project end date change request is approved."""

    try:
        request = ProjectEndDateChangeRequest.objects.get(uuid=request_uuid)
    except ProjectEndDateChangeRequest.DoesNotExist:
        logger.warning(
            "Project end date change request %s not found, skipping approved notification",
            request_uuid,
        )
        return

    if not request.created_by or not request.created_by.email:
        logger.info(
            "No requester email for project end date change request %s, skipping approved notification",
            request_uuid,
        )
        return

    if not request.created_by.notifications_enabled:
        logger.info(
            "Requester has notifications disabled for request %s, skipping approved notification",
            request_uuid,
        )
        return

    logger.info(
        "Sending project end date change request approved notification for request %s",
        request_uuid,
    )
    project_url = core_utils.format_homeport_link(
        "organization/{customer_uuid}/project-end-date-change-requests/",
        customer_uuid=request.project.customer.uuid.hex,
    )
    context = {
        "project_end_date_change_request": request,
        "project_url": project_url,
    }
    core_utils.broadcast_mail(
        "structure",
        "notification_project_end_date_change_request_approved",
        context,
        [request.created_by.email],
    )


@shared_task(
    name="waldur_core.structure.send_project_end_date_change_request_rejected_notification"
)
def send_project_end_date_change_request_rejected_notification(request_uuid):
    """Notify the requester when their project end date change request is rejected."""

    try:
        request = ProjectEndDateChangeRequest.objects.get(uuid=request_uuid)
    except ProjectEndDateChangeRequest.DoesNotExist:
        logger.warning(
            "Project end date change request %s not found, skipping rejected notification",
            request_uuid,
        )
        return

    if not request.created_by or not request.created_by.email:
        logger.info(
            "No requester email for project end date change request %s, skipping rejected notification",
            request_uuid,
        )
        return

    if not request.created_by.notifications_enabled:
        logger.info(
            "Requester has notifications disabled for request %s, skipping rejected notification",
            request_uuid,
        )
        return

    logger.info(
        "Sending project end date change request rejected notification for request %s",
        request_uuid,
    )
    project_url = core_utils.format_homeport_link(
        "organization/{customer_uuid}/project-end-date-change-requests/",
        customer_uuid=request.project.customer.uuid.hex,
    )
    context = {
        "project_end_date_change_request": request,
        "project_url": project_url,
    }
    core_utils.broadcast_mail(
        "structure",
        "notification_project_end_date_change_request_rejected",
        context,
        [request.created_by.email],
    )
