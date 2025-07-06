from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType


def log_roll_back_customer_credit(customer, old_value, new_value):
    event_logger.emit(
        "Customer credit for {customer_name} has been rolled back from {old_value} to {new_value}.",
        event_type=EventType.ROLL_BACK_CUSTOMER_CREDIT,
        event_context={
            "old_value": int(old_value),
            "new_value": int(new_value),
            "customer": customer,
        },
        scopes=[customer],
    )


def log_roll_back_project_credit(customer, project, old_value, new_value):
    event_logger.emit(
        "Project credit for {project_name} has been rolled back from {old_value} to {new_value}.",
        event_type=EventType.ROLL_BACK_PROJECT_CREDIT,
        event_context={
            "old_value": int(old_value),
            "new_value": int(new_value),
            "customer": customer,
            "project": project,
        },
        scopes=[customer, project],
    )


def log_changing_of_offerings(customer, old_offerings, new_offerings):
    old_offerings_names = (
        ", ".join([offering.name for offering in old_offerings]) or "none"
    )
    new_offerings_names = (
        ", ".join([offering.name for offering in new_offerings]) or "none"
    )

    event_logger.emit(
        "Allowed offerings of {customer_name} have been updated from {old_offerings} to {new_offerings}.",
        event_type=EventType.ALLOWED_OFFERINGS_HAVE_BEEN_UPDATED,
        event_context={
            "old_offerings": old_offerings_names,
            "new_offerings": new_offerings_names,
            "customer": customer,
        },
        scopes=[customer],
    )
