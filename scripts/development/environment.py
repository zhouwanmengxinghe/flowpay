"""仅依赖标准库的准备入口，可在项目依赖尚未安装时运行。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 会话与轨迹存在本地 SQLite，只有知识库需要 PostgreSQL。它只发布在 127.0.0.1，
# 里面是公开的教学数据，所以口令固定在 compose.yaml 与 Makefile 里，.env 不存凭据。


class Arguments(argparse.Namespace):
    """环境准备选项；显式声明类型以供入口检查。"""

    skip_knowledge: bool = False
    download_model: bool = False
    external_db: bool = False


class PreparationError(Exception):
    """可直接显示的操作提示，不包含连接字符串或密码。"""


def read_env(path: Path) -> dict[str, str]:
    """解析简单 KEY=value 文件，不执行 shell 或展开命令；保留现有文件原文。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        key, separator, value = line.partition("=")
        if not separator or not key.strip().isidentifier():
            raise PreparationError(f".env 第 {number} 行不是 KEY=value 格式。")
        try:
            parts = shlex.split(value, comments=False)
            if value.lstrip().startswith("#"):
                parts = []
            elif len(parts) > 1 and parts[1].startswith("#"):
                parts = parts[:1]
        except ValueError:
            raise PreparationError(f".env 第 {number} 行引号不完整。") from None
        literal = value.strip().startswith("'") and value.strip().endswith("'")
        if len(parts) > 1 or (not literal and ("$" in value or "`" in value)):
            raise PreparationError(
                f".env 第 {number} 行请使用普通值；包含空格请加引号，不支持变量展开。"
            )
        values[key.strip()] = parts[0] if parts else ""
    return values


def prepare_env(path: Path, inherited: dict[str, str]) -> dict[str, str]:
    """读取 .env 并补齐缺省项；shell 环境优先，已有内容不重写。"""
    existing = read_env(path)
    values = {**existing, **inherited}
    additions: dict[str, str] = {}
    if not values.get("OLLAMA_API_KEY"):
        additions["OLLAMA_API_KEY"] = "ollama"
    if additions:
        # 仅追加缺失配置，不重写用户注释及模型设置。创建文件时即使用私有权限。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write("\n# make setup 自动补齐的本地配置\n")
            for key, value in additions.items():
                handle.write(f"{key}={shlex.quote(value)}\n")
        path.chmod(0o600)
    return {**values, **additions}


def command(
    args: list[str], env: dict[str, str], *, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """捕获命令输出供检查；上层只展示脱敏错误，超时给出恢复提示。"""
    try:
        return subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PreparationError(f"{args[0]} 执行超时；检查服务后重新运行。") from None
    except OSError:
        raise PreparationError(f"无法执行 {args[0]}，请检查安装与 PATH。") from None


def execute(args: list[str], env: dict[str, str], label: str) -> None:
    """等待准备命令完成；持久化脱敏日志，失败时提供定位入口。"""
    print(f"… {label}（完成后日志保存到 var/setup/）", flush=True)
    directory = ROOT / "var/setup"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{Path(args[0]).name}-{Path(args[-1]).name}.log"
    # 子进程日志不流向终端，避免底层异常暴露连接字符串；落盘后仍作脱敏。
    result = command(args, env, timeout=3600)
    output = result.stdout + result.stderr
    for key, value in env.items():
        if value and any(word in key for word in ("PASSWORD", "DSN", "API_KEY")):
            output = output.replace(value, "[REDACTED]")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(output)
    if result.returncode:
        raise PreparationError(
            f"{label}未完成。请查看 {path.relative_to(ROOT)}，修复后重跑 make setup。"
        )
    print(f"✓ {label}", flush=True)


def check_tools(env: dict[str, str], knowledge: bool, external: bool = False) -> bool:
    """检查前置工具和 Docker 服务；不安装软件、不启动系统服务。"""
    good = True
    tools = (
        ["uv", "node", "npm"]
        + ([] if external else ["docker"])
        + (["ollama"] if knowledge else [])
    )
    for tool in tools:
        if not shutil.which(tool, path=env.get("PATH")):
            print(f"✗ 缺少 {tool}；请安装后重新执行。")
            good = False
        else:
            print(f"✓ {tool} 已安装")
    if not good:
        return False
    version = command(["node", "--version"], env).stdout.strip().lstrip("v").split(".")
    if len(version) < 2 or tuple(map(int, version[:2])) < (22, 12):
        print("✗ Node 版本过低；请安装 Node >=22.12。")
        good = False
    if external:
        return good
    if command(["docker", "info"], env).returncode:
        print("✗ Docker 未就绪；请启动 Docker Desktop。")
        good = False
    if command(["docker", "compose", "version"], env).returncode:
        print("✗ Docker Compose 不可用；请安装 Compose v2。")
        good = False
    return good


def dependencies_ready() -> bool:
    """对照锁文件指纹判断依赖是否经过本入口同步；不触发安装。"""
    marker = ROOT / "var/setup/dependencies.json"
    if not marker.exists() or not (ROOT / "frontend/node_modules/.bin/vite").exists():
        return False
    try:
        return marker.read_text() == json.dumps(dependency_hashes(), sort_keys=True)
    except ValueError:
        return False


def dependency_hashes() -> dict[str, str]:
    """记录会改变安装结果的清单和锁文件。"""
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "backend/uv.lock",
            "backend/pyproject.toml",
            "frontend/package-lock.json",
            "frontend/package.json",
        )
    }


def perform(operation: str) -> None:
    """统一准备或只读检查；任一前置条件失败就停止依赖步骤。"""
    parser = argparse.ArgumentParser(description="本地基础环境准备 / 检查")
    parser.add_argument(
        "--skip-knowledge",
        action="store_true",
        help="跳过 RAG 知识集合；准备后可使用 make demo 做模拟验收",
    )
    parser.add_argument(
        "--download-model",
        action="store_true",
        help="允许下载缺失的本地嵌入模型（可能数百 MB）",
    )
    parser.add_argument(
        "--external-db",
        action="store_true",
        help="使用现有 PostgreSQL，不启动本地 Docker 数据库",
    )
    args = parser.parse_args(namespace=Arguments())
    try:
        knowledge = not bool(args.skip_knowledge)
        env = {**read_env(ROOT / ".env"), **os.environ}
        if not check_tools(env, knowledge, args.external_db):
            raise PreparationError("前置工具未就绪；后续数据库和知识准备已跳过。")
        if operation == "setup":
            env = prepare_env(ROOT / ".env", dict(os.environ))
            print("✓ 本地配置已准备，原有配置保留")
            execute(
                ["uv", "sync", "--project", "backend", "--frozen"], env, "同步后端依赖"
            )
            marker = ROOT / "var/setup/dependencies.json"
            if not dependencies_ready():
                marker.unlink(missing_ok=True)
                execute(["npm", "--prefix", "frontend", "ci"], env, "同步前端依赖")
            else:
                print("✓ 前端依赖与锁文件匹配，跳过安装")
            marker.write_text(json.dumps(dependency_hashes(), sort_keys=True))
            if not args.external_db:
                execute(
                    ["docker", "compose", "up", "-d", "--wait"], env, "启动 PostgreSQL"
                )
            execute(
                [
                    str(ROOT / "backend/.venv/bin/python"),
                    "scripts/database.py",
                    "setup",
                ],
                env,
                "更新数据库结构与权限",
            )
        elif not dependencies_ready():
            print("✗ 依赖尚未通过统一入口同步，或锁文件已变化；请运行 make setup。")
            raise PreparationError("后续检查已跳过。")
        python = ROOT / "backend/.venv/bin/python"
        if not python.exists():
            raise PreparationError("缺少后端虚拟环境；请执行 make setup。")
        runtime = [str(python), "-m", "scripts.development.checks", operation]
        if not knowledge:
            runtime.append("--skip-knowledge")
        if args.download_model and operation == "setup":
            runtime.append("--download-model")
        sys.stdout.flush()
        result = subprocess.run(
            runtime, cwd=ROOT, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, check=False
        )
        if result.returncode:
            raise SystemExit(result.returncode)
    except (PreparationError, ValueError) as error:
        print(f"✗ {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("准备已中断；已完成的步骤保留，可重新执行 make setup。", file=sys.stderr)
        raise SystemExit(130) from None


@contextmanager
def setup_lock(operation: str) -> Generator[None]:
    """只为写入流程加锁，防止两个准备进程同时生成配置；doctor 不创建文件。"""
    if operation != "setup":
        yield
        return
    directory = ROOT / "var/setup"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "setup.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreparationError("另一个 make setup 正在执行，请等待完成。") from None
        yield


def main(operation: str) -> None:
    """持有准备锁直到所有子步骤结束；只读检查不加锁。"""
    try:
        with setup_lock(operation):
            perform(operation)
    except PreparationError as error:
        print(f"✗ {error}", file=sys.stderr)
        raise SystemExit(1) from None
