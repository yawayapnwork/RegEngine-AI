cmake_minimum_required(VERSION 3.16)
project(regengine_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

option(REGENGINE_BUILD_TESTS "Build the standalone C++ correctness test" ON)
option(REGENGINE_BUILD_BENCHMARKS "Build the Google Benchmark micro-benchmark suite (Requirement 3)" ON)
option(REGENGINE_BUILD_PYTHON_BINDING "Build the pybind11 Python extension via CMake (native/setup.py is the primary, verified-working path -- see its docstring; this option exists for a single unified CMake build, e.g. in CI)" OFF)

# --------------------------------------------------------------------------
# Core library: Requirement 1's ".so"/".dll" shared-library packaging.
# The hot-path evaluate()/evaluate_raw() itself is header-only (see
# policy_engine.h's module docstring on why -- full inlining into a
# statically-linked OMS is the fastest deployment shape); this target's
# only translation unit is the C-FFI surface (Requirement 2), which is
# what actually needs a stable, exported, versioned ABI.
# --------------------------------------------------------------------------
add_library(regengine_policy SHARED src/c_api.cpp)
target_include_directories(regengine_policy PUBLIC include)
target_compile_definitions(regengine_policy PRIVATE REGENGINE_BUILD_DLL)
set_target_properties(regengine_policy PROPERTIES
  CXX_VISIBILITY_PRESET hidden
  VISIBILITY_INLINES_HIDDEN ON
)
if(MSVC)
  target_compile_options(regengine_policy PRIVATE /W4 /O2)
else()
  target_compile_options(regengine_policy PRIVATE -Wall -Wextra -O3)
endif()

# A static variant of the same C-FFI translation unit, for a co-located
# OMS build that wants zero dynamic-loading overhead at process start
# and the option of link-time inlining across the .so boundary (LTO) --
# most HFT deployments will prefer linking this directly over dlopen()-
# ing the shared library at runtime.
add_library(regengine_policy_static STATIC src/c_api.cpp)
target_include_directories(regengine_policy_static PUBLIC include)
target_compile_definitions(regengine_policy_static PRIVATE REGENGINE_BUILD_DLL)
if(MSVC)
  target_compile_options(regengine_policy_static PRIVATE /O2)
else()
  target_compile_options(regengine_policy_static PRIVATE -O3)
endif()

# --------------------------------------------------------------------------
# Correctness test (native/tests/test_policy_engine.cpp) -- plain
# assertions, no test framework dependency, so this always builds even
# offline / with no package manager reachable.
# --------------------------------------------------------------------------
if(REGENGINE_BUILD_TESTS)
  add_executable(regengine_policy_test tests/test_policy_engine.cpp)
  target_link_libraries(regengine_policy_test PRIVATE regengine_policy_static)
  target_include_directories(regengine_policy_test PRIVATE include)
  enable_testing()
  add_test(NAME regengine_policy_test COMMAND regengine_policy_test)

  # FIX gateway layer (tag scanning, execution report construction,
  # end-to-end validate_new_order) -- header-only, so this only needs
  # the same include path, not a link against regengine_policy_static's
  # object code (policy_loader.h's load_policy() is header-only too).
  add_executable(regengine_fix_gateway_test tests/test_fix_gateway.cpp)
  target_include_directories(regengine_fix_gateway_test PRIVATE include)
  add_test(NAME regengine_fix_gateway_test COMMAND regengine_fix_gateway_test)
endif()

# --------------------------------------------------------------------------
# Requirement 3: Google Benchmark micro-benchmark suite. Fetched via
# FetchContent (needs network access at configure time -- see
# benchmarks/README note in bench_policy_eval.cpp's header comment for
# the offline alternative) rather than vendored, to always benchmark
# against a real, current Google Benchmark rather than a stale copy.
# --------------------------------------------------------------------------
if(REGENGINE_BUILD_BENCHMARKS)
  include(FetchContent)
  set(BENCHMARK_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_ENABLE_GTEST_TESTS OFF CACHE BOOL "" FORCE)
  set(BENCHMARK_DOWNLOAD_DEPENDENCIES ON CACHE BOOL "" FORCE)
  FetchContent_Declare(
    googlebenchmark
    GIT_REPOSITORY https://github.com/google/benchmark.git
    GIT_TAG v1.9.1
  )
  FetchContent_MakeAvailable(googlebenchmark)

  add_executable(bench_policy_eval benchmarks/bench_policy_eval.cpp)
  target_link_libraries(bench_policy_eval PRIVATE regengine_policy_static benchmark::benchmark)
  target_include_directories(bench_policy_eval PRIVATE include)
  if(NOT MSVC)
    target_link_libraries(bench_policy_eval PRIVATE pthread)
  endif()
  if(MSVC)
    target_compile_options(bench_policy_eval PRIVATE /O2)
  else()
    target_compile_options(bench_policy_eval PRIVATE -O3)
  endif()
endif()

# --------------------------------------------------------------------------
# Optional: pybind11 extension via CMake. native/setup.py (verified
# working against this project's own toolchain -- see its docstring) is
# the primary way to build the Python binding; this target is provided
# for a CI pipeline that wants one CMake invocation to build everything.
# --------------------------------------------------------------------------
if(REGENGINE_BUILD_PYTHON_BINDING)
  include(FetchContent)
  FetchContent_Declare(pybind11 GIT_REPOSITORY https://github.com/pybind/pybind11.git GIT_TAG v2.13.6)
  FetchContent_MakeAvailable(pybind11)
  pybind11_add_module(regengine_native bindings/pybind_module.cpp src/c_api.cpp)
  target_include_directories(regengine_native PRIVATE include)
endif()
