# `update_check.py`

One function: `check_for_update()` — a best-effort check for a newer
wharf release on GitHub, called from [`cli.md`](cli.md)'s `main()`
before every command except `ls`.

Compares the running `__version__` against the latest published git tag
(`GET /repos/<repo>/releases/latest`) and returns a one-line notice
string if a newer one exists, else `None`. wharf isn't published as
prebuilt release assets — it's installed by pinning a git tag — so this
never downloads or installs anything, it only prints a notice.

**Fails closed, always silently.** Any failure — no network, rate
limited, the repo not public, no releases published yet — is caught by
a broad `except` and treated identically to "nothing to report": an
update notice must never break a deploy. (The GitHub REST API requires
auth to read releases on a private repo, which this repo currently is —
until it's public, or this module is given a token, the check will
always fail closed via that same path.)

`_parse_version("v1.2.3")` → `(1, 2, 3)`; non-numeric parts become `0`,
so version comparison never raises on an unexpected tag format.
