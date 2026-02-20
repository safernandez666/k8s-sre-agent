"""
engine/react.py
Implementa el loop ReAct (Reason → Act → Observe) usando Kimi.
Kimi decide qué herramienta usar en cada paso, igual que Function Calling.
"""
import json
import re
import uuid
from typing import Callable
from openai import OpenAI


# ─── DEFINICIÓN DE HERRAMIENTAS ───────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_pod_logs",
            "description": "Obtiene los logs de un contenedor. Usar cuando necesites ver qué error está produciendo un pod.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod":       {"type": "string"},
                    "container": {"type": "string"},
                    "previous":  {"type": "boolean", "description": "true para ver logs del crash anterior"}
                },
                "required": ["namespace", "pod", "container"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_pod",
            "description": "Describe un pod (eventos, volúmenes, estado de contenedores). Útil para diagnóstico inicial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod":       {"type": "string"}
                },
                "required": ["namespace", "pod"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "Obtiene los eventos de Kubernetes para un namespace o recurso específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "resource":  {"type": "string", "description": "Nombre del pod/deployment (opcional)"}
                },
                "required": ["namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_rbac",
            "description": "Verifica los permisos RBAC de un ServiceAccount. Usar cuando sospechas errores 403/Forbidden.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace":      {"type": "string"},
                    "serviceaccount": {"type": "string"}
                },
                "required": ["namespace", "serviceaccount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "helm_upgrade",
            "description": "Ejecuta helm upgrade para modificar la configuración de un release. Usar para cambiar valores del chart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "release":    {"type": "string"},
                    "chart":      {"type": "string"},
                    "namespace":  {"type": "string"},
                    "set_values": {
                        "type": "object",
                        "description": "Dict de valores a setear (equivalente a --set key=value)"
                    }
                },
                "required": ["release", "chart", "namespace", "set_values"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kubectl_apply",
            "description": "Aplica un manifest YAML al cluster. Usar para crear/modificar recursos RBAC, ConfigMaps, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "manifest_yaml": {"type": "string", "description": "Manifest YAML completo como string"}
                },
                "required": ["manifest_yaml"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rollout_restart",
            "description": "Reinicia un deployment/daemonset/statefulset gracefully.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "resource":  {"type": "string", "description": "ej: deployment/prometheus-grafana"}
                },
                "required": ["namespace", "resource"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_loki",
            "description": "Consulta logs históricos en Loki. Útil para obtener contexto de errores pasados y ver logs de más de un contenedor. Usar cuando necesites ver logs de más de 1 hora atrás o buscar patrones de error históricos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace del pod"},
                    "pod":       {"type": "string", "description": "Nombre del pod o patrón regex (opcional)"},
                    "query":     {"type": "string", "description": "Filtro adicional de LogQL (ej: '|= \"error\"')"},
                    "limit":     {"type": "integer", "description": "Máximo de líneas a retornar", "default": 100},
                    "since":     {"type": "string", "description": "Rango de tiempo (1h, 30m, 1d, 7d)", "default": "1h"}
                },
                "required": ["namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_errors_in_loki",
            "description": "Busca patrones de error en logs históricos de Loki. Útil para diagnosticar problemas recurrentes o encontrar la causa raíz de fallos pasados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace a consultar"},
                    "pod":       {"type": "string", "description": "Nombre del pod (opcional)"},
                    "since":     {"type": "string", "description": "Cuánto tiempo atrás buscar (ej: 24h, 7d)", "default": "24h"}
                },
                "required": ["namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "Ejecuta una consulta PromQL en Prometheus. Útil para obtener métricas de CPU, memoria, restarts, etc. Usar cuando necesites ver el uso de recursos de un pod o detectar anomalías.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Query PromQL (ej: 'rate(container_cpu_usage_seconds_total[5m])')"},
                    "time_range": {"type": "string", "description": "Rango de tiempo (ej: 5m, 1h)", "default": "5m"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_metrics",
            "description": "Obtiene métricas de CPU, memoria y restarts de un pod específico. Útil para diagnosticar problemas de recursos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace del pod"},
                    "pod": {"type": "string", "description": "Nombre del pod"}
                },
                "required": ["namespace", "pod"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_high_resource_pods",
            "description": "Detecta pods con alta utilización de CPU o memoria (>80%). Útil para encontrar pods que pueden estar causando problemas de rendimiento.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace a consultar (opcional, vacío = todos)"},
                    "threshold": {"type": "number", "description": "Umbral de utilización (0.0-1.0, default 0.8)", "default": 0.8}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_pod_health",
            "description": "Análisis completo de salud de un pod usando métricas de Prometheus. Detecta problemas como alta utilización de recursos, restarts frecuentes, contenedores no listos, y riesgo de OOMKill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace del pod"},
                    "pod": {"type": "string", "description": "Nombre del pod"}
                },
                "required": ["namespace", "pod"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Llama cuando el problema está resuelto o cuando no hay más acciones posibles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "resolved":  {"type": "boolean"},
                    "summary":   {"type": "string", "description": "Resumen de lo que se hizo y el resultado"}
                },
                "required": ["resolved", "summary"]
            }
        }
    }
]


TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def _extract_json_objects(text: str) -> list[str]:
    """
    Extrae bloques JSON balanceados del texto, soportando anidamiento
    arbitrario. Recorre el string contando llaves abiertas/cerradas.
    """
    results = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_string = False
            escape = False
            for j in range(i, len(text)):
                ch = text[j]
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_string:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        results.append(text[start:j+1])
                        i = j
                        break
        i += 1
    return results


def _parse_tool_call_from_text(text: str):
    """
    Fallback: algunos modelos locales (ej. qwen via Ollama) devuelven el
    tool call como JSON en msg.content en lugar de usar el campo tool_calls.
    Intenta extraer {"name": "...", "arguments": {...}} del texto.
    Retorna una lista de objetos similares a tool_call, o None.
    """
    if not text:
        return None

    for raw in _extract_json_objects(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("name") in TOOL_NAMES and "arguments" in obj:
            tc = _SyntheticToolCall(
                id=f"syn_{uuid.uuid4().hex[:8]}",
                name=obj["name"],
                arguments=json.dumps(obj["arguments"], ensure_ascii=False),
            )
            return [tc]
    return None


class _SyntheticToolCall:
    """Imita la interfaz de openai.types.chat.ChatCompletionMessageToolCall."""
    def __init__(self, id: str, name: str, arguments: str):
        self.id = id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()
        self.type = "function"


SYSTEM_PROMPT = """Eres un agente SRE/SecOps experto en Kubernetes y ciberseguridad.
Tu trabajo es diagnosticar y remediar problemas del cluster de forma autónoma usando las herramientas disponibles.

PROCESO OBLIGATORIO:
1. OBSERVAR (máximo 2-3 llamadas): Recolecta información con get_pod_logs, describe_pod, get_events.
   - USA query_loki o search_errors_in_loki cuando necesites ver logs históricos (más de 1 hora) o buscar patrones de error.
   - NO repitas herramientas de observación si ya tienes suficiente información.
2. RAZONAR: Identifica la causa raíz con la información que ya tienes.
3. ACTUAR: Aplica el fix más conservador (helm_upgrade, kubectl_apply, rollout_restart).
4. VERIFICAR: Confirma que el fix funcionó (describe_pod o get_events).
5. FINALIZAR: Llama a finish() con el resultado.

REGLAS CRÍTICAS:
- Máximo 2-3 pasos de observación. Después DEBES actuar o llamar finish().
- NO repitas la misma herramienta con los mismos argumentos.
- Si ya identificaste la causa raíz, actúa INMEDIATAMENTE. No sigas recolectando datos.
- Prefiere fixes no destructivos (helm_upgrade > delete pod).
- Si el fix requiere kubectl_apply con RBAC, genera el manifest correcto y completo.
- SIEMPRE termina llamando a finish() con resolved=true/false y un resumen técnico.

CUÁNDO USAR LOKI (logs históricos):
- Usa query_loki cuando: necesites ver logs de más de 1 hora atrás, buscar en múltiples pods, o filtrar por patrones específicos.
- Usa search_errors_in_loki cuando: sospeches de errores recurrentes, necesites ver el historial de fallos, o get_pod_logs no muestre suficiente información.

CUÁNDO USAR PROMETHEUS (métricas):
- Usa analyze_pod_health cuando: un pod esté en CrashLoopBackOff o tenga problemas de rendimiento. Te dará un análisis completo de CPU, memoria, restarts y estado.
- Usa get_pod_metrics cuando: necesites ver métricas específicas de un pod (CPU, memoria, restarts).
- Usa get_high_resource_pods cuando: sospeches que hay problemas de recursos en el cluster y quieras encontrar los pods más pesados.
- Usa query_prometheus cuando: necesites hacer una consulta PromQL específica que no cubran las otras herramientas.

EJEMPLOS DE DIAGNÓSTICO CON MÉTRICAS:
- Pod con muchos restarts → usar analyze_pod_health para ver si es por OOM (memoria) o CPU.
- OOMKilled → verificar con get_pod_metrics si el uso de memoria está cerca del límite.
- Pod lento → usar get_high_resource_pods para detectar si hay saturación de CPU.
- CrashLoopBackOff → combinar analyze_pod_health + search_errors_in_loki para ver correlación entre métricas y errores.

EJEMPLOS DE DIAGNÓSTICO RÁPIDO:
- ImagePullBackOff → la imagen no existe o está mal tageada → helm_upgrade con la imagen correcta.
- CrashLoopBackOff → revisar logs → corregir config/secreto/RBAC según el error.
- OOMKilled → aumentar limits de memoria vía helm_upgrade.

CONTEXTO DEL CLUSTER:
- srv01: 192.168.1.100 (control plane)
- srv02: 192.168.1.101 (worker)
- Wazuh agents en ambos nodos
- Stack: kube-prometheus-stack + loki-stack en namespace 'monitoring'
- Chart de Grafana: prometheus-community/kube-prometheus-stack (release: prometheus)
"""


class ReActAgent:
    def __init__(self, cfg: dict, k8s_collector, log_callback: Callable = None):
        self.client = OpenAI(
            api_key=cfg['kimi']['api_key'],
            base_url=cfg['kimi']['base_url']
        )
        self.model = cfg['kimi']['model']
        self.k8s = k8s_collector
        self.dry_run = cfg['agent'].get('dry_run', False)
        self.max_iterations = cfg['agent'].get('max_iterations', 5)
        self.log = log_callback or print
        self.history: list[dict] = []

    def solve(self, issue_description: str) -> dict:
        """
        Punto de entrada principal.
        Recibe la descripción del problema y ejecuta el loop ReAct.
        Retorna un dict con resolved, summary, steps.
        """
        self.history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"PROBLEMA DETECTADO:\n{issue_description}\n\nInicia el diagnóstico."}
        ]

        steps = []
        for i in range(self.max_iterations):
            self.log(f"\n[ITERACIÓN {i+1}/{self.max_iterations}]")

            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.history,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.1
            )

            msg = response.choices[0].message

            tool_calls = msg.tool_calls

            # Fallback: si el modelo devolvió el tool call como texto plano
            if not tool_calls and msg.content:
                parsed = _parse_tool_call_from_text(msg.content)
                if parsed:
                    self.log(f"🔄 Fallback: tool call detectado en texto, parseando...")
                    tool_calls = parsed

            # Si el modelo quiere pensar/hablar antes de actuar
            if msg.content:
                self.log(f"🤔 RAZONAMIENTO: {msg.content}")

            # Sin tool calls = el modelo terminó sin llamar finish()
            if not tool_calls:
                self.log("⚠️  El modelo terminó sin llamar finish()")
                return {"resolved": False, "summary": msg.content or "Sin respuesta", "steps": steps}

            # Agregar el mensaje al historial (con tool_calls reales o sintéticos)
            if msg.tool_calls:
                self.history.append(msg)
            else:
                # Para tool calls sintéticos, agregar como assistant message
                self.history.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        }
                        for tc in tool_calls
                    ]
                })

            # Procesar cada tool call
            for tc in tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

                self.log(f"🔧 ACCIÓN: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")

                # Ejecutar la herramienta
                result = self._execute_tool(fn_name, fn_args)
                steps.append({"action": fn_name, "args": fn_args, "result": result[:500]})

                # Si es finish, terminamos
                if fn_name == "finish":
                    self.log(f"\n{'✅' if fn_args['resolved'] else '❌'} RESULTADO: {fn_args['summary']}")
                    return {
                        "resolved": fn_args["resolved"],
                        "summary": fn_args["summary"],
                        "steps": steps
                    }

                # Agregar resultado al historial
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:3000]  # limitar para no explotar el context window
                })

                self.log(f"📋 RESULTADO: {result[:300]}{'...' if len(result) > 300 else ''}")

        return {
            "resolved": False,
            "summary": f"Máximo de iteraciones ({self.max_iterations}) alcanzado",
            "steps": steps
        }

    def _execute_tool(self, name: str, args: dict) -> str:
        """Despacha la herramienta correcta."""
        dry = self.dry_run
        k = self.k8s

        try:
            match name:
                case "get_pod_logs":
                    return k.get_pod_logs(
                        args["namespace"], args["pod"], args["container"],
                        args.get("previous", True)
                    )
                case "describe_pod":
                    return k.describe_pod(args["namespace"], args["pod"])
                case "get_events":
                    return k.get_events(args["namespace"], args.get("resource"))
                case "check_rbac":
                    return k.get_rbac_for_sa(args["namespace"], args["serviceaccount"])
                case "helm_upgrade":
                    return k.helm_upgrade(
                        args["release"], args["chart"],
                        args["namespace"], args["set_values"], dry_run=dry
                    )
                case "kubectl_apply":
                    return k.kubectl_apply(args["manifest_yaml"], dry_run=dry)
                case "rollout_restart":
                    return k.rollout_restart(args["namespace"], args["resource"], dry_run=dry)
                case "query_loki":
                    return k.query_loki(
                        args["namespace"],
                        args.get("pod"),
                        args.get("query"),
                        args.get("limit", 100),
                        args.get("since", "1h")
                    )
                case "search_errors_in_loki":
                    return k.search_errors_in_loki(
                        args["namespace"],
                        args.get("pod"),
                        args.get("since", "24h")
                    )
                case "query_prometheus":
                    return k.query_prometheus(
                        args["query"],
                        args.get("time_range", "5m")
                    )
                case "get_pod_metrics":
                    return k.get_pod_metrics(
                        args["namespace"],
                        args["pod"]
                    )
                case "get_high_resource_pods":
                    return k.get_high_resource_pods(
                        args.get("namespace"),
                        args.get("threshold", 0.8)
                    )
                case "analyze_pod_health":
                    return k.analyze_pod_health(
                        args["namespace"],
                        args["pod"]
                    )
                case "finish":
                    return "OK"
                case _:
                    return f"Herramienta desconocida: {name}"
        except Exception as e:
            return f"ERROR ejecutando {name}: {e}"
