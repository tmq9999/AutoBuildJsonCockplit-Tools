"""Disposable PostgreSQL test databases. No production database targets accepted."""
import argparse
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import uuid4


def validate_test_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "postgresql+psycopg" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not re.fullmatch(r"/abgw_test_[a-f0-9]{32}", parsed.path)
            or parsed.query or parsed.fragment or parsed.port is None):
        raise ValueError("A dedicated loopback test database URL is required")
    return parsed


@contextmanager
def test_database(*, postgres_bin=None, url=None):
    import psycopg
    from psycopg import sql
    if url is not None:
        validate_test_url(url)
        admin_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        name = "abgw_test_" + uuid4().hex
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            try:
                yield url.rsplit("/", 1)[0] + "/" + name
            finally:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        return
    binary = Path(postgres_bin).resolve() if postgres_bin else None
    if not binary or not all((binary / name).is_file() for name in ("initdb", "pg_ctl")):
        raise ValueError("Provide --postgres-bin or a dedicated test database URL")
    with tempfile.TemporaryDirectory(prefix="abgw-pg-") as directory:
        root = Path(directory)
        data, password_file, log = root / "cluster", root / "password", root / "server.log"
        password = secrets.token_urlsafe(32)
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(password)
        subprocess.run([str(binary / "initdb"), "-D", str(data), "-U", "abgw_test",
                        "--auth=scram-sha-256", "--pwfile", str(password_file), "--no-locale", "-E", "UTF8"],
                       check=True, capture_output=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        started = False
        try:
            subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-l", str(log),
                            "-o", f"-h 127.0.0.1 -p {port} -k {root}", "-w", "-t", "15", "start"],
                           check=True, capture_output=True)
            started = True
            name = "abgw_test_" + uuid4().hex
            admin_url = f"postgresql://abgw_test:{password}@127.0.0.1:{port}/postgres"
            with psycopg.connect(admin_url, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            yield f"postgresql+psycopg://abgw_test:{password}@127.0.0.1:{port}/{name}"
        finally:
            if started:
                subprocess.run([str(binary / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                               check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin")
    parser.add_argument("--url-env")
    parser.add_argument("--acknowledge-test-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.acknowledge_test_only:
        parser.error("--acknowledge-test-only is required")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A test command is required")
    try:
        source_url = os.environ.get(args.url_env) if args.url_env else None
        if args.url_env and not source_url:
            raise ValueError("Missing test database environment variable")
        with test_database(postgres_bin=args.postgres_bin, url=source_url) as url:
            environment = dict(os.environ, AUTOBUILD_TEST_DATABASE_URL=url)
            return subprocess.run(command, env=environment).returncode
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Test database setup failed. Check dedicated URL/binaries; details may contain credentials.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
