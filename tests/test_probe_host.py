from __future__ import annotations

import json
from pathlib import Path

import paramiko

from scripts._common import validate_instance
from scripts.probe_host import (
    PROBE_COMMANDS,
    CommandResult,
    ParamikoTransport,
    collect_host_profile,
)

FIXTURE = Path(__file__).parent / "fixtures/host_probe_outputs.json"


class FakeTransport:
    def __init__(self, outputs: dict[str, str], failures: set[str] | None = None):
        self.outputs = outputs
        self.failures = failures or set()
        self.commands = []

    def run(self, argv, *, timeout=20, cwd=None, env=None):
        self.commands.append(tuple(argv))
        name = next(name for name, command in PROBE_COMMANDS.items() if tuple(argv) == command)
        return CommandResult(
            tuple(argv), 1 if name in self.failures else 0, self.outputs.get(name, "")
        )

    def close(self):
        pass


def test_collects_complete_schema_valid_host_profile():
    outputs = json.loads(FIXTURE.read_text())
    transport = FakeTransport(outputs)
    profile = collect_host_profile(
        transport, host_id="knode25", aliases=("h100-lab",), transport_name="LOCAL_FIXTURE"
    )

    validate_instance(profile, "host-profile.schema.json")
    assert profile["identity"]["addresses"] == ["10.0.0.25", "192.168.1.25"]
    assert profile["hardware"]["gpus"][0]["memory_total_bytes"] == 81920 * 1024 * 1024
    assert profile["software"]["docker"]["nvidia_runtime_available"] is True
    assert profile["network"]["listening_ports"] == [22, 30010]
    assert profile["runtime"]["model_services"] == [
        {"pid": 4242, "process_name": "python", "ports": [30010]}
    ]
    assert transport.commands == list(PROBE_COMMANDS.values())


def test_essential_ssh_failures_produce_failed_profile_without_exception_details():
    failures = set(PROBE_COMMANDS) - {"gpus", "cuda", "gpu_processes", "topology"}
    profile = collect_host_profile(
        FakeTransport({}, failures), host_id="knode25", transport_name="LOCAL_FIXTURE"
    )
    assert profile["probe"]["status"] == "FAILED"
    assert all("password" not in error.lower() for error in profile["probe"]["errors"])


def test_paramiko_authentication_is_key_first_then_environment_password():
    class Client:
        def __init__(self):
            self.calls = []

        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def close(self):
            pass

        def connect(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise paramiko.AuthenticationException("denied")

    client = Client()
    transport = ParamikoTransport.connect(
        "10.0.0.25",
        username="deploy",
        environ={"DEPLOY_SSH_PASSWORD": "not-printed"},
        client_factory=lambda: client,
    )
    assert transport._client is client
    assert client.calls[0]["password"] is None
    assert client.calls[0]["look_for_keys"] is True
    assert client.calls[1]["password"] == "not-printed"
    assert client.calls[1]["look_for_keys"] is False
