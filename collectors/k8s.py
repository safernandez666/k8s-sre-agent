"""
collectors/k8s.py
Interactúa con el cluster via subprocess (kubectl).
No requiere el SDK de kubernetes, funciona con cualquier kubeconfig.
"""
import subprocess
import json
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PodIssue:
    namespace: str
    pod: str
    container: str
    state: str          # CrashLoopBackOff, OOMKilled, ImagePullBackOff, etc.
    restart_count: int
    reason: str
    message: str


def _run(cmd: list[str]) -> tuple[str, str, int]:
    """Ejecuta un comando y retorna (stdout, stderr, returncode)."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout, result.stderr, result.returncode


class K8sCollector:
    def __init__(self, cfg: dict):
        self.namespace = cfg.get("namespace", "monitoring")
        self.kubeconfig = cfg.get("kubeconfig")
        self._base = ["kubectl"]
        if self.kubeconfig:
            self._base += ["--kubeconfig", self.kubeconfig]

    def _kubectl(self, *args) -> tuple[str, str, int]:
        return _run(self._base + list(args))

    # ─── OBSERVACIÓN ──────────────────────────────────────────────

    def get_unhealthy_pods(self, namespace: str = None) -> list[PodIssue]:
        ns = namespace or self.namespace
        stdout, _, _ = self._kubectl(
            "get", "pods", "-n", ns, "-o", "json"
        )
        issues = []
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return issues

        for pod in data.get("items", []):
            pod_name = pod["metadata"]["name"]
            for cs in pod.get("status", {}).get("containerStatuses", []):
                waiting = cs.get("state", {}).get("waiting", {})
                terminated = cs.get("state", {}).get("terminated", {})
                reason = waiting.get("reason") or terminated.get("reason", "")
                bad_states = {
                    "CrashLoopBackOff", "OOMKilled", "ImagePullBackOff",
                    "ErrImagePull", "Error", "CreateContainerConfigError",
                    "RunContainerError"
                }
                if reason in bad_states:
                    issues.append(PodIssue(
                        namespace=ns,
                        pod=pod_name,
                        container=cs["name"],
                        state=reason,
                        restart_count=cs.get("restartCount", 0),
                        reason=reason,
                        message=waiting.get("message") or terminated.get("message", "")
                    ))
        return issues

    def get_pod_logs(self, namespace: str, pod: str, container: str,
                     previous: bool = True, tail: int = 50) -> str:
        args = ["logs", "-n", namespace, pod, "-c", container, f"--tail={tail}"]
        if previous:
            args.append("--previous")
        stdout, stderr, _ = self._kubectl(*args)
        return stdout or stderr

    def describe_pod(self, namespace: str, pod: str) -> str:
        stdout, stderr, _ = self._kubectl("describe", "pod", "-n", namespace, pod)
        return stdout or stderr

    def get_events(self, namespace: str, resource: str = None) -> str:
        args = ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]
        if resource:
            args += ["--field-selector", f"involvedObject.name={resource}"]
        stdout, stderr, _ = self._kubectl(*args)
        return stdout or stderr

    def get_rbac_for_sa(self, namespace: str, serviceaccount: str) -> str:
        """Verifica los permisos de un ServiceAccount."""
        stdout, _, _ = self._kubectl(
            "get", "clusterrolebinding", "-o", "json"
        )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return "Error obteniendo RBAC"

        bindings = []
        for b in data.get("items", []):
            for subj in b.get("subjects", []):
                if (subj.get("name") == serviceaccount and
                        subj.get("namespace") == namespace):
                    bindings.append(b["metadata"]["name"])
        return f"ClusterRoleBindings para {serviceaccount}: {bindings}" if bindings \
               else f"Sin ClusterRoleBindings para {serviceaccount} en {namespace}"

    def check_pvc_status(self, namespace: str) -> str:
        stdout, _, _ = self._kubectl("get", "pvc", "-n", namespace, "-o", "wide")
        return stdout

    # ─── ACCIÓN ───────────────────────────────────────────────────

    def helm_upgrade(self, release: str, chart: str, namespace: str,
                     set_values: dict, dry_run: bool = False) -> str:
        args = ["helm", "upgrade", release, chart, "-n", namespace]
        for k, v in set_values.items():
            args += ["--set", f"{k}={v}"]
        if dry_run:
            args.append("--dry-run")
        stdout, stderr, rc = _run(args)
        return stdout if rc == 0 else f"ERROR: {stderr}"

    def kubectl_apply(self, manifest_yaml: str, dry_run: bool = False) -> str:
        args = ["kubectl", "apply", "-f", "-"]
        if dry_run:
            args += ["--dry-run=client"]
        result = subprocess.run(
            args, input=manifest_yaml,
            capture_output=True, text=True, timeout=30
        )
        return result.stdout if result.returncode == 0 else f"ERROR: {result.stderr}"

    def restart_pod(self, namespace: str, pod: str, dry_run: bool = False) -> str:
        if dry_run:
            return f"[DRY RUN] kubectl delete pod {pod} -n {namespace}"
        stdout, stderr, rc = self._kubectl("delete", "pod", pod, "-n", namespace)
        return stdout if rc == 0 else f"ERROR: {stderr}"

    def rollout_restart(self, namespace: str, resource: str, dry_run: bool = False) -> str:
        """ej: resource = 'deployment/prometheus-grafana'"""
        if dry_run:
            return f"[DRY RUN] kubectl rollout restart {resource} -n {namespace}"
        stdout, stderr, rc = self._kubectl(
            "rollout", "restart", resource, "-n", namespace
        )
        return stdout if rc == 0 else f"ERROR: {stderr}"

    def patch_resource(self, namespace: str, resource: str,
                       patch: dict, dry_run: bool = False) -> str:
        patch_str = json.dumps(patch)
        args = ["patch", resource, "-n", namespace,
                "--type=merge", f"--patch={patch_str}"]
        if dry_run:
            args.append("--dry-run=client")
        stdout, stderr, rc = self._kubectl(*args)
        return stdout if rc == 0 else f"ERROR: {stderr}"
