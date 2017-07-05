from .log import event_logger


def log_expert_request_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    event_logger.waldur_expert_request.info(
        'Expert request {expert_request_name} has been created.',
        event_type='expert_request_created',
        event_context={
            'expert_request': instance,
        })
