"""Load and validate wharf's YAML configuration format.

A wharf config file describes *one environment* (e.g. "production" or
"staging") for a project: where the deploy-time git remote lives, which
compose file to run, an optional shared Infisical secrets configuration,
and an ordered list of targets (hosts) to roll out to sequentially.

The schema is intentionally flat -- there is no "production:"/"staging:"
wrapper key. Which environment a file represents is a fact about the
*filename* (``deploy.yml`` vs ``deploy.staging.yml``), not something the
tool needs to know about. This keeps a single config file fully
self-describing and lets you point wharf at any file without an
``--environment`` flag.

Example (see docs/configuration.md for more):

    version: 1
    remote_repo: /srv/git/{repo}.git
    branch: main
    compose_file: docker-compose.prod.yml
    secrets:
      provider: infisical
      project_id: 11111111-2222-3333-4444-555555555555
      domain: https://eu.infisical.com
      environment: prod
    targets:
      - name: etl
        remote_dir: /opt/deploys/{repo}/app
        host: 203.0.113.10
        port: 22
        user: ubuntu
        host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
        order: 10
        healthcheck: https://etl.internal/health
        paths: ["/janus-etl-pipeline/"]

``{repo}`` in ``remote_repo`` and a target's ``remote_dir`` is substituted
with the project name (from ``--repo`` or inferred from the local git
checkout) at deploy time -- see :func:`render_repo_template`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

SUPPORTED_SECRETS_PROVIDERS = frozenset({"infisical"})
DEFAULT_BRANCH = "main"
DEFAULT_COMPOSE_FILE = "docker-compose.yml"


class ConfigError(ValueError):
    """Raised for any structurally or semantically invalid config file."""


def render_repo_template(template: str, repo: str) -> str:
    """Substitute the ``{repo}`` placeholder used in path-shaped fields.

    A plain ``str.replace`` (rather than ``str.format``) is used
    deliberately: these templates are filesystem/git paths, which may
    legitimately contain other brace-like characters we don't want to
    treat as format fields.
    """
    return template.replace("{repo}", repo)


@dataclass(frozen=True)
class SecretsDefaults:
    """Shared Infisical settings, defined once per config file.

    Only the *location* of the secrets is configured here (which project,
    which environment, which domain). Credentials themselves are never
    stored in the config -- they're expected to already be available as
    environment variables wherever the remote command actually runs (see
    docs/configuration.md's "Secrets" section).
    """

    provider: str
    project_id: str
    domain: str
    environment: str


@dataclass(frozen=True)
class Target:
    """A single deploy destination within a config file."""

    name: str
    remote_dir: str
    host: str
    port: int
    user: str
    host_key: str
    order: int
    healthcheck: str | None = None
    compose_file: str | None = None
    paths: tuple[str, ...] | None = None

    @property
    def uses_secrets(self) -> bool:
        """A target opts into secrets injection purely by declaring paths."""
        return bool(self.paths)


@dataclass(frozen=True)
class Config:
    """A fully parsed, validated config file."""

    remote_repo: str
    branch: str
    compose_file: str
    secrets: SecretsDefaults | None
    targets: tuple[Target, ...]  # always sorted by Target.order

    def compose_file_for(self, target: Target) -> str:
        """A target's own compose_file overrides the file-level default."""
        return target.compose_file or self.compose_file

    def select_targets(self, only: tuple[str, ...] = ()) -> tuple[Target, ...]:
        """Return targets in deploy order, optionally filtered by name.

        Raises ConfigError if a name in ``only`` doesn't match any target,
        so a typo in ``--only`` fails loudly instead of silently deploying
        nothing.
        """
        if not only:
            return self.targets
        known = {t.name for t in self.targets}
        unknown = set(only) - known
        if unknown:
            raise ConfigError(
                f"--only references unknown target(s): {', '.join(sorted(unknown))}"
            )
        return tuple(t for t in self.targets if t.name in only)


def _exact_keys(value: object, required: set[str], optional: frozenset[str] = frozenset(), *, label: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    keys = set(value)
    allowed = required | optional
    if not required <= keys <= allowed:
        missing = required - keys
        unexpected = keys - allowed
        problems = []
        if missing:
            problems.append(f"missing {', '.join(sorted(missing))}")
        if unexpected:
            problems.append(f"unexpected {', '.join(sorted(unexpected))}")
        raise ConfigError(f"invalid fields in {label}: {'; '.join(problems)}")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    return value


def _host_key(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    parts = text.split()
    if len(parts) != 2 or parts[0] != "ssh-ed25519":
        raise ConfigError(f"{label} must be an ssh-ed25519 public key")
    import base64

    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an ssh-ed25519 public key") from exc
    return text


def _url(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{label} must be an HTTP or HTTPS URL")
    return text


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty list of strings")
    return tuple(_nonempty_string(item, f"{label}[{i}]") for i, item in enumerate(value))


def _load_secrets_defaults(value: object) -> SecretsDefaults:
    _exact_keys(value, {"provider", "project_id", "domain", "environment"}, label="secrets")
    assert isinstance(value, dict)
    provider = _nonempty_string(value["provider"], "secrets.provider")
    if provider not in SUPPORTED_SECRETS_PROVIDERS:
        raise ConfigError(
            f"secrets.provider must be one of {sorted(SUPPORTED_SECRETS_PROVIDERS)}"
        )
    return SecretsDefaults(
        provider=provider,
        project_id=_nonempty_string(value["project_id"], "secrets.project_id"),
        domain=_url(value["domain"], "secrets.domain"),
        environment=_nonempty_string(value["environment"], "secrets.environment"),
    )


def _load_target(value: object, index: int, *, secrets_configured: bool) -> Target:
    label = f"targets[{index}]"
    required = {"name", "remote_dir", "host", "port", "user", "host_key", "order"}
    optional = frozenset({"healthcheck", "compose_file", "paths"})
    _exact_keys(value, required, optional, label=label)
    assert isinstance(value, dict)

    paths = _string_list(value["paths"], f"{label}.paths") if "paths" in value else None
    if paths and not secrets_configured:
        raise ConfigError(
            f"{label} declares paths but no top-level secrets block is configured"
        )

    return Target(
        name=_nonempty_string(value["name"], f"{label}.name"),
        remote_dir=_nonempty_string(value["remote_dir"], f"{label}.remote_dir"),
        host=_nonempty_string(value["host"], f"{label}.host"),
        port=_port(value["port"], f"{label}.port"),
        user=_nonempty_string(value["user"], f"{label}.user"),
        host_key=_host_key(value["host_key"], f"{label}.host_key"),
        order=_integer(value["order"], f"{label}.order"),
        healthcheck=_url(value["healthcheck"], f"{label}.healthcheck") if "healthcheck" in value else None,
        compose_file=_nonempty_string(value["compose_file"], f"{label}.compose_file") if "compose_file" in value else None,
        paths=paths,
    )


def _port(value: object, label: str) -> int:
    port = _integer(value, label)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label} must be between 1 and 65535")
    return port


def load_config(path: Path) -> Config:
    """Parse and fully validate a wharf config file.

    Raises ConfigError (structural/semantic problems), OSError (file not
    found/unreadable) or yaml.YAMLError (malformed YAML). Callers at the
    CLI boundary catch all three and turn them into a clean error message.
    """
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)

    _exact_keys(
        document,
        {"version", "remote_repo", "targets"},
        optional=frozenset({"branch", "compose_file", "secrets"}),
        label="root",
    )
    assert isinstance(document, dict)

    if _integer(document["version"], "version") != 1:
        raise ConfigError("version must be 1")

    secrets = _load_secrets_defaults(document["secrets"]) if "secrets" in document else None

    raw_targets = document["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("targets must be a non-empty list")

    targets = [
        _load_target(item, index, secrets_configured=secrets is not None)
        for index, item in enumerate(raw_targets)
    ]

    names = [t.name for t in targets]
    if len(names) != len(set(names)):
        raise ConfigError("target names must be unique")
    orders = [t.order for t in targets]
    if len(orders) != len(set(orders)):
        raise ConfigError("target order values must be unique")

    return Config(
        remote_repo=_nonempty_string(document["remote_repo"], "remote_repo"),
        branch=_nonempty_string(document["branch"], "branch") if "branch" in document else DEFAULT_BRANCH,
        compose_file=(
            _nonempty_string(document["compose_file"], "compose_file")
            if "compose_file" in document
            else DEFAULT_COMPOSE_FILE
        ),
        secrets=secrets,
        targets=tuple(sorted(targets, key=lambda t: t.order)),
    )
