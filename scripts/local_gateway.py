"""Run a loopback-only gateway with persistent, private development data.

No synthetic upstream or credentials are created. PostgreSQL binaries must be
provided by the operator; database/keyring survive a process or machine restart.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import fcntl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def private_file(path, create):
    if path.is_symlink():
        raise ValueError("symlink_refused")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            os.close(descriptor)
            raise ValueError("private_permissions_required")
        try:
            return os.read(descriptor, 1024 * 1024).decode()
        finally:
            os.close(descriptor)
    value = create()
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def require_private_regular(path):
    """Reject links/devices/world-readable files before handing them to a child."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("private_permissions_required")
    finally:
        os.close(descriptor)


def command(args):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, timeout=60)


async def import_records(path, environment):
    from autobuild_json.gateway.__main__ import build_services
    from autobuild_json.gateway.settings import ServiceSettings
    from autobuild_json.gateway.errors import GatewayError
    services = build_services(ServiceSettings(enabled=True,
        database_url=environment["AUTOBUILD_GATEWAY_DATABASE_URL"],
        master_key_file=environment["AUTOBUILD_GATEWAY_MASTER_KEY_FILE"]))
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("import_too_large")
        records = json.loads(path.read_text())
        if not isinstance(records, list) or not 1 <= len(records) <= 1000:
            raise ValueError("invalid_batch")
        imported = duplicate = failed = 0
        for record in records:
            try:
                _, created = await services.engine.credentials.import_record_result(record, None)
                imported += int(created)
                duplicate += int(not created)
            except GatewayError:
                failed += 1
        print(f"OAuth import: {imported} created, {duplicate} duplicate, {failed} invalid. No upstream calls.", flush=True)
    finally:
        await services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    parser.add_argument("--import-json", type=Path, help="Explicit local OAuth export to import; never prints tokens")
    args = parser.parse_args()
    binary = args.postgres_bin.resolve()
    for name in ("initdb", "pg_ctl"):
        if not (binary / name).is_file():
            parser.error("PostgreSQL initdb and pg_ctl are required")
    root = ROOT / "data" / "local-gateway"
    if root.is_symlink():
        parser.error("Private data path must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        parser.error("Private data directory must have mode 0700")
    lock_path = root / "runner.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(lock_descriptor)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_mode & 0o077:
        os.close(lock_descriptor)
        parser.error("Private runner lock must be a regular 0600 file")
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_descriptor)
        parser.error("Another local gateway runner is already using this data directory")
    for port in (55433, 8787, 8788):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                parser.error(f"Local port {port} already in use; no existing process was changed")
    cluster, password_file = root / "postgres", root / "db-password"
    password = private_file(password_file, lambda: secrets.token_urlsafe(32))
    if not (cluster / "PG_VERSION").is_file():
        command([binary / "initdb", "-D", cluster, "-U", "autobuild_local", "--auth=scram-sha-256",
                 "--pwfile", password_file, "--no-locale", "-E", "UTF8"])
    children = []
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    # Disable Unix sockets (project paths can exceed sockaddr_un); TCP is
    # loopback-only and requires the random SCRAM password.
    pg_started = False
    try:
        command([binary / "pg_ctl", "-D", cluster, "-l", root / "postgres.log", "-w", "-t", "20",
                 "-o", "-h 127.0.0.1 -p 55433 -k ''", "start"])
        pg_started = True
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=55433, user="autobuild_local", password=password,
                             dbname="postgres", autocommit=True) as connection:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname='autobuild_local'").fetchone():
                connection.execute("CREATE DATABASE autobuild_local")
        keyring = root / "keyring.json"
        if not keyring.exists():
            from autobuild_json.gateway.__main__ import initialize_keyring
            initialize_keyring(keyring)
        require_private_regular(keyring)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("AUTOBUILD_")}
        environment.update(
            AUTOBUILD_GATEWAY_DATABASE_URL=f"postgresql+psycopg://autobuild_local:{password}@127.0.0.1:55433/autobuild_local",
            AUTOBUILD_GATEWAY_MASTER_KEY_FILE=str(keyring), AUTOBUILD_GATEWAY_HOST="127.0.0.1",
            AUTOBUILD_GATEWAY_PORT="8788", AUTOBUILD_GATEWAY_ALLOWED_HOSTS='["127.0.0.1:8788","localhost:8788"]',
            AUTOBUILD_HOST="127.0.0.1", AUTOBUILD_PORT="8787", AUTOBUILD_DATA_DIR=str(root / "admin-data"))
        command_args = [sys.executable, "-m", "autobuild_json.gateway"]
        subprocess.run(command_args + ["migrate"], env=environment, cwd=ROOT, check=True, timeout=60)
        if args.import_json:
            asyncio.run(import_records(args.import_json.resolve(), environment))
        if stopped:
            return 0
        for action in ("serve", "admin"):
            children.append(subprocess.Popen(command_args + [action], env=environment, cwd=ROOT))
        print("Starting admin http://127.0.0.1:8787/service/ and gateway http://127.0.0.1:8788", flush=True)
        print(f"Admin token: private file {root / 'admin-data' / 'local-config.json'}", flush=True)
        # A child failure shuts down both listeners. No credential-bearing
        # command line/environment is printed on errors.
        while not stopped and all(child.poll() is None for child in children):
            try:
                children[0].wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        return 0 if stopped else 2
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if pg_started:
            command([binary / "pg_ctl", "-D", cluster, "-m", "fast", "-w", "-t", "20", "stop"])
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Local gateway startup failed. Check private logs/config; secrets are not printed.", file=sys.stderr)
        raise SystemExit(2)
