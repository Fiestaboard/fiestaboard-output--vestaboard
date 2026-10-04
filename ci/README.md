# CI for this repository

`ci.yml` here is this repository's GitHub Actions workflow. It lives in
`ci/` only because it was created by a token that cannot write
`.github/workflows/`. A maintainer installs it once:

```bash
mkdir -p .github/workflows
git mv ci/ci.yml .github/workflows/ci.yml
git commit -m "ci: run the tests and output conformance on every push"
```

Then delete this file.

## What it runs

- **versions**: `package.json` and `manifest.json` carry the same version.
  `package.json` publishes only the device data (`output/*.json`) so
  FiestaUI can depend on it; bump both together.
- **test**: checks out FiestaBoard core (`next` by default; the
  `core_ref` input of a manual run picks another branch, tag or commit),
  then runs `./run_tests.sh`: this plugin's tests, including core's
  `OutputConformanceSuite`, behind a network fence that allows loopback
  only, with at least 80% coverage. It also runs nightly against `next`.

## Running it locally

```bash
./run_tests.sh /path/to/FiestaBoard
```

The script builds an ignored `plugins/<id>` import scaffold so the tests
import this plugin as `plugins.<id>`, the name FiestaBoard gives it.

## How FiestaBoard picks up a new version

FiestaBoard bundles this plugin at the commit pinned in its
`outputs.lock.json`. A release here reaches users when a pull request to
FiestaBoard bumps that pin (`commit` and `tree_sha256`; print the digest
with `python scripts/seed_outputs.py digest <clean checkout>`).
