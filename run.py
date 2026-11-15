"""顶层启动脚本（双击 / ``python run.py``）。

保留了原来「exe 旁边放一个数据目录」的绿色软件习惯：
源码运行时数据仍然落在**项目根目录**的 ``点歌姬数据/``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 源码运行时把 src 加进 sys.path，免去 pip install -e 也能直接跑
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from diangeji.__main__ import main  # noqa: E402  (路径注入之后才能导入)

if __name__ == "__main__":
    sys.exit(main())
