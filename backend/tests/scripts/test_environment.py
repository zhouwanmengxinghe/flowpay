"""环境引导回归：配置保护、重复执行、只读检查和失败停止。"""

import os
import subprocess
from pathlib import Path

import pytest
from scripts.development import environment as env


def test_first_setup_and_repeat_keep_user_values(tmp_path: Path) -> None:
    """只补缺省项，重复执行不改变文件，用户填的值原样保留。"""
    path = tmp_path / ".env"
    path.write_text("# user configuration\nFLOWPAY_MODEL_API_KEY='example'\n")
    first = env.prepare_env(path, {})
    content = path.read_bytes()
    second = env.prepare_env(path, {})
    assert first == second
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o600
    assert first["FLOWPAY_MODEL_API_KEY"] == "example"
    assert first["OLLAMA_API_KEY"] == "ollama"


def test_no_database_credentials_are_written(tmp_path: Path) -> None:
    """数据库口令固定在 compose 与 Makefile 里，.env 不该出现任何凭据。"""
    path = tmp_path / ".env"
    path.write_text("FLOWPAY_MODEL_API_KEY='example'\n")
    env.prepare_env(path, {})
    assert "PASSWORD" not in path.read_text()
    assert "_DSN" not in path.read_text()


def test_env_is_parsed_without_shell_execution(tmp_path: Path) -> None:
    """保留单引号密码中的特殊字符，拒绝命令及环境变量展开。"""
    path = tmp_path / ".env"
    path.write_text("export PASSWORD='a$b#c'\nNAME=hello # comment\n")
    assert env.read_env(path) == {"PASSWORD": "a$b#c", "NAME": "hello"}
    path.write_text("TOKEN=$(touch unexpected)\n")
    with pytest.raises(env.PreparationError):
        env.read_env(path)
    assert not (tmp_path / "unexpected").exists()


def test_doctor_lock_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只读检查不创建 setup 目录或锁文件。"""
    monkeypatch.setattr(env, "ROOT", tmp_path)
    with env.setup_lock("doctor"):
        pass
    assert list(tmp_path.iterdir()) == []


def test_setup_lock_prevents_concurrent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个工作区不能同时准备两次。"""
    monkeypatch.setattr(env, "ROOT", tmp_path)
    with env.setup_lock("setup"):
        with pytest.raises(env.PreparationError, match="另一个"):
            with env.setup_lock("setup"):
                pass


def test_failed_command_is_redacted_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """底层连接错误不能泄露凭据，并阻止继续执行。"""
    monkeypatch.setattr(env, "ROOT", tmp_path)

    def failed(
        args: list[str], values: dict[str, str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """模拟包含敏感值的子进程失败。"""
        _ = values, timeout
        return subprocess.CompletedProcess(args, 1, "secret-password", "private-dsn")

    monkeypatch.setattr(env, "command", failed)
    with pytest.raises(env.PreparationError, match="未完成"):
        env.execute(
            ["example", "setup"],
            {"TEST_PASSWORD": "secret-password", "TEST_DSN": "private-dsn"},
            "数据库准备",
        )
    log = (tmp_path / "var/setup/example-setup.log").read_text()
    assert "secret-password" not in log and "private-dsn" not in log
    assert "[REDACTED]" in log


def test_environment_overrides_file_without_rewriting_it(tmp_path: Path) -> None:
    """保留已有 shell 环境变量优先级，不将外部密钥写进文件。"""
    path = tmp_path / ".env"
    env.prepare_env(path, {})
    before = path.read_bytes()
    values = env.prepare_env(path, {"FLOWPAY_MODEL_API_KEY": "external"})
    assert values["FLOWPAY_MODEL_API_KEY"] == "external"
    assert path.read_bytes() == before
    assert "external" not in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_setup_stops_after_failed_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖安装失败时不启动数据库或执行迁移；保留配置供下次继续。"""
    monkeypatch.setattr(env, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["setup.py"])

    def available(values: dict[str, str], knowledge: bool, external: bool = False) -> bool:
        """模拟已安装前置工具。"""
        _ = values, knowledge, external
        return True

    monkeypatch.setattr(env, "check_tools", available)
    monkeypatch.setattr(os, "environ", {})
    calls: list[str] = []

    def volume_list(
        args: list[str], values: dict[str, str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """模拟首次准备前没有数据卷。"""
        _ = values, timeout
        return subprocess.CompletedProcess(args, 0, "", "")

    def install(args: list[str], values: dict[str, str], label: str) -> None:
        """第一步安装失败，不允许继续。"""
        _ = values, label
        calls.append(args[0])
        raise env.PreparationError("同步依赖失败")

    monkeypatch.setattr(env, "command", volume_list)
    monkeypatch.setattr(env, "execute", install)
    with pytest.raises(SystemExit) as result:
        env.perform("setup")
    assert result.value.code == 1
    assert calls == ["uv"]
    assert (tmp_path / ".env").exists()
