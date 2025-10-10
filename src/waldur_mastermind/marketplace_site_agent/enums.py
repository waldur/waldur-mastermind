class AgentServiceState:
    ACTIVE = 1
    IDLE = 2
    ERROR = 3

    CHOICES = ((ACTIVE, "Active"), (IDLE, "Idle"), (ERROR, "Error"))

    VALUES = [val for (_, val) in CHOICES]
