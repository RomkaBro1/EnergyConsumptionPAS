"""One entry point for the local website and scheduled/manual data processing."""
import argparse

from gridpulse.config import load_config


def main():
    parser = argparse.ArgumentParser(description="GridPulse DE — локальная прогнозно-аналитическая система")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "prepare", "refresh"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "serve":
        from gridpulse.service import create_app
        app = create_app(cfg, initialize=True)
        app.run(host=cfg["host"], port=cfg["port"], debug=False, threaded=True, use_reloader=False)
    else:
        from gridpulse.pipeline import run_pipeline
        from gridpulse.storage import Store
        print(run_pipeline(Store(cfg["data_dir"]), cfg, online=args.command == "refresh", progress=lambda msg: print(msg, flush=True)))


if __name__ == "__main__":
    main()
