from enum import StrEnum
from typing import Literal


class RoleEnum(StrEnum):
    CUSTOMER_OWNER = "CUSTOMER.OWNER"
    CUSTOMER_SUPPORT = "CUSTOMER.SUPPORT"
    CUSTOMER_MANAGER = "CUSTOMER.MANAGER"
    CUSTOMER_READER = "CUSTOMER.READER"

    PROJECT_ADMIN = "PROJECT.ADMIN"
    PROJECT_MANAGER = "PROJECT.MANAGER"
    PROJECT_MEMBER = "PROJECT.MEMBER"

    OFFERING_MANAGER = "OFFERING.MANAGER"
    CALL_REVIEWER = "CALL.REVIEWER"
    CALL_MANAGER = "CALL.MANAGER"

    PROPOSAL_MEMBER = "PROPOSAL.MEMBER"
    PROPOSAL_MANAGER = "PROPOSAL.MANAGER"


SYSTEM_CUSTOMER_ROLES = (
    RoleEnum.CUSTOMER_MANAGER,
    RoleEnum.CUSTOMER_OWNER,
    RoleEnum.CUSTOMER_SUPPORT,
)

SYSTEM_PROJECT_ROLES = (
    RoleEnum.PROJECT_ADMIN,
    RoleEnum.PROJECT_MANAGER,
    RoleEnum.PROJECT_MEMBER,
)


TYPE_MAP = {
    "customer": ("structure", "customer"),
    "service_provider": ("marketplace", "serviceprovider"),
    "call_organizer": ("proposal", "callmanagingorganisation"),
    "project": ("structure", "project"),
    "offering": ("marketplace", "offering"),
    "call": ("proposal", "call"),
    "proposal": ("proposal", "proposal"),
}

TYPE_KEYS = Literal[
    "customer",
    "service_provider",
    "call_organizer",
    "project",
    "offering",
    "call",
    "proposal",
]


class PermissionEnum(StrEnum):
    REGISTER_SERVICE_PROVIDER = "SERVICE_PROVIDER.REGISTER"

    CREATE_OFFERING = "OFFERING.CREATE"
    DELETE_OFFERING = "OFFERING.DELETE"
    UPDATE_OFFERING_THUMBNAIL = "OFFERING.UPDATE_THUMBNAIL"
    UPDATE_OFFERING = "OFFERING.UPDATE"
    UPDATE_OFFERING_ATTRIBUTES = "OFFERING.UPDATE_ATTRIBUTES"
    UPDATE_OFFERING_LOCATION = "OFFERING.UPDATE_LOCATION"
    UPDATE_OFFERING_DESCRIPTION = "OFFERING.UPDATE_DESCRIPTION"
    UPDATE_OFFERING_OPTIONS = "OFFERING.UPDATE_OPTIONS"
    UPDATE_OFFERING_INTEGRATION = "OFFERING.UPDATE_INTEGRATION"
    ADD_OFFERING_ENDPOINT = "OFFERING.ADD_ENDPOINT"
    DELETE_OFFERING_ENDPOINT = "OFFERING.DELETE_ENDPOINT"
    UPDATE_OFFERING_COMPONENTS = "OFFERING.UPDATE_COMPONENTS"
    PAUSE_OFFERING = "OFFERING.PAUSE"
    UNPAUSE_OFFERING = "OFFERING.UNPAUSE"
    ARCHIVE_OFFERING = "OFFERING.ARCHIVE"
    DRY_RUN_OFFERING_SCRIPT = "OFFERING.DRY_RUN_SCRIPT"
    MANAGE_CAMPAIGN = "OFFERING.MANAGE_CAMPAIGN"
    MANAGE_OFFERING_USER_GROUP = "OFFERING.MANAGE_USER_GROUP"
    CREATE_OFFERING_PLAN = "OFFERING.CREATE_PLAN"
    UPDATE_OFFERING_PLAN = "OFFERING.UPDATE_PLAN"
    ARCHIVE_OFFERING_PLAN = "OFFERING.ARCHIVE_PLAN"
    CREATE_OFFERING_SCREENSHOT = "OFFERING.CREATE_SCREENSHOT"
    UPDATE_OFFERING_SCREENSHOT = "OFFERING.UPDATE_SCREENSHOT"
    DELETE_OFFERING_SCREENSHOT = "OFFERING.DELETE_SCREENSHOT"
    CREATE_OFFERING_USER = "OFFERING.CREATE_USER"
    UPDATE_OFFERING_USER = "OFFERING.UPDATE_USER"
    DELETE_OFFERING_USER = "OFFERING.DELETE_USER"
    MANAGE_OFFERING_USER_ROLE = "OFFERING.MANAGE_USER_ROLE"
    CREATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.CREATE_ROBOT_ACCOUNT"
    UPDATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.UPDATE_ROBOT_ACCOUNT"
    DELETE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.DELETE_ROBOT_ACCOUNT"

    LIST_ORDERS = "ORDER.LIST"
    CREATE_ORDER = "ORDER.CREATE"
    APPROVE_PRIVATE_ORDER = "ORDER.APPROVE_PRIVATE"
    APPROVE_ORDER = "ORDER.APPROVE"
    REJECT_ORDER = "ORDER.REJECT"
    DESTROY_ORDER = "ORDER.DESTROY"
    CANCEL_ORDER = "ORDER.CANCEL"
    SET_CONSUMER_ORDER_INFO = "ORDER.SET_CONSUMER_INFO"

    LIST_RESOURCES = "RESOURCE.LIST"
    UPDATE_RESOURCE = "RESOURCE.UPDATE"
    TERMINATE_RESOURCE = "RESOURCE.TERMINATE"
    LIST_IMPORTABLE_RESOURCES = "RESOURCE.LIST_IMPORTABLE"
    SET_RESOURCE_END_DATE = "RESOURCE.SET_END_DATE"
    SET_RESOURCE_USAGE = "RESOURCE.SET_USAGE"
    SWITCH_RESOURCE_PLAN = "RESOURCE.SET_PLAN"
    UPDATE_RESOURCE_LIMITS = "RESOURCE.SET_LIMITS"
    SET_RESOURCE_BACKEND_ID = "RESOURCE.SET_BACKEND_ID"
    SUBMIT_RESOURCE_REPORT = "RESOURCE.SUBMIT_REPORT"
    SET_RESOURCE_BACKEND_METADATA = "RESOURCE.SET_BACKEND_METADATA"
    SET_RESOURCE_STATE = "RESOURCE.SET_STATE"
    UPDATE_RESOURCE_OPTIONS = "RESOURCE.UPDATE_OPTIONS"
    ACCEPT_BOOKING_REQUEST = "RESOURCE.ACCEPT_BOOKING_REQUEST"
    REJECT_BOOKING_REQUEST = "RESOURCE.REJECT_BOOKING_REQUEST"
    MANAGE_RESOURCE_USERS = "RESOURCE.MANAGE_USERS"
    RESOURCE_CONSUMPTION_LIMITATION = "RESOURCE.CONSUMPTION_LIMITATION"
    MANAGE_OFFERING_BACKEND_RESOURCES = "OFFERING.MANAGE_BACKEND_RESOURCES"

    GET_SERVICE_PROVIDER_API_SECRET_CODE = "SERVICE_PROVIDER.GET_API_SECRET_CODE"
    GENERATE_SERVICE_PROVIDER_API_SECRET_CODE = (
        "SERVICE_PROVIDER.GENERATE_API_SECRET_CODE"
    )
    LIST_SERVICE_PROVIDER_CUSTOMERS = "SERVICE_PROVIDER.LIST_CUSTOMERS"
    LIST_SERVICE_PROVIDER_CUSTOMER_PROJECTS = "SERVICE_PROVIDER.LIST_CUSTOMER_PROJECTS"
    LIST_SERVICE_PROVIDER_PROJECTS = "SERVICE_PROVIDER.LIST_PROJECTS"
    LIST_SERVICE_PROVIDER_PROJECT_PERMISSIONS = (
        "SERVICE_PROVIDER.LIST_PROJECT_PERMISSIONS"
    )
    LIST_SERVICE_PROVIDER_KEYS = "SERVICE_PROVIDER.LIST_KEYS"
    LIST_SERVICE_PROVIDER_USERS = "SERVICE_PROVIDER.LIST_USERS"
    LIST_SERVICE_PROVIDER_USER_CUSTOMERS = "SERVICE_PROVIDER.LIST_USER_CUSTOMERS"
    LIST_SERVICE_PROVIDER_SERVICE_ACCOUNTS = "SERVICE_PROVIDER.LIST_SERVICE_ACCOUNTS"
    LIST_SERVICE_PROVIDER_COURSE_ACCOUNTS = "SERVICE_PROVIDER.LIST_COURSE_ACCOUNTS"
    SET_SERVICE_PROVIDER_OFFERINGS_USERNAME = "SERVICE_PROVIDER.SET_OFFERINGS_USERNAME"
    GET_SERVICE_PROVIDER_STATISTICS = "SERVICE_PROVIDER.GET_STATISTICS"
    GET_SERVICE_PROVIDER_REVENUE = "SERVICE_PROVIDER.GET_REVENUE"
    GET_SERVICE_PROVIDER_ROBOT_ACCOUNT_CUSTOMERS = (
        "SERVICE_PROVIDER.GET_ROBOT_ACCOUNT_CUSTOMERS"
    )
    GET_SERVICE_PROVIDER_ROBOT_ACCOUNT_PROJECTS = (
        "SERVICE_PROVIDER.GET_ROBOT_ACCOUNT_PROJECTS"
    )

    CREATE_PROJECT_PERMISSION = "PROJECT.CREATE_PERMISSION"
    CREATE_CUSTOMER_PERMISSION = "CUSTOMER.CREATE_PERMISSION"
    CREATE_OFFERING_PERMISSION = "OFFERING.CREATE_PERMISSION"
    CREATE_CALL_PERMISSION = "CALL.CREATE_PERMISSION"
    MANAGE_PROPOSAL = "PROPOSAL.MANAGE"
    MANAGE_PROPOSAL_REVIEW = "PROPOSAL.MANAGE_REVIEW"

    UPDATE_PROJECT_PERMISSION = "PROJECT.UPDATE_PERMISSION"
    UPDATE_CUSTOMER_PERMISSION = "CUSTOMER.UPDATE_PERMISSION"
    UPDATE_OFFERING_PERMISSION = "OFFERING.UPDATE_PERMISSION"
    UPDATE_CALL_PERMISSION = "CALL.UPDATE_PERMISSION"
    UPDATE_PROPOSAL_PERMISSION = "PROPOSAL.UPDATE_PERMISSION"

    DELETE_PROJECT_PERMISSION = "PROJECT.DELETE_PERMISSION"
    DELETE_CUSTOMER_PERMISSION = "CUSTOMER.DELETE_PERMISSION"
    DELETE_OFFERING_PERMISSION = "OFFERING.DELETE_PERMISSION"
    DELETE_CALL_PERMISSION = "CALL.DELETE_PERMISSION"
    DELETE_PROPOSAL_PERMISSION = "PROPOSAL.DELETE_PERMISSION"

    CREATE_LEXIS_LINK = "LEXIS_LINK.CREATE"
    DELETE_LEXIS_LINK = "LEXIS_LINK.DELETE"

    LIST_PROJECTS = "PROJECT.LIST"
    CREATE_PROJECT = "PROJECT.CREATE"
    DELETE_PROJECT = "PROJECT.DELETE"
    UPDATE_PROJECT = "PROJECT.UPDATE"
    UPDATE_PROJECT_METADATA = "PROJECT.UPDATE_METADATA"

    REVIEW_PROJECT_MEMBERSHIP = "PROJECT.REVIEW_MEMBERSHIP"

    UPDATE_CUSTOMER = "CUSTOMER.UPDATE"
    CUSTOMER_CONTACT_UPDATE = "CUSTOMER.CONTACT_UPDATE"

    LIST_CUSTOMER_USERS = "CUSTOMER.LIST_USERS"

    ACCEPT_REQUESTED_OFFERING = "OFFERING.ACCEPT_CALL_REQUEST"
    APPROVE_AND_REJECT_PROPOSALS = "CALL.APPROVE_AND_REJECT_PROPOSALS"
    CLOSE_ROUNDS = "CALL.CLOSE_ROUNDS"

    CREATE_ACCESS_SUBNET = "ACCESS_SUBNET.CREATE"
    UPDATE_ACCESS_SUBNET = "ACCESS_SUBNET.UPDATE"
    DELETE_ACCESS_SUBNET = "ACCESS_SUBNET.DELETE"

    UPDATE_OFFERING_USER_RESTRICTION = "OFFERINGUSER.UPDATE_RESTRICTION"

    LIST_INVITATIONS = "INVITATION.LIST"
    LIST_CUSTOMER_PERMISSION_REVIEWS = "CUSTOMER.LIST_PERMISSION_REVIEWS"

    LIST_CALLS = "CALL.LIST"
    CREATE_CALL = "CALL.CREATE"
    UPDATE_CALL = "CALL.UPDATE"

    LIST_ROUNDS = "ROUND.LIST"
    LIST_PROPOSALS = "PROPOSAL.LIST"

    MANAGE_SERVICE_ACCOUNT = "SERVICE_ACCOUNT.MANAGE"

    MANAGE_COURSE_ACCOUNT = "PROJECT.COURSE_ACCOUNT_MANAGE"

    SERVICE_PROVIDER_OPENSTACK_IMAGE_MANAGEMENT = (
        "SERVICE_PROVIDER.OPENSTACK_IMAGE_MANAGEMENT"
    )

    # OpenStack Instance permissions
    HAS_OPENSTACK_INSTANCE_CONSOLE_ACCESS = "OPENSTACK_INSTANCE.CONSOLE_ACCESS"
    CAN_MANAGE_OPENSTACK_INSTANCE_POWER = "OPENSTACK_INSTANCE.MANAGE_POWER"
    CAN_MANAGE_OPENSTACK_INSTANCE = "OPENSTACK_INSTANCE.MANAGE"

    # OpenStack Router permissions
    CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY = "OPENSTACK_ROUTER.MANAGE_GATEWAY"

    # Staff/support access scopes for PATs
    STAFF_ACCESS = "STAFF.ACCESS"
    SUPPORT_ACCESS = "SUPPORT.ACCESS"


CREATE_PERMISSIONS = {
    "customer": PermissionEnum.CREATE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.CREATE_PROJECT_PERMISSION,
    "offering": PermissionEnum.CREATE_OFFERING_PERMISSION,
    "call": PermissionEnum.CREATE_CALL_PERMISSION,
    "proposal": PermissionEnum.MANAGE_PROPOSAL,
    "call_organizer": PermissionEnum.CREATE_CALL_PERMISSION,
    "service_provider": PermissionEnum.CREATE_CUSTOMER_PERMISSION,
}


UPDATE_PERMISSIONS = {
    "customer": PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.UPDATE_PROJECT_PERMISSION,
    "offering": PermissionEnum.UPDATE_OFFERING_PERMISSION,
    "call": PermissionEnum.UPDATE_CALL_PERMISSION,
    "proposal": PermissionEnum.UPDATE_PROPOSAL_PERMISSION,
    "call_organizer": PermissionEnum.UPDATE_CALL_PERMISSION,
    "service_provider": PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
}


DELETE_PERMISSIONS = {
    "customer": PermissionEnum.DELETE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.DELETE_PROJECT_PERMISSION,
    "offering": PermissionEnum.DELETE_OFFERING_PERMISSION,
    "call": PermissionEnum.DELETE_CALL_PERMISSION,
    "proposal": PermissionEnum.DELETE_PROPOSAL_PERMISSION,
    "call_organizer": PermissionEnum.DELETE_CALL_PERMISSION,
    "service_provider": PermissionEnum.DELETE_CUSTOMER_PERMISSION,
}


def generate_permission_description():
    # Group permissions by category (based on prefix before the dot)
    categories = {}

    for perm in PermissionEnum:
        # Split the permission value to get category (e.g., "OFFERING" from "OFFERING.CREATE")
        parts = perm.value.split(".")
        if len(parts) != 2:
            continue  # Skip invalid format

        category, action = parts

        # Determine which category this permission belongs to
        display_category = categorize_permission(category, action)

        if display_category not in categories:
            categories[display_category] = []

        # Create a human-readable label
        label = create_readable_label(category, action)

        categories[display_category].append(
            {
                "label": label,
                "value": perm.value,
            }
        )

    # Create the PERMISSION_DESCRIPTION structure
    result = []
    for category, options in categories.items():
        # Check for duplicate labels and fix them
        label_counts = {}
        for option in options:
            label = option["label"]
            label_counts[label] = label_counts.get(label, 0) + 1

        # For any duplicate labels, modify them to include more context
        for i, option in enumerate(options):
            if label_counts[option["label"]] > 1:
                # Get the original category and action from the value
                orig_category, orig_action = option["value"].split(".")
                # Include the category name explicitly in the label
                options[i]["label"] = capitalize_first_word(
                    f"{option['label']} {orig_category.lower()}"
                )

        result.append(
            {"label": category, "options": sorted(options, key=lambda x: x["label"])}
        )

    return sorted(result, key=lambda x: x["label"])


def categorize_permission(category, action):
    """Map a permission category to a display category"""
    category_mapping = {
        "OFFERING": "Offering",
        "ORDER": "Order",
        "RESOURCE": "Provider actions"
        if action
        in [
            "SET_USAGE",
            "SET_BACKEND_ID",
            "SUBMIT_REPORT",
            "SET_END_DATE",
            "SET_STATE",
            "SET_BACKEND_METADATA",
            "CREATE_ROBOT_ACCOUNT",
            "UPDATE_ROBOT_ACCOUNT",
            "DELETE_ROBOT_ACCOUNT",
            "MANAGE_USERS",
        ]
        else "Customer actions for resources",
        "SERVICE_PROVIDER": "Provider actions",
        "INVITATION": "Team members",
        "CUSTOMER": "Team members" if "PERMISSION" in action else "Customer",
        "PROJECT": "Team members" if "PERMISSION" in action else "Project",
        "CALL": "Call management",
        "ROUND": "Call management",
        "PROPOSAL": "Call management",
        "SERVICE_ACCOUNT": "Provider actions",
        "LEXIS_LINK": "Other",
        "ACCESS_SUBNET": "Other",
        "OFFERINGUSER": "Offering",
        "OPENSTACK_INSTANCE": "Openstack",
        "OPENSTACK_ROUTER": "Openstack",
    }

    return category_mapping.get(category, category.capitalize())


def create_readable_label(category, action):
    """Create a readable label for a permission with only first word capitalized"""
    # Replace underscores with spaces
    words = action.replace("_", " ").lower().split()

    # If action is just a single word (like "CREATE"), include the category name
    if len(words) == 1:
        readable_category = category.lower().replace("_", " ")
        return capitalize_first_word(f"{words[0]} {readable_category}")

    # Special case transformations
    if category == "SERVICE_PROVIDER":
        # Transform "SERVICE_PROVIDER.GET_REVENUE" to "View provider revenue"
        if action.startswith("GET_"):
            return capitalize_first_word(f"View {action[4:].lower().replace('_', ' ')}")
        elif action.startswith("LIST_"):
            return capitalize_first_word(f"List {action[5:].lower().replace('_', ' ')}")
        elif action.startswith("SET_"):
            return capitalize_first_word(
                f"Manage {action[4:].lower().replace('_', ' ')}"
            )
        elif action.startswith("GENERATE_"):
            return capitalize_first_word(
                f"Generate {action[9:].lower().replace('_', ' ')}"
            )
        else:
            # Default format for other service provider actions
            return capitalize_first_word(
                f"{action.lower().replace('_', ' ')} for service provider"
            )

    # Handle the common cases
    if action.startswith("CREATE_"):
        prefix = "Create"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("UPDATE_"):
        prefix = "Update"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("DELETE_"):
        prefix = "Delete"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("LIST_"):
        prefix = "List"
        object_name = action[5:].lower().replace("_", " ")
    elif action.startswith("MANAGE_"):
        prefix = "Manage"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("GET_"):
        prefix = "View"
        object_name = action[4:].lower().replace("_", " ")
    elif action.startswith("SET_"):
        prefix = "Set"
        object_name = action[4:].lower().replace("_", " ")
    elif action.startswith("ACCEPT_"):
        prefix = "Accept"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("REJECT_"):
        prefix = "Reject"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("APPROVE_"):
        prefix = "Approve"
        object_name = action[8:].lower().replace("_", " ")
    elif action.startswith("CANCEL_"):
        prefix = "Cancel"
        object_name = action[7:].lower().replace("_", " ")
    elif action.startswith("CLOSE_"):
        prefix = "Close"
        object_name = action[6:].lower().replace("_", " ")
    elif action.startswith("TERMINATE_"):
        prefix = "Terminate"
        object_name = action[10:].lower().replace("_", " ")
    elif action == "CREATE" or action == "UPDATE" or action == "DELETE":
        # For single-word verbs, include the category
        return capitalize_first_word(f"{action.lower()} {category.lower()}")
    else:
        # For cases that don't follow the common patterns
        if len(words) == 1:  # If it's just a single action word
            return capitalize_first_word(f"{words[0]} {category.lower()}")
        return capitalize_first_word(" ".join(words))

    # If the object name is empty after removing the prefix, include the category name
    if not object_name.strip():
        object_name = category.lower().replace("_", " ")

    # Determine if we need to include the category name
    if category.lower() not in object_name.lower() and category not in [
        "CUSTOMER",
        "PROJECT",
        "OFFERING",
        "CALL",
        "PROPOSAL",
    ]:
        category_name = category.lower().replace("_", " ")
        if "robot" in object_name and "account" in object_name:
            return capitalize_first_word(f"{prefix} robot account")
        else:
            return capitalize_first_word(f"{prefix} {object_name} for {category_name}")

    # Special handlers for common terms
    if "robot account" in object_name:
        return capitalize_first_word(f"{prefix} robot account")

    return capitalize_first_word(f"{prefix} {object_name}")


def capitalize_first_word(text):
    """Capitalize only the first word in a string"""
    if not text:
        return text
    first_letter = text[0].upper()
    rest_of_text = text[1:]
    return first_letter + rest_of_text


# Example usage
PERMISSION_DESCRIPTION = generate_permission_description()
