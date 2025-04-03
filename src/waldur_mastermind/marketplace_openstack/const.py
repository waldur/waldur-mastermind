from waldur_mastermind.marketplace.models import OfferingComponent
from waldur_mastermind.marketplace.plugins import Component

from . import CORES_TYPE, RAM_TYPE, STORAGE_TYPE

LIMIT = OfferingComponent.BillingTypes.LIMIT
MONTH = OfferingComponent.LimitPeriods.MONTH

TENANT_COMPONENTS = (
    Component(
        type=CORES_TYPE,
        name="Cores",
        measured_unit="cores",
        billing_type=LIMIT,
        limit_period=MONTH,
    ),
    # Price is stored per GiB but size is stored per MiB
    # therefore we need to divide size by factor when price estimate is calculated.
    Component(
        type=RAM_TYPE,
        name="RAM",
        measured_unit="GB",
        billing_type=LIMIT,
        factor=1024,
        limit_period=MONTH,
    ),
    Component(
        type=STORAGE_TYPE,
        name="Storage",
        measured_unit="GB",
        billing_type=LIMIT,
        factor=1024,
        limit_period=MONTH,
    ),
)
