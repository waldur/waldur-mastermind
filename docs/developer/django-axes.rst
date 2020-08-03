Django-axes usage
=================

Django-axes prevents login brute forcing.
There are several subcommands awailable for the **waldur-shell**:
 - **axes_list_attempts** - list all login attempts;
 - **axes_reset** - reset all lockouts and access records;
 - **axes_reset_ip <IP>** - reset lockouts only for a provided **IP**;
 - **axes_reset_logs <age>** - reset all logs that are older than provided **age**;
 - **axes_reset_user <username>** - reset all lockouts and records for a provided **username**;
 - **axes_reset_username <username>** - same as **axes_reset_user**.

If you accidentally locked yourself during development, then enter:
 **waldur axes_reset_ip 127.0.0.1**
