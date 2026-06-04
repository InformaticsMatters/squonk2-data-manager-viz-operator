"""A kopf handler for the Squonk2 Data Manager visualisation CRD.

The operator watches for ``DataVisualisation`` custom resources and, for each
one, creates the Kubernetes **Deployment**, **Service** and **Ingress** that
run the ``squonk2-viz-app`` container image. The created objects are *adopted*
by the custom resource (via ``kopf.adopt``) so that Kubernetes garbage
collection removes them automatically when the custom resource is deleted.

The body-construction logic lives in small, pure ``build_*`` functions so it
can be unit-tested without a cluster (see ``tests/test_handlers.py``). The
``create`` handler reads configuration, calls the builders, and submits the
results via the Kubernetes API.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import kubernetes
import kopf

# Configuration of underlying API requests.
#
# Request timeout (from the Python Kubernetes API).
# If one number is provided it is the total request timeout. It can also be a
# pair (tuple) of (connection, read) timeouts.
_REQUEST_TIMEOUT = (30, 20)

# The container port the squonk2-viz-app Express server listens on.
# See the viz-app Dockerfile: 'ENV PORT=3000' / 'EXPOSE 3000'.
_CONTAINER_PORT: int = 3000
_PORT_NAME: str = "3000-tcp"

# Where the Data Manager Project PVC is mounted inside the container.
# The viz-app requires DM_PROJECT_DIR to be set to this path (it exits at
# start-up otherwise), so the mount path and the DM_PROJECT_DIR environment
# variable must always agree.
#
# NB: issue #1 states "no environment variables need to be set for the Pod",
# but the squonk2-viz-app image exits immediately unless DM_PROJECT_DIR is
# defined (see server/index.ts) and its architecture document requires the
# Project PVC to be mounted. We therefore mount the PVC and inject the DM_*
# variables; without them the Pod would crash-loop.
_PROJECT_MOUNT_PATH: str = "/project"

# Some (key) default deployment variables...
_DEFAULT_IMAGE: str = "ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4"
_DEFAULT_SA: str = "default"
_DEFAULT_CPU_LIMIT: str = "1"
_DEFAULT_CPU_REQUEST: str = "10m"
_DEFAULT_MEM_LIMIT: str = "1Gi"
_DEFAULT_MEM_REQUEST: str = "256Mi"
_DEFAULT_USER_ID: int = 1000
_DEFAULT_GROUP_ID: int = 100
_DEFAULT_INGRESS_PROXY_BODY_SIZE: str = "500m"
# The ingress class
_DEFAULT_INGRESS_CLASS: str = "nginx"


def _get_default_ingress_domain() -> str:
    """The default ingress domain.

    This is required operator configuration; the user can provide an
    alternative for a given instance via the custom resource.
    """
    return os.environ["INGRESS_DOMAIN"]


def _get_default_ingress_tls_secret() -> Optional[str]:
    """The default ingress TLS secret.

    If provided it is used as the Ingress secret and cert-manager is avoided.
    The user can provide their own for a given instance via the custom
    resource.
    """
    return os.environ.get("INGRESS_TLS_SECRET")


def _get_ingress_cert_issuer() -> Optional[str]:
    """The cert-manager issuer.

    Expected if an INGRESS_TLS_SECRET is not defined.
    """
    return os.environ.get("INGRESS_CERT_ISSUER")


def _get_pod_node_selector() -> Dict[str, str]:
    """The node selector applied to application Pods."""
    key = os.environ.get(
        "SVO_POD_NODE_SELECTOR_KEY",
        "informaticsmatters.com/purpose-application",
    )
    value = os.environ.get("SVO_POD_NODE_SELECTOR_VALUE", "yes")
    return {key: value}


def _get_pod_priority_class() -> Optional[str]:
    """The Pod priority class, if one is to be applied.

    Any value for SVO_APPLY_POD_PRIORITY_CLASS results in the Pod's priority
    class being set to SVO_DEFAULT_POD_PRIORITY_CLASS.
    """
    if os.environ.get("SVO_APPLY_POD_PRIORITY_CLASS"):
        return os.environ.get("SVO_DEFAULT_POD_PRIORITY_CLASS", "im-application-low")
    return None


def _get_image_pull_secrets() -> List[str]:
    """Default image pull Secret name(s) for instance Pods.

    Read from SVO_IMAGE_PULL_SECRET (a single dockerconfigjson Secret name).
    Empty when unset, in which case Pods are created without imagePullSecrets
    (e.g. for a public image). The named Secret must already exist in the
    instance's namespace; the operator never handles registry credentials.
    """
    name = os.environ.get("SVO_IMAGE_PULL_SECRET")
    return [name] if name else []


def image_pull_policy_for(image: str) -> str:
    """Return the imagePullPolicy appropriate for the given image reference.

    Mutable tags ('latest', 'stable', or no tag - which Kubernetes treats as
    'latest') are always pulled; everything else is pulled only if absent.
    """
    image_parts = image.split(":")
    image_tag = "latest" if len(image_parts) == 1 else image_parts[1]
    return "Always" if image_tag.lower() in ["latest", "stable"] else "IfNotPresent"


def build_environment(
    *,
    project_id: str,
    instance_id: str,
    instance_owner: str,
    project_mount_path: str,
) -> List[Dict[str, str]]:
    """Build the container environment expected by the squonk2-viz-app.

    DM_PROJECT_DIR is mandatory (the app exits without it); the remaining DM_*
    variables provide instance metadata.
    """
    return [
        {"name": "DM_PROJECT_DIR", "value": project_mount_path},
        {"name": "DM_PROJECT_ID", "value": str(project_id)},
        {"name": "DM_INSTANCE_ID", "value": str(instance_id)},
        {"name": "DM_INSTANCE_OWNER", "value": str(instance_owner)},
    ]


def build_deployment_body(
    *,
    name: str,
    image: str,
    image_pull_policy: str,
    service_account: str,
    cpu_request: str,
    cpu_limit: str,
    memory_request: str,
    memory_limit: str,
    run_as_user: int,
    run_as_group: int,
    project_claim_name: str,
    project_id: str,
    project_mount_path: str,
    env: List[Dict[str, str]],
    node_selector: Dict[str, str],
    pod_priority_class: Optional[str] = None,
    extra_labels: Optional[Dict[str, str]] = None,
    image_pull_secrets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the Deployment object for a visualisation instance.

    A single replica runs the viz-app container with the Project PVC mounted at
    ``project_mount_path`` (the same path advertised via DM_PROJECT_DIR). The
    'Recreate' strategy avoids two Pods briefly sharing the Project volume.
    """
    deployment_body: Dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"deployment": name}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": {"deployment": name}},
                "spec": {
                    "serviceAccountName": service_account,
                    "nodeSelector": dict(node_selector),
                    "containers": [
                        {
                            "name": "viz",
                            "image": image,
                            "imagePullPolicy": image_pull_policy,
                            "resources": {
                                "requests": {
                                    "memory": memory_request,
                                    "cpu": cpu_request,
                                },
                                "limits": {
                                    "memory": memory_limit,
                                    "cpu": cpu_limit,
                                },
                            },
                            "ports": [
                                {
                                    "name": _PORT_NAME,
                                    "containerPort": _CONTAINER_PORT,
                                    "protocol": "TCP",
                                }
                            ],
                            "env": list(env),
                            "volumeMounts": [
                                {
                                    "name": "project",
                                    "mountPath": project_mount_path,
                                    "subPath": project_id,
                                }
                            ],
                        }
                    ],
                    "securityContext": {
                        "runAsUser": run_as_user,
                        "runAsGroup": run_as_group,
                        "fsGroup": _DEFAULT_GROUP_ID,
                    },
                    "volumes": [
                        {
                            "name": "project",
                            "persistentVolumeClaim": {"claimName": project_claim_name},
                        }
                    ],
                },
            },
        },
    }

    # Insert a Pod priority class?
    if pod_priority_class:
        deployment_body["spec"]["template"]["spec"][
            "priorityClassName"
        ] = pod_priority_class

    # Additional labels (e.g. the Data Manager 'instance' label)?
    if extra_labels:
        deployment_body["spec"]["template"]["metadata"]["labels"].update(extra_labels)

    # Image pull secret(s) for a private registry?
    if image_pull_secrets:
        deployment_body["spec"]["template"]["spec"]["imagePullSecrets"] = [
            {"name": secret_name} for secret_name in image_pull_secrets
        ]

    return deployment_body


def build_service_body(*, name: str) -> Dict[str, Any]:
    """Build the ClusterIP Service that fronts the viz-app container."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "type": "ClusterIP",
            "ports": [
                {
                    "name": _PORT_NAME,
                    "port": _CONTAINER_PORT,
                    "protocol": "TCP",
                    "targetPort": _CONTAINER_PORT,
                }
            ],
            "selector": {"deployment": name},
        },
    }


def build_ingress_body(
    *,
    name: str,
    ingress_path: str,
    ingress_domain: str,
    ingress_class: str,
    ingress_proxy_body_size: str,
    ingress_tls_secret: Optional[str],
    ingress_cert_issuer: Optional[str],
) -> Dict[str, Any]:
    """Build the path-based Ingress that exposes the instance.

    When an explicit TLS secret is supplied it is used directly. Otherwise, if
    a cert-manager issuer is configured, the cert-manager annotation is added
    so a certificate is provisioned automatically.
    """
    ingress_body: Dict[str, Any] = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "labels": {"app": name},
            "annotations": {
                "kubernetes.io/ingress.class": ingress_class,
                "nginx.ingress.kubernetes.io/proxy-body-size": (
                    f"{ingress_proxy_body_size}"
                ),
            },
        },
        "spec": {
            "tls": [{"hosts": [ingress_domain], "secretName": ingress_tls_secret}],
            "rules": [
                {
                    "host": ingress_domain,
                    "http": {
                        "paths": [
                            {
                                "path": ingress_path,
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": name,
                                        "port": {"number": _CONTAINER_PORT},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        },
    }

    # Defer to cert-manager only when no explicit TLS secret was provided.
    if not ingress_tls_secret and ingress_cert_issuer:
        ingress_body["metadata"]["annotations"][
            "cert-manager.io/cluster-issuer"
        ] = ingress_cert_issuer

    return ingress_body


def _create_ignoring_conflict(create_call: Any, description: str) -> None:
    """Create a Kubernetes object, tolerating a 409/Conflict.

    A 409/Conflict means the object already exists - which happens when the
    operator retries a create after a partial failure - so we log and continue.
    Any other ApiException is re-raised so it is never swallowed silently.
    """
    try:
        create_call()
        logging.debug("Created %s", description)
    except kubernetes.client.exceptions.ApiException as ex:
        if ex.status != 409 or ex.reason != "Conflict":
            raise
        logging.debug(
            "Got 409/Conflict creating %s. Ignoring - object already present",
            description,
        )


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """The operator startup handler."""
    # Adjust the logging level.
    settings.posting.level = logging.INFO

    # Attempt to protect ourselves from missing watch events.
    # See https://github.com/nolar/kopf/issues/698
    # Added in an attempt to prevent the operator "falling silent".
    settings.watching.server_timeout = 120
    settings.watching.client_timeout = 150


# For TEMPORARY errors (i.e. those that are not kopf.PermanentError)
# we retry after 20 seconds and only retry 6 times.
@kopf.on.create(
    "squonk.it", "v1", "datavisualisations", id="viz", backoff=20, retries=6
)
def create(spec: Dict[str, Any], name: str, namespace: str, **_: Any) -> Dict[str, Any]:
    """Handler for CRD create events.

    Here we construct the required Kubernetes objects, adopting them in kopf
    (so they are garbage-collected with the custom resource) before using the
    corresponding Kubernetes API to create them.
    """
    logging.info("Creating %s (namespace=%s)...", name, namespace)
    logging.info("Incoming %s spec=%s", name, spec)

    # All Data-Manager provided material is namespaced under 'imDataManager'.
    material: Dict[str, Any] = spec.get("imDataManager", {})

    image = material.get("image", _DEFAULT_IMAGE)
    image_pull_policy = image_pull_policy_for(image)

    service_account = material.get("serviceAccountName", _DEFAULT_SA)

    resources = material.get("resources", {})
    cpu_limit = resources.get("limits", {}).get("cpu", _DEFAULT_CPU_LIMIT)
    cpu_request = resources.get("requests", {}).get("cpu", _DEFAULT_CPU_REQUEST)
    memory_limit = resources.get("limits", {}).get("memory", _DEFAULT_MEM_LIMIT)
    memory_request = resources.get("requests", {}).get("memory", _DEFAULT_MEM_REQUEST)

    # Data Manager API compliance.
    #
    # The user and group IDs we're asked to run as. The files in the container
    # Project volume will be owned by this user and group; we must run as
    # group 100 (fsGroup) so we can manipulate them.
    sc_run_as_user = material.get("securityContext", {}).get(
        "runAsUser", _DEFAULT_USER_ID
    )
    sc_run_as_group = material.get("securityContext", {}).get(
        "runAsGroup", _DEFAULT_GROUP_ID
    )

    # Project storage.
    project_claim_name = material.get("project", {}).get("claimName")
    project_id = material.get("project", {}).get("id")

    # Ingress configuration.
    ingress_proxy_body_size = material.get(
        "ingressProxyBodySize", _DEFAULT_INGRESS_PROXY_BODY_SIZE
    )
    ingress_class = material.get("ingressClass", _DEFAULT_INGRESS_CLASS)
    ingress_domain = material.get("ingressDomain", _get_default_ingress_domain())
    ingress_tls_secret = material.get(
        "ingressTlsSecret", _get_default_ingress_tls_secret()
    )
    ingress_cert_issuer = _get_ingress_cert_issuer()
    ingress_path = f"/{name}"

    # Image pull secret(s) for the (private) registry. A per-instance list
    # overrides the operator default (SVO_IMAGE_PULL_SECRET). The named Secret
    # must already exist in the instance's namespace.
    image_pull_secrets = material.get("imagePullSecrets", _get_image_pull_secrets())
    if isinstance(image_pull_secrets, str):
        image_pull_secrets = [image_pull_secrets]

    # Additional labels?
    #
    # The Data Manager provides labels as 'key=value' strings. We copy them to
    # the Pod template and pick out the owner and instance-id for the
    # container environment.
    extra_labels: Dict[str, str] = {}
    instance_owner = "Unknown"
    instance_id = "Unknown"
    for label in material.get("labels", []):
        key, value = label.split("=")
        extra_labels[key] = value
        if key.endswith("/owner"):
            instance_owner = value
        elif key.endswith("/instance-id"):
            instance_id = value

    env = build_environment(
        project_id=project_id,
        instance_id=instance_id,
        instance_owner=instance_owner,
        project_mount_path=_PROJECT_MOUNT_PATH,
    )

    apps_api = kubernetes.client.AppsV1Api()
    core_api = kubernetes.client.CoreV1Api()
    networking_api = kubernetes.client.NetworkingV1Api()

    # Deployment
    # ----------
    logging.info("Creating Deployment %s...", name)
    deployment_body = build_deployment_body(
        name=name,
        image=image,
        image_pull_policy=image_pull_policy,
        service_account=service_account,
        cpu_request=cpu_request,
        cpu_limit=cpu_limit,
        memory_request=memory_request,
        memory_limit=memory_limit,
        run_as_user=sc_run_as_user,
        run_as_group=sc_run_as_group,
        project_claim_name=project_claim_name,
        project_id=project_id,
        project_mount_path=_PROJECT_MOUNT_PATH,
        env=env,
        node_selector=_get_pod_node_selector(),
        pod_priority_class=_get_pod_priority_class(),
        extra_labels=extra_labels,
        image_pull_secrets=image_pull_secrets,
    )
    kopf.adopt(deployment_body)
    _create_ignoring_conflict(
        lambda: apps_api.create_namespaced_deployment(
            namespace, deployment_body, _request_timeout=_REQUEST_TIMEOUT
        ),
        f"Deployment {name}",
    )

    # Service
    # -------
    logging.info("Creating Service %s...", name)
    service_body = build_service_body(name=name)
    kopf.adopt(service_body)
    _create_ignoring_conflict(
        lambda: core_api.create_namespaced_service(
            namespace, service_body, _request_timeout=_REQUEST_TIMEOUT
        ),
        f"Service {name}",
    )

    # Ingress
    # -------
    logging.info("Creating Ingress %s...", name)
    ingress_body = build_ingress_body(
        name=name,
        ingress_path=ingress_path,
        ingress_domain=ingress_domain,
        ingress_class=ingress_class,
        ingress_proxy_body_size=ingress_proxy_body_size,
        ingress_tls_secret=ingress_tls_secret,
        ingress_cert_issuer=ingress_cert_issuer,
    )
    kopf.adopt(ingress_body)
    _create_ignoring_conflict(
        lambda: networking_api.create_namespaced_ingress(
            namespace, ingress_body, _request_timeout=_REQUEST_TIMEOUT
        ),
        f"Ingress {name}",
    )

    # Done
    # ----
    url = f"https://{ingress_domain}{ingress_path}"
    logging.info("Done %s (namespace=%s url=%s)", name, namespace, url)

    return {
        "viz": {"url": url},
        "image": image,
        "serviceAccountName": service_account,
        "resources": {
            "requests": {"memory": memory_request},
            "limits": {"memory": memory_limit},
        },
        "project": {"claimName": project_claim_name, "id": project_id},
    }
