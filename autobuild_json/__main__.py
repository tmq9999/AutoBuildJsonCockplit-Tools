import argparse
import sys

import uvicorn

from .api import create_app
from .errors import FlowError
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Local HTTP OAuth workbench")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.port is not None:
            if not 1 <= args.port <= 65535:
                raise FlowError("CONFIGURATION_ERROR")
            settings.port = args.port
        if settings.host not in {"127.0.0.1", "localhost"}:
            raise FlowError("CONFIGURATION_ERROR")
        app = create_app(settings)
    except Exception:
        print("Configuration failed. Check .env and private data directory.", file=sys.stderr)
        return 1
    print(f"Local API: http://127.0.0.1:{settings.port}")
    print(f"Admin token is stored privately in {settings.data_dir / 'local-config.json'}; never share it.")
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
