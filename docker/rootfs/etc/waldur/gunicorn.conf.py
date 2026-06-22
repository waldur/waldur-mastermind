import os

bind = ":8080"
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


def post_fork(server, worker):
    # With preload the master imports the WSGI app before forking; drop any
    # database connections it may have opened so each worker reconnects on its
    # own. When preload is off the app is not yet loaded at this point, so skip
    # to avoid configuring Django settings prematurely in the forked worker.
    from django.conf import settings

    if settings.configured:
        from django.db import connections

        connections.close_all()
