"""
collectors/k8s.py
Interactúa con el cluster via subprocess (kubectl).
No requiere el SDK de kubernetes, funciona con cualquier kubeconfig.
"""
import subprocess
import json
import yaml
import logging
import requests
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("k8s")


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
    if result.returncode != 0:
        log.warning(f"Command failed ({result.returncode}): {' '.join(cmd[:5])}...")
        log.debug(f"stderr: {result.stderr[:500]}")
    return result.stdout, result.stderr, result.returncode


class K8sCollector:
    def __init__(self, cfg: dict):
        self.namespace = cfg.get("namespace", "monitoring")
        self.kubeconfig = cfg.get("kubeconfig")
        self.loki_url = cfg.get("loki_url", "http://loki.monitoring.svc.cluster.local:3100")
        self._base = ["kubectl"]
        if self.kubeconfig:
            self._base += ["--kubeconfig", self.kubeconfig]

    def _kubectl(self, *args) -> tuple[str, str, int]:
        return _run(self._base + list(args))

    # ─── OBSERVACIÓN ──────────────────────────────────────────────

    def get_unhealthy_pods(self, namespace: str = None) -> list[PodIssue]:
        """
        Obtiene pods en mal estado. Soporta múltiples namespaces separados por coma.
        """
        ns_param = namespace or self.namespace
        # Soportar namespaces separados por coma (ej: "monitoring,default")
        namespaces = [ns.strip() for ns in ns_param.split(",") if ns.strip()]
        
        all_issues = []
        for ns in namespaces:
            stdout, stderr, rc = self._kubectl(
                "get", "pods", "-n", ns, "-o", "json"
            )
            if rc != 0:
                log.warning(f"No se pudo obtener pods en namespace '{ns}': {stderr[:200]}")
                continue
                
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError as e:
                log.warning(f"Error parseando JSON para namespace '{ns}': {e}")
                continue

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
                        all_issues.append(PodIssue(
                            namespace=ns,
                            pod=pod_name,
                            container=cs["name"],
                            state=reason,
                            restart_count=cs.get("restartCount", 0),
                            reason=reason,
                            message=waiting.get("message") or terminated.get("message", "")
                        ))
        return all_issues

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


    # ─── LOKI INTEGRATION ─────────────────────────────────────────

    def query_loki(self, namespace: str, pod: str = None, 
                   query: str = None, limit: int = 100, 
                   since: str = "1h") -> str:
        """
        Consulta logs en Loki para obtener contexto histórico.
        
        Args:
            namespace: Namespace a consultar
            pod: Nombre del pod (opcional, puede ser regex)
            query: Query de LogQL adicional (ej: '|= "error"')
            limit: Cantidad máxima de líneas
            since: Rango de tiempo (ej: "1h", "30m", "1d")
        """
        # Construir query de LogQL
        if pod:
            # Escapar caracteres especiales en el nombre del pod
            pod_filter = f'pod=~"{pod}"'
        else:
            pod_filter = ''
        
        base_query = f'{{namespace="{namespace}"{pod_filter}}}'
        
        if query:
            base_query = f'{base_query} {query}'
        
        # Construir URL
        params = {
            'query': base_query,
            'limit': limit,
            'since': since
        }
        
        try:
            # Intentar consultar Loki
            loki_url = f"{self.loki_url}/loki/api/v1/query_range"
            response = requests.get(loki_url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('data', {}).get('result', [])
                
                if not results:
                    return f"No se encontraron logs en Loki para: {base_query}"
                
                # Formatear resultados
                logs = []
                for stream in results:
                    labels = stream.get('stream', {})
                    values = stream.get('values', [])
                    for timestamp, log_line in values:
                        logs.append(f"[{timestamp}] {log_line}")
                
                return "\n".join(logs[:limit]) if logs else "No hay logs disponibles"
            else:
                return f"Error consultando Loki (HTTP {response.status_code}): {response.text[:200]}"
                
        except requests.exceptions.ConnectionError:
            return f"Error: No se pudo conectar a Loki en {self.loki_url}. Verificar que Loki esté corriendo."
        except Exception as e:
            return f"Error consultando Loki: {e}"

    def search_errors_in_loki(self, namespace: str, pod: str = None, 
                              since: str = "24h") -> str:
        """
        Busca errores específicos en logs de Loki.
        Útil para encontrar patrones de fallos históricos.
        """
        error_patterns = [
            '|= "error"',
            '|= "ERROR"',
            '|= "exception"',
            '|= "Exception"',
            '|= "panic"',
            '|= "fatal"',
            '|= "CRASH"'
        ]
        
        all_errors = []
        for pattern in error_patterns:
            result = self.query_loki(namespace, pod, query=pattern, 
                                    limit=50, since=since)
            if result and not result.startswith("Error") and not result.startswith("No se"):
                all_errors.append(f"--- Patrón: {pattern} ---")
                all_errors.append(result)
        
        if all_errors:
            return "\n\n".join(all_errors[:500])  # Limitar tamaño
        return f"No se encontraron errores en logs de Loki para {namespace}/{pod or 'todos los pods'}"


    # ─── PROMETHEUS INTEGRATION ───────────────────────────────────

    def query_prometheus(self, query: str, time_range: str = "5m") -> str:
        """
        Ejecuta una consulta PromQL en Prometheus.
        
        Args:
            query: Query PromQL (ej: 'rate(container_cpu_usage_seconds_total[5m])')
            time_range: Rango de tiempo para queries de rango (ej: "5m", "1h")
        """
        prometheus_url = "http://prometheus-kube-prometheus-prometheus:9090"
        
        try:
            # Determinar si es query instantánea o de rango
            if time_range:
                end_time = "now()"
                start_time = f"{time_range}"
                url = f"{prometheus_url}/api/v1/query_range"
                params = {
                    'query': query,
                    'start': start_time,
                    'end': end_time,
                    'step': '15s'
                }
            else:
                url = f"{prometheus_url}/api/v1/query"
                params = {'query': query}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    results = data.get('data', {}).get('result', [])
                    if not results:
                        return f"No hay datos para la query: {query}"
                    
                    # Formatear resultados
                    output = []
                    for result in results[:20]:  # Limitar a 20 resultados
                        metric = result.get('metric', {})
                        pod = metric.get('pod', 'unknown')
                        namespace = metric.get('namespace', 'unknown')
                        value = result.get('value') or result.get('values', [['', 'N/A']])[-1]
                        val_str = value[1] if isinstance(value, list) else str(value)
                        output.append(f"{namespace}/{pod}: {val_str}")
                    
                    return "\n".join(output)
                else:
                    return f"Error en query: {data.get('error', 'Unknown error')}"
            else:
                return f"Error HTTP {response.status_code}: {response.text[:200]}"
                
        except requests.exceptions.ConnectionError:
            return f"Error: No se pudo conectar a Prometheus en {prometheus_url}"
        except Exception as e:
            return f"Error consultando Prometheus: {e}"

    def get_pod_metrics(self, namespace: str, pod: str) -> str:
        """
        Obtiene métricas de CPU, memoria y restarts de un pod específico.
        """
        queries = {
            'CPU Usage (cores)': f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod="{pod}"}}[5m])',
            'Memory Usage (bytes)': f'container_memory_usage_bytes{{namespace="{namespace}", pod="{pod}"}}',
            'Memory Limit (bytes)': f'container_spec_memory_limit_bytes{{namespace="{namespace}", pod="{pod}"}}',
            'Restarts': f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod="{pod}"}}',
            'Container Ready': f'kube_pod_container_status_ready{{namespace="{namespace}", pod="{pod}"}}'
        }
        
        results = []
        results.append(f"=== Métricas para {namespace}/{pod} ===\n")
        
        for metric_name, query in queries.items():
            result = self.query_prometheus(query, time_range=None)
            results.append(f"{metric_name}:")
            results.append(result)
            results.append("")
        
        return "\n".join(results)

    def get_high_resource_pods(self, namespace: str = None, threshold: float = 0.8) -> str:
        """
        Detecta pods con alta utilización de CPU o memoria.
        
        Args:
            namespace: Filtrar por namespace (opcional)
            threshold: Umbral de utilización (0.0 - 1.0, default 0.8 = 80%)
        """
        ns_filter = f', namespace="{namespace}"' if namespace else ''
        
        queries = {
            'High CPU': f'rate(container_cpu_usage_seconds_total{{container!=""{ns_filter}}}[5m]) > {threshold}',
            'High Memory': f'container_memory_usage_bytes{{container!=""{ns_filter}}} / container_spec_memory_limit_bytes{{container!=""{ns_filter}}} > {threshold}',
            'High Disk': f'container_fs_usage_bytes{{container!=""{ns_filter}}} / container_fs_limit_bytes{{container!=""{ns_filter}}} > {threshold}'
        }
        
        results = []
        results.append(f"=== Pods con alta utilización (>{threshold*100}%) ===\n")
        
        found_any = False
        for resource_type, query in queries.items():
            result = self.query_prometheus(query, time_range="5m")
            if result and not result.startswith("No hay datos"):
                found_any = True
                results.append(f"{resource_type}:")
                results.append(result)
                results.append("")
        
        if not found_any:
            return f"No se encontraron pods con utilización >{threshold*100}%"
        
        return "\n".join(results)

    def analyze_pod_health(self, namespace: str, pod: str) -> str:
        """
        Análisis completo de salud de un pod usando métricas de Prometheus.
        Detecta problemas como:
        - Alta utilización de recursos
        - Restarts frecuentes
        - Contenedores no listos
        - OOMKills cercanos al límite de memoria
        """
        analysis = []
        analysis.append(f"=== Análisis de salud: {namespace}/{pod} ===\n")
        
        # Verificar restarts
        restarts_query = f'kube_pod_container_status_restarts_total{{namespace="{namespace}", pod="{pod}"}}'
        restarts_result = self.query_prometheus(restarts_query, time_range=None)
        if "N/A" not in restarts_result:
            try:
                restarts = float(restarts_result.split(":")[-1].strip())
                if restarts > 5:
                    analysis.append(f"⚠️ ALTO: El pod tiene {restarts} restarts. Posible CrashLoopBackOff.")
                elif restarts > 0:
                    analysis.append(f"ℹ️ INFO: El pod tiene {restarts} restarts.")
                else:
                    analysis.append(f"✅ OK: El pod no tiene restarts.")
            except:
                analysis.append(f"Restarts: {restarts_result}")
        
        # Verificar uso de memoria vs límite
        memory_usage_query = f'container_memory_usage_bytes{{namespace="{namespace}", pod="{pod}"}}'
        memory_limit_query = f'container_spec_memory_limit_bytes{{namespace="{namespace}", pod="{pod}"}}'
        
        usage_result = self.query_prometheus(memory_usage_query, time_range=None)
        limit_result = self.query_prometheus(memory_limit_query, time_range=None)
        
        try:
            usage_str = usage_result.split(":")[-1].strip()
            limit_str = limit_result.split(":")[-1].strip()
            usage = float(usage_str)
            limit = float(limit_str)
            
            if limit > 0:
                percentage = (usage / limit) * 100
                if percentage > 90:
                    analysis.append(f"🚨 CRÍTICO: Uso de memoria al {percentage:.1f}%. Próximo a OOMKill!")
                elif percentage > 80:
                    analysis.append(f"⚠️ ALTO: Uso de memoria al {percentage:.1f}%. Considerar aumentar límite.")
                else:
                    analysis.append(f"✅ OK: Uso de memoria al {percentage:.1f}%.")
            else:
                analysis.append(f"ℹ️ INFO: No hay límite de memoria definido.")
        except:
            analysis.append(f"Memory: {usage_result} / {limit_result}")
        
        # Verificar uso de CPU
        cpu_query = f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod="{pod}"}}[5m])'
        cpu_result = self.query_prometheus(cpu_query, time_range=None)
        if "N/A" not in cpu_result:
            try:
                cpu_val = float(cpu_result.split(":")[-1].strip())
                if cpu_val > 0.8:
                    analysis.append(f"⚠️ ALTO: Uso de CPU alto ({cpu_val:.2f} cores).")
                else:
                    analysis.append(f"✅ OK: Uso de CPU normal ({cpu_val:.2f} cores).")
            except:
                analysis.append(f"CPU: {cpu_result}")
        
        # Verificar estado del contenedor
        ready_query = f'kube_pod_container_status_ready{{namespace="{namespace}", pod="{pod}"}}'
        ready_result = self.query_prometheus(ready_query, time_range=None)
        if "0" in ready_result:
            analysis.append(f"🚨 CRÍTICO: El contenedor NO está listo (ready=0).")
        elif "1" in ready_result:
            analysis.append(f"✅ OK: El contenedor está listo.")
        
        return "\n".join(analysis)
