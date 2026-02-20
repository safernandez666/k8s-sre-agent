# K8s SRE/SecOps Agent

Agente ReAct (Reason → Act → Observe) para diagnóstico y remediación autónoma
de incidentes en Kubernetes, potenciado por Kimi como LLM.

## Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                    LOOP CONTINUO (30s)                       │
│                                                              │
│   ClusterMonitor                                             │
│   ─────────────                                              │
│   get_unhealthy_pods()                                       │
│          │                                                   │
│          ▼ (CrashLoopBackOff / OOMKilled / etc.)             │
│                                                              │
│   ┌──────────────────────────────────────────────┐           │
│   │           AGENTE ReAct (Kimi)                 │           │
│   │                                              │           │
│   │  ITER 1: describe_pod()    ← OBSERVE         │           │
│   │  ITER 2: get_pod_logs()    ← OBSERVE         │           │
│   │  ITER 3: check_rbac()      ← OBSERVE         │           │
│   │  ITER 4: kubectl_apply()   ← ACT             │           │
│   │  ITER 5: get_pod_logs()    ← VERIFY          │           │
│   │          finish(resolved=True)               │           │
│   └──────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
```

## Instalación

```bash
cd k8s-agent
pip install -r requirements.txt
```

## Configuración

Editá `config.yaml` con tu API key de Kimi y los datos del cluster.

```yaml
kimi:
  api_key: "sk-..."
  model: "moonshot-v1-8k"

agent:
  auto_remediate: false   # true para modo autónomo completo
  dry_run: false          # true para simular sin ejecutar
```

## Uso

```bash
# Monitor continuo (pregunta antes de remediar)
python main.py

# Monitor continuo autónomo (sin confirmación)
python main.py --auto

# Simular sin ejecutar nada
python main.py --dry-run --auto

# Fix directo para el problema actual de Grafana
python main.py --fix "Pod prometheus-grafana en CrashLoopBackOff.
Los sidecars grafana-sc-datasources y grafana-sc-dashboard crashean.
Namespace: monitoring"

# Un solo ciclo de detección
python main.py --once
```

## Herramientas disponibles para el agente

| Herramienta      | Tipo      | Descripción                              |
|------------------|-----------|------------------------------------------|
| get_pod_logs     | Observar  | Logs del contenedor (actual o anterior)  |
| describe_pod     | Observar  | kubectl describe pod                     |
| get_events       | Observar  | Eventos del namespace/recurso            |
| check_rbac       | Observar  | Permisos del ServiceAccount              |
| helm_upgrade     | Actuar    | Modifica valores del chart               |
| kubectl_apply    | Actuar    | Aplica manifest YAML                     |
| rollout_restart  | Actuar    | Reinicio graceful de deployment          |
| finish           | Terminar  | Cierra el loop con resultado             |

## Roadmap

```
v0.1 (PoC actual)
└── ReAct sobre kubectl
└── Detección de CrashLoop / OOMKill / ImagePull
└── Fix de RBAC, Helm, manifests

v0.2 (próximo)
└── Integración Wazuh (alertas EDR)
└── Integración Loki (correlación de logs)
└── Memoria de incidentes (SQLite)
└── Notificaciones (Slack/webhook)

v0.3
└── Gestión de identidades (IAM)
└── Correlación usuario → alerta → pod
└── MITRE ATT&CK mapping
└── Multi-agente (un agente por nodo)
```
