# Named key identities and `wharf rotate`

## Problem

`wharf setup` generates exactly one keypair per project (`.wharf/deploy_key`), appends its public half to every target's `authorized_keys`, and never removes anything. Re-running `setup` after deleting the key file mints a fresh one and *adds* the new public key alongside the old — rotation today is purely additive, so a lost laptop or leaked CI secret has no built-in revocation path short of hand-editing `authorized_keys` on every target.

Separately, wharf only ever generates one key at all, undifferentiated — there's no way to tell, from the key file or the `authorized_keys` comment, which consumer (CI, a bot, some other automation) a given line is *for*. That's the second thing this design fixes: a name, not just a rotate command.

## Non-goals

- **No human/teammate access management.** Interactive local use keeps riding on the operator's own ambient SSH agent, exactly as today (`SessionAuth.resolve()` returns `identity_file=None` when not CI — see `ssh.py:69-79`). wharf does not provision or manage individual humans' keys.
- **No per-target CI keys.** CI reads one secret (`DEPLOY_SSH_KEY`) into one identity file per run; splitting that per target would need per-target secrets or a bundle format CI has to parse. Out of scope.
- **No per-target key files for any identity in v1.** Every identity (including named ones) gets one key, shared across all the targets it's provisioned to — not one keypair per `(identity, target)` pair. This is a deliberate simplification: the two identities that exist today (`default`, `ci`) are both forced into "shared across targets" anyway (backward compat for `default`; the single-secret constraint for CI), and nobody has a concrete case yet for a third, locally-run identity that needs true per-target isolation. Additive later if one shows up.
- **No automated retirement of a *different* identity's old key.** Moving from `default` to a named `ci` identity is documented as a manual cleanup (see Migration below), not a `--retire` flag.

## Identity model

An **identity** is a name (string) a keypair and its `authorized_keys` entries belong to. Resolution order, used identically by `setup` and `rotate`:

1. `--identity NAME` if given.
2. Else, `"ci"` if CI is detected (same detection `SessionAuth` already uses: `--ci`, or the `CI` env var when neither `--ci` nor `--interactive` is passed).
3. Else, `"default"`.

`"default"` exists solely to make everything below **exactly** what `wharf setup` does today for anyone who never passes `--identity`. No existing user has to change anything.

Identity names are restricted to `^[a-z0-9][a-z0-9-]*$` — this keeps them safe to embed directly in a filename and in a `grep` pattern on the remote host (see Marker below) without needing to escape regex metacharacters.

This resolution rule, the name validation, and the path/comment helpers below are shared by `setup`, `rotate`, `identities`, and `SessionAuth` — they live in one new module, `src/wharf/identity.py`, rather than being duplicated or bolted onto `setup.py`.

## Local file layout

`identity.py` exposes `key_paths(identity: str, key_dir: Path) -> tuple[Path, Path]` (private, public), used by `ensure_deploy_keypair` (now taking an `identity: str` parameter) and by `rotate`:

- `identity == "default"` → `.wharf/deploy_key` / `.wharf/deploy_key.pub` (unchanged path).
- any other identity → `.wharf/keys/<identity>_key` / `.wharf/keys/<identity>_key.pub`.

Same idempotency as today: if the private key file already exists, leave it and say so.

## `authorized_keys` marker

`ssh-keygen -C` today hardcodes the comment `wharf-deploy` for every key, regardless of identity. That becomes identity-scoped:

- `identity == "default"` → comment stays the literal `wharf-deploy` (unchanged, so existing installs need no migration to remain recognizable).
- any other identity → comment is `wharf:<identity>`.

The comment is what `ssh-keygen` writes into the public key file, so it travels verbatim into the `authorized_keys` line when `_provision_target` appends it. This is the entire mechanism `rotate` needs to find "lines that belong to this identity" — no separate state file, no local bookkeeping of "what did we last install." The marker *is* the bookkeeping, read back from the target itself.

`identity.py` exposes `key_comment(identity: str) -> str` for this — the single place both `setup` (writing it) and `rotate` (matching it in a `grep`/`grep -v` pattern) get the marker string from, so the two can never drift apart.

## CLI changes

### `wharf setup <config.yml> [--identity NAME] [--only NAME...] [--repo NAME]`

Adds `--identity`. Threads the resolved identity into `ensure_deploy_keypair` and into the `-C` comment used when provisioning. Everything else (per-target bare repo creation, additive `authorized_keys` append, the final "add this to your CI secret" printout) is unchanged, just parameterized by identity instead of hardcoded.

### `wharf rotate <config.yml> [--identity NAME] [--only NAME...] [--repo NAME]`

New subcommand. Unlike `setup`, it always produces a new key and actively removes the old one — it is not the idempotent "leave it as-is" operation `setup` is.

1. Resolve identity (same rule as `setup`).
2. Generate the replacement keypair to a **staged** path, `<key-path>.new` / `<key-path>.new.pub`, without touching the live key files yet. If a staged pair already exists from a previous interrupted `rotate` run, reuse it instead of generating another (mirrors `ensure_deploy_keypair`'s existing "if it exists, leave it" idempotency, extended to the staged file).
3. For each selected target, in config order, stop on first failure (same pattern as `setup`/`deploy`): connect using the operator's own ambient SSH identity (never the key being rotated — same anti-chicken-and-egg reasoning `setup` already relies on), then in one remote script:
   - append the new public key to `authorized_keys` (skip if already present, same idempotent check `_provision_target` does today),
   - remove any `authorized_keys` line ending in this identity's marker (`wharf-deploy` for `default`, `wharf:<identity>` otherwise) that is *not* the new key.
   Add-then-remove in that order, within one script (so a target is never left with neither key mid-script — `set -euo pipefail` still means a failure aborts before the target's `authorized_keys` is touched at all, since both lines run in the same remote invocation).
4. Only after **every** selected target succeeds: promote the staged files to the live key path (delete the old private key, rename `.new` → live) and delete the staged suffix.
5. Print the same "update your CI secret" instruction `setup` prints, pointing at the (now-live) new private key path.

If interrupted between targets: the live key files are untouched (old key still authorized on not-yet-rotated targets, still valid locally), and the staged `.new` files persist for the next `rotate` invocation to pick up and continue from. Already-rotated targets now trust only the new key — re-running `rotate` is the recovery path, not a separate resume command.

### `wharf identities`

New subcommand, no config file argument — identity key files live under `.wharf/` at the project root (cwd), not per config file (the same reason `deploy.yml` and `deploy.staging.yml` today share one `.wharf/deploy_key`; see the migration note added to `configuration.md`). Lists every identity with a local key file: `"default"` (`.wharf/deploy_key`, if present) plus every `.wharf/keys/*_key` file found.

**Local-only, deliberately** — it reads only what's on disk in the current checkout, never SSHes anywhere. It cannot tell you whether a target's `authorized_keys` actually matches (a manual edit, a partial `rotate`, or a key generated on a different machine would all be invisible to it). The command's own `--help` text and its output both say this explicitly — e.g. a trailing line on every run: `Local key files only -- not checked against any target's authorized_keys.` This scope is a deliberate first cut (see Non-goals); a `--verify` flag that SSHes to each target and greps for markers is the natural follow-up if drift turns out to be a real problem in practice, not built now.

Per identity, reports:
- **name**
- **key path** (`.wharf/deploy_key` or `.wharf/keys/<identity>_key`)
- **fingerprint** — via `ssh-keygen -lf <pub>` (shelling out, same as key generation itself — `setup.py`'s docstring already justifies this over adding a crypto dependency)
- **`authorized_keys` marker** — the exact comment string (`wharf-deploy` / `wharf:<identity>`) an operator could `grep` for by hand on a target to cross-check this command's local view against reality
- **status** — `ok`, or `rotation in progress (staged key pending)` if a `<key-path>.new` file exists (an interrupted `rotate` for that identity)

Example:

```
$ wharf identities
IDENTITY   KEY PATH                  FINGERPRINT                                       MARKER          STATUS
default    .wharf/deploy_key         SHA256:2Jk9...                                     wharf-deploy    ok
ci         .wharf/keys/ci_key        SHA256:qP1x...                                     wharf:ci        rotation in progress (staged key pending)

Local key files only -- not checked against any target's authorized_keys.
```

`identity.py` exposes `list_identities(key_dir: Path) -> list[IdentityInfo]` (a small dataclass: name, private/public paths, comment, staged-pending bool) doing the directory scan; `cli.py` calls it, shells out to `ssh-keygen -lf` per identity for the fingerprint, and formats the table (same pattern `ls` already uses for printing).

## Help text

Every subcommand and every new/changed flag gets a real explanation in its argparse `help=`, not a fragment — `rotate` and `identities` are new and non-obvious, so terse one-liners aren't enough on their own:

- `rotate`: `help="replace an identity's deploy key, removing the old authorized_keys entry once every target has the new one"`.
- `identities`: `help="list locally known deploy-key identities and their key files (local only, not verified against targets)"`.
- `--identity` (on `setup`, `rotate`, `deploy`, `down`, `reload`): `help="named identity for the deploy key (default: 'ci' when running in CI, 'default' otherwise)"`.

The module docstring at the top of `cli.py` (the usage summary already listing `deploy`/`down`/`reload`/`ls`/`setup`) gains `rotate` and `identities` lines in the same style.

## `SessionAuth` extension (local named identities)

`SessionAuth.resolve()` currently branches only on CI-vs-not. Extend it to accept an optional `identity: str | None`:

- `identity is None` or `"default"`: unchanged behavior (CI → `DEPLOY_SSH_KEY` env var; local → ambient agent, `identity_file=None`).
- any other identity, **in CI**: unchanged — still reads `DEPLOY_SSH_KEY` (identity naming doesn't change the secret's env var name; a given CI job/secret store only ever populates one secret per run, matching the "CI shares one key across targets" non-goal above).
- any other identity, **locally** (not CI): `identity_file` points directly at that identity's key file on disk (`.wharf/keys/<identity>_key`), no tempfile/env var extraction needed since the file already exists. Errors clearly if the file doesn't exist (tell the operator to run `wharf setup --identity NAME` first).

This is what lets a bot/automation script running outside a CI-detected environment say `--identity release-bot` and get a dedicated key instead of the ambient agent, without inventing a new mechanism — it reuses the same file layout `setup`/`rotate` already produce.

`deploy`/`down`/`reload` gain a matching `--identity` CLI flag, threaded into their `SessionAuth.resolve()` call, defaulting the same way as `setup`/`rotate`.

## Migration for existing users

No action required for anyone who never passes `--identity` — `"default"` is defined to be identical to today's behavior, file path and `authorized_keys` marker both unchanged.

Moving an existing single-key setup onto a named identity (e.g. `default` → `ci`) is a manual, one-time sequence, documented in `docs/configuration.md`, not a built command:

1. `wharf setup --identity ci` — additive, generates and installs the new key alongside the old one.
2. Update the CI secret to the new key's contents.
3. Confirm a real CI deploy succeeds with the new key.
4. Manually remove the old `wharf-deploy`-commented line from each target's `authorized_keys`, and delete `.wharf/deploy_key` locally.

## Testing

- New `tests/test_setup.py` (this module currently has no direct test coverage):
  - `ensure_deploy_keypair("default", ...)` writes to `.wharf/deploy_key` with comment `wharf-deploy`, matching today's output exactly.
  - `ensure_deploy_keypair("ci", ...)` writes to `.wharf/keys/ci_key` with comment `wharf:ci`.
  - Re-running `ensure_deploy_keypair` for an identity whose key already exists leaves it untouched (existing idempotency, now parameterized).
- New `tests/test_rotate.py` (or extend `test_setup.py`), exercising the remote-script generation (same style as `test_remote_script.py`'s string-based assertions, no real SSH):
  - The generated per-target script for `rotate` appends the new key and removes lines matching the old identity's marker, in that order.
  - The marker pattern for `identity="default"` matches `wharf-deploy`; for `identity="ci"` matches `wharf:ci` and does **not** match `wharf:ci-staging` or other identities' lines.
  - A staged `.new` keypair already on disk is reused, not regenerated, on a second `rotate` call.
  - After all targets succeed, the staged files are promoted and the old private key file no longer exists; if a target fails, the live key files are untouched and the staged files remain.
- `tests/test_ssh.py`: `SessionAuth.resolve(identity="ci")` in CI mode still reads `DEPLOY_SSH_KEY` (unchanged from `identity=None`); `SessionAuth.resolve(identity="release-bot")` locally (not CI) resolves `identity_file` to `.wharf/keys/release-bot_key` and raises a clear error if that file doesn't exist.
- New `tests/test_identity.py`: `resolve_identity` picks `--identity` over CI-detection over `"default"`, in that priority order; the name regex rejects uppercase/underscore/empty; `key_paths`/`key_comment` return the `"default"`-vs-named split described above; `list_identities` finds `.wharf/deploy_key` and every `.wharf/keys/*_key`, and flags a `<key-path>.new` file as staged-pending.

## Documentation

New code, per this repo's existing convention (`src/wharf/README.md`: "one page per file, living next to the code it documents"):

- New `src/wharf/identity.md` and `src/wharf/rotate.md`, matching the style of the existing per-file pages (e.g. `setup.md`).
- `src/wharf/README.md`: add both new files to the file table, and add `identity.py`/`rotate.py` to the call graph (`rotate.py` depends on `identity.py` and `ssh.py`; `setup.py` and `cli.py` both gain a dependency on `identity.py`).
- `src/wharf/setup.md`: update for `ensure_deploy_keypair`'s new `identity` parameter.
- `src/wharf/ssh.md`: update for `SessionAuth.resolve`'s new `identity` parameter and its CI/local branches.
- `src/wharf/cli.md`: update for the two new subcommands and the `--identity` flag on the existing ones.

Existing user-facing docs to update:

- `docs/configuration.md`: replace the "Repeat steps 1-2 per config file... each currently shares the same `.wharf/deploy_key`" caveat added in the CI-onboarding walkthrough with the actual fix — naming the CI identity per environment (or noting `"ci"` is still shared across config files in the same checkout unless named explicitly) — plus a new subsection covering `--identity`, `wharf rotate`, and `wharf identities`, including the manual migration steps from the Migration section above.
- `README.md`: add `rotate` and `identities` rows to the Commands table; mention `--identity` in the line describing flags common to multiple commands.
