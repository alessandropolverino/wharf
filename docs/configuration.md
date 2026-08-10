# Configuration reference

A wharf config file describes **one environment** for a project — where
its git remote lives on each target, which compose file to run, and an
ordered list of targets to roll out to. There's no environment concept
inside the schema itself: `deploy.yml`, `deploy.staging.yml`,
`deploy.canary.yml`, whatever you call it, is a plain file wharf reads
top to bottom. Which environment it represents is a fact about the
filename, not the config.

## Full schema

```yaml
version: 1                              # required, must be 1

remote_repo: /srv/git/{repo}.git        # required. Path to the bare git
                                         # repo on each target. {repo} is
                                         # substituted with the project
                                         # name (see "The {repo} placeholder").

branch: main                            # optional, default "main".
                                         # The branch ref wharf pushes to
                                         # on each target's bare repo.

compose_file: docker-compose.prod.yml   # optional, default "docker-compose.yml".
                                         # File-level default; a target
                                         # may override it.

ensure_branch: main                     # optional, no default (unset = no check).
                                         # Every command refuses to run
                                         # unless the local checkout is
                                         # currently on this branch --
                                         # a guard against e.g. running
                                         # deploy.yml from a feature
                                         # branch by accident.

secrets:                                # optional. Omit entirely if no
                                         # target in this file uses secrets.
  provider: infisical                   # required if `secrets:` present.
                                         # Currently the only supported value.
  project_id: e291cf43-50b5-...         # required. Infisical project ID.
  domain: https://eu.infisical.com      # required. Infisical API domain,
                                         # https only -- this is where the
                                         # machine-identity credentials
                                         # get sent.
  environment: prod                     # required. Infisical environment
                                         # slug (prod / staging / dev / ...).

targets:                                # required, non-empty list.
  - name: etl                           # required, unique within the file.
    remote_dir: /opt/deploys/{repo}/app # required. Where the working
                                         # tree is checked out and compose
                                         # runs from on this target.
    host: 203.0.113.10                  # required.
    port: 22                            # required, 1-65535.
    user: ubuntu                        # required. SSH user.
    host_key: ssh-ed25519 AAAA...       # required. Pinned host key —
                                         # wharf never trusts an
                                         # unrecognized host key.
    order: 10                           # required, unique within the file.
                                         # Targets are always deployed in
                                         # ascending order, sequentially.
    healthcheck: https://etl/health     # optional. HTTP(S) URL polled
                                         # (10x / 6s) after `deploy` and
                                         # `reload` on this target.
    compose_file: docker-compose.yml    # optional. Overrides the
                                         # file-level compose_file for
                                         # just this target.
    paths: ["/etl/", "/shared/"]        # optional. Infisical secret
                                         # paths to inject for this
                                         # target. A target uses secrets
                                         # if and only if it sets `paths`
                                         # — requires a top-level
                                         # `secrets:` block to exist.
    pre_up:                              # optional. Compose service names
                                         # run via `docker compose run
                                         # --rm -T --build <service>
                                         # </dev/null`, in order, after
                                         # checkout and before `up`. Not
                                         # run by `wharf reload`.
      - migrate                         # plain string: wrapped with this
                                         # target's own `paths` (or not
                                         # wrapped at all if the target
                                         # doesn't set `paths`) -- same as
                                         # `up`.
      - service: bootstrap              # mapping form: overrides `paths`
        paths: ["/svc/bootstrap/"]      # for just this one command, so it
                                         # doesn't receive secrets the
                                         # target's `up` command gets (or
                                         # vice versa). Requires the same
                                         # top-level `secrets:` block as
                                         # `paths` does.
```

`pre_up` requires a reasonably current Docker Compose v2 on the target
host -- `--build` on `run` isn't supported by older Compose builds or
Compose v1, and will fail with `unknown flag: --build` at deploy time
(not at config-load time, since wharf can't inspect the target's
Compose version ahead of the SSH call).

If a `pre_up` command fails, the deploy aborts *after* the code checkout
has already happened but *before* `up` runs -- so the target's working
tree will have the new revision's code while the old containers are
still running. This is intentional (fail before starting anything with
the new code), but worth knowing when debugging a failed migration: the
checkout has already moved even though nothing new is running yet.

### The `{repo}` placeholder

`remote_repo` and each target's `remote_dir` may contain a literal
`{repo}` placeholder, substituted with the project name at run time.
By default that name comes from the `origin` git remote of the checkout
you're running wharf from (falling back to the current directory's
name); override it with `--repo` on any command.

This is what lets the *same* config file work whether wharf is invoked
from your laptop or from a CI runner: both resolve to the same project
name, because both are operating on a checkout of the same repo.

## Examples

### 1. Single target, no secrets

The simplest useful config — one service, one host, plain
`docker compose up`:

```yaml
version: 1
remote_repo: /srv/git/{repo}.git
compose_file: docker-compose.prod.yml
targets:
  - name: app
    remote_dir: /opt/deploys/{repo}/app
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
    healthcheck: https://app.example.com/health
```

```
wharf deploy deploy.yml
```

### 2. Multiple targets, sequential rollout

Three independent services on two hosts, rolled out in a specific order
(e.g. a core service before the things that depend on it):

```yaml
version: 1
remote_repo: /srv/git/{repo}.git
compose_file: docker-compose.prod.yml
targets:
  - name: core
    remote_dir: /opt/deploys/{repo}/core
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
  - name: worker
    remote_dir: /opt/deploys/{repo}/worker
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 20
  - name: observability
    remote_dir: /opt/deploys/{repo}/observability
    host: 203.0.113.11
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGw13VQZ9o5ryMlAFxRrQDa3I6/r2BRA/uZuzv6bTg22
    order: 30
```

```
# deploy everything, in order 10 -> 20 -> 30
wharf deploy deploy.yml

# deploy just the worker (order/other targets ignored)
wharf deploy deploy.yml --only worker
```

### 3. With Infisical secrets, per-target paths

Two targets sharing one Infisical project, but pulling different secret
paths — a common shape when services have different credentials:

```yaml
version: 1
remote_repo: /srv/git/{repo}.git
compose_file: docker-compose.prod.yml
secrets:
  provider: infisical
  project_id: 11111111-2222-3333-4444-555555555555
  domain: https://eu.infisical.com
  environment: prod
targets:
  - name: api
    remote_dir: /opt/deploys/{repo}/api
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
    paths: ["/api/", "/shared/"]
  - name: worker
    remote_dir: /opt/deploys/{repo}/worker
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 20
    paths: ["/worker/", "/shared/"]
```

`api` and `worker` both authenticate to the same Infisical project/
environment, but each only receives the secrets under its own paths.
A target in the same file that omits `paths` entirely would deploy with
no secrets injection at all — secrets are opt-in per target, not
all-or-nothing for the file.

### 4. Staging vs. prod as a config pair

Since the schema has no environment concept, staging and prod are just
two sibling files — typically with different hosts, a different branch,
and sometimes different auth:

`deploy.yml` (prod):

```yaml
version: 1
remote_repo: /srv/git/{repo}.git
branch: main
ensure_branch: main                     # refuse to run this file from anywhere but main
compose_file: docker-compose.prod.yml
targets:
  - name: app
    remote_dir: /opt/deploys/prod/{repo}/app
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
    healthcheck: https://app.example.com/health
```

`deploy.staging.yml`:

```yaml
version: 1
remote_repo: /srv/git/{repo}.git
branch: staging
compose_file: docker-compose.staging.yml
targets:
  - name: app
    remote_dir: /opt/deploys/staging/{repo}/app
    host: 192.168.1.50
    port: 2222
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPfTiB0UywP0NxRed/GHZCrqvO//I8EJFsgKuAH4hgi2
    order: 10
```

```
wharf deploy deploy.yml            # prod
wharf deploy deploy.staging.yml    # staging
```

## Running from CI

Any CI system that sets the standard `CI` environment variable (GitHub
Actions, GitLab CI, CircleCI, ... all do this automatically) is detected
by wharf without any extra flags. A minimal GitHub Actions job:

```yaml
name: Deploy to production
on:
  push:
    branches: ["main"]
jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: pipx run "wharf @ git+https://github.com/alessandropolverino/wharf.git@v0.2.0" deploy deploy.yml
```

That's the entire workflow — no separate "load targets" step, no
`TARGETS_JSON` env var round-trip. `deploy.yml`'s targets, `--repo`
(inferred from the checkout), and `HEAD` (inferred as the revision) are
all wharf needs.

## Local vs. CI auth

wharf picks its SSH auth mode from the `CI` environment variable
(override with `--ci` / `--interactive` on any command):

- **Local (not CI):** no `BatchMode`, no explicit identity file — SSH
  uses your normal agent/default identity, and passphrase or password
  prompts work exactly as they would typing the `ssh` command yourself.
- **CI:** `BatchMode=yes` and an identity file written from the
  `DEPLOY_SSH_KEY` environment variable (the private key `wharf setup`
  generates). Nothing can be prompted for on a CI runner, so the key
  must be fully usable non-interactively — no passphrase.

## Secrets

Only the *location* of your secrets (project ID, domain, environment,
paths) lives in the config. The Infisical machine-identity credentials
(`INFISICAL_MACHINE_IDENTITY_ID`, `INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET`)
are never generated, stored, or passed by wharf — they're expected to
already exist as environment variables **on the target host** (e.g.
sourced from `/etc/profile.d/` at login), the same way they would for
any other process running there. wharf's `deploy` and `reload` actions
run their remote script via a login shell (`ssh ... bash -l -s`)
specifically so that `/etc/profile.d/*.sh` gets sourced before
`infisical login` + `infisical run -- docker compose ...` runs on the
target when a target declares `paths` -- a plain non-login shell
wouldn't source it, and the credentials would appear to be missing.
