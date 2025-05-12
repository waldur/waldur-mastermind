from waldur_core.core.enums import CoreStates
from waldur_core.core.tasks import ErrorStateTransitionTask


class SetInstanceErredTask(ErrorStateTransitionTask):
    """Mark instance as erred and delete resources that were not created."""

    def execute(self, instance):
        super().execute(instance)

        # delete volume if it were not created on backend,
        # mark as erred if creation was started, but not ended,
        volume = instance.volume_set.first()
        if volume.state == CoreStates.CREATION_SCHEDULED:
            volume.delete()
        elif volume.state == CoreStates.OK:
            pass
        else:
            volume.set_erred()
            volume.save(update_fields=["state"])
