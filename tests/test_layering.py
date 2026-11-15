"""分层守卫：把架构规则变成会失败的测试。

`docs/architecture.md` 里写的依赖方向如果只靠自觉，迟早被人一次「就加个 import」
打破。这里用 AST 直接扫源码，把规则变成红线。

规则（见 docs/architecture.md）：

    ui / web  →  services  →  core / sources / live  →  utils

**只能往下，不能往上，也不能横跳。**
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "diangeji"

# 每一层允许依赖的层。没列出来的一律禁止。
ALLOWED: dict[str, set[str]] = {
    "utils": {"logging_setup"},                      # 工具层只许碰日志
    "core": {"utils", "constants", "logging_setup"},  # 领域模型 + 常量 + 日志
    "sources": {"core", "utils", "constants", "logging_setup", "paths"},
    "live": {"core", "utils", "constants", "logging_setup", "paths"},
    "auth": {"core", "utils", "constants", "logging_setup", "paths"},
    "services": {"core", "sources", "live", "auth", "utils",
                 "constants", "logging_setup", "paths", "config"},
    "web": {"services", "core", "sources", "live", "auth", "utils",
            "constants", "logging_setup", "paths", "config"},
    "ui": {"services", "core", "sources", "live", "auth", "utils",
           "constants", "logging_setup", "paths", "config"},
}

# 顶层模块也算「层」
TOP_MODULES = {"app", "config", "constants", "logging_setup", "paths", "__main__"}

# 允许 import 任何东西的模块（组合根和入口）
OMNIPOTENT = {"app", "__main__"}


def layer_of(path: Path) -> str:
    """``src/diangeji/core/queue.py`` → ``core``；``src/diangeji/app.py`` → ``app``。"""
    relative = path.relative_to(PACKAGE)
    if len(relative.parts) == 1:
        return relative.stem
    return relative.parts[0]


def package_targets(path: Path) -> set[str]:
    """解析一个模块 import 了本包内的哪些东西。

    处理 ``from ..core.queue import PlayQueue`` 和 ``from . import x``
    这类相对导入：按文件所在的包深度还原出绝对目标。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    here = path.relative_to(PACKAGE).parts[:-1]     # 当前模块所在包
    found: set[str] = set()

    for node in tree.body + [n for n in ast.walk(tree)
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if isinstance(node, ast.If) and _is_type_checking(node):
            continue        # `if TYPE_CHECKING:` 里的导入运行时不执行，不算依赖
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom):
                if _in_type_checking_block(child, tree):
                    continue
                if child.level:                          # 相对导入
                    base = list(here)
                    for _ in range(child.level - 1):
                        base.pop()
                    if child.module:
                        base.extend(child.module.split("."))
                    if base:
                        found.add(base[0])
                elif child.module and child.module.startswith("diangeji"):
                    parts = child.module.split(".")
                    if len(parts) > 1:
                        found.add(parts[1])

    return found - {"__future__"}


def _is_type_checking(node: ast.If) -> bool:
    """判断 ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:``。"""
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
           (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _in_type_checking_block(target: ast.AST, tree: ast.Module) -> bool:
    """目标语句是否位于某个 ``if TYPE_CHECKING:`` 分支内部。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node):
            for child in ast.walk(node):
                if child is target:
                    return True
    return False


def python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


class TestLayering:
    def test_no_circular_upward_dependency(self):
        """凡是层，都必须只往下依赖。"""
        violations = []
        for path in python_files():
            name = path.stem
            if name in OMNIPOTENT:
                continue
            layer = layer_of(path)
            if layer not in ALLOWED:
                continue
            allowed = ALLOWED[layer]
            for target in package_targets(path):
                if target == layer or target in allowed:
                    continue
                if target in TOP_MODULES or target in ALLOWED:
                    violations.append(
                        f"{path.relative_to(PACKAGE)}: {layer} → {target}（不允许）")
        assert not violations, "分层违规：\n" + "\n".join(violations)

    def test_nothing_imports_ui(self):
        """界面是最容易变的东西，不能被任何非 ui 模块依赖。"""
        offenders = []
        for path in python_files():
            if layer_of(path) == "ui":
                continue
            if path.stem in OMNIPOTENT:
                continue
            if "ui" in package_targets(path):
                offenders.append(str(path.relative_to(PACKAGE)))
        assert not offenders, f"这些模块 import 了 ui：{offenders}"

    def test_only_composition_root_touches_web(self):
        """web 是表现层，除了组合根和入口，谁都不该碰。"""
        offenders = []
        for path in python_files():
            if layer_of(path) == "web" or path.stem in OMNIPOTENT:
                continue
            if "web" in package_targets(path):
                offenders.append(str(path.relative_to(PACKAGE)))
        assert not offenders, f"这些模块 import 了 web：{offenders}"

    def test_core_does_not_depend_on_implementations(self):
        """core 只该碰 utils；碰 sources/services 就说明模型层被污染了。"""
        offenders = []
        for path in python_files():
            if layer_of(path) != "core":
                continue
            bad = package_targets(path) & {"sources", "services", "web", "ui", "live", "auth"}
            if bad:
                offenders.append(f"{path.name} → {sorted(bad)}")
        assert not offenders, f"core 依赖了上层：{offenders}"

    def test_utils_is_standalone(self):
        """工具层原样搬到别的项目应该还能用 —— 所以它不能碰业务模块。

        唯一例外是 ``logging_setup``：日志是横切关注点，不属于任何一层。
        """
        infrastructure = {"utils", "logging_setup"}
        offenders = []
        for path in python_files():
            if layer_of(path) != "utils":
                continue
            bad = package_targets(path) - infrastructure
            if bad:
                offenders.append(f"{path.name} → {sorted(bad)}")
        assert not offenders, f"utils 依赖了业务模块：{offenders}"

    def test_services_do_not_touch_presentation(self):
        offenders = []
        for path in python_files():
            if layer_of(path) != "services":
                continue
            bad = package_targets(path) & {"web", "ui"}
            if bad:
                offenders.append(f"{path.name} → {sorted(bad)}")
        assert not offenders, f"services 依赖了表现层：{offenders}"

    def test_no_function_level_import_patching_cycles(self):
        """函数内 import 常用于绕开循环依赖 —— 那是设计味道，数一下。

        允许少量（比如为了省启动时间），但不能多到说明结构有问题。
        """
        offenders = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom) and child.level:
                        offenders.append(
                            f"{path.relative_to(PACKAGE)}:{child.lineno} "
                            f"{node.name}() 内 import")
        # 阈值：超过 3 处就该重构了
        assert len(offenders) <= 3, "函数内 import 太多，疑似循环依赖：\n" + "\n".join(offenders)


class TestPublicApi:
    def test_each_package_has_init(self):
        """每个子包都要有 __init__.py，否则打包会漏。"""
        for directory in ALLOWED:
            target = PACKAGE / directory
            if target.is_dir():
                assert (target / "__init__.py").is_file(), f"{directory}/ 缺 __init__.py"

    def test_dunder_all_matches_exports(self):
        """``__all__`` 里写的东西必须真的能取到。"""
        import importlib
        broken = []
        for directory in ALLOWED:
            module_name = f"diangeji.{directory}"
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:      # pragma: no cover
                broken.append(f"{module_name}: 导入失败 {exc}")
                continue
            for name in getattr(module, "__all__", ()):
                if not hasattr(module, name):
                    broken.append(f"{module_name}.__all__ 里的 {name} 不存在")
        assert not broken, "\n".join(broken)

    def test_package_exposes_version(self):
        import diangeji
        assert diangeji.__version__
        assert diangeji.__app_name__

    def test_all_modules_import_cleanly(self):
        """所有模块都得能单独导入（打包时按需加载不会漏依赖）。"""
        import importlib
        failures = []
        for path in python_files():
            relative = path.relative_to(PACKAGE).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            name = "diangeji." + ".".join(parts)
            try:
                importlib.import_module(name)
            except Exception as exc:        # noqa: BLE001 - 就是要抓全部
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        assert not failures, "模块导入失败：\n" + "\n".join(failures)


class TestNoStrayModules:
    def test_package_root_has_no_business_modules(self):
        """包根只该有入口、装配、路径、配置这类横切模块。"""
        allowed = {"__init__.py", "__main__.py", "app.py", "paths.py",
                   "constants.py", "config.py", "logging_setup.py"}
        actual = {p.name for p in PACKAGE.glob("*.py")}
        stray = actual - allowed
        assert not stray, f"包根出现业务模块：{sorted(stray)}"

    def test_every_subpackage_is_known_layer(self):
        actual = {p.name for p in PACKAGE.iterdir() if p.is_dir()
                  and p.name != "__pycache__"}
        unknown = actual - set(ALLOWED)
        assert not unknown, f"新增了未在 ALLOWED 里登记的层：{sorted(unknown)}"
