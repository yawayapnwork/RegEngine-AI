"""Python build script for the `regengine_native` pybind11 extension
(Requirement 2's Python binding).

Verified working on this project's actual dev machine: MSVC (via
`setuptools`'s standard compiler auto-detection -- no manual
`vcvars64.bat` invocation needed) on Windows, and the standard
GCC/Clang C++17 path on Linux/macOS. `setup.py build_ext --inplace`
compiles `pybind_module.cpp` + the engine/loader/C-API sources into a
single extension module.

Usage:
    cd native
    pip install pybind11
    python setup.py build_ext --inplace
    python -c "import regengine_native; print(regengine_native.__file__)"

Or, for an installable wheel: `pip install .` (see pyproject.toml).
"""
from __future__ import annotations

import pybind11
from setuptools import Extension, setup

extra_compile_args: list[str] = []
extra_link_args: list[str] = []

# MSVC (cl.exe) and GCC/Clang spell "use C++17" differently; setuptools
# doesn't pick a sensible default for either on its own.
import sys

if sys.platform == "win32":
    extra_compile_args += ["/std:c++17", "/O2", "/EHsc"]
else:
    extra_compile_args += ["-std=c++17", "-O3", "-fvisibility=hidden"]

ext_modules = [
    Extension(
        "regengine_native",
        sources=[
            "bindings/pybind_module.cpp",
            "src/c_api.cpp",
        ],
        include_dirs=[pybind11.get_include(), "include"],
        language="c++",
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    )
]

setup(
    name="regengine_native",
    version="1.0.0",
    description="RegEngine AI ultra-low-latency native policy evaluation kernel (pybind11 binding).",
    ext_modules=ext_modules,
    zip_safe=False,
)
