# Re-exported. Defined in marketplace.enums next to the User-Agent mark
# ``waldur-site-agent-<mode>``, so the service mode and the request that
# refreshes usage reporting stay the same word. A quiet report service is
# waiting for the provider's next accounting run, so the idle check skips it.
from waldur_mastermind.marketplace.enums import (
    REPORT_SERVICE_MODE as REPORT_SERVICE_MODE,
)


class LogLevel:
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    CHOICES = (
        (DEBUG, "Debug"),
        (INFO, "Info"),
        (WARNING, "Warning"),
        (ERROR, "Error"),
        (CRITICAL, "Critical"),
    )

    VALUES = [val for (val, _) in CHOICES]


class AgentServiceState:
    ACTIVE = 1
    IDLE = 2
    ERROR = 3

    CHOICES = ((ACTIVE, "Active"), (IDLE, "Idle"), (ERROR, "Error"))

    VALUES = [val for (_, val) in CHOICES]
