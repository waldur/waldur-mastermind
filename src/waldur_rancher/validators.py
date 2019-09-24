import re

from django.core.validators import RegexValidator
from django.utils.translation import ugettext_lazy as _

ClusterNameValidator = RegexValidator(
    regex=re.compile(r"^[a-z0-9]([-a-z0-9])+[a-z0-9]$"),
    message=_('Name must consist of lower case alphanumeric characters or \'-\', '
              'and must start and end with an alphanumeric character')
)
