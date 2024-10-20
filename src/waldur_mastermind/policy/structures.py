from collections.abc import Callable
from dataclasses import dataclass

from . import enums


@dataclass
class PolicyAction:
    action_type: enums.PolicyActionTypes
    method: Callable
    reset_method: Callable | None = None
