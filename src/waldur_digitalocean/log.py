from waldur_core.logging.loggers import EventLogger, event_logger


class DropletResizeEventLogger(EventLogger):
    droplet = 'waldur_digitalocean.Droplet'
    size = 'waldur_digitalocean.Size'

    class Meta:
        event_types = ('droplet_resize_scheduled',
                       'droplet_resize_succeeded')

    @staticmethod
    def get_scopes(event_context):
        resource = event_context['droplet']
        project = resource.service_project_link.project
        return {resource, project, project.customer}


event_logger.register('droplet_resize', DropletResizeEventLogger)
