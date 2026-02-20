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


def main():
    parser = argparse.ArgumentParser(description="K8s SRE/SecOps Agent")
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--once",     action="store_true", help="Un ciclo y sale")
    parser.add_argument("--fix",      type=str, help="Descripción del problema a resolver directamente")
    parser.add_argument("--dry-run",  action="store_true", help="Simula sin ejecutar cambios")
    parser.add_argument("--auto",     action="store_true", help="Auto-remediate sin confirmación")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("main")

    cfg = load_config(args.config)

    # Overrides desde CLI
    if args.dry_run:
        cfg['agent']['dry_run'] = True
    if args.auto:
        cfg['agent']['auto_remediate'] = True

    # Inicializar componentes
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
