from waldur_core.core import managers as core_managers

from . import models


class ApplicationSummaryQuerySet(core_managers.SummaryQuerySet):
    @property
    def model(self):
        return models.ApplicationModel
