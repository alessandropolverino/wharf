"""Best-effort check for a newer wharf release on GitHub.

wharf isn't published as prebuilt release assets anywhere yet -- it's
installed by pinning a git tag (see README's Install section). So this
module doesn't download or install anything; it only compares the
latest published git tag against the running version and returns a
one-line notice for the CLI to print. Any failure (no network, repo not
public yet, no releases published, rate-limited) is treated the same as
"nothing to report" -- an update notice must never break a deploy.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError

from . import __version__

# Note: the GitHub REST API requires authentication to read releases on a
# private repo, which this repo currently is -- until it's made public (or
# this module is given a token), check_for_update() will always fail closed
# via the except clause below and never report anything. That's fine: it's
# still a silent no-op, exactly like every other failure mode here.
GITHUB_REPO = "alessandropolverino/wharf"
_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_TIMEOUT_SECONDS = 2.0


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); non-numeric parts become 0."""
    digits_only = []
    for piece in tag.lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        digits_only.append(int(digits) if digits else 0)
    return tuple(digits_only)


def check_for_update() -> str | None:
    """Return a one-line notice if a newer release exists, else None."""
    try:
        with urllib.request.urlopen(_RELEASES_URL, timeout=_TIMEOUT_SECONDS) as response:
            latest_tag = json.load(response)["tag_name"]
        if not isinstance(latest_tag, str):
            return None
        if _parse_version(latest_tag) > _parse_version(__version__):
            return (
                f"wharf: a newer release is available ({latest_tag}, running "
                f"{__version__}) -- see https://github.com/{GITHUB_REPO}/releases"
            )
    except (URLError, TimeoutError, KeyError, ValueError, OSError, AttributeError, TypeError):
        pass
    return None
