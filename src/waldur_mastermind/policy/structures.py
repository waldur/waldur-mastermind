from collections.abc import Callable, Collection
from dataclasses import dataclass

from . import enums


@dataclass
class PolicyAction:
    action_type: enums.PolicyActionTypes
    method: Callable
    reset_method: Callable | None = None
    options_validator: Callable | None = None
    ignored_fields: Collection[str] | None = None
