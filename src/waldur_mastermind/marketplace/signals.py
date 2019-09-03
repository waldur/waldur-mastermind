from django.dispatch import Signal

limit_update_succeeded = Signal(providing_args=['order_item'])
limit_update_failed = Signal(providing_args=['order_item', 'error_message'])
resource_creation_succeeded = Signal(providing_args=['instance'])
resource_update_succeeded = Signal(providing_args=['instance'])
resource_deletion_succeeded = Signal(providing_args=['instance'])
