"""配置公共入口：值类型与加载流程各自独立。"""

from .loader import load_settings
from .settings import ModelSettings, Settings

__all__ = ["ModelSettings", "Settings", "load_settings"]
