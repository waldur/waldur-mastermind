import os

# Bind the IPv6 wildcard when the kernel has IPv6: with the Linux default
# net.ipv6.bindv6only=0 that single socket also accepts IPv4 connections (as
# v4-mapped), so it is correct on IPv4-only, dual-stack and IPv6-only clusters
# alike. A bare ":8080" resolves to host "" -> AF_INET -> 0.0.0.0 only, leaving
# the pod unreachable at its IPv6 address. /proc/net/if_inet6 is absent exactly
# when IPv6 is compiled out or disabled at boot, where binding "[::]" would fail
# outright. Override with GUNICORN_CMD_ARGS="--bind <addr>" if needed.
bind = "[::]:8080" if os.path.exists("/proc/net/if_inet6") else ":8080"
# `... or <default>` so an env var that is present but empty (e.g. passed
# through from docker-compose / helm without a value) falls back to the default
# rather than blowing up on int("") or silently disabling preload.
workers = int(os.environ.get("GUNICORN_WORKERS") or 4)
# Preload imports the application in the master process before forking workers,
# so the read-only import footprint is shared copy-on-write instead of duplicated
# per worker. Recycling workers after a bounded number of requests wipes any
# accumulated high-watermark memory.
preload_app = (os.environ.get("GUNICORN_PRELOAD") or "true").lower() == "true"
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS") or 1000)
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER") or 50)
forwarded_allow_ips = "*"
proxy_allow_ips = "*"
accesslog = "-"
access_log_format = (
    '%({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
)
errorlog = "-"
capture_output = True

# A Matrix homeserver authenticates its calls to the appservice with
# ?access_token=<hs_token>, and all of them go to /_matrix/app/. Their request
# line is logged without the query string; every other request keeps its own.
APPSERVICE_PATH_PREFIX = "/_matrix/app/"


def logger_class():
    # Gunicorn calls a function given as logger_class and uses the class it
    # returns, so gunicorn is imported only when gunicorn loads this file; tests
    # exec it in an environment without gunicorn.
    from gunicorn.glogging import Logger

    class AccessLogger(Logger):
        def atoms(self, resp, req, environ, request_time):
            atoms = super().atoms(resp, req, environ, request_time)
            path = environ.get("PATH_INFO") or ""
            if path.startswith(APPSERVICE_PATH_PREFIX):
                atoms["r"] = "%s %s %s" % (
                    environ["REQUEST_METHOD"],
                    path,
                    environ["SERVER_PROTOCOL"],
                )
                # The same query reaches the format through these atoms too.
                atoms["q"] = ""
                atoms["{query_string}e"] = ""
                atoms["{raw_uri}e"] = path
            return atoms

    return AccessLogger


def post_fork(server, worker):
    # With preload the master imports the WSGI app before forking; drop any
    # database connections it may have opened so each worker reconnects on its
    # own. When preload is off the app is not yet loaded at this point, so skip
    # to avoid configuring Django settings prematurely in the forked worker.
    from django.conf import settings

    if settings.configured:
        from django.db import connections

        connections.close_all()
