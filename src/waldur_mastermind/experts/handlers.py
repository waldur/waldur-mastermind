from __future__ import unicode_literals

from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import registrators as invoices_registrators

from . import models, tasks, quotas
from .log import event_logger


def update_project_quota_when_request_is_saved(sender, instance, created=False, **kwargs):
    if created or instance.tracker.has_changed('state'):
        transaction.on_commit(lambda: quotas.update_project_quota(instance.project))


def update_project_quota_when_request_is_deleted(sender, instance, **kwargs):
    transaction.on_commit(lambda: quotas.update_project_quota(instance.project))


def update_customer_quota_when_request_is_saved(sender, instance, created=False, **kwargs):
    if created or instance.tracker.has_changed('state'):
        customer = quotas.get_request_customer(instance)
        transaction.on_commit(lambda: quotas.update_customer_quota(customer))


def update_customer_quota_when_request_is_deleted(sender, instance, **kwargs):
    customer = quotas.get_request_customer(instance)
    transaction.on_commit(lambda: quotas.update_customer_quota(customer))


def update_customer_quota_when_contract_is_created(sender, instance, created=False, **kwargs):
    if created:
        customer = quotas.get_contract_customer(instance)
        transaction.on_commit(lambda: quotas.update_customer_quota(customer))


def update_customer_quota_when_contract_is_deleted(sender, instance, **kwargs):
    customer = quotas.get_contract_customer(instance)
    transaction.on_commit(lambda: quotas.update_customer_quota(customer))


def log_expert_request_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    event_logger.waldur_expert_request.info(
        'User {user_username} with full name {user_full_name} has created '
        'request for experts under {customer_name} / {project_name}.',
        event_type='expert_request_created',
        event_context={
            'expert_request': instance,
        })


def log_expert_request_state_changed(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.state == models.ExpertRequest.States.ACTIVE:
        event_logger.waldur_expert_request.info(
            'Expert request {expert_request_name} has been activated.',
            event_type='expert_request_activated',
            event_context={
                'expert_request': instance,
            })
    elif instance.state == models.ExpertRequest.States.CANCELLED:
        event_logger.waldur_expert_request.info(
            'Expert request {expert_request_name} has been cancelled.',
            event_type='expert_request_cancelled',
            event_context={
                'expert_request': instance,
            })
    elif instance.state == models.ExpertRequest.States.COMPLETED:
        event_logger.waldur_expert_request.info(
            'Expert request {expert_request_name} has been completed.',
            event_type='expert_request_completed',
            event_context={
                'expert_request': instance,
            })


def log_expert_bid_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    event_logger.waldur_expert_bid.info(
        'User {user_username} with full name {user_full_name} has created '
        'bid for request {request_name} under {customer_name} / {project_name}.',
        event_type='expert_bid_created',
        event_context={
            'expert_bid': instance,
        })


def add_completed_expert_request_to_invoice(sender, instance, created=False, **kwargs):
    if created:
        return

    state = instance.state
    if state != models.ExpertRequest.States.COMPLETED or state == instance.tracker.previous('state'):
        return

    if not instance.issue or not hasattr(instance, 'contract'):
        return

    invoices_registrators.RegistrationManager.register(instance, timezone.now())


def terminate_invoice_when_expert_request_deleted(sender, instance, **kwargs):
    invoices_registrators.RegistrationManager.terminate(instance, timezone.now())


def set_project_name_on_expert_request_creation(sender, instance, created=False, **kwargs):
    if created:
        request = instance
        request.project_name = request.project.name
        request.project_uuid = request.project.uuid.hex
        request.customer = request.project.customer
        request.save(update_fields=('project_name', 'project_uuid', 'customer'))


def update_expert_request_on_project_name_update(sender, instance, **kwargs):
    project = instance
    if project.tracker.has_changed('name'):
        models.ExpertRequest.objects.filter(project=project).update(project_name=project.name)


def set_team_name_on_expert_contract_creation(sender, instance, created=False, **kwargs):
    if created:
        contract = instance
        contract.team_name = contract.team.name
        contract.team_uuid = contract.team.uuid.hex
        contract.team_customer = contract.team.customer
        contract.save(update_fields=('team_name', 'team_uuid', 'team_customer'))


def update_expert_contract_on_project_name_update(sender, instance, **kwargs):
    project = instance
    if project.tracker.has_changed('name'):
        models.ExpertContract.objects.filter(team=project).update(team_name=project.name)


def notify_expert_providers_about_new_request(sender, instance, created=False, **kwargs):
    if created:
        transaction.on_commit(lambda:
                              tasks.send_new_request.delay(instance.uuid.hex))


def notify_customer_owners_about_new_bid(sender, instance, created=False, **kwargs):
    if created:
        transaction.on_commit(lambda:
                              tasks.send_new_bid.delay(instance.uuid.hex))


def notify_customer_owners_about_new_contract(sender, instance, created=False, **kwargs):
    if created:
        transaction.on_commit(lambda:
                              tasks.send_new_contract.delay(instance.request.uuid.hex))


def send_expert_comment_added_notification(sender, instance, created=False, **kwargs):
    # Send Expert notifications
    comment = instance

    if not created or not comment.is_public:
        return

    serialized_comment = core_utils.serialize_instance(comment)
    transaction.on_commit(lambda:
                          tasks.send_expert_comment_added_notification.delay(serialized_comment))
