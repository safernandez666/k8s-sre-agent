# K8s SRE/SecOps Agent

Agente ReAct (Reason → Act → Observe) para diagnóstico y remediación autónoma
de incidentes en Kubernetes, potenciado por LLM (compatible con Ollama, Kimi, OpenAI, etc.)

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
│   │           AGENTE ReAct (LLM)                  │           │
│   │                                              │           │
│   │  ITER 1: describe_pod()    ← OBSERVE         │           │
│   │  ITER 2: query_loki()      ← OBSERVE (logs)  │           │
│   │  ITER 3: get_pod_logs()    ← OBSERVE         │           │
│   │  ITER 4: check_rbac()      ← OBSERVE         │           │
│   │  ITER 5: helm_upgrade()    ← ACT             │           │
│   │  ITER 6: get_events()      ← VERIFY          │           │
│   │          finish(resolved=True)               │           │
│   └──────────────────────────────────────────────┘           │
│                          ▲                                   │
│                          │                                   │
│                   ┌──────┴──────┐                            │
│                   │    Loki     │ ← Logs históricos          │
│                   │  (LogsQL)   │    24h/7d/30d              │
│                   └─────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

## Requisitos

- Python 3.10+
- Kubernetes cluster (kubectl configurado)
- Ollama (para correr el LLM localmente) o API key de Kimi/OpenAI
- (Opcional) Loki instalado en el cluster para logs históricos

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/safernandez666/k8s-sre-agent.git
cd k8s-sre-agent
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Instalar Ollama (opción local, recomendado)

**Linux/Mac:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**Windows:**
Descargar desde: https://ollama.com/download/windows

### 4. Descargar un modelo con Ollama

```bash
# Modelo recomendado (rápido y bueno para coding)
ollama pull qwen2.5-coder:7b

# Alternativas:
# ollama pull llama3.2:3b        # Más liviano
# ollama pull codellama:7b       # Especializado en código
# ollama pull mistral:7b         # Buen balance
```

### 5. Verificar que Ollama esté corriendo

```bash
ollama list
# Debería mostrar el modelo descargado

# Probar el modelo
ollama run qwen2.5-coder:7b "Hola"
```

## Configuración

Editá `config.yaml` con tu configuración:

### Opción A: Usar Ollama (local, gratis)

```yaml
kimi:
  api_key: "ollama"        # Cualquier string, ollama no valida
  model: "qwen2.5-coder:7b"
  base_url: "http://localhost:11434/v1"

kubernetes:
  namespace: "monitoring,default,kube-system"
  kubeconfig: null         # null = usa ~/.kube/config

agent:
  poll_interval: 30        # segundos entre ciclos
  auto_remediate: false    # true = actúa sin confirmación
  max_iterations: 8        # máximo pasos ReAct
  dry_run: false

loki:
  url: "http://loki.monitoring.svc.cluster.local:3100"
  enabled: true            # true = usa logs históricos
```

### Opción B: Usar Kimi (API en la nube)

```yaml
kimi:
  api_key: "sk-..."        # Tu API key de Kimi
  model: "moonshot-v1-8k"
  base_url: "https://api.moonshot.cn/v1"
```

## Uso

```bash
# Monitor continuo (pregunta antes de remediar)
python main.py

# Monitor continuo autónomo (sin confirmación)
python main.py --auto

# Simular sin ejecutar nada
python main.py --dry-run --auto

# Fix directo para un problema específico
python main.py --fix "Pod prometheus-grafana en CrashLoopBackOff"

# Un solo ciclo de detección
python main.py --once
```

## Herramientas disponibles para el agente

| Herramienta           | Tipo      | Descripción                                          |
|----------------------|-----------|------------------------------------------------------|
| get_pod_logs         | Observar  | Logs del contenedor (último crash)                   |
| describe_pod         | Observar  | kubectl describe pod                                 |
| get_events           | Observar  | Eventos del namespace/recurso                        |
| check_rbac           | Observar  | Permisos del ServiceAccount                          |
| **query_loki**       | Observar  | Logs históricos de Loki (LogQL)                      |
| **search_errors_in_loki** | Observar | Busca patrones de error en logs históricos      |
| helm_upgrade         | Actuar    | Modifica valores del chart                           |
| kubectl_apply        | Actuar    | Aplica manifest YAML                                 |
| rollout_restart      | Actuar    | Reinicio graceful de deployment                      |
| finish               | Terminar  | Cierra el loop con resultado                         |

### Ejemplos de consultas Loki

El agente puede usar LogQL para obtener contexto histórico:

```logql
# Logs de los últimos 24 horas
{namespace="monitoring", pod=~"grafana.*"}

# Buscar errores en el último día
{namespace="default"} |= "error"

# Logs de múltiples pods simultáneamente
{namespace="kube-system", pod=~"calico.*"}
```

## Integración con Loki + Grafana (Opcional pero recomendado)

Para que el agente tenga acceso a logs históricos:

### 1. Instalar Loki

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm upgrade --install loki grafana/loki-stack \
  --namespace monitoring \
  --set promtail.enabled=true
```

### 2. Exponer Grafana con Ingress

```bash
# Aplicar configuración de MetalLB + Ingress
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.9.6/deploy/static/provider/cloud/deploy.yaml

# Configurar Ingress para Grafana
kubectl apply -f loki-datasource.yaml  # Ver archivo en repo
```

### 3. Configurar /etc/hosts

```
192.168.1.240   grafana.local
```

Acceder a: http://grafana.local

## Roadmap

```
v0.1 (actual) ✅
└── ReAct sobre kubectl
└── Detección de CrashLoop / OOMKill / ImagePull
└── Fix de RBAC, Helm, manifests
└── Integración Loki para logs históricos ✅

v0.2 (próximo)
└── Integración Wazuh (alertas EDR)
└── Memoria de incidentes (SQLite)
└── Notificaciones (Slack/webhook)
└── Dashboard web de incidentes

v0.3
└── Gestión de identidades (IAM)
└── Correlación usuario → alerta → pod
└── MITRE ATT&CK mapping
└── Multi-agente (un agente por nodo)
```

## Troubleshooting

### Ollama no responde

```bash
# Verificar que ollama esté corriendo
curl http://localhost:11434/api/tags

# Reiniciar ollama
ollama serve
```

### El agente no encuentra el cluster

```bash
# Verificar kubectl
kubectl get nodes

# Especificar kubeconfig en config.yaml
kubernetes:
  kubeconfig: "/ruta/a/tu/config"
```

### Loki no conecta

```bash
# Verificar que Loki esté corriendo
kubectl get pods -n monitoring | grep loki

# Probar desde el pod de Grafana
kubectl exec -it -n monitoring deployment/prometheus-grafana -- \
  wget -qO- http://loki.monitoring.svc.cluster.local:3100/ready
```

## Licencia

MIT
