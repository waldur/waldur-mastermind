from enum import Enum, StrEnum


class EventType(StrEnum):
    ACCESS_SUBNET_CREATION_SUCCEEDED = "access_subnet_creation_succeeded"
    ACCESS_SUBNET_DELETION_SUCCEEDED = "access_subnet_deletion_succeeded"
    ACCESS_SUBNET_UPDATE_SUCCEEDED = "access_subnet_update_succeeded"
    ALLOWED_OFFERINGS_HAVE_BEEN_UPDATED = "allowed_offerings_have_been_updated"
    ATTACHMENT_CREATED = "attachment_created"
    ATTACHMENT_DELETED = "attachment_deleted"
    ATTACHMENT_UPDATED = "attachment_updated"
    AUTH_LOGGED_IN_WITH_SAML2 = "auth_logged_in_with_saml2"
    AUTH_LOGGED_IN_WITH_USERNAME = "auth_logged_in_with_username"
    AUTH_LOGGED_IN_WITH_OAUTH = "auth_logged_in_with_oauth"
    AUTH_LOGGED_OUT = "auth_logged_out"
    AUTH_LOGGED_OUT_WITH_SAML2 = "auth_logged_out_with_saml2"
    AUTH_LOGIN_FAILED_WITH_USERNAME = "auth_login_failed_with_username"
    BLOCK_CREATION_OF_NEW_RESOURCES = "block_creation_of_new_resources"
    BLOCK_MODIFICATION_OF_EXISTING_RESOURCES = (
        "block_modification_of_existing_resources"
    )
    CALL_DOCUMENT_ADDED = "call_document_added"
    CALL_DOCUMENT_REMOVED = "call_document_removed"
    CREATE_OF_CREDIT_BY_STAFF = "create_of_credit_by_staff"
    CREATE_OF_PROJECT_CREDIT_BY_STAFF = "create_of_project_credit_by_staff"
    CUSTOM_NOTIFICATION = "custom_notification"
    CUSTOMER_CREATION_SUCCEEDED = "customer_creation_succeeded"
    CUSTOMER_DELETION_SUCCEEDED = "customer_deletion_succeeded"
    CUSTOMER_UPDATE_SUCCEEDED = "customer_update_succeeded"
    CUSTOMER_PERMISSION_REVIEW_CREATED = "customer_permission_review_created"
    CUSTOMER_PERMISSION_REVIEW_CLOSED = "customer_permission_review_closed"
    DROPLET_RESIZE_SCHEDULED = "droplet_resize_scheduled"
    DROPLET_RESIZE_SUCCEEDED = "droplet_resize_succeeded"
    FREEIPA_PROFILE_CREATED = "freeipa_profile_created"
    FREEIPA_PROFILE_DELETED = "freeipa_profile_deleted"
    FREEIPA_PROFILE_DISABLED = "freeipa_profile_disabled"
    FREEIPA_PROFILE_ENABLED = "freeipa_profile_enabled"
    INVOICE_CANCELED = "invoice_canceled"
    INVOICE_CREATED = "invoice_created"
    INVOICE_ITEM_CREATED = "invoice_item_created"
    INVOICE_ITEM_DELETED = "invoice_item_deleted"
    INVOICE_ITEM_UPDATED = "invoice_item_updated"
    INVOICE_PAID = "invoice_paid"
    ISSUE_CREATION_SUCCEEDED = "issue_creation_succeeded"
    ISSUE_DELETION_SUCCEEDED = "issue_deletion_succeeded"
    ISSUE_UPDATE_SUCCEEDED = "issue_update_succeeded"
    MARKETPLACE_OFFERING_COMPONENT_CREATED = "marketplace_offering_component_created"
    MARKETPLACE_OFFERING_COMPONENT_DELETED = "marketplace_offering_component_deleted"
    MARKETPLACE_OFFERING_COMPONENT_UPDATED = "marketplace_offering_component_updated"
    MARKETPLACE_OFFERING_CREATED = "marketplace_offering_created"
    MARKETPLACE_OFFERING_UPDATED = "marketplace_offering_updated"
    MARKETPLACE_OFFERING_USER_CREATED = "marketplace_offering_user_created"
    MARKETPLACE_OFFERING_USER_UPDATED = "marketplace_offering_user_updated"
    MARKETPLACE_OFFERING_USER_DELETED = "marketplace_offering_user_deleted"
    MARKETPLACE_OFFERING_USER_RESTRICTION_UPDATED = (
        "marketplace_offering_user_restriction_updated"
    )
    MARKETPLACE_ORDER_APPROVED = "marketplace_order_approved"
    MARKETPLACE_ORDER_COMPLETED = "marketplace_order_completed"
    MARKETPLACE_ORDER_CREATED = "marketplace_order_created"
    MARKETPLACE_ORDER_FAILED = "marketplace_order_failed"
    MARKETPLACE_ORDER_REJECTED = "marketplace_order_rejected"
    MARKETPLACE_ORDER_TERMINATED = "marketplace_order_terminated"
    MARKETPLACE_ORDER_UNLINKED = "marketplace_order_unlinked"
    MARKETPLACE_PLAN_ARCHIVED = "marketplace_plan_archived"
    MARKETPLACE_PLAN_COMPONENT_CURRENT_PRICE_UPDATED = (
        "marketplace_plan_component_current_price_updated"
    )
    MARKETPLACE_PLAN_COMPONENT_FUTURE_PRICE_UPDATED = (
        "marketplace_plan_component_future_price_updated"
    )
    MARKETPLACE_PLAN_COMPONENT_QUOTA_UPDATED = (
        "marketplace_plan_component_quota_updated"
    )
    MARKETPLACE_PLAN_CREATED = "marketplace_plan_created"
    MARKETPLACE_PLAN_UPDATED = "marketplace_plan_updated"
    MARKETPLACE_PLAN_DELETED = "marketplace_plan_deleted"
    MARKETPLACE_RESOURCE_CREATE_CANCELED = "marketplace_resource_create_canceled"
    MARKETPLACE_RESOURCE_CREATE_FAILED = "marketplace_resource_create_failed"
    MARKETPLACE_RESOURCE_CREATE_REQUESTED = "marketplace_resource_create_requested"
    MARKETPLACE_RESOURCE_CREATE_SUCCEEDED = "marketplace_resource_create_succeeded"
    MARKETPLACE_RESOURCE_DOWNSCALED = "marketplace_resource_downscaled"
    MARKETPLACE_RESOURCE_ERRED_ON_BACKEND = "marketplace_resource_erred_on_backend"
    MARKETPLACE_RESOURCE_PAUSED = "marketplace_resource_paused"
    MARKETPLACE_RESOURCE_TERMINATE_CANCELED = "marketplace_resource_terminate_canceled"
    MARKETPLACE_RESOURCE_TERMINATE_FAILED = "marketplace_resource_terminate_failed"
    MARKETPLACE_RESOURCE_TERMINATE_REQUESTED = (
        "marketplace_resource_terminate_requested"
    )
    MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED = (
        "marketplace_resource_terminate_succeeded"
    )
    MARKETPLACE_RESOURCE_UNLINKED = "marketplace_resource_unlinked"
    MARKETPLACE_RESOURCE_UPDATE_CANCELED = "marketplace_resource_update_canceled"
    MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED = (
        "marketplace_resource_update_end_date_succeeded"
    )
    MARKETPLACE_RESOURCE_UPDATE_FAILED = "marketplace_resource_update_failed"
    MARKETPLACE_RESOURCE_UPDATE_LIMITS_FAILED = (
        "marketplace_resource_update_limits_failed"
    )
    MARKETPLACE_RESOURCE_UPDATE_LIMITS_SUCCEEDED = (
        "marketplace_resource_update_limits_succeeded"
    )
    MARKETPLACE_RESOURCE_UPDATE_REQUESTED = "marketplace_resource_update_requested"
    MARKETPLACE_RESOURCE_UPDATE_SUCCEEDED = "marketplace_resource_update_succeeded"
    MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_CREATED = (
        "marketplace_resource_limit_change_request_created"
    )
    MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_APPROVED = (
        "marketplace_resource_limit_change_request_approved"
    )
    MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_REJECTED = (
        "marketplace_resource_limit_change_request_rejected"
    )
    NOTIFY_EXTERNAL_USER = "notify_external_user"
    NOTIFY_ORGANIZATION_OWNERS = "notify_organization_owners"
    NOTIFY_PROJECT_TEAM = "notify_project_team"
    OPENSTACK_FLOATING_IP_ATTACHED = "openstack_floating_ip_attached"
    OPENSTACK_FLOATING_IP_CONNECTED = "openstack_floating_ip_connected"
    OPENSTACK_FLOATING_IP_DESCRIPTION_UPDATED = (
        "openstack_floating_ip_description_updated"
    )
    OPENSTACK_FLOATING_IP_DETACHED = "openstack_floating_ip_detached"
    OPENSTACK_FLOATING_IP_DISCONNECTED = "openstack_floating_ip_disconnected"
    OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED = (
        "openstack_instance_security_groups_changed"
    )
    OPENSTACK_NETWORK_CLEANED = "openstack_network_cleaned"
    OPENSTACK_NETWORK_CREATED = "openstack_network_created"
    OPENSTACK_NETWORK_DELETED = "openstack_network_deleted"
    OPENSTACK_NETWORK_IMPORTED = "openstack_network_imported"
    OPENSTACK_NETWORK_PULLED = "openstack_network_pulled"
    OPENSTACK_NETWORK_UPDATED = "openstack_network_updated"
    OPENSTACK_LOAD_BALANCER_CREATED = "openstack_load_balancer_created"
    OPENSTACK_LOAD_BALANCER_UPDATED = "openstack_load_balancer_updated"
    OPENSTACK_LOAD_BALANCER_DELETED = "openstack_load_balancer_deleted"
    OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED = (
        "openstack_load_balancer_security_groups_changed"
    )
    OPENSTACK_LISTENER_CREATED = "openstack_listener_created"
    OPENSTACK_LISTENER_UPDATED = "openstack_listener_updated"
    OPENSTACK_LISTENER_DELETED = "openstack_listener_deleted"
    OPENSTACK_POOL_CREATED = "openstack_pool_created"
    OPENSTACK_POOL_UPDATED = "openstack_pool_updated"
    OPENSTACK_POOL_DELETED = "openstack_pool_deleted"
    OPENSTACK_POOL_MEMBER_CREATED = "openstack_pool_member_created"
    OPENSTACK_POOL_MEMBER_UPDATED = "openstack_pool_member_updated"
    OPENSTACK_POOL_MEMBER_DELETED = "openstack_pool_member_deleted"
    OPENSTACK_PORT_CLEANED = "openstack_port_cleaned"
    OPENSTACK_PORT_CREATED = "openstack_port_created"
    OPENSTACK_PORT_DELETED = "openstack_port_deleted"
    OPENSTACK_PORT_IMPORTED = "openstack_port_imported"
    OPENSTACK_PORT_PULLED = "openstack_port_pulled"
    OPENSTACK_PORT_UPDATED = "openstack_port_updated"
    OPENSTACK_PORT_SECURITY_ENABLED = "openstack_port_security_enabled"
    OPENSTACK_PORT_SECURITY_DISABLED = "openstack_port_security_disabled"
    OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED = (
        "openstack_port_allowed_address_pairs_changed"
    )
    OPENSTACK_PORT_SECURITY_GROUPS_CHANGED = "openstack_port_security_groups_changed"
    OPENSTACK_ROUTER_UPDATED = "openstack_router_updated"
    OPENSTACK_SECURITY_GROUP_CLEANED = "openstack_security_group_cleaned"
    OPENSTACK_SECURITY_GROUP_CREATED = "openstack_security_group_created"
    OPENSTACK_SECURITY_GROUP_DELETED = "openstack_security_group_deleted"
    OPENSTACK_SECURITY_GROUP_IMPORTED = "openstack_security_group_imported"
    OPENSTACK_SECURITY_GROUP_PULLED = "openstack_security_group_pulled"
    OPENSTACK_SECURITY_GROUP_RULE_CLEANED = "openstack_security_group_rule_cleaned"
    OPENSTACK_SECURITY_GROUP_RULE_CREATED = "openstack_security_group_rule_created"
    OPENSTACK_SECURITY_GROUP_RULE_DELETED = "openstack_security_group_rule_deleted"
    OPENSTACK_SECURITY_GROUP_RULE_IMPORTED = "openstack_security_group_rule_imported"
    OPENSTACK_SECURITY_GROUP_RULE_UPDATED = "openstack_security_group_rule_updated"
    OPENSTACK_SECURITY_GROUP_RULES_CHANGED = "openstack_security_group_rules_changed"
    OPENSTACK_SECURITY_GROUP_UPDATED = "openstack_security_group_updated"
    OPENSTACK_SECURITY_GROUP_ADDED_REMOTELY = "openstack_security_group_added_remotely"
    OPENSTACK_SECURITY_GROUP_REMOVED_REMOTELY = (
        "openstack_security_group_removed_remotely"
    )
    OPENSTACK_SECURITY_GROUP_ADDED_LOCALLY = "openstack_security_group_added_locally"
    OPENSTACK_SECURITY_GROUP_REMOVED_LOCALLY = (
        "openstack_security_group_removed_locally"
    )
    OPENSTACK_SERVER_GROUP_CLEANED = "openstack_server_group_cleaned"
    OPENSTACK_SERVER_GROUP_CREATED = "openstack_server_group_created"
    OPENSTACK_SERVER_GROUP_DELETED = "openstack_server_group_deleted"
    OPENSTACK_SERVER_GROUP_IMPORTED = "openstack_server_group_imported"
    OPENSTACK_SERVER_GROUP_PULLED = "openstack_server_group_pulled"
    OPENSTACK_SUBNET_CLEANED = "openstack_subnet_cleaned"
    OPENSTACK_SUBNET_CREATED = "openstack_subnet_created"
    OPENSTACK_SUBNET_DELETED = "openstack_subnet_deleted"
    OPENSTACK_SUBNET_IMPORTED = "openstack_subnet_imported"
    OPENSTACK_SUBNET_PULLED = "openstack_subnet_pulled"
    OPENSTACK_SUBNET_UPDATED = "openstack_subnet_updated"
    OPENSTACK_TENANT_QUOTA_LIMIT_UPDATED = "openstack_tenant_quota_limit_updated"
    PAYMENT_ADDED = "payment_added"
    PAYMENT_CREATED = "payment_created"
    PAYMENT_REMOVED = "payment_removed"
    POLICY_NOTIFICATION = "policy_notification"
    PROJECT_CREATION_SUCCEEDED = "project_creation_succeeded"
    PROJECT_DELETION_SUCCEEDED = "project_deletion_succeeded"
    PROJECT_DELETION_TRIGGERED = "project_deletion_triggered"
    PROJECT_UPDATE_REQUEST_APPROVED = "project_update_request_approved"
    PROJECT_UPDATE_REQUEST_CREATED = "project_update_request_created"
    PROJECT_UPDATE_REQUEST_REJECTED = "project_update_request_rejected"
    PROJECT_END_DATE_CHANGE_REQUEST_APPROVED = (
        "project_end_date_change_request_approved"
    )
    PROJECT_END_DATE_CHANGE_REQUEST_CREATED = "project_end_date_change_request_created"
    PROJECT_END_DATE_CHANGE_REQUEST_REJECTED = (
        "project_end_date_change_request_rejected"
    )
    PROJECT_UPDATE_SUCCEEDED = "project_update_succeeded"
    PROJECT_PERMISSION_REVIEW_CREATED = "project_permission_review_created"
    PROJECT_PERMISSION_REVIEW_CLOSED = "project_permission_review_closed"
    PROPOSAL_CANCELED = "proposal_canceled"
    PROPOSAL_DOCUMENT_ADDED = "proposal_document_added"
    PROPOSAL_DOCUMENT_REMOVED = "proposal_document_removed"
    PROPOSAL_WORKFLOW_ADVANCED = "proposal_workflow_advanced"
    QUERY_EXECUTED = "query_executed"
    REDUCTION_OF_CUSTOMER_CREDIT = "reduction_of_customer_credit"
    REDUCTION_OF_CUSTOMER_CREDIT_DUE_TO_MINIMAL_CONSUMPTION = (
        "reduction_of_customer_credit_due_to_minimal_consumption"
    )
    REDUCTION_OF_CUSTOMER_EXPECTED_CONSUMPTION = (
        "reduction_of_customer_expected_consumption"
    )
    REDUCTION_OF_PROJECT_CREDIT = "reduction_of_project_credit"
    REDUCTION_OF_PROJECT_CREDIT_DUE_TO_MINIMAL_CONSUMPTION = (
        "reduction_of_project_credit_due_to_minimal_consumption"
    )
    REDUCTION_OF_PROJECT_EXPECTED_CONSUMPTION = (
        "reduction_of_project_expected_consumption"
    )
    REQUEST_DOWNSCALING = "request_downscaling"
    REQUEST_PAUSING = "request_pausing"
    REQUEST_SLURM_RESOURCE_DOWNSCALING = "request_slurm_resource_downscaling"
    REQUEST_SLURM_RESOURCE_PAUSING = "request_slurm_resource_pausing"
    RESET_DOWNSCALING = "reset_downscaling"
    RESET_MEMBER_RESTRICTION = "reset_member_restriction"
    RESET_PAUSING = "reset_pausing"
    RESOURCE_ASSIGN_FLOATING_IP_FAILED = "resource_assign_floating_ip_failed"
    RESOURCE_ASSIGN_FLOATING_IP_SCHEDULED = "resource_assign_floating_ip_scheduled"
    RESOURCE_ASSIGN_FLOATING_IP_SUCCEEDED = "resource_assign_floating_ip_succeeded"
    RESOURCE_ATTACH_FAILED = "resource_attach_failed"
    RESOURCE_ATTACH_SCHEDULED = "resource_attach_scheduled"
    RESOURCE_ATTACH_SUCCEEDED = "resource_attach_succeeded"
    RESOURCE_BACKUP_CREATION_FAILED = "resource_backup_creation_failed"
    RESOURCE_BACKUP_CREATION_SCHEDULED = "resource_backup_creation_scheduled"
    RESOURCE_BACKUP_CREATION_SUCCEEDED = "resource_backup_creation_succeeded"
    RESOURCE_BACKUP_DELETION_FAILED = "resource_backup_deletion_failed"
    RESOURCE_BACKUP_DELETION_SCHEDULED = "resource_backup_deletion_scheduled"
    RESOURCE_BACKUP_DELETION_SUCCEEDED = "resource_backup_deletion_succeeded"
    RESOURCE_BACKUP_RESTORATION_FAILED = "resource_backup_restoration_failed"
    RESOURCE_BACKUP_RESTORATION_SCHEDULED = "resource_backup_restoration_scheduled"
    RESOURCE_BACKUP_RESTORATION_SUCCEEDED = "resource_backup_restoration_succeeded"
    RESOURCE_CHANGE_FLAVOR_FAILED = "resource_change_flavor_failed"
    RESOURCE_CHANGE_FLAVOR_SCHEDULED = "resource_change_flavor_scheduled"
    RESOURCE_CHANGE_FLAVOR_SUCCEEDED = "resource_change_flavor_succeeded"
    RESOURCE_CREATION_FAILED = "resource_creation_failed"
    RESOURCE_CREATION_SCHEDULED = "resource_creation_scheduled"
    RESOURCE_CREATION_SUCCEEDED = "resource_creation_succeeded"
    RESOURCE_DELETION_FAILED = "resource_deletion_failed"
    RESOURCE_DELETION_SCHEDULED = "resource_deletion_scheduled"
    RESOURCE_DELETION_SUCCEEDED = "resource_deletion_succeeded"
    RESOURCE_DETACH_FAILED = "resource_detach_failed"
    RESOURCE_DETACH_SCHEDULED = "resource_detach_scheduled"
    RESOURCE_DETACH_SUCCEEDED = "resource_detach_succeeded"
    RESOURCE_EXTEND_FAILED = "resource_extend_failed"
    RESOURCE_EXTEND_SCHEDULED = "resource_extend_scheduled"
    RESOURCE_EXTEND_SUCCEEDED = "resource_extend_succeeded"
    RESOURCE_EXTEND_VOLUME_FAILED = "resource_extend_volume_failed"
    RESOURCE_EXTEND_VOLUME_SCHEDULED = "resource_extend_volume_scheduled"
    RESOURCE_EXTEND_VOLUME_SUCCEEDED = "resource_extend_volume_succeeded"
    RESOURCE_IMPORT_SUCCEEDED = "resource_import_succeeded"
    RESOURCE_PULL_FAILED = "resource_pull_failed"
    RESOURCE_PULL_SCHEDULED = "resource_pull_scheduled"
    RESOURCE_PULL_SUCCEEDED = "resource_pull_succeeded"
    RESOURCE_RESTART_FAILED = "resource_restart_failed"
    RESOURCE_RESTART_SCHEDULED = "resource_restart_scheduled"
    RESOURCE_RESTART_SUCCEEDED = "resource_restart_succeeded"
    RESOURCE_RETYPE_FAILED = "resource_retype_failed"
    RESOURCE_RETYPE_SCHEDULED = "resource_retype_scheduled"
    RESOURCE_RETYPE_SUCCEEDED = "resource_retype_succeeded"
    RESOURCE_ROBOT_ACCOUNT_CREATED = "resource_robot_account_created"
    RESOURCE_ROBOT_ACCOUNT_DELETED = "resource_robot_account_deleted"
    RESOURCE_ROBOT_ACCOUNT_STATE_CHANGED = "resource_robot_account_state_changed"
    RESOURCE_ROBOT_ACCOUNT_UPDATED = "resource_robot_account_updated"
    RESOURCE_START_FAILED = "resource_start_failed"
    RESOURCE_START_SCHEDULED = "resource_start_scheduled"
    RESOURCE_START_SUCCEEDED = "resource_start_succeeded"
    RESOURCE_STOP_FAILED = "resource_stop_failed"
    RESOURCE_STOP_SCHEDULED = "resource_stop_scheduled"
    RESOURCE_STOP_SUCCEEDED = "resource_stop_succeeded"
    RESOURCE_UNASSIGN_FLOATING_IP_FAILED = "resource_unassign_floating_ip_failed"
    RESOURCE_UNASSIGN_FLOATING_IP_SCHEDULED = "resource_unassign_floating_ip_scheduled"
    RESOURCE_UNASSIGN_FLOATING_IP_SUCCEEDED = "resource_unassign_floating_ip_succeeded"
    RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_FAILED = (
        "resource_update_allowed_address_pairs_failed"
    )
    RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_SCHEDULED = (
        "resource_update_allowed_address_pairs_scheduled"
    )
    RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_SUCCEEDED = (
        "resource_update_allowed_address_pairs_succeeded"
    )
    RESOURCE_UPDATE_FLOATING_IPS_FAILED = "resource_update_floating_ips_failed"
    RESOURCE_UPDATE_FLOATING_IPS_SCHEDULED = "resource_update_floating_ips_scheduled"
    RESOURCE_UPDATE_FLOATING_IPS_SUCCEEDED = "resource_update_floating_ips_succeeded"
    RESOURCE_UPDATE_PORTS_FAILED = "resource_update_ports_failed"
    RESOURCE_UPDATE_PORTS_SCHEDULED = "resource_update_ports_scheduled"
    RESOURCE_UPDATE_PORTS_SUCCEEDED = "resource_update_ports_succeeded"
    RESOURCE_UPDATE_SECURITY_GROUPS_FAILED = "resource_update_security_groups_failed"
    RESOURCE_UPDATE_SECURITY_GROUPS_SCHEDULED = (
        "resource_update_security_groups_scheduled"
    )
    RESOURCE_UPDATE_SECURITY_GROUPS_SUCCEEDED = (
        "resource_update_security_groups_succeeded"
    )
    RESOURCE_UPDATE_SUCCEEDED = "resource_update_succeeded"
    RESTRICT_MEMBERS = "restrict_members"
    REVIEW_CANCELED = "review_canceled"
    ROLE_GRANTED = "role_granted"
    ROLE_REVOKED = "role_revoked"
    ROLE_UPDATED = "role_updated"
    ROLL_BACK_CUSTOMER_CREDIT = "roll_back_customer_credit"
    ROLL_BACK_PROJECT_CREDIT = "roll_back_project_credit"
    SERVICE_ACCOUNT_CREATED = "service_account_created"
    SERVICE_ACCOUNT_DELETED = "service_account_deleted"
    SERVICE_ACCOUNT_UPDATED = "service_account_updated"
    SET_TO_ZERO_OVERDUE_CREDIT = "set_to_zero_overdue_credit"
    SLURM_POLICY_EVALUATION = "slurm_policy_evaluation"
    SSH_KEY_CREATION_SUCCEEDED = "ssh_key_creation_succeeded"
    SSH_KEY_DELETION_SUCCEEDED = "ssh_key_deletion_succeeded"
    TERMINATE_RESOURCES = "terminate_resources"
    TOKEN_CREATED = "token_created"
    TOKEN_LIFETIME_UPDATED = "token_lifetime_updated"
    UPDATE_OF_CREDIT_BY_STAFF = "update_of_credit_by_staff"
    UPDATE_OF_PROJECT_CREDIT_BY_STAFF = "update_of_project_credit_by_staff"
    AUTOMATIC_CREDIT_ADJUSTMENT = "automatic_credit_adjustment"
    USER_ACTIVATED = "user_activated"
    USER_CREATION_SUCCEEDED = "user_creation_succeeded"
    USER_DATA_ACCESSED = "user_data_accessed"
    USER_DEACTIVATED = "user_deactivated"
    USER_DEACTIVATED_NO_ROLES = "user_deactivated_no_roles"
    USER_DELETION_SUCCEEDED = "user_deletion_succeeded"
    USER_DETAILS_UPDATE_SUCCEEDED = "user_details_update_succeeded"
    USER_HAS_BEEN_CREATED_BY_STAFF = "user_has_been_created_by_staff"
    USER_PASSWORD_UPDATED = "user_password_updated"
    USER_PASSWORD_UPDATED_BY_STAFF = "user_password_updated_by_staff"
    USER_PASSWORD_REMOVED_BY_STAFF = "user_password_removed_by_staff"
    USER_UPDATE_SUCCEEDED = "user_update_succeeded"
    USER_GROUP_INVITATION_UPDATED = "user_group_invitation_updated"
    USER_INVITATION_UPDATED = "user_invitation_updated"
    USER_INVITATION_DELETED = "user_invitation_deleted"
    TERMS_OF_SERVICE_CONSENT_GRANTED = "terms_of_service_consent_granted"
    TERMS_OF_SERVICE_CONSENT_REVOKED = "terms_of_service_consent_revoked"
    CHAT_SESSION_ACCESSED = "chat_session_accessed"
    CHAT_THREAD_ACCESSED = "chat_thread_accessed"
    CHAT_INJECTION_DETECTED = "chat_injection_detected"
    CHAT_PII_DETECTED = "chat_pii_detected"
    CHAT_FEEDBACK_SUBMITTED = "chat_feedback_submitted"
    ONBOARDING_VERIFICATION_DELETED = "onboarding_verification_deleted"
    ONBOARDING_VERIFICATION_DELETED_BY_TASK = "onboarding_verification_deleted_by_task"
    PAT_CREATED = "pat_created"
    PAT_REVOKED = "pat_revoked"
    PAT_ROTATED = "pat_rotated"
    PAT_EXPIRED = "pat_expired"
    PAT_USED_FROM_NEW_IP = "pat_used_from_new_ip"


class EventGroup(StrEnum):
    ACCESS_SUBNETS = "access_subnets"
    AUTH = "auth"
    CALL = "call"
    CHAT = "chat"
    CREDITS = "credits"
    CUSTOMERS = "customers"
    INVOICES = "invoices"
    OFFERING_ACCOUNTING = "offering_accounting"
    ONBOARDING = "onboarding"
    PERMISSIONS = "permissions"
    PROJECTS = "projects"
    PROPOSAL = "proposal"
    PROVIDERS = "providers"
    RESOURCES = "resources"
    REVIEW = "review"
    SSH = "ssh"
    SUPPORT = "support"
    USERS = "users"
    TERMS_OF_SERVICE = "terms_of_service"


EVENT_GROUP_MAPPING = {
    EventGroup.ACCESS_SUBNETS: [
        EventType.ACCESS_SUBNET_CREATION_SUCCEEDED,
        EventType.ACCESS_SUBNET_DELETION_SUCCEEDED,
        EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED,
    ],
    EventGroup.AUTH: [
        EventType.AUTH_LOGGED_IN_WITH_USERNAME,
        EventType.AUTH_LOGGED_OUT,
        EventType.AUTH_LOGIN_FAILED_WITH_USERNAME,
        EventType.TOKEN_CREATED,
        EventType.TOKEN_LIFETIME_UPDATED,
        EventType.PAT_CREATED,
        EventType.PAT_REVOKED,
        EventType.PAT_ROTATED,
        EventType.PAT_EXPIRED,
        EventType.PAT_USED_FROM_NEW_IP,
    ],
    EventGroup.CALL: [
        EventType.CALL_DOCUMENT_ADDED,
        EventType.CALL_DOCUMENT_REMOVED,
    ],
    EventGroup.CREDITS: [
        EventType.ALLOWED_OFFERINGS_HAVE_BEEN_UPDATED,
        EventType.AUTOMATIC_CREDIT_ADJUSTMENT,
        EventType.CREATE_OF_CREDIT_BY_STAFF,
        EventType.CREATE_OF_PROJECT_CREDIT_BY_STAFF,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_CUSTOMER_EXPECTED_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_CREDIT,
        EventType.REDUCTION_OF_PROJECT_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_EXPECTED_CONSUMPTION,
        EventType.ROLL_BACK_CUSTOMER_CREDIT,
        EventType.ROLL_BACK_PROJECT_CREDIT,
        EventType.SET_TO_ZERO_OVERDUE_CREDIT,
        EventType.UPDATE_OF_CREDIT_BY_STAFF,
        EventType.UPDATE_OF_PROJECT_CREDIT_BY_STAFF,
    ],
    EventGroup.CUSTOMERS: [
        EventType.ALLOWED_OFFERINGS_HAVE_BEEN_UPDATED,
        EventType.AUTOMATIC_CREDIT_ADJUSTMENT,
        EventType.CREATE_OF_CREDIT_BY_STAFF,
        EventType.CREATE_OF_PROJECT_CREDIT_BY_STAFF,
        EventType.CUSTOMER_CREATION_SUCCEEDED,
        EventType.CUSTOMER_DELETION_SUCCEEDED,
        EventType.CUSTOMER_UPDATE_SUCCEEDED,
        EventType.PAYMENT_ADDED,
        EventType.PAYMENT_REMOVED,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_CUSTOMER_EXPECTED_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_CREDIT,
        EventType.REDUCTION_OF_PROJECT_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_EXPECTED_CONSUMPTION,
        EventType.ROLL_BACK_CUSTOMER_CREDIT,
        EventType.ROLL_BACK_PROJECT_CREDIT,
        EventType.SET_TO_ZERO_OVERDUE_CREDIT,
        EventType.UPDATE_OF_CREDIT_BY_STAFF,
        EventType.UPDATE_OF_PROJECT_CREDIT_BY_STAFF,
        EventType.CUSTOMER_PERMISSION_REVIEW_CREATED,
        EventType.CUSTOMER_PERMISSION_REVIEW_CLOSED,
    ],
    EventGroup.INVOICES: [
        EventType.ALLOWED_OFFERINGS_HAVE_BEEN_UPDATED,
        EventType.AUTOMATIC_CREDIT_ADJUSTMENT,
        EventType.CREATE_OF_CREDIT_BY_STAFF,
        EventType.CREATE_OF_PROJECT_CREDIT_BY_STAFF,
        EventType.INVOICE_CANCELED,
        EventType.INVOICE_CREATED,
        EventType.INVOICE_ITEM_CREATED,
        EventType.INVOICE_ITEM_DELETED,
        EventType.INVOICE_ITEM_UPDATED,
        EventType.INVOICE_PAID,
        EventType.PAYMENT_CREATED,
        EventType.PAYMENT_REMOVED,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT,
        EventType.REDUCTION_OF_CUSTOMER_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_CUSTOMER_EXPECTED_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_CREDIT,
        EventType.REDUCTION_OF_PROJECT_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
        EventType.REDUCTION_OF_PROJECT_EXPECTED_CONSUMPTION,
        EventType.ROLL_BACK_CUSTOMER_CREDIT,
        EventType.ROLL_BACK_PROJECT_CREDIT,
        EventType.SET_TO_ZERO_OVERDUE_CREDIT,
        EventType.UPDATE_OF_CREDIT_BY_STAFF,
        EventType.UPDATE_OF_PROJECT_CREDIT_BY_STAFF,
    ],
    EventGroup.OFFERING_ACCOUNTING: [
        EventType.MARKETPLACE_OFFERING_COMPONENT_CREATED,
        EventType.MARKETPLACE_OFFERING_COMPONENT_DELETED,
        EventType.MARKETPLACE_OFFERING_COMPONENT_UPDATED,
        EventType.MARKETPLACE_PLAN_ARCHIVED,
        EventType.MARKETPLACE_PLAN_COMPONENT_CURRENT_PRICE_UPDATED,
        EventType.MARKETPLACE_PLAN_COMPONENT_FUTURE_PRICE_UPDATED,
        EventType.MARKETPLACE_PLAN_COMPONENT_QUOTA_UPDATED,
        EventType.MARKETPLACE_PLAN_CREATED,
        EventType.MARKETPLACE_PLAN_UPDATED,
        EventType.MARKETPLACE_PLAN_DELETED,
    ],
    EventGroup.PERMISSIONS: [
        EventType.ROLE_GRANTED,
        EventType.ROLE_REVOKED,
        EventType.ROLE_UPDATED,
    ],
    EventGroup.PROJECTS: [
        EventType.PROJECT_CREATION_SUCCEEDED,
        EventType.PROJECT_DELETION_SUCCEEDED,
        EventType.PROJECT_DELETION_TRIGGERED,
        EventType.PROJECT_UPDATE_REQUEST_APPROVED,
        EventType.PROJECT_UPDATE_REQUEST_CREATED,
        EventType.PROJECT_UPDATE_REQUEST_REJECTED,
        EventType.PROJECT_UPDATE_SUCCEEDED,
        EventType.PROJECT_PERMISSION_REVIEW_CREATED,
        EventType.PROJECT_PERMISSION_REVIEW_CLOSED,
    ],
    EventGroup.PROPOSAL: [
        EventType.PROPOSAL_CANCELED,
        EventType.PROPOSAL_DOCUMENT_ADDED,
        EventType.PROPOSAL_DOCUMENT_REMOVED,
        EventType.PROPOSAL_WORKFLOW_ADVANCED,
    ],
    EventGroup.PROVIDERS: [
        EventType.MARKETPLACE_RESOURCE_CREATE_CANCELED,
        EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_CREATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_CREATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_FAILED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_CREATED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_APPROVED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_REJECTED,
        EventType.RESOURCE_ROBOT_ACCOUNT_CREATED,
        EventType.RESOURCE_ROBOT_ACCOUNT_DELETED,
        EventType.RESOURCE_ROBOT_ACCOUNT_STATE_CHANGED,
        EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
    ],
    EventGroup.RESOURCES: [
        EventType.MARKETPLACE_ORDER_APPROVED,
        EventType.MARKETPLACE_ORDER_COMPLETED,
        EventType.MARKETPLACE_ORDER_CREATED,
        EventType.MARKETPLACE_ORDER_FAILED,
        EventType.MARKETPLACE_ORDER_REJECTED,
        EventType.MARKETPLACE_ORDER_TERMINATED,
        EventType.MARKETPLACE_ORDER_UNLINKED,
        EventType.MARKETPLACE_RESOURCE_CREATE_CANCELED,
        EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_CREATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_CREATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_DOWNSCALED,
        EventType.MARKETPLACE_RESOURCE_ERRED_ON_BACKEND,
        EventType.MARKETPLACE_RESOURCE_PAUSED,
        EventType.REQUEST_DOWNSCALING,
        EventType.REQUEST_PAUSING,
        EventType.REQUEST_SLURM_RESOURCE_DOWNSCALING,
        EventType.REQUEST_SLURM_RESOURCE_PAUSING,
        EventType.RESET_DOWNSCALING,
        EventType.RESET_MEMBER_RESTRICTION,
        EventType.RESET_PAUSING,
        EventType.RESTRICT_MEMBERS,
        EventType.SLURM_POLICY_EVALUATION,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_CANCELED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_UNLINKED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_CANCELED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_FAILED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_REQUESTED,
        EventType.MARKETPLACE_RESOURCE_UPDATE_SUCCEEDED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_CREATED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_APPROVED,
        EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_REJECTED,
        EventType.OPENSTACK_FLOATING_IP_ATTACHED,
        EventType.OPENSTACK_FLOATING_IP_CONNECTED,
        EventType.OPENSTACK_FLOATING_IP_DESCRIPTION_UPDATED,
        EventType.OPENSTACK_FLOATING_IP_DETACHED,
        EventType.OPENSTACK_FLOATING_IP_DISCONNECTED,
        EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED,
        EventType.OPENSTACK_NETWORK_CLEANED,
        EventType.OPENSTACK_NETWORK_CREATED,
        EventType.OPENSTACK_NETWORK_DELETED,
        EventType.OPENSTACK_NETWORK_IMPORTED,
        EventType.OPENSTACK_NETWORK_PULLED,
        EventType.OPENSTACK_NETWORK_UPDATED,
        EventType.OPENSTACK_LOAD_BALANCER_CREATED,
        EventType.OPENSTACK_LOAD_BALANCER_UPDATED,
        EventType.OPENSTACK_LOAD_BALANCER_DELETED,
        EventType.OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED,
        EventType.OPENSTACK_LISTENER_CREATED,
        EventType.OPENSTACK_LISTENER_UPDATED,
        EventType.OPENSTACK_LISTENER_DELETED,
        EventType.OPENSTACK_POOL_CREATED,
        EventType.OPENSTACK_POOL_UPDATED,
        EventType.OPENSTACK_POOL_DELETED,
        EventType.OPENSTACK_POOL_MEMBER_CREATED,
        EventType.OPENSTACK_POOL_MEMBER_UPDATED,
        EventType.OPENSTACK_POOL_MEMBER_DELETED,
        EventType.OPENSTACK_PORT_CLEANED,
        EventType.OPENSTACK_PORT_CREATED,
        EventType.OPENSTACK_PORT_DELETED,
        EventType.OPENSTACK_PORT_IMPORTED,
        EventType.OPENSTACK_PORT_PULLED,
        EventType.OPENSTACK_PORT_UPDATED,
        EventType.OPENSTACK_PORT_SECURITY_ENABLED,
        EventType.OPENSTACK_PORT_SECURITY_DISABLED,
        EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED,
        EventType.OPENSTACK_PORT_SECURITY_GROUPS_CHANGED,
        EventType.OPENSTACK_ROUTER_UPDATED,
        EventType.OPENSTACK_SECURITY_GROUP_CLEANED,
        EventType.OPENSTACK_SECURITY_GROUP_CREATED,
        EventType.OPENSTACK_SECURITY_GROUP_DELETED,
        EventType.OPENSTACK_SECURITY_GROUP_IMPORTED,
        EventType.OPENSTACK_SECURITY_GROUP_PULLED,
        EventType.OPENSTACK_SECURITY_GROUP_RULE_CLEANED,
        EventType.OPENSTACK_SECURITY_GROUP_RULE_CREATED,
        EventType.OPENSTACK_SECURITY_GROUP_RULE_DELETED,
        EventType.OPENSTACK_SECURITY_GROUP_RULE_IMPORTED,
        EventType.OPENSTACK_SECURITY_GROUP_RULE_UPDATED,
        EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED,
        EventType.OPENSTACK_SECURITY_GROUP_UPDATED,
        EventType.OPENSTACK_SERVER_GROUP_CLEANED,
        EventType.OPENSTACK_SERVER_GROUP_CREATED,
        EventType.OPENSTACK_SERVER_GROUP_DELETED,
        EventType.OPENSTACK_SERVER_GROUP_IMPORTED,
        EventType.OPENSTACK_SERVER_GROUP_PULLED,
        EventType.OPENSTACK_SUBNET_CLEANED,
        EventType.OPENSTACK_SUBNET_CREATED,
        EventType.OPENSTACK_SUBNET_DELETED,
        EventType.OPENSTACK_SUBNET_IMPORTED,
        EventType.OPENSTACK_SUBNET_PULLED,
        EventType.OPENSTACK_SUBNET_UPDATED,
        EventType.OPENSTACK_TENANT_QUOTA_LIMIT_UPDATED,
        EventType.RESOURCE_ASSIGN_FLOATING_IP_FAILED,
        EventType.RESOURCE_ASSIGN_FLOATING_IP_SCHEDULED,
        EventType.RESOURCE_ASSIGN_FLOATING_IP_SUCCEEDED,
        EventType.RESOURCE_ATTACH_FAILED,
        EventType.RESOURCE_ATTACH_SCHEDULED,
        EventType.RESOURCE_ATTACH_SUCCEEDED,
        EventType.RESOURCE_CHANGE_FLAVOR_FAILED,
        EventType.RESOURCE_CHANGE_FLAVOR_SCHEDULED,
        EventType.RESOURCE_CHANGE_FLAVOR_SUCCEEDED,
        EventType.RESOURCE_CREATION_FAILED,
        EventType.RESOURCE_CREATION_SCHEDULED,
        EventType.RESOURCE_CREATION_SUCCEEDED,
        EventType.RESOURCE_DELETION_FAILED,
        EventType.RESOURCE_DELETION_SCHEDULED,
        EventType.RESOURCE_DELETION_SUCCEEDED,
        EventType.RESOURCE_DETACH_FAILED,
        EventType.RESOURCE_DETACH_SCHEDULED,
        EventType.RESOURCE_DETACH_SUCCEEDED,
        EventType.RESOURCE_EXTEND_FAILED,
        EventType.RESOURCE_EXTEND_SCHEDULED,
        EventType.RESOURCE_EXTEND_SUCCEEDED,
        EventType.RESOURCE_EXTEND_VOLUME_FAILED,
        EventType.RESOURCE_EXTEND_VOLUME_SCHEDULED,
        EventType.RESOURCE_EXTEND_VOLUME_SUCCEEDED,
        EventType.RESOURCE_IMPORT_SUCCEEDED,
        EventType.RESOURCE_PULL_FAILED,
        EventType.RESOURCE_PULL_SCHEDULED,
        EventType.RESOURCE_PULL_SUCCEEDED,
        EventType.RESOURCE_RESTART_FAILED,
        EventType.RESOURCE_RESTART_SCHEDULED,
        EventType.RESOURCE_RESTART_SUCCEEDED,
        EventType.RESOURCE_RETYPE_FAILED,
        EventType.RESOURCE_RETYPE_SCHEDULED,
        EventType.RESOURCE_RETYPE_SUCCEEDED,
        EventType.RESOURCE_ROBOT_ACCOUNT_CREATED,
        EventType.RESOURCE_ROBOT_ACCOUNT_DELETED,
        EventType.RESOURCE_ROBOT_ACCOUNT_STATE_CHANGED,
        EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
        EventType.RESOURCE_START_FAILED,
        EventType.RESOURCE_START_SCHEDULED,
        EventType.RESOURCE_START_SUCCEEDED,
        EventType.RESOURCE_STOP_FAILED,
        EventType.RESOURCE_STOP_SCHEDULED,
        EventType.RESOURCE_STOP_SUCCEEDED,
        EventType.RESOURCE_UNASSIGN_FLOATING_IP_FAILED,
        EventType.RESOURCE_UNASSIGN_FLOATING_IP_SCHEDULED,
        EventType.RESOURCE_UNASSIGN_FLOATING_IP_SUCCEEDED,
        EventType.RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_FAILED,
        EventType.RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_SCHEDULED,
        EventType.RESOURCE_UPDATE_ALLOWED_ADDRESS_PAIRS_SUCCEEDED,
        EventType.RESOURCE_UPDATE_FLOATING_IPS_FAILED,
        EventType.RESOURCE_UPDATE_FLOATING_IPS_SCHEDULED,
        EventType.RESOURCE_UPDATE_FLOATING_IPS_SUCCEEDED,
        EventType.RESOURCE_UPDATE_PORTS_FAILED,
        EventType.RESOURCE_UPDATE_PORTS_SCHEDULED,
        EventType.RESOURCE_UPDATE_PORTS_SUCCEEDED,
        EventType.RESOURCE_UPDATE_SECURITY_GROUPS_FAILED,
        EventType.RESOURCE_UPDATE_SECURITY_GROUPS_SCHEDULED,
        EventType.RESOURCE_UPDATE_SECURITY_GROUPS_SUCCEEDED,
        EventType.RESOURCE_UPDATE_SUCCEEDED,
    ],
    EventGroup.REVIEW: [
        EventType.REVIEW_CANCELED,
    ],
    EventGroup.SSH: [
        EventType.SSH_KEY_CREATION_SUCCEEDED,
        EventType.SSH_KEY_DELETION_SUCCEEDED,
    ],
    EventGroup.SUPPORT: [
        EventType.ATTACHMENT_CREATED,
        EventType.ATTACHMENT_DELETED,
        EventType.ATTACHMENT_UPDATED,
        EventType.ISSUE_CREATION_SUCCEEDED,
        EventType.ISSUE_DELETION_SUCCEEDED,
        EventType.ISSUE_UPDATE_SUCCEEDED,
    ],
    EventGroup.USERS: [
        EventType.AUTH_LOGGED_IN_WITH_SAML2,
        EventType.AUTH_LOGGED_OUT_WITH_SAML2,
        EventType.FREEIPA_PROFILE_CREATED,
        EventType.FREEIPA_PROFILE_DELETED,
        EventType.FREEIPA_PROFILE_DISABLED,
        EventType.FREEIPA_PROFILE_ENABLED,
        EventType.MARKETPLACE_OFFERING_USER_CREATED,
        EventType.MARKETPLACE_OFFERING_USER_DELETED,
        EventType.MARKETPLACE_OFFERING_USER_RESTRICTION_UPDATED,
        EventType.SSH_KEY_CREATION_SUCCEEDED,
        EventType.SSH_KEY_DELETION_SUCCEEDED,
        EventType.USER_ACTIVATED,
        EventType.USER_CREATION_SUCCEEDED,
        EventType.USER_DATA_ACCESSED,
        EventType.USER_DEACTIVATED,
        EventType.USER_DELETION_SUCCEEDED,
        EventType.USER_DETAILS_UPDATE_SUCCEEDED,
        EventType.USER_HAS_BEEN_CREATED_BY_STAFF,
        EventType.USER_PASSWORD_UPDATED,
        EventType.USER_PASSWORD_UPDATED_BY_STAFF,
        EventType.USER_PASSWORD_REMOVED_BY_STAFF,
        EventType.USER_UPDATE_SUCCEEDED,
        EventType.USER_GROUP_INVITATION_UPDATED,
        EventType.USER_INVITATION_UPDATED,
        EventType.USER_INVITATION_DELETED,
    ],
    EventGroup.TERMS_OF_SERVICE: [
        EventType.TERMS_OF_SERVICE_CONSENT_GRANTED,
        EventType.TERMS_OF_SERVICE_CONSENT_REVOKED,
    ],
    EventGroup.ONBOARDING: [
        EventType.ONBOARDING_VERIFICATION_DELETED,
        EventType.ONBOARDING_VERIFICATION_DELETED_BY_TASK,
    ],
    EventGroup.CHAT: [
        EventType.CHAT_SESSION_ACCESSED,
        EventType.CHAT_THREAD_ACCESSED,
        EventType.CHAT_INJECTION_DETECTED,
        EventType.CHAT_PII_DETECTED,
        EventType.CHAT_FEEDBACK_SUBMITTED,
    ],
}

RESOURCE_CHANGE_EVENTS = (
    EventType.MARKETPLACE_RESOURCE_CREATE_SUCCEEDED,
    EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
    EventType.MARKETPLACE_RESOURCE_CREATE_CANCELED,
    EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
    EventType.MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED,
    EventType.MARKETPLACE_RESOURCE_TERMINATE_FAILED,
    EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_FAILED,
)


class ObservableObjectType(Enum):
    ORDER = "order"
    USER_ROLE = "user_role"
    RESOURCE = "resource"
    OFFERING_USER = "offering_user"
    IMPORTABLE_RESOURCES = "importable_resources"
    SERVICE_ACCOUNT = "service_account"
    COURSE_ACCOUNT = "course_account"
    RESOURCE_PERIODIC_LIMITS = "resource_periodic_limits"

    @classmethod
    def choices(cls):
        return [(t.value, t.value) for t in cls]
