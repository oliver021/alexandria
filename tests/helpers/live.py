from __future__ import annotations

import asyncio
import atexit
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import configure_sqlite_engine
from app.models.user import UserRole
from app.repositories import user_repo

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"
HEALTH_PATH = "/api/v1/health"
STARTUP_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 20.0
KILL_TIMEOUT = 10.0
COMMAND_TIMEOUT = 120.0
POLL_INTERVAL = 0.05
HEALTH_TIMEOUT = 2.0
START_ATTEMPTS = 3
ADDRESS_IN_USE = "address already in use"
LOG_TAIL_LINES = 60
PR_SET_PDEATHSIG = 1
PARENT_DEATH_BOOTSTRAP = (
    "import ctypes, os, signal, sys\n"
    "try:\n"
    f"    ctypes.CDLL(None).prctl({PR_SET_PDEATHSIG}, signal.SIGKILL)\n"
    "except (AttributeError, OSError):\n"
    "    pass\n"
    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
)

SETTING_VARIABLES = (
    "JWT_SECRET",
    "DATABASE_URL",
    "LOGGING_LEVEL",
    "PUBLIC_URL",
    "STORAGE_PATH",
    "PUBLIC_READ",
    "AUTO_PUBLISH",
    "ALEXANDRIA_ENV",
    "CORS_ORIGINS",
)
CLIENT_VARIABLES = ("ALX_URL", "ALX_TOKEN", "ALX_DEBUG")
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def wait_until(
    condition: Callable[[], bool], timeout: float, interval: float = POLL_INTERVAL
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def clean_environment() -> dict[str, str]:
    excluded = {*SETTING_VARIABLES, *CLIENT_VARIABLES, *PROXY_VARIABLES}
    environment = {key: value for key, value in os.environ.items() if key not in excluded}
    environment["NO_PROXY"] = environment["no_proxy"] = f"{LOOPBACK},localhost"
    environment["PYTHONUNBUFFERED"] = "1"
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(ROOT) if not inherited else f"{ROOT}{os.pathsep}{inherited}"
    return environment


def signal_group(pid: int, number: int) -> None:
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_group(process: subprocess.Popen[Any], grace: float = SHUTDOWN_TIMEOUT) -> int | None:
    signal_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        signal_group(process.pid, signal.SIGKILL)
        process.wait(timeout=KILL_TIMEOUT)
    signal_group(process.pid, signal.SIGKILL)
    return process.returncode


def spawn_group(
    argv: list[str], environment: dict[str, str], log_path: Path, cwd: Path = ROOT
) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as log:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def run_command(
    argv: list[str], environment: dict[str, str], cwd: Path, timeout: float = COMMAND_TIMEOUT
) -> CommandResult:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def run_checked(argv: list[str], environment: dict[str, str], cwd: Path) -> CommandResult:
    result = run_command(argv, environment, cwd)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed with {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result


def create_user(database_path: Path, username: str, password: str, role: UserRole) -> None:
    async def create() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        configure_sqlite_engine(engine)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                await user_repo.create_user(
                    db,
                    username=username,
                    email=f"{username}@live.test",
                    password=password,
                    role=role,
                )
                await db.commit()
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, create()).result()


@dataclass
class LiveServer:
    process: subprocess.Popen[bytes]
    port: int
    workdir: Path
    log_path: Path
    database_path: Path
    storage_path: Path
    admin_username: str
    admin_password: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    stopped: bool = False

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    @property
    def api(self) -> str:
        return f"{self.url}/api/v1"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"

    def running(self) -> bool:
        return self.process.poll() is None

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def log_tail(self) -> str:
        return "\n".join(self.log().splitlines()[-LOG_TAIL_LINES:])

    def stop(self, grace: float = SHUTDOWN_TIMEOUT) -> int | None:
        if self.stopped:
            return self.process.returncode
        self.stopped = True
        return terminate_group(self.process, grace)

    def http(self, token: str | None = None, **options: Any) -> httpx.Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(
            base_url=self.url, headers=headers, timeout=30.0, trust_env=False, **options
        )

    def alx(
        self, *argv: object, token: str | None = None, cwd: Path | None = None
    ) -> CommandResult:
        environment = clean_environment()
        environment["ALX_URL"] = self.url
        if token is not None:
            environment["ALX_TOKEN"] = token
        command = [sys.executable, "-m", "app.cli", *(str(item) for item in argv)]
        return run_command(command, environment, cwd or self.workdir)


def is_healthy(client: httpx.Client) -> bool:
    try:
        return client.get(HEALTH_PATH).status_code == 200
    except httpx.TransportError:
        return False


def wait_for_health(server: LiveServer, timeout: float = STARTUP_TIMEOUT) -> bool:
    healthy = False

    def probe() -> bool:
        nonlocal healthy
        if not server.running():
            return True
        healthy = is_healthy(client)
        return healthy

    with httpx.Client(base_url=server.url, timeout=HEALTH_TIMEOUT, trust_env=False) as client:
        wait_until(probe, timeout)
    return healthy


def uvicorn_command(port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        PARENT_DEATH_BOOTSTRAP,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        LOOPBACK,
        "--port",
        str(port),
        "--lifespan",
        "on",
        "--log-level",
        "info",
    ]


def start_server(
    workdir: Path,
    *,
    admin_username: str = "admin",
    extra_environment: dict[str, str] | None = None,
) -> LiveServer:
    workdir.mkdir(parents=True, exist_ok=True)
    database_path = workdir / "alexandria.db"
    storage_path = workdir / "blobs"
    jwt_secret = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(18)
    environment = clean_environment()
    environment.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
            "STORAGE_PATH": str(storage_path),
            "JWT_SECRET": jwt_secret,
            "ALEXANDRIA_ENV": "development",
            "LOGGING_LEVEL": "INFO",
            "PUBLIC_READ": "true",
            "AUTO_PUBLISH": "false",
            "CORS_ORIGINS": f"http://{LOOPBACK}",
            **(extra_environment or {}),
        }
    )
    run_checked([sys.executable, "-m", "alembic", "upgrade", "head"], environment, ROOT)
    create_user(database_path, admin_username, admin_password, UserRole.ADMIN)
    log_path = workdir / "server.log"
    for _ in range(START_ATTEMPTS):
        port = free_port()
        launch_environment = {**environment, "PUBLIC_URL": f"http://{LOOPBACK}:{port}"}
        process = spawn_group(uvicorn_command(port), launch_environment, log_path)
        server = LiveServer(
            process=process,
            port=port,
            workdir=workdir,
            log_path=log_path,
            database_path=database_path,
            storage_path=storage_path,
            admin_username=admin_username,
            admin_password=admin_password,
            jwt_secret=jwt_secret,
            environment=launch_environment,
        )
        atexit.register(server.stop)
        if wait_for_health(server):
            return server
        exited = not server.running()
        log = server.log()
        server.stop()
        if not (exited and ADDRESS_IN_USE in log.lower()):
            raise RuntimeError(
                f"the live server did not become healthy (exited: {exited})\n"
                f"--- server log ---\n{log}"
            )
        log_path.unlink()
    raise RuntimeError(f"no free port found after {START_ATTEMPTS} attempts")
