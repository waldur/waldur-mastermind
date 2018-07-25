from waldur_core.core.tasks import ErrorStateTransitionTask

from .models import Volume


class SetInstanceErredTask(ErrorStateTransitionTask):
    """ Mark instance as erred and delete resources that were not created. """

    def execute(self, instance):
        super(SetInstanceErredTask, self).execute(instance)

        # delete volume if it were not created on backend,
        # mark as erred if creation was started, but not ended,
        volume = instance.volume_set.first()
        if volume.state == Volume.States.CREATION_SCHEDULED:
            volume.delete()
        elif volume.state == Volume.States.OK:
            pass
        else:
            volume.set_erred()
            volume.save(update_fields=['state'])
