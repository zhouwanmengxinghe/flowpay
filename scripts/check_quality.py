"""统一检查教学基座；真实供应商验收单独执行，不隐式产生 API 用量。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from flowpay.core.json import (
    parse_json,
    require_int,
    require_list,
    require_object,
)
from flowpay.dataset import snapshot_ready

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "backend/.venv/bin/python"
BIN = PYTHON.parent
SCOPES = ["backend/src", "backend/tests", "backend/migrations", "scripts"]
SKIPPED_WITHOUT_DATASET = (
    "WARNING: 未配置数据集，s3/s4 与 benchmark 相关用例已跳过；"
    "设置 FLOWPAY_DATASET_ROOT 后重跑才算完整门禁。"
)


def run(*command: str, cwd: Path = ROOT) -> None:
    """任一检查失败立即退出，避免后续成功掩盖前面的失败。"""
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def check_types() -> None:
    # 保留完整报告；错误和警告均阻止验收，避免编辑器问题被门禁忽略。
    result = subprocess.run(
        [str(BIN / "basedpyright"), "--outputjson", *SCOPES],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr or result.stdout)
    report = require_object(parse_json(result.stdout))
    summary = require_object(report["summary"])
    output = ROOT / "var/quality/basedpyright.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.stdout)
    for item in require_list(report["generalDiagnostics"]):
        diagnostic = require_object(item)
        if diagnostic["severity"] in {"error", "warning"}:
            position = require_object(require_object(diagnostic["range"])["start"])
            line = require_int(position["line"]) + 1
            print(f"{diagnostic['file']}:{line}: {diagnostic['message']}")
    print(f"Type checks: {report['summary']}; all diagnostics: {output}", flush=True)
    if summary["errorCount"] or summary["warningCount"]:
        raise SystemExit(1)


class Arguments(argparse.Namespace):
    """本命令解析后的参数类型，供调用入口读取。"""

    database: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", action="store_true", help="运行独立 PostgreSQL 库与 HTTP/SSE 验收"
    )
    args = parser.parse_args(namespace=Arguments())
    run(str(BIN / "ruff"), "check", *SCOPES)
    run(str(BIN / "ruff"), "format", "--check", *SCOPES)
    check_types()
    if args.database:
        run(str(PYTHON), "scripts/verify_database.py")
    else:
        run(str(PYTHON), "-m", "pytest", "backend/tests", "-q")
        if not snapshot_ready():
            print(SKIPPED_WITHOUT_DATASET)
        print("PostgreSQL/HTTP regression omitted; use --database for full acceptance.")
    for command in ("format:check", "test", "build"):
        run("npm", "run", command, cwd=ROOT / "frontend")
    print("Requested quality checks passed.")


if __name__ == "__main__":
    main()
