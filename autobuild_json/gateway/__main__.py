import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import signal


def initialize_keyring(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    data = {"active": "v1", "keys": {name: base64.b64encode(secrets.token_bytes(32)).decode()
                                    for name in ("v1", "client_keys")}}
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())


def build_services(settings):
    from .admin.services import AdminServices
    from .storage.db import make_database
    from .secrets import Vault
    from .transport.egress import EgressPolicy
    vault = Vault.from_file(settings.master_key_file)
    return AdminServices(make_database(settings.database_url), vault,
        vault.derive_key("client_keys", "client-key-hmac"),
        service_settings=settings,
        egress=EgressPolicy(private_origins=settings.private_origins, allowed_networks=settings.allowed_networks,
                            trusted_proxy_origins=settings.trusted_egress_proxies))


async def run_maintenance(services, stopped):
    from .maintenance import Maintenance
    loop = asyncio.get_running_loop()
    handlers = []
    def stop():
        stopped.set()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
            handlers.append(sig)
    except (NotImplementedError, RuntimeError):
        handlers = []
    try:
        await Maintenance(services.db, worker=services.build_catalog_worker()).run(stopped)
    finally:
        for sig in handlers:
            loop.remove_signal_handler(sig)
        await services.close()


def main():
    parser = argparse.ArgumentParser(description="Optional multi-protocol gateway; private administration is separate")
    sub = parser.add_subparsers(dest="action", required=True)
    initialize = sub.add_parser("init-keyring")
    initialize.add_argument("--key-file", type=Path, required=True)
    sub.add_parser("migrate")
    sub.add_parser("serve")
    admin = sub.add_parser("admin")
    admin.add_argument("--port", type=int, default=8787)
    sub.add_parser("maintenance")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--acknowledge-empty-target", action="store_true", required=True)
    args = parser.parse_args()
    if args.action == "init-keyring":
        try:
            initialize_keyring(args.key_file)
            print("Private keyring created. Back it up separately; never share its contents.")
            return 0
        except OSError:
            print("Keyring creation refused. Check path/permissions; existing files are never overwritten.")
            return 2
    try:
        import uvicorn
        from .settings import ServiceSettings
        settings = ServiceSettings(enabled=True)
        services = build_services(settings)
        if args.action == "migrate":
            from .storage.migrate import upgrade
            async def migrate():
                try:
                    await upgrade(services.db)
                finally:
                    await services.close()
            asyncio.run(migrate())
            print("Gateway database migrations applied.")
        elif args.action in {"backup", "restore"}:
            from .storage.backup import BackupService
            async def snapshot():
                try:
                    if args.action == "backup":
                        await BackupService(services.db, services.vault).export(args.output)
                    else:
                        await BackupService(services.db, services.vault).restore(args.input)
                finally:
                    await services.close()
            asyncio.run(snapshot())
            print("Gateway snapshot operation completed.")
        elif args.action == "maintenance":
            asyncio.run(run_maintenance(services, asyncio.Event()))
        elif args.action == "admin":
            from ..api import create_app
            from ..settings import Settings
            app = create_app(Settings(port=args.port), gateway_services=services)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
        else:
            from .http.app import create_gateway_app
            app = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=settings.allowed_hosts)
            uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_level="warning",
                        timeout_graceful_shutdown=settings.request_timeout+15, proxy_headers=bool(settings.trusted_proxy_ips),
                        forwarded_allow_ips=",".join(settings.trusted_proxy_ips))
        return 0
    except Exception:
        print("Gateway configuration or startup failed. Check private database/keyring settings; credentials are not printed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
