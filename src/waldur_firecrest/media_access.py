"""Media access rules for files owned by the firecrest plugin.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_firecrest.models import Job

# Batch scripts follow BaseResource.Permissions (project / project__customer),
# the same rule ResourceViewSet lists jobs with.
access.register(
    access.upload_prefix(Job, "file"),
    access.queryset_rule(Job, ["file"], filter_queryset_for_user),
)
