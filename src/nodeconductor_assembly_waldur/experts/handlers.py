from .log import event_logger


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
