"""通过可注入命令传输执行只读 Linux/NVIDIA 主机发现。"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    from scripts._common import (
        ROOT,
        SUPPORTED_CREDENTIAL_ENVIRONMENT_NAMES,
        HarnessError,
        atomic_write_json,
        validate_instance,
    )
    from scripts.deployment_archive import DeploymentArchive
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from _common import (  # type: ignore[no-redef]
        ROOT,
        SUPPORTED_CREDENTIAL_ENVIRONMENT_NAMES,
        HarnessError,
        atomic_write_json,
        validate_instance,
    )
    from deployment_archive import DeploymentArchive  # type: ignore[no-redef]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandTransport(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = 20,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...

    def close(self) -> None: ...


def _dotenv(path: Path | None) -> dict[str, str]:
    """读取小型未跟踪 dotenv 文件，不修改进程环境。"""
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def secret_environment(
    environ: Mapping[str, str] | None = None, dotenv_path: Path | None = None
) -> dict[str, str]:
    """返回支持的密钥，优先使用进程环境而非 .env。"""
    env = dict(environ if environ is not None else os.environ)
    file_values = _dotenv(dotenv_path or Path.cwd() / ".env")
    return {
        name: env.get(name, file_values.get(name, ""))
        for name in SUPPORTED_CREDENTIAL_ENVIRONMENT_NAMES
        if env.get(name, file_values.get(name, ""))
    }


class ParamikoTransport:
    """Paramiko 传输：优先公钥认证，失败后再回退密码认证。"""

    def __init__(self, client: object):
        self._client = client

    @classmethod
    def connect(
        cls,
        host: str,
        *,
        username: str,
        port: int = 22,
        connect_timeout: int = 10,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
        client_factory: object | None = None,
    ) -> ParamikoTransport:
        # 延迟导入，使解析器和测试无需依赖具备网络能力的组件。
        import paramiko

        factory = client_factory or paramiko.SSHClient
        client = factory()  # type: ignore[operator]
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        secrets = secret_environment(environ, dotenv_path)
        key_filename = secrets.get("DEPLOY_SSH_KEY_PATH")
        base = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": connect_timeout,
            "banner_timeout": connect_timeout,
            "auth_timeout": connect_timeout,
        }
        try:
            client.connect(
                **base,
                key_filename=key_filename,
                passphrase=secrets.get("DEPLOY_SSH_KEY_PASSPHRASE"),
                allow_agent=True,
                look_for_keys=True,
                password=None,
            )
        except paramiko.AuthenticationException:
            password = secrets.get("DEPLOY_SSH_PASSWORD")
            if not password:
                client.close()
                raise
            # 密码绝不进入 argv、诊断信息或返回制品。
            try:
                client.connect(
                    **base,
                    key_filename=None,
                    allow_agent=False,
                    look_for_keys=False,
                    password=password,
                )
            except Exception:
                client.close()
                raise
        return cls(client)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = 20,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if not argv or any("\x00" in part for part in argv):
            raise ValueError("argv 必须非空且不含 NUL 字符")
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("SSH 传输不可用")
        channel = transport.open_session(timeout=timeout)
        channel.settimeout(timeout)
        for name, value in (env or {}).items():
            # Paramiko 在同步拒绝时抛出异常。一些 SSH 服务器会静默忽略环境请求；
            # 此时计划中的命令必须自行失败即阻断。
            channel.set_environment_variable(name, value)
        command = "exec " + shlex.join(tuple(argv))
        if cwd is not None:
            command = f"cd -- {shlex.quote(cwd)} && {command}"
        channel.exec_command(command)
        stdout = channel.makefile("rb", -1).read().decode("utf-8", "replace")
        stderr = channel.makefile_stderr("rb", -1).read().decode("utf-8", "replace")
        return CommandResult(tuple(argv), channel.recv_exit_status(), stdout, stderr)

    def close(self) -> None:
        self._client.close()

    def upload_new(self, source: Path, destination: str, *, timeout: int = 300) -> None:
        """Upload only to a previously absent target, without replacing remote files."""
        if not source.is_file():
            raise FileNotFoundError(source)
        sftp = self._client.open_sftp()
        try:
            with source.open("rb") as local, sftp.open(destination, "x+b") as remote:
                while chunk := local.read(1024 * 1024):
                    remote.write(chunk)
                remote.flush()
        finally:
            sftp.close()


PROBE_COMMANDS: dict[str, tuple[str, ...]] = {
    "hostname": ("hostname",),
    "addresses": ("hostname", "-I"),
    "os": ("cat", "/etc/os-release"),
    "kernel": ("uname", "-r"),
    "architecture": ("uname", "-m"),
    "cpu": ("lscpu", "-J"),
    "memory": ("cat", "/proc/meminfo"),
    "gpus": (
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.free,driver_version",
        "--format=csv,noheader,nounits",
    ),
    "cuda": ("nvidia-smi",),
    "gpu_processes": (
        "nvidia-smi",
        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ),
    "topology": ("nvidia-smi", "topo", "-m"),
    "filesystems": ("df", "-B1", "--output=target,size,avail,fstype"),
    "mounts": ("findmnt", "-rn", "-o", "SOURCE,TARGET,OPTIONS"),
    "docker_version": ("docker", "--version"),
    "docker_runtimes": ("docker", "info", "--format", "{{json .Runtimes}}"),
    "python3": ("python3", "--version"),
    "python": ("python", "--version"),
    "uv": ("uv", "--version"),
    "conda": ("conda", "--version"),
    "ports": ("ss", "-lntpH"),
    "processes": ("ps", "-eo", "pid=,user=,comm=,args="),
    "connect_huggingface": (
        "curl",
        "--head",
        "--silent",
        "--show-error",
        "--max-time",
        "5",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        "https://huggingface.co",
    ),
    "connect_github": (
        "curl",
        "--head",
        "--silent",
        "--show-error",
        "--max-time",
        "5",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        "https://github.com",
    ),
}

OPTIONAL_COMMANDS = {
    "cuda",
    "gpus",
    "gpu_processes",
    "topology",
    "docker_version",
    "docker_runtimes",
    "python3",
    "python",
    "uv",
    "conda",
    "connect_huggingface",
    "connect_github",
}


def _kv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def _csv_rows(text: str) -> list[list[str]]:
    return [[item.strip() for item in row] for row in csv.reader(text.splitlines()) if row]


def _parse_cpu(text: str) -> tuple[str, int]:
    try:
        fields = {entry["field"].rstrip(":"): entry["data"] for entry in json.loads(text)["lscpu"]}
        return fields.get("Model name", "unknown"), int(fields.get("CPU(s)", "1"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "unknown", 1


def _parse_memory(text: str) -> tuple[int, int]:
    total = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    available = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    if available is None:
        available = re.search(r"^MemFree:\s+(\d+)\s+kB", text, re.MULTILINE)
    return (
        int(total.group(1)) * 1024 if total else 0,
        int(available.group(1)) * 1024 if available else 0,
    )


def _parse_topology(text: str, uuid_by_index: Mapping[int, str]) -> list[dict[str, object]]:
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if not rows or not rows[0][0].startswith("GPU"):
        return []
    headers = [h for h in rows[0] if re.fullmatch(r"GPU\d+", h)]
    result = []
    for row in rows[1:]:
        if not row or not re.fullmatch(r"GPU\d+", row[0]):
            continue
        a = int(row[0][3:])
        for offset, header in enumerate(headers):
            b = int(header[3:])
            if b <= a or offset + 1 >= len(row):
                continue
            result.append(
                {
                    "gpu_a": uuid_by_index.get(a, a),
                    "gpu_b": uuid_by_index.get(b, b),
                    "link": row[offset + 1],
                }
            )
    return result


def _parse_ports(text: str) -> tuple[list[int], dict[int, list[int]]]:
    ports: set[int] = set()
    by_pid: dict[int, list[int]] = {}
    for line in text.splitlines():
        port_match = re.search(r"(?:\]|:)(\d+)\s", line + " ")
        if not port_match:
            continue
        port = int(port_match.group(1))
        ports.add(port)
        for pid in re.findall(r"pid=(\d+)", line):
            by_pid.setdefault(int(pid), []).append(port)
    return sorted(ports), by_pid


def _parse_model_services(
    text: str, ports_by_pid: Mapping[int, list[int]]
) -> list[dict[str, object]]:
    markers = ("sglang", "vllm", "model_server", "openai.api_server")
    services = []
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 3 or not any(marker in line.lower() for marker in markers):
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        services.append(
            {"pid": pid, "process_name": parts[2], "ports": sorted(set(ports_by_pid.get(pid, [])))}
        )
    return services


def collect_host_profile(
    transport: CommandTransport,
    *,
    host_id: str,
    aliases: Sequence[str] = (),
    timeout: int = 20,
    transport_name: str = "SSH",
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """运行固定的只读探测集，并返回兼容 HostProfile 的字典。"""
    if not host_id:
        raise ValueError("必须提供 host_id；网络地址不是持久身份")
    results: dict[str, CommandResult] = {}
    errors: list[str] = []
    for name, argv in PROBE_COMMANDS.items():
        try:
            result = transport.run(argv, timeout=timeout)
        except Exception as exc:  # 传输错误也是数据；绝不包含可能带密钥的 repr
            result = CommandResult(argv, 255, "", exc.__class__.__name__)
        results[name] = result
        if result.returncode and name not in OPTIONAL_COMMANDS:
            errors.append(f"{name}: command failed with exit status {result.returncode}")

    os_release = _kv(results["os"].stdout)
    cpu_model, logical_cores = _parse_cpu(results["cpu"].stdout)
    gpu_rows = _csv_rows(results["gpus"].stdout) if results["gpus"].returncode == 0 else []
    gpus = []
    uuid_by_index: dict[int, str] = {}
    driver_version: str | None = None
    for row in gpu_rows:
        if len(row) < 6:
            continue
        try:
            index = int(row[0])
            uuid_by_index[index] = row[1]
            driver_version = driver_version or row[5]
            gpus.append(
                {
                    "index": index,
                    "uuid": row[1],
                    "model": row[2],
                    "memory_total_bytes": int(float(row[3]) * 1024 * 1024),
                    "memory_free_bytes": int(float(row[4]) * 1024 * 1024),
                }
            )
        except ValueError:
            continue
    cuda_match = re.search(r"CUDA Version:\s*([\d.]+)", results["cuda"].stdout)
    gpu_processes = []
    for row in _csv_rows(results["gpu_processes"].stdout):
        if len(row) < 4:
            continue
        try:
            gpu_processes.append(
                {
                    "gpu_id": row[0],
                    "pid": int(row[1]),
                    "process_name": row[2],
                    "memory_bytes": int(float(row[3]) * 1024 * 1024),
                }
            )
        except ValueError:
            continue
    filesystems = []
    for line in results["filesystems"].stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("/"):
            with contextlib.suppress(ValueError):
                filesystems.append(
                    {
                        "path": parts[0],
                        "total_bytes": int(parts[1]),
                        "available_bytes": int(parts[2]),
                        "filesystem_type": parts[3],
                    }
                )
    mounts = []
    for line in results["mounts"].stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[1].startswith("/"):
            mounts.append(
                {
                    "source": parts[0],
                    "target": parts[1],
                    "options": parts[2].split(",") if len(parts) == 3 else [],
                }
            )
    python = []
    for executable in ("python3", "python"):
        result = results[executable]
        version = (result.stdout or result.stderr).strip()
        if result.returncode == 0 and version:
            python.append({"executable": executable, "version": version})
    ports, ports_by_pid = _parse_ports(results["ports"].stdout)
    docker_output = results["docker_version"].stdout.strip()
    runtimes = results["docker_runtimes"].stdout.lower()
    connectivity = []
    for key, destination in (
        ("connect_huggingface", "https://huggingface.co"),
        ("connect_github", "https://github.com"),
    ):
        result = results[key]
        reachable = (
            result.returncode == 0
            and result.stdout.strip().isdigit()
            and result.stdout.strip() != "000"
        )
        connectivity.append(
            {
                "destination": destination,
                "reachable": reachable,
                "detail": f"HTTP {result.stdout.strip()}"
                if reachable
                else "连通性检查失败",
            }
        )
    observed = observed_at or datetime.now(timezone.utc)
    hostname = results["hostname"].stdout.strip() or "unknown"
    addresses = list(dict.fromkeys(results["addresses"].stdout.split())) or [hostname]
    status = "COMPLETE" if not errors else ("PARTIAL" if hostname != "unknown" else "FAILED")
    memory_total, memory_available = _parse_memory(results["memory"].stdout)
    return {
        "schema_version": "1.0",
        "host_id": host_id,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "probe": {"status": status, "transport": transport_name, "errors": errors},
        "identity": {
            "hostname": hostname,
            "addresses": addresses,
            "aliases": list(dict.fromkeys(aliases)),
        },
        "hardware": {
            "cpu": {
                "model": cpu_model,
                "logical_cores": logical_cores,
                "architecture": results["architecture"].stdout.strip() or "unknown",
            },
            "memory_bytes": memory_total,
            "memory": {"total_bytes": memory_total, "available_bytes": memory_available},
            "gpus": gpus,
            "gpu_topology": _parse_topology(results["topology"].stdout, uuid_by_index),
        },
        "software": {
            "os": {
                "name": os_release.get("PRETTY_NAME") or os_release.get("NAME", "unknown"),
                "version": os_release.get("VERSION_ID") or os_release.get("VERSION", "unknown"),
            },
            "kernel": results["kernel"].stdout.strip() or "unknown",
            "nvidia": {
                "driver_version": driver_version,
                "cuda_compatibility": cuda_match.group(1) if cuda_match else None,
            },
            "docker": {
                "installed": results["docker_version"].returncode == 0,
                "version": docker_output or None,
                "nvidia_runtime_available": "nvidia" in runtimes,
            },
            "python": python,
            "uv_version": (results["uv"].stdout or results["uv"].stderr).strip() or None,
            "conda_version": (results["conda"].stdout or results["conda"].stderr).strip() or None,
        },
        "storage": {"filesystems": filesystems, "mounts": mounts},
        "network": {"listening_ports": ports, "connectivity_checks": connectivity},
        "runtime": {
            "gpu_processes": gpu_processes,
            "model_services": _parse_model_services(results["processes"].stdout, ports_by_pid),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行固定的只读主机发现探测")
    parser.add_argument("--host", required=True, help="来自明确请求的 SSH 地址")
    parser.add_argument("--host-id", required=True, help="稳定身份；绝不从 IP 推断")
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--deployment-id", help="关联的部署 ID；提供后自动写入全过程档案")
    parser.add_argument(
        "--archive-root", type=Path, default=ROOT / "deployments", help="部署归档根目录"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    transport: ParamikoTransport | None = None
    try:
        transport = ParamikoTransport.connect(
            args.host,
            username=args.username,
            port=args.port,
            dotenv_path=args.env_file,
        )
        profile = collect_host_profile(transport, host_id=args.host_id, aliases=args.alias)
        validate_instance(profile, "host-profile.schema.json")
        atomic_write_json(args.output, profile)
        if args.deployment_id:
            DeploymentArchive(args.deployment_id, root=args.archive_root).record(
                stage="HOST_DISCOVERY",
                status="PASS" if profile["probe"]["status"] == "COMPLETE" else "INCOMPLETE",
                summary="只读主机检查完成",
                host_id=args.host_id,
                artifacts=(args.output,),
                details={"probe_status": profile["probe"]["status"]},
            )
        print(f"{profile['probe']['status']}: {args.output}")
        return 0 if profile["probe"]["status"] == "COMPLETE" else 3
    except (HarnessError, OSError, ValueError, ConnectionError) as exc:
        if args.deployment_id:
            DeploymentArchive(args.deployment_id, root=args.archive_root).record(
                stage="HOST_DISCOVERY",
                status="BLOCKED",
                summary="只读主机检查未能完成",
                host_id=args.host_id,
                details={"error_type": exc.__class__.__name__},
            )
        print(f"BLOCKED: {exc.__class__.__name__}")
        return 2
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
