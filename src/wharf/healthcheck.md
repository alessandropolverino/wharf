# `healthcheck.py`

One function: `wait_healthy(url, attempts=10, delay=6.0)`.

Polls a target's `healthcheck` URL — after `deploy` and `reload` — until
it responds successfully (any non-error HTTP response) or raises after
exhausting `attempts`.

Polled from **wherever wharf itself runs** (laptop or CI runner), not
from the target host — matching the config's intent that `healthcheck`
be a URL reachable from *outside*, e.g. a public load balancer or health
endpoint, not `localhost` on the target.

Uses `urllib.request` rather than shelling out to `curl`: it's stdlib,
so no extra tool needs to be installed on whatever machine runs wharf.
