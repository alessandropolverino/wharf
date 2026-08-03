"""HTTP healthcheck polling, run from the machine orchestrating the deploy.

The healthcheck URL is polled from wherever wharf itself runs (your
laptop, or the CI runner) rather than from the target host -- matching
the config's intent that it be a URL reachable from outside, e.g. a
public load balancer or health endpoint.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

ATTEMPTS = 10
DELAY_SECONDS = 6.0


def wait_healthy(url: str, *, attempts: int = ATTEMPTS, delay: float = DELAY_SECONDS) -> None:
    """Poll ``url`` until it responds successfully, or raise.

    Uses :mod:`urllib` rather than shelling out to curl: it's stdlib, so
    no extra tool needs to be installed on whatever machine runs wharf.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=5):
                print("Healthy.")
                return
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                print(f"Attempt {attempt} not healthy yet ({exc}), retrying in {delay}s...")
                time.sleep(delay)
    raise RuntimeError(
        f"Health check for {url} failed after {attempts} attempts"
    ) from last_error
