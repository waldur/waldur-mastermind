from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.plugins import Component

from . import CORES_TYPE, RAM_TYPE, STORAGE_TYPE

TENANT_COMPONENTS = (
    Component(
        type=CORES_TYPE,
        name="Cores",
        measured_unit="cores",
        billing_type=BillingTypes.LIMIT,
        limit_period=LimitPeriods.MONTH,
    ),
    # Price is stored per GiB but size is stored per MiB
    # therefore we need to divide size by factor when price estimate is calculated.
    Component(
        type=RAM_TYPE,
        name="RAM",
        measured_unit="GB",
        billing_type=BillingTypes.LIMIT,
        factor=1024,
        limit_period=LimitPeriods.MONTH,
    ),
    Component(
        type=STORAGE_TYPE,
        name="Storage",
        measured_unit="GB",
        billing_type=BillingTypes.LIMIT,
        factor=1024,
        limit_period=LimitPeriods.MONTH,
    ),
)
