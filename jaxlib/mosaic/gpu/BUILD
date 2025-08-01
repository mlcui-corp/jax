# Copyright 2024 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

load("@rules_python//python:defs.bzl", "py_library")
load("//jaxlib:jax.bzl", "nanobind_extension")

package(
    default_applicable_licenses = [],
    default_visibility = ["//jax:mosaic_gpu_users"],
)

py_library(
    name = "mosaic_gpu",
    data = [":libmosaic_gpu_runtime.so"],
    deps = [":_mosaic_gpu_ext"],
)

cc_library(
    name = "mosaic_gpu_support",
    deps = [
        ":custom_call",
        ":runtime",
    ],
)

cc_library(
    name = "target",
    srcs = ["target.cc"],
    hdrs = ["target.h"],
    deps = [
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
        "@com_google_absl//absl/strings:str_format",
        "@llvm-project//llvm:MC",
    ],
)

cc_library(
    name = "passes",
    srcs = [
        "launch_lowering.cc",
        "passes.cc",
        "serde.cc",
    ],
    hdrs = [
        "launch_lowering.h",
        "passes.h",
        "serde.h",
    ],
    deps = [
        "//jaxlib/mosaic:pass_boilerplate",
        "//jaxlib/mosaic:serde",
        "@llvm-project//llvm:Support",
        "@llvm-project//mlir:DataLayoutInterfaces",
        "@llvm-project//mlir:FuncDialect",
        "@llvm-project//mlir:GPUDialect",
        "@llvm-project//mlir:GPUToGPURuntimeTransforms",
        "@llvm-project//mlir:IR",
        "@llvm-project//mlir:LLVMCommonConversion",
        "@llvm-project//mlir:LLVMDialect",
        "@llvm-project//mlir:Pass",
        "@llvm-project//mlir:Support",
        "@llvm-project//mlir:TransformUtils",
        "@llvm-project//mlir:VectorDialect",
    ],
)

CAPI_SOURCES = [
    "integrations/c/passes.cc",
]

CAPI_HEADERS = [
    "integrations/c/passes.h",
]

cc_library(
    name = "mlir_capi",
    srcs = CAPI_SOURCES,
    hdrs = CAPI_HEADERS,
    deps = [
        ":passes",
        "@llvm-project//mlir:CAPIIRHeaders",
    ],
)

# Header-only target, used when using the C API from a separate shared library.
cc_library(
    name = "mlir_capi_headers",
    hdrs = CAPI_HEADERS,
    deps = [
        "@llvm-project//mlir:CAPIIRHeaders",
    ],
)

# Alwayslink target, used when exporting the C API from a shared library.
cc_library(
    name = "mlir_capi_objects",
    srcs = CAPI_SOURCES,
    hdrs = CAPI_HEADERS,
    deps = [
        ":passes",
        "@llvm-project//mlir:CAPIIRObjects",
    ],
    alwayslink = True,
)

cc_library(
    name = "runtime",
    srcs = ["runtime.cc"],
    # Linker may prune these symbols if they are not explicitly exported.
    linkopts = [
        "-Wl,--export-dynamic-symbol='mosaic_gpu_*'",
        "-Wl,--export-dynamic-symbol='nvshmem_my_pe'",
        "-Wl,--export-dynamic-symbol='nvshmem_ptr'",
        "-Wl,--export-dynamic-symbol='nvshmemx_barrier_all_on_stream'",
        "-Wl,--export-dynamic-symbol='nvshmemx_cumodule_init'",
        "-Wl,--export-dynamic-symbol='nvshmemx_init_status'",
    ],
    deps = [
        ":nvshmem",
        "@local_config_cuda//cuda:cuda_headers",
    ],
    alwayslink = True,
)

cc_library(
    name = "nvshmem",
    hdrs = ["nvshmem.h"],
    deps = [
        "@local_config_cuda//cuda:cuda_headers",
        "@xla//xla/tsl/cuda:cudart",
    ],
)

cc_library(
    name = "custom_call",
    srcs = ["custom_call.cc"],
    deps = [
        ":library_paths",
        ":nvshmem",
        ":passes",
        ":target",
        "//jaxlib/cuda:cuda_vendor",
        "//jaxlib/mosaic/dialect/gpu:mosaic_gpu",
        "@com_google_absl//absl/base",
        "@com_google_absl//absl/base:core_headers",
        "@com_google_absl//absl/cleanup",
        "@com_google_absl//absl/container:flat_hash_map",
        "@com_google_absl//absl/container:flat_hash_set",
        "@com_google_absl//absl/log:check",
        "@com_google_absl//absl/numeric:bits",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
        "@com_google_absl//absl/strings:str_format",
        "@com_google_absl//absl/synchronization",
        "@llvm-project//llvm:Support",
        "@llvm-project//mlir:ArithDialect",
        "@llvm-project//mlir:ArithToLLVM",
        "@llvm-project//mlir:ArithTransforms",
        "@llvm-project//mlir:BuiltinToLLVMIRTranslation",
        "@llvm-project//mlir:ComplexToLLVM",
        "@llvm-project//mlir:ControlFlowDialect",
        "@llvm-project//mlir:ControlFlowToLLVM",
        "@llvm-project//mlir:ConversionPasses",
        "@llvm-project//mlir:ExecutionEngine",
        "@llvm-project//mlir:ExecutionEngineUtils",
        "@llvm-project//mlir:FuncDialect",
        "@llvm-project//mlir:FuncToLLVM",
        "@llvm-project//mlir:GPUDialect",
        "@llvm-project//mlir:GPUToLLVMIRTranslation",
        "@llvm-project//mlir:GPUTransforms",
        "@llvm-project//mlir:IR",
        "@llvm-project//mlir:IndexToLLVM",
        "@llvm-project//mlir:LLVMDialect",
        "@llvm-project//mlir:LLVMIRTransforms",
        "@llvm-project//mlir:LLVMToLLVMIRTranslation",
        "@llvm-project//mlir:MathDialect",
        "@llvm-project//mlir:MathToLLVM",
        "@llvm-project//mlir:MemRefDialect",
        "@llvm-project//mlir:MemRefToLLVM",
        "@llvm-project//mlir:MemRefTransforms",
        "@llvm-project//mlir:NVGPUDialect",
        "@llvm-project//mlir:NVVMDialect",
        "@llvm-project//mlir:NVVMTarget",
        "@llvm-project//mlir:NVVMToLLVM",
        "@llvm-project//mlir:NVVMToLLVMIRTranslation",
        "@llvm-project//mlir:Parser",
        "@llvm-project//mlir:Pass",
        "@llvm-project//mlir:SCFDialect",
        "@llvm-project//mlir:Support",
        "@llvm-project//mlir:Transforms",
        "@llvm-project//mlir:UBToLLVM",
        "@llvm-project//mlir:VectorDialect",
        "@llvm-project//mlir:VectorToLLVM",
        "@tsl//tsl/profiler/lib:traceme",
        "@xla//xla/ffi",
        "@xla//xla/ffi:ffi_api",
        "@xla//xla/service:custom_call_status",
        "@xla//xla/service:custom_call_target_registry",
    ],
    alwayslink = True,
)

nanobind_extension(
    name = "_mosaic_gpu_ext",
    srcs = ["mosaic_gpu_ext.cc"],
    copts = [
        "-fexceptions",
        "-fno-strict-aliasing",
    ],
    deps = [
        "//jaxlib:kernel_nanobind_helpers",
        "//jaxlib/cuda:cuda_vendor",
        "@com_google_absl//absl/cleanup",
        "@com_google_absl//absl/strings",
        "@nanobind",
        "@xla//xla/ffi/api:c_api",
        "@xla//xla/ffi/api:ffi",
        "@xla//xla/tsl/cuda:cudart",
    ],
)

cc_binary(
    name = "libmosaic_gpu_runtime.so",
    srcs = ["runtime.cc"],
    copts = ["-fvisibility=default"],
    linkshared = 1,
    tags = [
        "manual",
        "notap",
    ],
    deps = [
        ":nvshmem",
        "@local_config_cuda//cuda:cuda_headers",
        "@xla//xla/tsl/cuda:cudart",
    ],
)

cc_library(
    name = "library_paths",
    hdrs = ["library_paths.h"],
)
