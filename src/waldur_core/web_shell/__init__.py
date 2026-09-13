"""Browser access to `waldur shell` for staff on development deployments.

The API mints single-use tickets (tickets.py, views.py). A separate process,
`waldur web_shell` (server.py), redeems them and runs `waldur shell` in a PTY
behind a ghostty-web terminal. Nothing here is reachable unless DEBUG and
WALDUR_CORE["WEB_SHELL_ENABLED"] are both on.
"""
