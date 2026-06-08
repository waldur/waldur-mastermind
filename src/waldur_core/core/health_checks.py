"""
Custom health check backends for Waldur.

This module provides optimized health check implementations that address
performance issues with the default django-health-check celery_ping backend.
"""

import logging

from celery import current_app
from health_check.backends import BaseHealthCheckBackend
from health_check.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)


class CeleryWorkersHealthCheck(BaseHealthCheckBackend):
    """
    Fast Celery health check using targeted pings.

    This is an optimized replacement for health_check.contrib.celery_ping that:
    1. Uses targeted pings instead of broadcast to avoid waiting for timeout
    2. Returns as soon as workers respond instead of waiting for full timeout
    3. Uses a fresh, non-pooled broker connection per check so stale pooled
       sockets cannot produce false-positive outage alerts (CSCS-5B3)

    Performance comparison:
    - Default celery_ping (broadcast): 1000-5000ms per check
    - This implementation (targeted): ~50-90ms per check (includes TCP+AMQP
      handshake; was 17-40ms when pooling was used)

    The default broadcast ping always waits for the full timeout duration to
    collect responses from any possible workers. This causes issues when:
    - Health checks run frequently (e.g., every 2 minutes from monitoring)
    - Workers are temporarily busy with long-running tasks
    - Network has minor fluctuations

    This implementation pings workers individually and returns immediately
    when they respond, making health checks ~50x faster and more reliable.
    """

    # Don't mark as critical - API can still serve requests even if workers are slow
    critical_service = False

    def check_status(self):
        timeout = 2  # seconds per worker ping
        # Short discovery timeout - workers should respond within 200ms if healthy
        # This reduces health check time from ~1000ms to ~260ms
        discovery_timeout = 0.2

        try:
            # Use a fresh, non-pooled broker connection for each check.
            #
            # The broker connection pool (connection_or_acquire) does not
            # liveness-check on borrow, so it can hand back sockets that the
            # broker or kernel TCP keepalive closed while the connection sat
            # idle in the pool between checks. In a sync gunicorn worker
            # there is no event loop running heartbeat_check(), so an idle
            # pooled connection is reliably killed after ~60s with the
            # current BROKER_HEARTBEAT=30s. The first publish on the stale
            # socket then raises BrokenPipeError / ConnectionResetError and
            # we report a false-positive broker outage (CSCS-5B3).
            #
            # ensure_connection(max_retries=1) retries once if the initial
            # connect races with a broker restart.
            with current_app.connection() as conn:
                conn.ensure_connection(max_retries=1)
                # Quick discovery: which workers are available?
                # Use short timeout since we just need to find responsive workers
                discovery_result = current_app.control.ping(
                    timeout=discovery_timeout, connection=conn
                )

                if not discovery_result:
                    self.add_error(
                        ServiceUnavailable("No Celery workers responded to ping")
                    )
                    return

                # Extract worker names from discovery
                workers = [list(r.keys())[0] for r in discovery_result]
                logger.debug(f"Discovered {len(workers)} Celery workers: {workers}")

                # Verify each worker is responsive with targeted ping
                # This is fast because targeted pings return immediately on response
                for worker in workers:
                    ping_result = current_app.control.ping(
                        destination=[worker], timeout=timeout, connection=conn
                    )

                    if not ping_result:
                        self.add_error(
                            ServiceUnavailable(f"Worker {worker} did not respond")
                        )
                    else:
                        # Verify we got the expected "pong" response
                        response = ping_result[0].get(worker, {})
                        if response.get("ok") != "pong":
                            self.add_error(
                                ServiceUnavailable(
                                    f"Worker {worker} responded incorrectly: {response}"
                                )
                            )

        except OSError as e:
            self.add_error(ServiceUnavailable("Broker connection error"), e)
        except Exception as e:
            logger.exception("Celery health check failed")
            self.add_error(ServiceUnavailable(f"Celery check failed: {e}"))

    def identifier(self):
        return "Celery Workers"
