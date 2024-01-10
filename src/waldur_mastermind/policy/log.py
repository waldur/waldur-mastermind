from waldur_core.logging.loggers import EventLogger, event_logger


class PolicyActionOrderLogger(EventLogger):
    policy_uuid = str

    class Meta:
        event_types = (
            "notify_project_team",
            "notify_organization_owners",
            "block_creation_of_new_resources",
            "block_modification_of_existing_resources",
            "terminate_resources",
            "request_downscaling",
        )


event_logger.register("policy_action", PolicyActionOrderLogger)


class SendEmailLogger(EventLogger):
    scope = str
    policy_uuid = str
    emails = str

    class Meta:
        event_types = ("policy_notification",)


event_logger.register("policy_notification", SendEmailLogger)
