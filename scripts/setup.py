"""基础环境准备入口；项目依赖未安装时也能运行。"""

import sys
from pathlib import Path


def main() -> None:
    """加载项目维护脚本，无需安装后端包。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.development.environment import main as prepare

    prepare("setup")


if __name__ == "__main__":
    main()
