"""测试环境准备：把仓库目录注册为 ``dsh2qingcibot`` 包。

生产环境由 Qingci-Bot 以 ``plugins.whale_companion`` 包名加载，插件内部
统一使用相对导入。测试通过显式注册包名导入，避免依赖仓库检出目录名。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

_PACKAGE = "dsh2qingcibot"
if _PACKAGE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    if _spec is not None and _spec.loader is not None:
        _module = importlib.util.module_from_spec(_spec)
        sys.modules[_PACKAGE] = _module
        _spec.loader.exec_module(_module)
