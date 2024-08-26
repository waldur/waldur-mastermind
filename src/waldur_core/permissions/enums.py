from enum import Enum


class RoleEnum(str, Enum):
    CUSTOMER_OWNER = "CUSTOMER.OWNER"
    CUSTOMER_SUPPORT = "CUSTOMER.SUPPORT"
    CUSTOMER_MANAGER = "CUSTOMER.MANAGER"

    PROJECT_ADMIN = "PROJECT.ADMIN"
    PROJECT_MANAGER = "PROJECT.MANAGER"
    PROJECT_MEMBER = "PROJECT.MEMBER"

    OFFERING_MANAGER = "OFFERING.MANAGER"
    CALL_REVIEWER = "CALL.REVIEWER"
    CALL_MANAGER = "CALL.MANAGER"
    PROPOSAL_MEMBER = "PROPOSAL.MEMBER"


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
    "project": ("structure", "project"),
    "offering": ("marketplace", "offering"),
    "call": ("proposal", "call"),
    "proposal": ("proposal", "proposal"),
}


class PermissionEnum(str, Enum):
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
    MANAGE_OFFERING_USER_ROLE = "OFFERING.MANAGE_USER_ROLE"
    CREATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.CREATE_ROBOT_ACCOUNT"
    UPDATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.UPDATE_ROBOT_ACCOUNT"
    DELETE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.DELETE_ROBOT_ACCOUNT"

    APPROVE_PRIVATE_ORDER = "ORDER.APPROVE_PRIVATE"
    APPROVE_ORDER = "ORDER.APPROVE"
    REJECT_ORDER = "ORDER.REJECT"
    DESTROY_ORDER = "ORDER.DESTROY"
    CANCEL_ORDER = "ORDER.CANCEL"

    TERMINATE_RESOURCE = "RESOURCE.TERMINATE"
    LIST_IMPORTABLE_RESOURCES = "RESOURCE.LIST_IMPORTABLE"
    SET_RESOURCE_END_DATE = "RESOURCE.SET_END_DATE"
    SET_RESOURCE_USAGE = "RESOURCE.SET_USAGE"
    SWITCH_RESOURCE_PLAN = "RESOURCE.SET_PLAN"
    UPDATE_RESOURCE_LIMITS = "RESOURCE.SET_LIMITS"
    SET_RESOURCE_BACKEND_ID = "RESOURCE.SET_BACKEND_ID"
    SUBMIT_RESOURCE_REPORT = "RESOURCE.SUBMIT_REPORT"
    UPDATE_RESOURCE_OPTIONS = "RESOURCE.UPDATE_OPTIONS"
    LIST_RESOURCE_USERS = "RESOURCE.LIST_USERS"
    COMPLETE_RESOURCE_DOWNSCALING = "RESOURCE.COMPLETE_DOWNSCALING"
    ACCEPT_BOOKING_REQUEST = "RESOURCE.ACCEPT_BOOKING_REQUEST"
    REJECT_BOOKING_REQUEST = "RESOURCE.REJECT_BOOKING_REQUEST"
    MANAGE_RESOURCE_USERS = "RESOURCE.MANAGE_USERS"

    CREATE_PROJECT_PERMISSION = "PROJECT.CREATE_PERMISSION"
    CREATE_CUSTOMER_PERMISSION = "CUSTOMER.CREATE_PERMISSION"
    CREATE_OFFERING_PERMISSION = "OFFERING.CREATE_PERMISSION"
    CREATE_CALL_PERMISSION = "CALL.CREATE_PERMISSION"
    CREATE_PROPOSAL_PERMISSION = "PROPOSAL.CREATE_PERMISSION"

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

    CREATE_PROJECT = "PROJECT.CREATE"
    DELETE_PROJECT = "PROJECT.DELETE"
    UPDATE_PROJECT = "PROJECT.UPDATE"

    CREATE_CUSTOMER = "CUSTOMER.CREATE"
    UPDATE_CUSTOMER = "CUSTOMER.UPDATE"
    DELETE_CUSTOMER = "CUSTOMER.DELETE"

    ACCEPT_REQUESTED_OFFERING = "OFFERING.ACCEPT_CALL_REQUEST"
    APPROVE_AND_REJECT_PROPOSALS = "CALL.APPROVE_AND_REJECT_PROPOSALS"
    CLOSE_ROUNDS = "CALL.CLOSE_ROUNDS"

    CREATE_ACCESS_SUBNET = "ACCESS_SUBNET.CREATE"
    UPDATE_ACCESS_SUBNET = "ACCESS_SUBNET.UPDATE"
    DELETE_ACCESS_SUBNET = "ACCESS_SUBNET.DELETE"

    UPDATE_OFFERING_USER_RESTRICTION = "OFFERINGUSER.UPDATE_RESTRICTION"


CREATE_PERMISSIONS = {
    "customer": PermissionEnum.CREATE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.CREATE_PROJECT_PERMISSION,
    "offering": PermissionEnum.CREATE_OFFERING_PERMISSION,
    "call": PermissionEnum.CREATE_CALL_PERMISSION,
    "proposal": PermissionEnum.CREATE_PROPOSAL_PERMISSION,
}


UPDATE_PERMISSIONS = {
    "customer": PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.UPDATE_PROJECT_PERMISSION,
    "offering": PermissionEnum.UPDATE_OFFERING_PERMISSION,
    "call": PermissionEnum.UPDATE_CALL_PERMISSION,
    "proposal": PermissionEnum.UPDATE_PROPOSAL_PERMISSION,
}


DELETE_PERMISSIONS = {
    "customer": PermissionEnum.DELETE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.DELETE_PROJECT_PERMISSION,
    "offering": PermissionEnum.DELETE_OFFERING_PERMISSION,
    "call": PermissionEnum.DELETE_CALL_PERMISSION,
    "proposal": PermissionEnum.DELETE_PROPOSAL_PERMISSION,
}

PERMISSION_DESCRIPTION = [
    {
        "label": "Offering",
        "options": [
            {
                "label": "Create offering",
                "value": "OFFERING.CREATE",
            },
            {
                "label": "Delete offering",
                "value": "OFFERING.DELETE",
            },
            {
                "label": "Update offering thumbnail",
                "value": "OFFERING.UPDATE_THUMBNAIL",
            },
            {
                "label": "Update offering",
                "value": "OFFERING.UPDATE",
            },
            {
                "label": "Update offering attributes",
                "value": "OFFERING.UPDATE_ATTRIBUTES",
            },
            {
                "label": "Update offering location",
                "value": "OFFERING.UPDATE_LOCATION",
            },
            {
                "label": "Update offering description",
                "value": "OFFERING.UPDATE_DESCRIPTION",
            },
            {
                "label": "Update offering options",
                "value": "OFFERING.UPDATE_OPTIONS",
            },
            {
                "label": "Add offering endpoint",
                "value": "OFFERING.ADD_ENDPOINT",
            },
            {
                "label": "Delete offering endpoint",
                "value": "OFFERING.DELETE_ENDPOINT",
            },
            {
                "label": "Update offering components",
                "value": "OFFERING.UPDATE_COMPONENTS",
            },
            {
                "label": "Pause offering",
                "value": "OFFERING.PAUSE",
            },
            {
                "label": "Unpause offering",
                "value": "OFFERING.UNPAUSE",
            },
            {
                "label": "Archive offering",
                "value": "OFFERING.ARCHIVE",
            },
            {
                "label": "Dry run offering script",
                "value": "OFFERING.DRY_RUN_SCRIPT",
            },
            {
                "label": "Manage campaign",
                "value": "OFFERING.MANAGE_CAMPAIGN",
            },
            {
                "label": "Manage offering user group",
                "value": "OFFERING.MANAGE_USER_GROUP",
            },
            {
                "label": "Create offering plan",
                "value": "OFFERING.CREATE_PLAN",
            },
            {
                "label": "Update offering plan",
                "value": "OFFERING.UPDATE_PLAN",
            },
            {
                "label": "Archive offering plan",
                "value": "OFFERING.ARCHIVE_PLAN",
            },
            {
                "label": "Create offering screenshot",
                "value": "OFFERING.CREATE_SCREENSHOT",
            },
            {
                "label": "Update offering screenshot",
                "value": "OFFERING.UPDATE_SCREENSHOT",
            },
            {
                "label": "Delete offering screenshot",
                "value": "OFFERING.DELETE_SCREENSHOT",
            },
            {
                "label": "Create offering user",
                "value": "OFFERING.CREATE_USER",
            },
            {
                "label": "Update offering user",
                "value": "OFFERING.UPDATE_USER",
            },
        ],
    },
    {
        "label": "Order",
        "options": [
            {"label": "Approve order", "value": "ORDER.APPROVE"},
            {
                "label": "Approve private order",
                "value": "ORDER.APPROVE_PRIVATE",
            },
            {"label": "Reject order", "value": "ORDER.REJECT"},
            {"label": "Destroy order", "value": "ORDER.DESTROY"},
            {
                "label": "Cancel order",
                "value": "ORDER.CANCEL",
            },
        ],
    },
    {
        "label": "Resource",
        "options": [
            {"label": "Terminate resource", "value": "RESOURCE.TERMINATE"},
            {
                "label": "List importable resources",
                "value": "RESOURCE.LIST_IMPORTABLE",
            },
            {
                "label": "Set resource end date",
                "value": "RESOURCE.SET_END_DATE",
            },
            {"label": "Set resource usage", "value": "RESOURCE.SET_USAGE"},
            {"label": "Switch resource plan", "value": "RESOURCE.SET_PLAN"},
            {
                "label": "Update resource limits",
                "value": "RESOURCE.SET_LIMITS",
            },
            {
                "label": "Set resource backend id",
                "value": "RESOURCE.SET_BACKEND_ID",
            },
            {
                "label": "Submit resource report",
                "value": "RESOURCE.SUBMIT_REPORT",
            },
            {"label": "List resource users", "value": "RESOURCE.LIST_USERS"},
            {
                "label": "Complete resource downscaling",
                "value": "RESOURCE.COMPLETE_DOWNSCALING",
            },
            {
                "label": "Accept booking request",
                "value": "RESOURCE.ACCEPT_BOOKING_REQUEST",
            },
            {
                "label": "Reject booking request",
                "value": "RESOURCE.REJECT_BOOKING_REQUEST",
            },
            {
                "label": "Create robot account",
                "value": "RESOURCE.CREATE_ROBOT_ACCOUNT",
            },
            {
                "label": "Update robot account",
                "value": "RESOURCE.UPDATE_ROBOT_ACCOUNT",
            },
            {
                "label": "Delete robot account",
                "value": "RESOURCE.DELETE_ROBOT_ACCOUNT",
            },
        ],
    },
    {
        "label": "Team members",
        "options": [
            {
                "label": "Create project permission",
                "value": "PROJECT.CREATE_PERMISSION",
            },
            {
                "label": "Create customer permission",
                "value": "CUSTOMER.CREATE_PERMISSION",
            },
            {
                "label": "Create offering permission",
                "value": "OFFERING.CREATE_PERMISSION",
            },
            {
                "label": "Update project permission",
                "value": "PROJECT.UPDATE_PERMISSION",
            },
            {
                "label": "Update customer permission",
                "value": "CUSTOMER.UPDATE_PERMISSION",
            },
            {
                "label": "Update offering permission",
                "value": "OFFERING.UPDATE_PERMISSION",
            },
            {
                "label": "Delete project permission",
                "value": "PROJECT.DELETE_PERMISSION",
            },
            {
                "label": "Delete customer permission",
                "value": "CUSTOMER.DELETE_PERMISSION",
            },
            {
                "label": "Delete offering permission",
                "value": "OFFERING.DELETE_PERMISSION",
            },
        ],
    },
    {
        "label": "Project",
        "options": [
            {
                "label": "Create project",
                "value": "PROJECT.CREATE",
            },
            {
                "label": "Update project",
                "value": "PROJECT.UPDATE",
            },
            {
                "label": "Delete project",
                "value": "PROJECT.DELETE",
            },
        ],
    },
]
