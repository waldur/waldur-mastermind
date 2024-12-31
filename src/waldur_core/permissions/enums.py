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
    DELETE_OFFERING_USER = "OFFERING.DELETE_USER"
    MANAGE_OFFERING_USER_ROLE = "OFFERING.MANAGE_USER_ROLE"
    CREATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.CREATE_ROBOT_ACCOUNT"
    UPDATE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.UPDATE_ROBOT_ACCOUNT"
    DELETE_RESOURCE_ROBOT_ACCOUNT = "RESOURCE.DELETE_ROBOT_ACCOUNT"

    LIST_ORDERS = "ORDER.LIST"
    APPROVE_PRIVATE_ORDER = "ORDER.APPROVE_PRIVATE"
    APPROVE_ORDER = "ORDER.APPROVE"
    REJECT_ORDER = "ORDER.REJECT"
    DESTROY_ORDER = "ORDER.DESTROY"
    CANCEL_ORDER = "ORDER.CANCEL"

    LIST_RESOURCES = "RESOURCE.LIST"
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

    LIST_INVITATIONS = "INVITATION.LIST"
    LIST_CUSTOMER_PERMISSION_REVIEWS = "CUSTOMER.LIST_PERMISSION_REVIEWS"
    LIST_PROPOSALS = "PROPOSAL.LIST"


CREATE_PERMISSIONS = {
    "customer": PermissionEnum.CREATE_CUSTOMER_PERMISSION,
    "project": PermissionEnum.CREATE_PROJECT_PERMISSION,
    "offering": PermissionEnum.CREATE_OFFERING_PERMISSION,
    "call": PermissionEnum.CREATE_CALL_PERMISSION,
    "proposal": PermissionEnum.MANAGE_PROPOSAL,
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
            {"label": "List orders", "value": "ORDER.LIST"},
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
        "label": "Provider actions",
        "options": [
            {"label": "Set resource usage", "value": "RESOURCE.SET_USAGE"},
            {
                "label": "Set resource backend id",
                "value": "RESOURCE.SET_BACKEND_ID",
            },
            {
                "label": "Submit resource report",
                "value": "RESOURCE.SUBMIT_REPORT",
            },
            {
                "label": "Set resource end date",
                "value": "RESOURCE.SET_END_DATE",
            },
            {
                "label": "Set resource state",
                "value": "RESOURCE.SET_STATE",
            },
            {
                "label": "Set resource backend metadata",
                "value": "RESOURCE.SET_BACKEND_METADATA",
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
            {
                "label": "Manage resource users",
                "value": "RESOURCE.MANAGE_USERS",
            },
            {
                "value": "SERVICE_PROVIDER.GET_API_SECRET_CODE",
                "label": "Get API secret code",
            },
            {
                "value": "SERVICE_PROVIDER.GENERATE_API_SECRET_CODE",
                "label": "Generate API secret code",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_CUSTOMERS",
                "label": "List service provider customers",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_CUSTOMER_PROJECTS",
                "label": "List service provider customer projects",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_PROJECTS",
                "label": "List service provider projects",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_PROJECT_PERMISSIONS",
                "label": "List service provider project permissions",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_KEYS",
                "label": "List service provider keys",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_USERS",
                "label": "List service provider users",
            },
            {
                "value": "SERVICE_PROVIDER.LIST_USER_CUSTOMERS",
                "label": "List service provider user customers",
            },
            {
                "value": "SERVICE_PROVIDER.SET_OFFERINGS_USERNAME",
                "label": "Set offerings username",
            },
            {
                "value": "SERVICE_PROVIDER.GET_STATISTICS",
                "label": "Get service provider statistics",
            },
            {
                "value": "SERVICE_PROVIDER.GET_REVENUE",
                "label": "Get service provider revenue",
            },
            {
                "value": "SERVICE_PROVIDER.GET_ROBOT_ACCOUNT_CUSTOMERS",
                "label": "Get service provider robot account customers",
            },
            {
                "value": "SERVICE_PROVIDER.GET_ROBOT_ACCOUNT_PROJECTS",
                "label": "Get service provider robot account projects",
            },
        ],
    },
    {
        "label": "Customer actions for resources",
        "options": [
            {"label": "List resources", "value": "RESOURCE.LIST"},
            {
                "label": "Set resource end date",
                "value": "RESOURCE.SET_END_DATE",
            },
            {"label": "Terminate resource", "value": "RESOURCE.TERMINATE"},
            {
                "label": "List importable resources",
                "value": "RESOURCE.LIST_IMPORTABLE",
            },
            {"label": "Switch resource plan", "value": "RESOURCE.SET_PLAN"},
            {
                "label": "Update resource limits",
                "value": "RESOURCE.SET_LIMITS",
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
                "label": "Update resource options",
                "value": "RESOURCE.UPDATE_OPTIONS",
            },
            {
                "label": "Set resource consumption limitation",
                "value": "RESOURCE.CONSUMPTION_LIMITATION",
            },
        ],
    },
    {
        "label": "Team members",
        "options": [
            {
                "value": "INVITATION.LIST",
                "label": "List invitations",
            },
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
            {"label": "List projects", "value": "PROJECT.LIST"},
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
