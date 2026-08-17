from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEYS = {
    "password",
    "passphrase",
    "secret",
    "ssh_password",
    "private_key",
    "hf_token",
    "access_token",
    "api_key",
    "token",
    "model_token",
}
SECRET_VALUE_ENVIRONMENT_NAMES = {
    "DEPLOY_SSH_PASSWORD",
    "DEPLOY_SSH_KEY_PASSPHRASE",
    "HF_TOKEN",
    "MODEL_TOKEN",
}
SUPPORTED_CREDENTIAL_ENVIRONMENT_NAMES = SECRET_VALUE_ENVIRONMENT_NAMES | {"DEPLOY_SSH_KEY_PATH"}


class HarnessError(RuntimeError):
    """面向用户的失败即阻断工具错误。"""


def load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    value = json.loads(text) if source.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(value, dict):
        raise HarnessError(f"{source}：应为对象")
    return value


def schema_registry() -> Registry:
    registry = Registry()
    for path in sorted(SCHEMAS.glob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(contents)
        registry = registry.with_resource(contents["$id"], resource)
    return registry


def validate_instance(instance: dict[str, Any], schema_name: str) -> None:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema, registry=schema_registry(), format_checker=FormatChecker()
    )
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    if errors:
        details = []
        for error in errors[:12]:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            details.append(f"{location}: {error.message}")
        raise HarnessError("模式校验失败：" + "；".join(details))


def canonical_plan_sha256(plan: dict[str, Any]) -> str:
    canonical = deepcopy(plan)
    review = canonical.get("review")
    if not isinstance(review, dict):
        raise HarnessError("plan.review 必须为对象")
    review["plan_sha256"] = ""
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_identifier(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise HarnessError(f"{label} 无效：仅可使用字母、数字、点、下划线或连字符")
    return value


def assert_no_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                raise HarnessError(
                    f"持久化制品中禁止出现疑似密钥字段：{path}.{key}"
                )
            assert_no_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            raise HarnessError(f"{path} 中禁止出现私钥内容")
        dotenv_values: dict[str, str] = {}
        dotenv_path = ROOT / ".env"
        if dotenv_path.is_file():
            for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
                if "=" not in raw_line or raw_line.lstrip().startswith("#"):
                    continue
                name, raw_value = raw_line.split("=", 1)
                if name.strip() in SECRET_VALUE_ENVIRONMENT_NAMES:
                    dotenv_values[name.strip()] = raw_value.strip().strip("\"'")
        for name in SECRET_VALUE_ENVIRONMENT_NAMES:
            secret = os.environ.get(name) or dotenv_values.get(name)
            if secret and secret in value:
                raise HarnessError(f"在 {path} 中发现密钥环境变量 {name} 的值")


def atomic_write_json(path: str | Path, value: dict[str, Any]) -> None:
    assert_no_secrets(value)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
