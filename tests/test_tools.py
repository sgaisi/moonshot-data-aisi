import importlib.util
import inspect
import os
import sys
import ast
from pathlib import Path
import pytest

TOOLS_DIR = (Path(__file__).parent / ".." / "tools").resolve()


def clear_tools_from_sys_modules(prefix="test_tools_"):
    """
    Remove all loaded tool modules with given prefix from sys.modules.
    """
    to_remove = [name for name in sys.modules if name.startswith(prefix)]
    for name in to_remove:
        del sys.modules[name]


def check_syntax(file_path: str):
    """
    Parse Python file using AST to catch syntax errors.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            ast.parse(f.read(), filename=file_path)
        except SyntaxError as e:
            raise SyntaxError(f"Syntax error in {file_path}:\n{e}")


def load_module_from_file(file_path: str, module_name: str):
    """
    Dynamically load a Python module from a file.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.abspath(file_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_all_tools(module):
    """
    Return all functions defined in the module
    """
    tools = {name: obj for name, obj in inspect.getmembers(module, inspect.isfunction)}
    return tools


@pytest.mark.parametrize("file", [f for f in TOOLS_DIR.iterdir() if f.suffix == ".py"])
def test_tools_syntax(file):
    try:
        check_syntax(str(file))
        module = load_module_from_file(str(file), module_name=f"test_tools_{file.stem}")
        tools = discover_all_tools(module)
    except SyntaxError as e:
        pytest.fail(str(e))
    except Exception as e:
        pytest.fail(f"Failed to load tools from {file.name}: {e}")

    if not tools:
        pytest.fail(f"No tools found in {file.name}")

    clear_tools_from_sys_modules()

    print(f"Discovered {len(tools)} tools from {file.name}: {list(tools.keys())}\n")
