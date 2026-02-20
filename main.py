"""
main.py
Entry point del agente SRE/SecOps.

Modos de uso:
  python main.py                          # Monitor continuo
  python main.py --once                   # Un ciclo y sale
  python main.py --fix "descripción"      # Remediación directa (sin monitor)
  python main.py --dry-run                # Simula sin ejecutar nada
"""
import argparse
import logging
import sys
import yaml

from collectors.k8s import K8sCollector
from engine.react import ReActAgent
from engine.monitor import ClusterMonitor


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def select_llm_provider(cfg: dict, cli_llm: str = None) -> dict:
    """Selecciona el proveedor de LLM y retorna su configuración."""
    providers = cfg.get('llm', {})
    available = list(providers.keys())

    if not available:
        print("ERROR: No hay proveedores LLM configurados en config.yaml")
        sys.exit(1)

    # Si se especificó por CLI, usar ese
    if cli_llm:
        if cli_llm not in providers:
            print(f"ERROR: Proveedor '{cli_llm}' no encontrado. Disponibles: {available}")
            sys.exit(1)
        return providers[cli_llm], cli_llm

    # Si solo hay uno, usarlo directo
    if len(available) == 1:
        name = available[0]
        return providers[name], name

    # Preguntar al usuario
    print("\n┌─────────────────────────────────┐")
    print("│   Seleccionar proveedor LLM     │")
    print("├─────────────────────────────────┤")
    for i, name in enumerate(available, 1):
        model = providers[name].get('model', '?')
        print(f"│  {i}) {name:<10} ({model})")
    print("└─────────────────────────────────┘")

    while True:
        try:
            choice = input(f"\nElegir [{1}-{len(available)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                name = available[idx]
                return providers[name], name
            print(f"Opción inválida. Elegir entre 1 y {len(available)}")
        except (ValueError, EOFError):
            print(f"Opción inválida. Elegir entre 1 y {len(available)}")


def main():
    parser = argparse.ArgumentParser(description="K8s SRE/SecOps Agent")
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--once",     action="store_true", help="Un ciclo y sale")
    parser.add_argument("--fix",      type=str, help="Descripción del problema a resolver directamente")
    parser.add_argument("--dry-run",  action="store_true", help="Simula sin ejecutar cambios")
    parser.add_argument("--auto",     action="store_true", help="Auto-remediate sin confirmación")
    parser.add_argument("--llm",      type=str, help="Proveedor LLM (ej: ollama, kimi)")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("main")

    cfg = load_config(args.config)

    # Seleccionar proveedor LLM
    llm_cfg, llm_name = select_llm_provider(cfg, args.llm)
    cfg['kimi'] = llm_cfg  # ReActAgent espera cfg['kimi']
    log.info(f"LLM provider: {llm_name} | model: {llm_cfg['model']}")

    # Overrides desde CLI
    if args.dry_run:
        cfg['agent']['dry_run'] = True
    if args.auto:
        cfg['agent']['auto_remediate'] = True

    # Inicializar componentes
    # Pasar configuración de Loki a kubernetes si está habilitado
    if cfg.get('loki', {}).get('enabled', False):
        cfg['kubernetes']['loki_url'] = cfg['loki']['url']
        log.info(f"Loki integration enabled: {cfg['loki']['url']}")

    # Pasar configuración de Prometheus a kubernetes si está habilitado
    if cfg.get('prometheus', {}).get('enabled', False):
        cfg['kubernetes']['prometheus_url'] = cfg['prometheus']['url']
        log.info(f"Prometheus integration enabled: {cfg['prometheus']['url']}")

    k8s   = K8sCollector(cfg['kubernetes'])
    agent = ReActAgent(cfg, k8s, log_callback=log.info)

    if args.fix:
        # Modo directo: resolver problema específico
        log.info(f"Modo fix directo: {args.fix}")
        result = agent.solve(args.fix)
        log.info(f"Resultado: {result}")
        sys.exit(0 if result["resolved"] else 1)

    # Monitor continuo (o un ciclo con --once)
    monitor = ClusterMonitor(k8s, agent, cfg)

    if args.once:
        monitor._cycle()
    else:
        monitor.run()


if __name__ == "__main__":
    main()
