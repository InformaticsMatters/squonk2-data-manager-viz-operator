"""Unit tests for the viz-operator's pure Kubernetes-object builders.

These tests exercise the body-construction logic in ``operator/handlers.py``
without talking to a Kubernetes cluster. The ``create`` handler itself is a
thin wrapper that reads configuration, calls these builders, and submits the
results via the Kubernetes API, so testing the builders covers the logic that
is most likely to regress.
"""

import os
import sys
from typing import Any, Dict, List

import kopf
import pytest

# Make 'operator/handlers.py' importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operator"))

import handlers  # noqa: E402  pylint: disable=wrong-import-position

CONTAINER_PORT = 5170
PROJECT_MOUNT_PATH = "/project"


def _example_environment() -> List[Dict[str, str]]:
    return handlers.build_environment(
        project_id="project-000",
        instance_id="instance-111",
        instance_owner="dlister",
        project_mount_path=PROJECT_MOUNT_PATH,
    )


def _example_deployment(**overrides: Any) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "name": "viz-abcdef",
        "image": "ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4",
        "image_pull_policy": "IfNotPresent",
        "service_account": "default",
        "cpu_request": "10m",
        "cpu_limit": "1",
        "memory_request": "256Mi",
        "memory_limit": "1Gi",
        "run_as_user": 1000,
        "run_as_group": 100,
        "project_claim_name": "claim-1",
        "project_id": "project-000",
        "project_mount_path": PROJECT_MOUNT_PATH,
        "env": _example_environment(),
        "node_selector": {"informaticsmatters.com/purpose-application": "yes"},
    }
    kwargs.update(overrides)
    return handlers.build_deployment_body(**kwargs)


# --- image ------------------------------------------------------------------


def test_build_image_combines_repository_and_tag() -> None:
    assert (
        handlers.build_image(
            image="ghcr.io/informaticsmatters/squonk2-viz-app", image_tag="0.1.4"
        )
        == "ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4"
    )


def test_build_image_without_tag_is_a_permanent_error() -> None:
    # A missing imageTag is unrecoverable - the operator must not retry.
    with pytest.raises(kopf.PermanentError):
        handlers.build_image(
            image="ghcr.io/informaticsmatters/squonk2-viz-app", image_tag=None
        )


def test_build_image_with_empty_tag_is_a_permanent_error() -> None:
    with pytest.raises(kopf.PermanentError):
        handlers.build_image(
            image="ghcr.io/informaticsmatters/squonk2-viz-app", image_tag=""
        )


def test_default_image_falls_back_to_the_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SVO_IMAGE", raising=False)
    assert handlers._get_default_image() == handlers._DEFAULT_IMAGE


def test_default_image_is_overridden_by_svo_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SVO_IMAGE", "registry.example.com/viz-app")
    assert handlers._get_default_image() == "registry.example.com/viz-app"


# --- image pull policy ------------------------------------------------------


def test_image_pull_policy_for_pinned_tag_is_if_not_present() -> None:
    assert handlers.image_pull_policy_for("a/b:0.1.4") == "IfNotPresent"


def test_image_pull_policy_for_latest_is_always() -> None:
    assert handlers.image_pull_policy_for("a/b:latest") == "Always"


def test_image_pull_policy_for_stable_is_always() -> None:
    assert handlers.image_pull_policy_for("a/b:STABLE") == "Always"


def test_image_pull_policy_for_untagged_is_always() -> None:
    # An untagged image is treated as ':latest' by Kubernetes.
    assert handlers.image_pull_policy_for("a/b") == "Always"


# --- environment ------------------------------------------------------------


def test_build_environment_sets_dm_project_dir_to_mount_path() -> None:
    env = _example_environment()
    by_name = {item["name"]: item["value"] for item in env}
    assert by_name["DM_PROJECT_DIR"] == PROJECT_MOUNT_PATH


def test_build_environment_exposes_dm_metadata() -> None:
    env = _example_environment()
    by_name = {item["name"]: item["value"] for item in env}
    assert by_name["DM_PROJECT_ID"] == "project-000"
    assert by_name["DM_INSTANCE_ID"] == "instance-111"
    assert by_name["DM_INSTANCE_OWNER"] == "dlister"


def test_build_environment_sets_dm_base_path_from_instance_id() -> None:
    # The viz-app serves under a sub-path when DM_BASE_PATH is set; the operator
    # mirrors the instance id (prefixed with '/') so assets, client-side routes
    # and API calls resolve under the Ingress prefix.
    env = _example_environment()
    by_name = {item["name"]: item["value"] for item in env}
    assert by_name["DM_BASE_PATH"] == "/instance-111"


def test_build_environment_omits_dm_base_path_when_instance_id_absent() -> None:
    # Without an instance id there is no sub-path; the app then serves at '/'.
    env = handlers.build_environment(
        project_id="project-000",
        instance_id="",
        instance_owner="dlister",
        project_mount_path=PROJECT_MOUNT_PATH,
    )
    by_name = {item["name"]: item["value"] for item in env}
    assert "DM_BASE_PATH" not in by_name


# --- service ----------------------------------------------------------------


def test_build_service_body_is_clusterip_on_container_port() -> None:
    body = handlers.build_service_body(name="viz-abcdef")
    assert body["apiVersion"] == "v1"
    assert body["kind"] == "Service"
    assert body["spec"]["type"] == "ClusterIP"
    port = body["spec"]["ports"][0]
    assert port["port"] == CONTAINER_PORT
    assert port["targetPort"] == CONTAINER_PORT
    assert body["spec"]["selector"] == {"deployment": "viz-abcdef"}


# --- deployment -------------------------------------------------------------


def test_build_deployment_body_basic_shape() -> None:
    body = _example_deployment()
    assert body["apiVersion"] == "apps/v1"
    assert body["kind"] == "Deployment"
    spec = body["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"
    assert spec["selector"]["matchLabels"] == {"deployment": "viz-abcdef"}
    pod_spec = spec["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "default"


def test_build_deployment_body_runs_the_viz_image_on_container_port() -> None:
    container = _example_deployment()["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == ("ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4")
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["ports"][0]["containerPort"] == CONTAINER_PORT


def test_build_deployment_body_injects_dm_environment() -> None:
    container = _example_deployment()["spec"]["template"]["spec"]["containers"][0]
    by_name = {item["name"]: item["value"] for item in container["env"]}
    assert by_name["DM_PROJECT_DIR"] == PROJECT_MOUNT_PATH


def test_build_deployment_body_mounts_project_pvc() -> None:
    pod_spec = _example_deployment()["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    mount = next(m for m in container["volumeMounts"] if m["name"] == "project")
    assert mount["mountPath"] == PROJECT_MOUNT_PATH
    assert mount["subPath"] == "project-000"
    volume = next(v for v in pod_spec["volumes"] if v["name"] == "project")
    assert volume["persistentVolumeClaim"]["claimName"] == "claim-1"


def test_build_deployment_body_sets_security_context() -> None:
    pod_spec = _example_deployment()["spec"]["template"]["spec"]
    sec = pod_spec["securityContext"]
    assert sec["runAsUser"] == 1000
    assert sec["runAsGroup"] == 100
    assert sec["fsGroup"] == 100


def test_build_deployment_body_applies_resources() -> None:
    container = _example_deployment()["spec"]["template"]["spec"]["containers"][0]
    res = container["resources"]
    assert res["requests"] == {"memory": "256Mi", "cpu": "10m"}
    assert res["limits"] == {"memory": "1Gi", "cpu": "1"}


def test_build_deployment_body_omits_priority_class_by_default() -> None:
    pod_spec = _example_deployment()["spec"]["template"]["spec"]
    assert "priorityClassName" not in pod_spec


def test_build_deployment_body_adds_priority_class_when_requested() -> None:
    pod_spec = _example_deployment(pod_priority_class="im-application-low")["spec"][
        "template"
    ]["spec"]
    assert pod_spec["priorityClassName"] == "im-application-low"


def test_build_deployment_body_adds_extra_labels_to_pod_template() -> None:
    labels = _example_deployment(
        extra_labels={"data-manager.informaticsmatters.com/instance": "viz-abcdef"}
    )["spec"]["template"]["metadata"]["labels"]
    assert labels["deployment"] == "viz-abcdef"
    assert labels["data-manager.informaticsmatters.com/instance"] == "viz-abcdef"


def test_build_deployment_body_omits_image_pull_secrets_by_default() -> None:
    pod_spec = _example_deployment()["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod_spec


def test_build_deployment_body_adds_image_pull_secrets_when_given() -> None:
    pod_spec = _example_deployment(image_pull_secrets=["ghcr-pull-secret"])["spec"][
        "template"
    ]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "ghcr-pull-secret"}]


# --- image pull secret config -----------------------------------------------


def test_get_image_pull_secrets_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SVO_IMAGE_PULL_SECRET", "ghcr-pull-secret")
    assert handlers._get_image_pull_secrets() == ["ghcr-pull-secret"]


def test_get_image_pull_secrets_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SVO_IMAGE_PULL_SECRET", raising=False)
    assert handlers._get_image_pull_secrets() == []


# --- ingress ----------------------------------------------------------------


def test_build_ingress_body_is_path_based_to_the_service() -> None:
    body = handlers.build_ingress_body(
        name="viz-abcdef",
        ingress_path="/viz-abcdef",
        ingress_domain="example.com",
        ingress_class="nginx",
        ingress_proxy_body_size="500m",
        ingress_tls_secret="tls-secret",
        ingress_cert_issuer=None,
    )
    assert body["apiVersion"] == "networking.k8s.io/v1"
    rule = body["spec"]["rules"][0]
    assert rule["host"] == "example.com"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/viz-abcdef"
    assert path["pathType"] == "Prefix"
    backend = path["backend"]["service"]
    assert backend["name"] == "viz-abcdef"
    assert backend["port"]["number"] == CONTAINER_PORT


def test_build_ingress_body_uses_tls_secret_when_provided() -> None:
    body = handlers.build_ingress_body(
        name="viz-abcdef",
        ingress_path="/viz-abcdef",
        ingress_domain="example.com",
        ingress_class="nginx",
        ingress_proxy_body_size="500m",
        ingress_tls_secret="tls-secret",
        ingress_cert_issuer="letsencrypt",
    )
    assert body["spec"]["tls"][0]["secretName"] == "tls-secret"
    # With an explicit TLS secret we must NOT defer to cert-manager.
    assert "cert-manager.io/cluster-issuer" not in body["metadata"]["annotations"]


def test_build_ingress_body_uses_cert_manager_without_tls_secret() -> None:
    body = handlers.build_ingress_body(
        name="viz-abcdef",
        ingress_path="/viz-abcdef",
        ingress_domain="example.com",
        ingress_class="nginx",
        ingress_proxy_body_size="500m",
        ingress_tls_secret=None,
        ingress_cert_issuer="letsencrypt",
    )
    annotations = body["metadata"]["annotations"]
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt"


# --- ingress config ---------------------------------------------------------


def test_default_ingress_proxy_body_size_is_one_megabyte() -> None:
    assert handlers._DEFAULT_INGRESS_PROXY_BODY_SIZE == "1m"


def test_default_ingress_class_falls_back_to_the_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SVO_INGRESS_CLASS", raising=False)
    assert handlers._get_default_ingress_class() == handlers._DEFAULT_INGRESS_CLASS


def test_default_ingress_class_is_overridden_by_svo_ingress_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SVO_INGRESS_CLASS", "traefik")
    assert handlers._get_default_ingress_class() == "traefik"


# --- status / application URL location --------------------------------------

# The CRD carries the annotation
# 'data-manager.informaticsmatters.com/application-url-location: viz.url'.
# The Data Manager reads the URL from the 'status'-relative path it names, i.e.
# custom_resource['status']['viz']['url']. The create handler has id='viz', so
# kopf nests its return value under 'status.viz'; therefore 'url' MUST be a
# top-level key of the returned dict for the two to line up.
APPLICATION_URL_LOCATION = "viz.url"


def _example_status(**overrides: Any) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "url": "https://example.com/viz-abcdef",
        "image": "ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4",
        "service_account": "default",
        "memory_request": "256Mi",
        "memory_limit": "1Gi",
        "project_claim_name": "claim-1",
        "project_id": "project-000",
    }
    kwargs.update(overrides)
    return handlers.build_status(**kwargs)


def test_build_status_places_url_at_the_annotated_location() -> None:
    # Reproduces the reporting bug: the URL must be reachable at the path named
    # by the CRD's 'application-url-location' annotation once kopf has nested
    # the handler's return value under 'status.<handler-id>' ('status.viz').
    handler_id = "viz"
    status = {handler_id: _example_status(url="https://example.com/viz-abcdef")}

    cursor: Any = status
    for key in APPLICATION_URL_LOCATION.split("."):
        cursor = cursor[key]
    assert cursor == "https://example.com/viz-abcdef"


def test_build_status_does_not_double_nest_under_viz() -> None:
    # Guard against the regression where the URL was returned as
    # {'viz': {'url': ...}}, landing at status.viz.viz.url and so invisible to
    # the Data Manager (which only looks at status.viz.url).
    status = _example_status()
    assert "viz" not in status
    assert status["url"] == "https://example.com/viz-abcdef"


def test_build_status_reports_instance_metadata() -> None:
    status = _example_status()
    assert status["image"] == "ghcr.io/informaticsmatters/squonk2-viz-app:0.1.4"
    assert status["serviceAccountName"] == "default"
    assert status["resources"]["requests"]["memory"] == "256Mi"
    assert status["resources"]["limits"]["memory"] == "1Gi"
    assert status["project"] == {"claimName": "claim-1", "id": "project-000"}
