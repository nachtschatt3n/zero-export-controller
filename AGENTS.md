# AGENTS.md

Operating guide for AI coding agents (Claude Code, etc.) working on this repo.
Humans can read it too — it's just dense.

## What this repo is

`zero-export-controller` is a single-binary Python service that runs on
Kubernetes and clamps a balcony PV inverter setup to never export to the
grid. It reads grid power from Home Assistant, computes per-inverter limits
honoring a 800 W system cap with single-inverter burst, and writes the
limits back through HA's OpenDTU integration.

The deployment lives in a separate cluster-config repo
(`nachtschatt3n/cberg-home-nextgen`) under
`kubernetes/apps/home-automation/zero-export-controller/`. **This repo
publishes the image; that repo consumes it.** Don't try to deploy from this
repo directly — open a PR in the cluster repo to bump the `image.tag`.

## Layout

```
.
├── controller.py            ~ 400 LOC; entire runtime, single module
├── tests/test_controller.py ~ 200 LOC; pytest, all-pure tests
├── pyproject.toml           deps + pytest config (no setup.py, no requirements.txt)
├── Dockerfile               python:3.12-slim, runs as UID 10001
├── .github/workflows/
│   ├── ci.yml               pytest on push/PR
│   └── container.yml        multi-arch image build to ghcr.io/<repo>
├── README.md                user-facing docs
├── AGENTS.md                this file
└── LICENSE                  MIT
```

There is no `src/` layer, no package directory, no abstraction layers.
`controller.py` is meant to stay flat and readable. Resist the urge to split
it up.

## Code conventions

- **Pure functions stay pure.** `compute_desired`, `compute_ceilings`, and
  `distribute` take plain dataclasses / dicts and return new ones. Tests
  exercise them directly without mocks. Keep it that way.
- **Async isolated to I/O.** Only `HAClient` methods and the loop are async.
  Don't make `compute_*` async.
- **httpx + prometheus_client are the only runtime deps.** Don't add a third
  unless there's a strong reason. Standard library covers the rest.
- **No comments unless the why is non-obvious.** A module-level docstring on
  `compute_ceilings` explains the east/west rationale; that's the bar.
  Don't restate what the code says.
- **Logging at INFO** is the operator-visible record of every tick. Format
  is stable: `grid=…W pv=…W target=…W desired=…W ceilings={…} limits={…}`.
  Anything we add must keep that line greppable.
- **Metrics names are stable API.** `zec_*` is our prefix. If you rename
  one, expect alert rules in the cluster repo to break.

## Local dev loop

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -v        # all tests should pass; aim for <1 s wall-clock
```

There is no formatter pinned (yet). If you reach for one, prefer `ruff`
defaults, single-pass.

## CI

- **`ci.yml`** runs `pytest -v` on every push and PR.
- **`container.yml`** builds and pushes a multi-arch image to GHCR on every
  push to `main` and on every `v*` tag. Tags published:
  - `:main` — moving tip of main
  - `:sha-<short>` — exact commit
  - `:0.1.0`, `:0.1` — on `v0.1.0` semver tags

A docs-only commit on `main` will rebuild the image with a new `sha-*` tag,
which is wasteful but harmless. Add `[skip ci]` to the commit message to skip
both workflows if you really want to.

## Releases

Tag a release to publish a stable, semver-pinnable image:

```sh
git tag v0.2.0 -m "v0.2.0 — <short summary>"
git push origin v0.2.0
gh run watch -R nachtschatt3n/zero-export-controller \
  --exit-status $(gh run list -w Container -L 1 --json databaseId --jq '.[0].databaseId')
```

Then bump `image.tag` in the cluster repo (`cberg-home-nextgen`):
`kubernetes/apps/home-automation/zero-export-controller/app/helmrelease.yaml`.
Renovate will usually open the PR for you.

Do **not** force-push a tag that has been pulled by the cluster — pin
versions for a reason.

## Coupling with the cluster repo

| Concern | Lives in |
|---|---|
| Controller source, tests, image | this repo |
| HelmRelease, Service, ServiceMonitor, PrometheusRule, SOPS secret | `cberg-home-nextgen` |
| HA helpers, sensors, dashboards | `hactl` (HA-config tooling) |

When you change behaviour, ask: does the operator-visible interface change?
If yes, you probably need a coordinated PR in the cluster repo or `hactl`.
Examples:

- **New env var** → add to `helmrelease.yaml` env block.
- **New metric** → consider a Prometheus rule update.
- **New required HA helper** → create it via `hactl` before the new image
  rolls out, or the controller fails open into safe-fallback.
- **Renamed log line format** → check `Homepage` tile parsing and any log
  alerts before merging.

## Behavioral invariants (don't break these)

1. **`sum(limits) ≤ cap_w`** in every tick, in every code path. The legal
   800 W cap is non-negotiable. The relevant tests are
   `test_distribute_*`; if you touch `distribute()` or `compute_ceilings()`,
   add tests asserting the sum invariant for your new case.
2. **`number.set_value` only writes `_nonpersistent_*` entities.** Writing
   the persistent variant wears flash. The string `_persistent_` should
   never appear in the codebase.
3. **Kill switch off ⇒ no writes.** When `enabled=False`, `loop_once` must
   `return` before the write loop. There is no "kill switch off but still
   nudge things" mode.
4. **Sensors stale ⇒ safe distribution, not free run.** Don't revert to
   "set everything to per_max_w" on stale sensors — that violates the cap
   when more than one inverter is reachable.
5. **Hoymiles needs ~18 s to react.** `LOOP_PERIOD_S < 10` is unsupported by
   the algorithm; the slow-approx assumes the inverter is converging
   between ticks.
6. **Reachability is HA-state-driven.** `binary_sensor.*_reachable` only
   updates `last_updated` on transitions; treat its `is_on` as authoritative
   regardless of age.

If you find yourself wanting to relax one of these, that's the moment to
flag it to the human, not silently change it.

## Testing scope

The full test suite is unit-only and pure-function-only. There is no
integration test against a real HA instance — the cluster itself is the
integration test.

When adding a feature, prefer:

1. A pure-function test in `tests/test_controller.py`.
2. A short manual verification plan you can paste into the PR body
   (e.g. "boil a kettle for 60 s; expect limit to ramp to 800 W within
   30 s").

## Common tasks

| Task | First step |
|---|---|
| Tune control gain | edit `input_number.solar_slow_approx` in HA — no redeploy needed |
| Add a metric | `Gauge(...)` near top of `controller.py`; set in `loop_once` |
| Change algorithm | edit `compute_ceilings` / `distribute` / `compute_desired`; add a test that captures the *behavioural* change before changing the code |
| Cut a release | `git tag vX.Y.Z -m '…' && git push origin vX.Y.Z` |
| Rollback in cluster | revert the `image.tag` bump PR in `cberg-home-nextgen` |

## Things to NOT do

- Don't introduce a config file. Env + HA helpers are the two surfaces;
  adding a third is churn.
- Don't add a web UI. The HA dashboard is the operator UI.
- Don't add MQTT-direct control. Going through the HA OpenDTU integration
  keeps a single source of truth and avoids hardcoding inverter serials.
- Don't add `--no-verify` to git commits.
- Don't change the public `zec_*` metric names without coordinating the
  alert rules in the cluster repo.
