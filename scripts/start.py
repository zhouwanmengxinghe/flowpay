"""一条命令把整套环境跑起来：校验 .env、准备数据库、导入知识库、起全栈。

只用标准库和 docker，因此不需要先建出 backend 虚拟环境。可以重复执行：
.env 里已有的值不覆盖，数据库已迁移过不重跑，知识集合已就绪就跳过导入。

数据集路径接受绝对路径、相对路径和 ~ 开头的路径，也接受还没解开的 .tar.gz 或 .zip。
解析出来的绝对路径通过子进程环境交给 compose（shell 环境优先于 .env），
因此用户写在 .env 里的相对路径会原样保留。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.development.environment import prepare_env

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "flowpay_docs_v1_qwen3_06b"
KNOWLEDGE_DUMP = "postgres/flowpay_knowledge.dump"
# 数据集根目录必须有这两项，缺了说明路径指错了。知识库 dump 只在真要导入时才需要，
# 因此不列在这里：已经灌过库的开发副本里通常没有它。
LAYOUT = ("data/knowledge", "benchmark/design")
DATASET_KEY = "FLOWPAY_DATASET_ROOT"
MODEL_KEY = "FLOWPAY_MODEL_API_KEY"


class Arguments(argparse.Namespace):
    """命令行参数；默认起全栈，可选只做准备。"""

    build: bool = False
    ollama: bool = False


def say(message: str) -> None:
    """进度提示；每一步都打印，失败时能看出停在哪。"""
    print(f"› {message}", flush=True)


def compose(*args: str, env: dict[str, str] | None = None) -> None:
    """在仓库根目录执行 docker compose；失败直接抛出。"""
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, env=env)


def read_env() -> dict[str, str]:
    """读取 .env；不存在就从 .env.example 复制一份再让用户去填。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        shutil.copy(ROOT / ".env.example", env_path)
        env_path.chmod(0o600)
        created = f"已从 .env.example 创建 {env_path}"
        raise SystemExit(f"{created}，请填写 {MODEL_KEY} 与 {DATASET_KEY} 后重新执行。")
    values = prepare_env(env_path, dict(os.environ))
    for key, hint in ((MODEL_KEY, "模型密钥"), (DATASET_KEY, "数据包位置")):
        if not values.get(key):
            missing = f"{env_path} 里的 {key}（{hint}）还是空的，填好再执行。"
            # 早先的版本用供应商名当变量名，这里指个路，省得对着报错猜。
            if key == MODEL_KEY and values.get("DEEPSEEK_API_KEY"):
                missing = f"{missing}密钥变量已改名，把 DEEPSEEK_API_KEY 换成 {MODEL_KEY} 即可。"
            raise SystemExit(missing)
    return values


def open_zip(bundle: Path) -> zipfile.ZipFile:
    """打开 ZIP；兼容未标注 UTF-8 的文件名，调用方负责用 with 关闭。"""
    try:
        return zipfile.ZipFile(bundle, metadata_encoding="utf-8")
    except UnicodeDecodeError:
        # 旧 ZIP 使用 CP437；UTF-8 解码失败时回到标准库的默认编码。
        return zipfile.ZipFile(bundle)


def unpack(bundle: Path) -> Path:
    """解开数据包到它所在的目录，返回解出来的那个目录；已解开过就直接复用。"""
    with (
        open_zip(bundle) if bundle.suffix.lower() == ".zip" else tarfile.open(bundle)
    ) as archive:
        names = (
            archive.namelist()
            if isinstance(archive, zipfile.ZipFile)
            else archive.getnames()
        )
        if isinstance(archive, zipfile.ZipFile):
            # Finder 附带的资源分支和目录缓存不参与顶层目录判断，也不解压。
            names = [
                name
                for name in names
                if name.split("/")[0] != "__MACOSX" and Path(name).name != ".DS_Store"
            ]
        top = {
            name.split("/")[0] for name in names if name and not name.startswith(".")
        }
        if len(top) != 1:
            raise SystemExit(f"数据包应当只有一个顶层目录，实际有：{sorted(top)}")
        target = bundle.parent / top.pop()
        if isinstance(archive, zipfile.ZipFile):
            # ZIP 没有 tar 的 data 过滤器，先检查全部成员，避免写到数据集目录之外。
            root = target.resolve()
            for name in names:
                member = Path(name)
                destination = (bundle.parent / member).resolve()
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or "\\" in name
                    or not destination.is_relative_to(root)
                ):
                    raise SystemExit(f"ZIP 数据包包含不安全的路径：{name}")
        if target.exists():
            say(f"数据包已解开过，直接用 {target}")
            return target
        say(f"解开数据包到 {target}（约 160 MB，稍等）")
        if isinstance(archive, zipfile.ZipFile):
            archive.extractall(bundle.parent, members=names)
        else:
            archive.extractall(bundle.parent, filter="data")
    return target


def resolve_dataset(raw: str) -> Path:
    """把用户填的路径变成能交给 compose 的绝对路径。

    相对路径按仓库根目录解析，这样在项目里写 ../flowpay-dataset-v1 也能用。
    指向 .tar.gz 或 .zip 时先解开，指向目录时直接校验布局。
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        raise SystemExit(f"{DATASET_KEY} 指向的位置不存在：{path}")
    if path.is_file():
        suffixes = [suffix.lower() for suffix in path.suffixes]
        if suffixes[-1:] != [".zip"] and suffixes[-2:] != [".tar", ".gz"]:
            raise SystemExit(
                f"{DATASET_KEY} 要指向数据集目录、.tar.gz 或 .zip，当前是：{path}"
            )
        path = unpack(path)
    missing = [part for part in LAYOUT if not (path / part).exists()]
    if missing:
        raise SystemExit(f"{path} 里缺少：{'、'.join(missing)}；确认数据包完整。")
    return path


def knowledge_ready(env: dict[str, str]) -> bool:
    """知识集合是否已就绪；库还没建起来时也算未就绪。"""
    query = f"select 1 from collections where collection_id='{COLLECTION}' and status='ready'"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "flowpay_admin",
            "-d",
            "flowpay_knowledge",
            "-tAc",
            query,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def load_knowledge(dump: Path, env: dict[str, str]) -> None:
    """建表、授权，再把 712 个向量灌进去。"""
    if not dump.exists():
        absent = f"知识集合还没导入，但找不到 {KNOWLEDGE_DUMP}：{dump}"
        raise SystemExit(f"{absent}；请把 {DATASET_KEY} 指向数据包解开后的目录。")
    say("执行迁移与角色授权（在 backend 容器里跑，宿主机不需要 Python 依赖）")
    compose(
        "run",
        "--rm",
        "--no-deps",
        "backend",
        "python",
        "scripts/database.py",
        "setup",
        env=env,
    )
    say(f"导入知识库 {dump.name}（100 篇文档、712 个向量）")
    compose("cp", str(dump), "postgres:/tmp/knowledge.dump", env=env)
    # --data-only：结构已由迁移建好。--disable-triggers：绕过只追加保护，
    # 它防的是运行期改写，不该拦住一次显式的恢复。只点名这三张：alembic_version
    # 由迁移自己维护，连它一起恢复会撞主键。
    compose(
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "-U",
        "flowpay_admin",
        "-d",
        "flowpay_knowledge",
        "--data-only",
        "--disable-triggers",
        "--no-owner",
        "--no-acl",
        "-t",
        "collections",
        "-t",
        "documents",
        "-t",
        "chunks",
        "/tmp/knowledge.dump",
        env=env,
    )
    compose("exec", "-T", "postgres", "rm", "-f", "/tmp/knowledge.dump", env=env)


def main() -> None:
    """校验配置、准备数据、起全栈；每一步都可以单独重跑。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="强制重新构建镜像")
    parser.add_argument("--ollama", action="store_true", help="同时启动容器内嵌入服务")
    args = parser.parse_args(namespace=Arguments())
    values = read_env()
    dataset = resolve_dataset(values[DATASET_KEY])
    say(f"数据集：{dataset}")
    # compose 读 shell 环境优先于 .env，因此这里传绝对路径，用户写的相对路径原样保留。
    env = {**os.environ, DATASET_KEY: str(dataset)}
    try:
        say("启动 PostgreSQL")
        compose("up", "-d", "--wait", "postgres", env=env)
        if knowledge_ready(env):
            say("知识集合已就绪，跳过导入")
        else:
            load_knowledge(dataset / KNOWLEDGE_DUMP, env)
        say("启动后端与前端")
        profile = ["--profile", "ollama"] if args.ollama else []
        build = ["--build"] if args.build else []
        compose(*profile, "up", "-d", *build, env=env)
    except subprocess.CalledProcessError as error:
        parts: list[str] = [str(part) for part in list(error.cmd)]  # pyright: ignore[reportAny]
        raise SystemExit(f"命令失败：{' '.join(parts)}") from None
    print("\n✓ 网页 http://127.0.0.1:5173/   接口 http://127.0.0.1:8000/")


if __name__ == "__main__":
    main()
