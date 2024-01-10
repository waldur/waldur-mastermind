from waldur_core.logging.loggers import EventLogger, event_logger


class ProjectUpdateRequestLogger(EventLogger):
    project = "structure.Project"
    offering = "marketplace.Offering"

    class Meta:
        event_types = (
            "project_update_request_created",
            "project_update_request_approved",
            "project_update_request_rejected",
        )
        event_groups = {
            "projects": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["project"]}


event_logger.register("project_update_request", ProjectUpdateRequestLogger)
