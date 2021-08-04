from django.db import models

from waldur_core.structure import models as structure_models


class CustomerGroup(models.Model):
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    backend_id = models.UUIDField()


class ProjectGroup(models.Model):
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    backend_id = models.UUIDField()
