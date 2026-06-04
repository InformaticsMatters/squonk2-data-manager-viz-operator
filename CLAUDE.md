# CLAUDE.md

Guidance for working in this repository.

## What this is

A Python [kopf] Kubernetes operator for the **Squonk2 Data Manager**. It
watches for `DataVisualisation` custom resources (`squonk.it/v1`,
plural `datavisualisations`) and, for each one, creates a **Deployment**,
**Service** and **Ingress** that run the [squonk2-viz-app] container image
(`ghcr.io/informaticsmatters/squonk2-viz-app`).

It is modelled on the sibling [squonk2-data-manager-jupyter-operator]; consult
that repo when a behaviour here is unclear.

## Layout

- `operator/handlers.py` — the kopf handlers and the pure `build_*` body
  builders. **This is the operator logic.**
- `operator/Dockerfile`, `operator/entrypoint.sh`, `operator/requirements.txt`
  — the operator container image (Python 3.14, runs `kopf run`).
- `tests/test_handlers.py` — unit tests for the `build_*` functions.
- `docker-compose.yaml` — builds/pushes the operator image.
- `.github/workflows/` — CI (lint + test + multi-arch image build).

## Conventions (must follow)

- **TDD**: write/extend a test in `tests/` before changing operator logic.
  Keep the cluster-facing `create` handler thin and put testable logic in
  pure `build_*` functions.
- **Never let errors pass silently.** When creating Kubernetes objects, only a
  `409`/`Conflict` (object already exists, e.g. on retry) is tolerated; every
  other `ApiException` is re-raised.
- **No secrets in code or logs.** Use environment variables (see the
  `SVO_`-prefixed operator config and the `INGRESS_*` variables in the
  `README.md`).
- Commit messages are [Conventional Commits]; `pre-commit` enforces black,
  mypy (strict), pylint, yamllint and commitizen.

## Key facts

- The viz-app container listens on port **3000** and **exits at start-up
  unless `DM_PROJECT_DIR` is set**. The operator mounts the Project PVC at
  `/project` and injects `DM_PROJECT_DIR`, `DM_PROJECT_ID`, `DM_INSTANCE_ID`
  and `DM_INSTANCE_OWNER`. (Issue #1 said "no Pod env vars", but the image
  requires these — see the note in `README.md` and `handlers.py`.)
- Created objects are adopted via `kopf.adopt`, so Kubernetes garbage-collects
  them when the custom resource is deleted (no explicit delete handler).
- The operator image tag's **major** version tracks the `kubernetes` PyPI
  package major (currently `35`, for Kubernetes 1.35).

## Local checks

    python -m venv venv && source venv/bin/activate
    pip install -r operator/requirements.txt -r build-requirements.txt
    pytest
    pre-commit run --all-files

[kopf]: https://pypi.org/project/kopf/
[conventional commits]: https://www.conventionalcommits.org/en/v1.0.0/
[squonk2-viz-app]: https://github.com/InformaticsMatters/squonk2-viz-app
[squonk2-data-manager-jupyter-operator]: https://github.com/InformaticsMatters/squonk2-data-manager-jupyter-operator
