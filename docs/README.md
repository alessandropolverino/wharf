# wharf docs

- **[how-it-works.md](how-it-works.md)** — architecture: the end-to-end
  deploy flow, and what wharf adds on top of plain `docker compose`
  (no-registry git delivery, sequential multi-target rollout, `pre_up`
  migrations, secrets injection, locking, image pruning, healthchecks).
- **[configuration.md](configuration.md)** — the full config file
  schema (`deploy.yml`), field by field, plus worked examples (single
  target, multi-target sequential rollout, Infisical secrets, staging
  vs. prod) and running from CI.
- **[src/wharf/](../src/wharf/README.md)** — one page per file, living
  next to the code itself, for reading or changing the source.

Start with `how-it-works.md` for the big picture, `configuration.md`
when you're writing or editing a config file, and `src/wharf/` when
you're in the source.
