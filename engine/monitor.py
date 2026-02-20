"""
engine/monitor.py
Loop de observación continua. Detecta pods en mal estado y dispara el agente ReAct.

Namespace config:
  - "monitoring"           → solo ese namespace
  - "monitoring,default"   → múltiples namespaces
  - "*"                    → todos los namespaces del cluster
"""
import time
import logging
from collections import defaultdict
from collectors.k8s import K8sCollector, PodIssue

log = logging.getLogger("monitor")


class ClusterMonitor:
    def __init__(self, k8s: K8sCollector, agent, cfg: dict):
        self.k8s = k8s
        self.agent = agent
        self.poll_interval = cfg['agent']['poll_interval']
        self.auto_remediate = cfg['agent']['auto_remediate']
        self.namespace = cfg['kubernetes']['namespace']
        # Tracking para no re-disparar el mismo incidente
        self._active_incidents: dict[str, int] = defaultdict(int)

    def _get_namespaces(self) -> list[str]:
        """Resuelve la configuración de namespace a una lista concreta."""
        if self.namespace == "*":
            stdout, _, _ = self.k8s._kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}")
            return stdout.strip().split() if stdout.strip() else ["default"]
        return [ns.strip() for ns in self.namespace.split(",") if ns.strip()]

    def run(self):
        namespaces = self._get_namespaces()
        log.info(f"Monitor iniciado | namespaces={namespaces} | interval={self.poll_interval}s")
        log.info(f"Auto-remediate: {'ON' if self.auto_remediate else 'OFF (modo confirmación)'}")

        while True:
            try:
                self._cycle()
            except KeyboardInterrupt:
                log.info("Monitor detenido por usuario")
                break
            except Exception as e:
                log.error(f"Error en ciclo de monitoreo: {e}", exc_info=True)
            time.sleep(self.poll_interval)

    def _cycle(self):
        namespaces = self._get_namespaces()

        all_issues = []
        for ns in namespaces:
            all_issues.extend(self.k8s.get_unhealthy_pods(ns))

        if not all_issues:
            log.info(f"✅ Cluster saludable | namespaces={namespaces}")
            self._active_incidents.clear()
            return

        for issue in all_issues:
            incident_key = f"{issue.pod}/{issue.container}"
            self._active_incidents[incident_key] += 1
            count = self._active_incidents[incident_key]

            # Solo actuar en el primer ciclo o cada 5 ciclos (evitar spam)
            if count != 1 and count % 5 != 0:
                log.warning(f"⚠️  {incident_key} sigue en {issue.state} (ciclo #{count})")
                continue

            log.warning(f"\n{'='*60}")
            log.warning(f"🚨 INCIDENTE DETECTADO")
            log.warning(f"   Pod:       {issue.pod}")
            log.warning(f"   Container: {issue.container}")
            log.warning(f"   Estado:    {issue.state}")
            log.warning(f"   Reinicios: {issue.restart_count}")
            log.warning(f"{'='*60}")

            if self.auto_remediate:
                self._remediate(issue)
            else:
                self._ask_and_remediate(issue)

    def _remediate(self, issue: PodIssue):
        description = self._build_issue_description(issue)
        log.info("🤖 Iniciando agente ReAct...")
        result = self.agent.solve(description)
        self._log_result(result)

    def _ask_and_remediate(self, issue: PodIssue):
        print(f"\n¿Iniciar remediación automática para {issue.pod}/{issue.container}? [s/N]: ", end="")
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = "n"

        if answer in ("s", "si", "sí", "y", "yes"):
            self._remediate(issue)
        else:
            log.info("Remediación omitida por usuario")

    def _build_issue_description(self, issue: PodIssue) -> str:
        return f"""
Pod en mal estado detectado:
- Namespace: {issue.namespace}
- Pod: {issue.pod}
- Contenedor: {issue.container}
- Estado: {issue.state}
- Reinicios: {issue.restart_count}
- Mensaje: {issue.message or 'Sin mensaje'}

Por favor diagnostica la causa raíz y aplica el fix correspondiente.
"""

    def _log_result(self, result: dict):
        symbol = "✅" if result["resolved"] else "❌"
        log.info(f"\n{symbol} INCIDENTE {'RESUELTO' if result['resolved'] else 'SIN RESOLVER'}")
        log.info(f"   {result['summary']}")
        log.info(f"   Pasos ejecutados: {len(result['steps'])}")