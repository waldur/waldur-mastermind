from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.plugins import Component

OS_LB_SECURITY_GROUPS = ["k8s_admin", "k8s_public"]
OS_SUBNET_4_OCTET_START_IP = 11
OS_SUBNET_4_OCTET_END_IP = 200
OS_LB_VM_4_OCTET_IP = 10
OS_LB_PREFIX = "k8s-lb-"

RANCHER_BILLING_COMPONENTS = [
    Component(
        type="cpu_hours",
        name="CPU hours",
        measured_unit="vCPU-hours",
        billing_type=BillingTypes.USAGE,
    ),
    Component(
        type="ram_hours",
        name="RAM hours",
        measured_unit="GB-hours",
        billing_type=BillingTypes.USAGE,
    ),
    Component(
        type="storage_hours",
        name="Storage hours",
        measured_unit="GB-hours",
        billing_type=BillingTypes.USAGE,
    ),
]


DEPLOYMENT_MODE_MANAGED = "managed"
DEPLOYMENT_MODE_SELF_MANAGED = "self_managed"
