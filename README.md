# A Visualisation Application Operator (for the Data Manager API)

[![Data Manager: Application](https://img.shields.io/badge/squonk2%20data%20manager-application-000000?labelColor=dc332e)]()
[![Dev Stage: 1](https://img.shields.io/badge/dev%20stage-★☆☆%20%281%29-000000?labelColor=dc332e)](https://github.com/InformaticsMatters/code-repository-development-stages)

![Architecture](https://img.shields.io/badge/architecture-amd64%20%7C%20arm64-lightgrey)

[![build](https://github.com/informaticsmatters/squonk2-data-manager-viz-operator/actions/workflows/build.yaml/badge.svg)](https://github.com/informaticsmatters/squonk2-data-manager-viz-operator/actions/workflows/build.yaml)
[![build tag](https://github.com/informaticsmatters/squonk2-data-manager-viz-operator/actions/workflows/build-tag.yaml/badge.svg)](https://github.com/informaticsmatters/squonk2-data-manager-viz-operator/actions/workflows/build-tag.yaml)

[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://conventionalcommits.org)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

This repo contains a Kubernetes _Operator_ based on the [kopf] and [kubernetes]
Python packages that is used by the **Informatics Matters Squonk2 Data Manager
API** to create interactive data **visualisation** instances for the Data
Manager service.

It follows the same pattern as the [Squonk2 Jupyter operator]: the Data Manager
creates a custom resource and this operator responds by creating a Kubernetes
**Deployment**, **Service** and **Ingress** that run the
[squonk2-viz-app] container image.

By default, the operator creates instances using the image: -

- `ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4` (see `operator/handlers.py`)

## The Custom Resource

The operator watches for the following Custom Resource: -

- **Group**: `squonk.it`
- **Version**: `v1`
- **Kind**: `DataVisualisation` (plural `datavisualisations`)

Data-Manager-provided material is namespaced under the `imDataManager` property
of the resource `spec`. Recognised properties (all optional, with operator
defaults) include `image`, `serviceAccountName`, `resources`,
`securityContext` (`runAsUser`, `runAsGroup`), `project` (`claimName`, `id`),
`ingressClass`, `ingressDomain`, `ingressTlsSecret`, `ingressProxyBodySize`
and `labels` (a list of `key=value` strings).

## The Pod environment and Project volume

> Issue #1 stated that no environment variables are needed for the Pod.
> In practice the `squonk2-viz-app` image **exits at start-up** unless
> `DM_PROJECT_DIR` is set, and reads its data from a mounted Project volume.
> The operator therefore mounts the Project PVC and injects the `DM_*`
> variables described below; without them the Pod would crash-loop.

For each instance the operator: -

- Mounts the Data Manager **Project** PVC (`project.claimName`, sub-path
  `project.id`) at `/project`.
- Injects the environment variables `DM_PROJECT_DIR` (`/project`),
  `DM_PROJECT_ID`, `DM_INSTANCE_ID` and `DM_INSTANCE_OWNER`.

The viz-app's Express server listens on container port **3000**, which is
exposed by the Service and routed to by the path-based Ingress.

## Operator configuration (environment variables)

Following the Jupyter operator's `JO_` convention, operator-controlling
variables are prefixed `SVO_` (Squonk2 Viz Operator): -

| Variable | Default | Purpose |
|----------|---------|---------|
| `INGRESS_DOMAIN` | _(required)_ | Default ingress host for instances |
| `INGRESS_TLS_SECRET` | _(unset)_ | Default TLS secret; if unset, cert-manager is used |
| `INGRESS_CERT_ISSUER` | _(unset)_ | cert-manager cluster issuer (when no TLS secret) |
| `SVO_POD_NODE_SELECTOR_KEY` | `informaticsmatters.com/purpose-application` | Pod node-selector key |
| `SVO_POD_NODE_SELECTOR_VALUE` | `yes` | Pod node-selector value |
| `SVO_APPLY_POD_PRIORITY_CLASS` | _(unset)_ | Any value applies a Pod priority class |
| `SVO_DEFAULT_POD_PRIORITY_CLASS` | `im-application-low` | Priority class to apply |

## Contributing

The project uses: -

- [pre-commit] to enforce linting of files prior to committing them to the
  upstream repository
- [Commitizen] to enforce a [Conventional Commit] commit message format
- [Black] as a code formatter

You **MUST** comply with these choices in order to contribute to the project.

To get started, set up your local clone: -

    pip install -r build-requirements.txt
    pre-commit install -t commit-msg -t pre-commit

Now the project's rules will run on every commit, and you can check the
current health of your clone with: -

    pre-commit run --all-files

### Running the tests

The operator logic has unit tests (see `tests/`). Install the operator's
runtime requirements and run them with `pytest`: -

    python -m venv venv
    source venv/bin/activate
    pip install -r operator/requirements.txt -r build-requirements.txt
    pytest

## Building the operator (local development)

Pre-requisites: -

- Docker Compose (v2)

The operator container, residing in the `operator` directory, is automatically
built and pushed using GitHub Actions. You can build and push the image
yourself using docker-compose. The following will build an operator image with
a specific tag: -

    export IMAGE_TAG=35.0.0-alpha.1
    docker compose build
    docker compose push

> The image tag's **major** version must match the major version of the
  `kubernetes` PyPI package the operator is built against (currently `35`,
  for Kubernetes 1.35).

# Data Manager Application Compliance

In order to expose the CRD as an _Application_ in the Data Manager API service
you will need to a) annotate the CRD and b) provide a **Role** and
**RoleBinding**.

## Custom Resource Definition (CRD) annotations

For the **CRD** to be recognised by the Data Manager API it will need a number
of annotations in its `metadata -> annotations` block: -

- `data-manager.informaticsmatters.com/application` set to `'yes'`
- `data-manager.informaticsmatters.com/application-namespaces` set to a
  colon-separated list of namespaces the Application is to be used in,
  e.g. `'data-manager-api:data-manager-api-staging'`
- `data-manager.informaticsmatters.com/application-url-location` set to
  `viz.url` — the operator writes the instance URL to the custom resource's
  `status.viz.url`.

## Pod labels

So that **Pod** instances can be recognised by the Data Manager API the
application's **Pod** must contain the label: -

    data-manager.informaticsmatters.com/instance

with a value matching the `name` given to the operator by the Data Manager.
The Data Manager passes this in the `imDataManager.labels` list; the operator
copies all such labels onto the Pod template.

## Security context

The Custom Resource must expose properties that allow a custom
**SecurityContext** to be applied, otherwise the application instance will not
be able to access the Data Manager Project files: -

- `spec.imDataManager.securityContext.runAsUser`
- `spec.imDataManager.securityContext.runAsGroup`

The container runs without privileges, as the user/group assigned by the Data
Manager API, with `fsGroup` 100 so the Project files are accessible.

## Storage volume

To place Data-Manager Project files the **CRD** must expose: -

- `spec.imDataManager.project.claimName`
- `spec.imDataManager.project.id`

These provide the Project PVC and sub-path mounted at `/project`.

---

[black]: https://black.readthedocs.io/en/stable
[commitizen]: https://commitizen-tools.github.io/commitizen/
[conventional commit]: https://www.conventionalcommits.org/en/v1.0.0/
[kopf]: https://pypi.org/project/kopf/
[kubernetes]: https://pypi.org/project/kubernetes/
[pre-commit]: https://pre-commit.com
[squonk2 jupyter operator]: https://github.com/InformaticsMatters/squonk2-data-manager-jupyter-operator
[squonk2-viz-app]: https://github.com/InformaticsMatters/squonk2-viz-app
