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
