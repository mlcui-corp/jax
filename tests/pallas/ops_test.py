# Copyright 2023 The JAX Authors.
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

from collections.abc import Sequence
import functools
import itertools
import math
import sys
from typing import Any
from collections.abc import Callable
import unittest

from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import api_util
from jax import lax
from jax import random
from jax._src import dtypes
from jax._src import linear_util as lu
from jax._src import state
from jax._src import test_util as jtu
from jax._src.pallas import pallas_call
from jax.experimental import pallas as pl
from jax.interpreters import partial_eval as pe
import jax.numpy as jnp
import numpy as np

if sys.platform != "win32":
  try:
    from jax.experimental.pallas import mosaic_gpu as plgpu_mgpu
  except ImportError:
    plgpu_mgpu = None
  from jax.experimental.pallas import triton as plgpu_triton
  from jax.experimental.pallas import tpu as pltpu
else:
  plgpu_mgpu = None
  plgpu_triton = None
  pltpu = None

import hypothesis as hp
import hypothesis.extra.numpy as hnp
import hypothesis.strategies as hps


# There are many inherited redefinitions of _
# ruff: noqa: F811

jax.config.parse_flags_with_absl()
jtu.setup_hypothesis(max_examples=50)

use_mosaic_gpu = pallas_call._PALLAS_USE_MOSAIC_GPU.value

intx = dtypes.canonicalize_dtype(jnp.int64)
floatx = dtypes.canonicalize_dtype(jnp.float64)

def wrap_init(f: Callable, nr_args: int):
  # wrapper for lu.wrap_init with debugging info
  return lu.wrap_init(
      f,
      debug_info=api_util.debug_info("state_test", f, (0,) * nr_args, {}))

def is_power_of_two(n: int) -> bool:
  return (n > 0) and (n & (n - 1) == 0)


def smem_on_tpu():
  if jtu.test_device_matches(["tpu"]):
    return pltpu.SMEM
  else:
    return None


def _random_value(key: jax.Array, shape_dtype: jax.ShapeDtypeStruct
                  ) -> jax.Array:
  if jnp.issubdtype(shape_dtype.dtype, jnp.floating):
    return random.normal(key, shape_dtype.shape, dtype=shape_dtype.dtype)
  elif jnp.issubdtype(shape_dtype.dtype, jnp.integer):
    return random.randint(
        key, shape_dtype.shape, minval=-4, maxval=4, dtype=shape_dtype.dtype
    )
  raise NotImplementedError(shape_dtype)


_DTYPES_32BIT = (
    "float32",
    "int32",
    "uint32",
)
# TODO(apaszke): Add 8-bit floats.
_DTYPES_SUB_32BIT = (
    "bfloat16",
    "float16",
    "int16",
    "int8",
    "int4",
    "int2",
    "uint16",
    "uint8",
    "uint4",
    "uint2",
    "bool",
    "float8_e4m3b11fnuz",
    "float8_e5m2",
    "float8_e4m3fn",
)
_DTYPES = (*_DTYPES_32BIT, *_DTYPES_SUB_32BIT)


@hps.composite
def make_shape_dtype_strategy(
    draw, *,
    min_rank: int,
    max_rank: int,
    min_size_exp: int,
    max_size_exp: int,
    valid_dtypes: Sequence[jnp.dtype],
    max_bytes: int = 2**16,
) -> jax.ShapeDtypeStruct:
  dtype = draw(hps.sampled_from(valid_dtypes))
  # To generate shapes with power-of-two sizes, we draw the exponents of the
  # sizes, and then generate the sizes from the exponents.
  shape_exponents = tuple(
      draw(hps.lists(
          hps.integers(min_value=min_size_exp, max_value=max_size_exp),
          min_size=min_rank, max_size=max_rank))
  )
  shape = tuple(2**exp for exp in shape_exponents)
  size = np.prod(shape) * dtype.itemsize
  hp.assume(size <= max_bytes)  # Make sure we don't take more than 4K VMEM
  return jax.ShapeDtypeStruct(shape, dtype)


@hps.composite
def arrays(
    draw, shape: tuple[int, ...], dtype: np.dtype,
    *, elements: hps.SearchStrategy[Any] | None = None,
) -> np.ndarray:
  cast_to_bf16 = False
  if dtype == np.dtype(jnp.bfloat16):
    dtype = np.dtype('float32')
    cast_to_bf16 = True
  arr = draw(hnp.arrays(shape=shape, dtype=dtype, elements=elements))
  if cast_to_bf16:
    arr = arr.astype(np.dtype(jnp.bfloat16))
  return arr


@hps.composite
def select_n_strategy(
    draw, *, max_cases: int = 4,
    min_rank: int = 0, max_rank: int = 2,
    min_size_exp: int = 0, max_size_exp: int = 8,
) -> tuple[np.ndarray, ...]:
  n_cases = draw(hps.integers(min_value=1, max_value=max_cases))
  case_shape_dtype = draw(
      make_shape_dtype_strategy(
          min_rank=min_rank, max_rank=max_rank,
          min_size_exp=min_size_exp, max_size_exp=max_size_exp,
          valid_dtypes=[
              np.dtype("int32"),
              np.dtype("float32"),
              # TODO(sharadmv,apaszke): enable bf16
              # np.dtype(jnp.bfloat16),
          ],
      )
  )
  allowed_elements = hps.integers(min_value=0, max_value=n_cases - 1)
  pred_shape = draw(hps.sampled_from([(), case_shape_dtype.shape]))
  # TODO(sharadmv,apaszke): enable passing bool arrays into Pallas kernels
  if n_cases == 2 and not pred_shape:
    pred_dtype = draw(hps.sampled_from([np.dtype(np.bool_),
                                        np.dtype(np.int32)]))
    allowed_elements = hps.booleans()
  else:
    pred_dtype = np.int32
  pred = draw(arrays(shape=pred_shape, dtype=pred_dtype,
                    elements=allowed_elements))
  cases = (
      draw(
          arrays(shape=case_shape_dtype.shape, dtype=case_shape_dtype.dtype)
      )
      for _ in range(n_cases)
  )
  return pred, *cases


UNARY_PRIMITIVES = [
    # TODO(sharadmv,apaszke): enable zero rank
    # TODO(sharadmv,apaszke): enable one rank
    # TODO(sharadmv,apaszke): enable zero dim sizes
    # TODO(sharadmv,apaszke): enable one dim sizes
    (
        lax.neg_p, {},
        make_shape_dtype_strategy(
            min_rank=2,
            max_rank=3,
            min_size_exp=1,
            max_size_exp=6,
            valid_dtypes=[jnp.dtype("float32"), jnp.dtype("int32")],
        ),
    ),
    (
        lax.not_p, {},
        make_shape_dtype_strategy(
            min_rank=2,
            max_rank=3,
            min_size_exp=1,
            max_size_exp=6,
            valid_dtypes=[jnp.dtype("int32")],
        ),
    ),
    *[
        (
            prim,
            params,
            make_shape_dtype_strategy(
                min_rank=2,
                max_rank=3,
                min_size_exp=1,
                max_size_exp=6,
                valid_dtypes=[jnp.dtype("float32")],
            ),
        )
        for prim, params in [
            (lax.abs_p, {}),
            (lax.exp_p, {"accuracy": None}),
            (lax.tanh_p, {"accuracy": None}),
            (lax.logistic_p, {"accuracy": None}),
            (lax.rsqrt_p, {"accuracy": None}),
            (lax.log_p, {"accuracy": None}),
            (lax.exp2_p, {"accuracy": None}),
            (lax.log1p_p, {"accuracy": None}),
            (lax.sin_p, {"accuracy": None}),
            (lax.sqrt_p, {"accuracy": None}),
        ]
    ],
]

UNARY_FUNCTIONS = [
    (prim.name, functools.partial(prim.bind, **params), strategy) for prim, params, strategy in UNARY_PRIMITIVES
] + [
    (
        name,
        func,
        make_shape_dtype_strategy(
            min_rank=2,
            max_rank=3,
            min_size_exp=1,
            max_size_exp=6,
            valid_dtypes=[jnp.dtype("float32")],
        ),
    )
    for name, func in [
        ("relu", jax.nn.relu),
        ("pow2", lambda x: jnp.power(2, x)),
        ("square", jnp.square),
        ("reciprocal", jnp.reciprocal),
        ("round", jnp.round),
        ("rint", jnp.rint),
    ]
]


class PallasBaseTest(jtu.JaxTestCase):
  INTERPRET = False

  def setUp(self):
    if not self.INTERPRET:
      if jtu.device_under_test() == "cpu":
        self.skipTest("Only interpret mode supported on CPU")
      if (jtu.test_device_matches(["cuda"]) and
          not jtu.is_cuda_compute_capability_at_least("8.0")):
        self.skipTest("Only works on GPUs with capability >= sm80")
      if (jtu.test_device_matches(["cuda"]) and use_mosaic_gpu and
          not jtu.is_cuda_compute_capability_at_least("9.0")):
        self.skipTest("Mosaic GPU requires capability >= sm90")

    super().setUp()

  @classmethod
  def pallas_call(cls, *args, **kwargs):
    if jtu.test_device_matches(["cuda"]) and use_mosaic_gpu:
      assert plgpu_mgpu is not None
      compiler_params = plgpu_mgpu.CompilerParams(
          lowering_semantics=plgpu_mgpu.LoweringSemantics.Warpgroup
      )
      kwargs["compiler_params"] = compiler_params

    return pl.pallas_call(*args, interpret=cls.INTERPRET, **kwargs)

  def skip_if_mosaic_gpu(self):
    if jtu.test_device_matches(["gpu"]) and use_mosaic_gpu:
      self.skipTest("TODO: Mosaic GPU does not support this yet")


@jtu.thread_unsafe_test_class()  # hypothesis is not thread safe
class OpsTest(PallasBaseTest):

  @parameterized.named_parameters(
      (fn.__name__, fn, dtype) for fn, dtype in [
          (lax.pow, jnp.float32),
          (lax.bitwise_and, jnp.int32),
          (lax.bitwise_or, jnp.int32),
          (lax.bitwise_xor, jnp.int32),
          (lax.shift_left, jnp.int32),
          (lax.shift_right_arithmetic, jnp.int32),
          (lax.shift_right_logical, jnp.int32),
      ]
  )
  def test_weak_dtype(self, fn, dtype):
    self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((8, 128), dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[...] = fn(x_ref[...], y_ref[...])

    x = jnp.full((8, 128), 4, dtype=dtype)
    y = jnp.full((8, 128), 2 if jnp.issubdtype(dtype, jnp.integer) else 2.0,
                dtype=dtype)
    np.testing.assert_allclose(kernel(x, y), fn(x, y))

  @parameterized.named_parameters(
      ('integer_1_1', (1, 1)),
      ('integer_1_16', (1, 16)),
      ('integer_16_1', (16, 1)),
      ('integer_-1_1', (-1, 1)),
      ('integer_1_-1', (1, -1)),
      ('float_1_1', (1.0, 1.0)),
      ('float_1_16', (1.0, 16.0)),
      ('float_16_1', (16.0, 1.0)),
      ('float_-1_1', (-1.0, 1.0)),
      ('float_1_-1', (1.0, -1.0)),
      ('float_1_inf', (1.0, float('inf'))),
      ('float_inf_1', (float('inf'), 1.0)),
      ('float_inf_inf', (float('inf'), float('inf'))),
      ('float_1_nan', (1.0, float('nan'))),
      ('float_nan_1', (float('nan'), 1.0)),
      ('float_nan_nan', (float('nan'), float('nan'))),
      ('float_inf_nan', (float('inf'), float('nan'))),
      ('float_nan_inf', (float('inf'), float('inf'))),
  )
  def test_scalar_compare(self, params):
    """Test some scalar compares.

    We don't really expect that the results would be wrong, but rather we want
    to exercise the lowering rules.
    """
    self.skip_if_mosaic_gpu()

    def kernel(x_ref, y_ref, o_ref):
      x = x_ref[0, 0]
      y = y_ref[0, 0]
      o_ref[0, 0] = jax.lax.select(x == y, 1, 0)
      o_ref[0, 1] = jax.lax.select(x != y, 1, 0)
      o_ref[0, 2] = jax.lax.select(x < y, 1, 0)
      o_ref[0, 3] = jax.lax.select(x <= y, 1, 0)
      o_ref[0, 4] = jax.lax.select(x > y, 1, 0)
      o_ref[0, 5] = jax.lax.select(x >= y, 1, 0)

    x, y = params
    r = jnp.array(
        [
            [x == y, x != y, x < y, x <= y, x > y, x >= y],
        ],
        jnp.int32,
    )
    x = jnp.array([[x]])
    y = jnp.array([[y]])

    result = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct([1, 128], intx),
        in_specs=[
            pl.BlockSpec(memory_space=smem_on_tpu()),
            pl.BlockSpec(memory_space=smem_on_tpu()),
        ],
        out_specs=pl.BlockSpec(
            (1, 128), lambda i: (0, 0), memory_space=smem_on_tpu()
        ),
        grid=(1,),
    )(x, y)
    np.testing.assert_array_equal(r, result[..., 0:6])

  @parameterized.named_parameters(
      ('integer_1_1', (1, 1)),
      ('integer_1_16', (1, 16)),
      ('integer_16_1', (16, 1)),
      ('integer_-1_1', (-1, 1)),
      ('integer_1_-1', (1, -1)),
      ('float_1_1', (1.0, 1.0)),
      ('float_1_16', (1.0, 16.0)),
      ('float_16_1', (16.0, 1.0)),
      ('float_-1_1', (-1.0, 1.0)),
      ('float_1_-1', (1.0, -1.0)),
      ('float_1_inf', (1.0, float('inf'))),
      ('float_inf_1', (float('inf'), 1.0)),
      ('float_inf_inf', (float('inf'), float('inf'))),
      ('float_1_nan', (1.0, float('nan'))),
      ('float_nan_1', (float('nan'), 1.0)),
      ('float_nan_nan', (float('nan'), float('nan'))),
      ('float_inf_nan', (float('inf'), float('nan'))),
      ('float_nan_inf', (float('inf'), float('inf'))),
  )
  def test_vector_compare(self, params):
    """Test some vector compares.

    We don't really expect that the results would be wrong, but rather we want
    to exercise the lowering rules.
    """
    self.skip_if_mosaic_gpu()

    def kernel(x_ref, y_ref, o_ref):
      x = x_ref[:]
      y = y_ref[:]
      one = jnp.ones([8, 128], dtype=jnp.int32)
      zero = jnp.zeros([8, 128], dtype=jnp.int32)
      o_ref[0] = jax.lax.select(x == y, one, zero)
      o_ref[1] = jax.lax.select(x != y, one, zero)
      o_ref[2] = jax.lax.select(x < y, one, zero)
      o_ref[3] = jax.lax.select(x <= y, one, zero)
      o_ref[4] = jax.lax.select(x > y, one, zero)
      o_ref[5] = jax.lax.select(x >= y, one, zero)

    # Widen out our params to (8, 128) vectors.
    x, y = params
    x = jnp.full([8, 128], x)
    y = jnp.full([8, 128], y)

    r = [x == y, x != y, x < y, x <= y, x > y, x >= y]

    result = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct([6, 8, 128], jnp.int32),
        in_specs=[
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
        ],
        out_specs=pl.BlockSpec((6, 8, 128), lambda *_: (0, 0, 0)),
        grid=(1,),
    )(x, y)
    np.testing.assert_array_equal(r[0], result[0])
    np.testing.assert_array_equal(r[1], result[1])
    np.testing.assert_array_equal(r[2], result[2])
    np.testing.assert_array_equal(r[3], result[3])
    np.testing.assert_array_equal(r[4], result[4])
    np.testing.assert_array_equal(r[5], result[5])

  @parameterized.named_parameters(
      ("reduce_all_true", "all_true", jnp.all, True),
      ("reduce_all_false", "all_false", jnp.all, False),
      ("reduce_all_mixed", "one_false", jnp.all, False),
      ("reduce_any_true", "all_true", jnp.any, True),
      ("reduce_any_false", "all_false", jnp.any, False),
      ("reduce_any_mixed", "one_false", jnp.any, True),
  )
  def test_reduce_boolean(self, input_type, reduction_op, expected_result):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("TODO: error on GPU")

    def kernel(x_ref, ones_ref, o_ref):
      # Convert float to bool with a comparison.
      bool_x = x_ref[...] == ones_ref[...]
      reduced_as_bool = reduction_op(bool_x, keepdims=True)
      # Convert bool to float with a select.
      float_value = jnp.where(reduced_as_bool, 1.0, 0.0)
      o_ref[0, 0] = float_value[0, 0]

    if input_type == "all_true":
      x = jnp.ones((8, 128), dtype=jnp.float32)
    elif input_type == "all_false":
      x = jnp.zeros((8, 128), dtype=jnp.float32)
    elif input_type == "one_false":
      x = jnp.ones((8, 128), dtype=jnp.float32)
      x = x.at[0, 0].set(0.0)
    else:
      raise ValueError(f"Unknown input type: {input_type}")
    ones = jnp.ones_like(x)

    result = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
        ],
        out_specs=pl.BlockSpec(block_shape=(1, 1), memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct([1, 1], floatx),
        grid=(1,),
    )(x, ones)
    np.testing.assert_array_equal(result[0, 0], float(expected_result))

  @parameterized.named_parameters(
      ("sum", jnp.sum,), ("max", jnp.max,), ("min", jnp.min,)
  )
  def test_reduce_float(self, reduction_op):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("TODO: error on GPU")

    def kernel(x_ref, o_ref):
      o_ref[0, 0] = reduction_op(x_ref[...])

    x = jax.random.normal(jax.random.key(0), (8, 128))
    result = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
        ],
        out_specs=pl.BlockSpec((1, 1), memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct([1, 1], floatx),
        grid=(1,),
    )(x)

    np.testing.assert_allclose(result[0, 0], reduction_op(x), atol=1e-5)

  # TODO(sharadmv): test rank < 2, size < 2
  @hp.given(select_n_strategy(max_cases=2, min_rank=2, max_rank=4,
                              min_size_exp=1))
  def test_select_n(self, args):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("TODO: error on GPU, lowering bug for select_n")
    pred, *cases = args
    scalar_pred = not pred.shape

    def kernel(*refs):
      if scalar_pred:
        *case_refs, o_ref = refs
        pred_ = pred
      else:
        pred_ref, *case_refs, o_ref = refs
        pred_ = pred_ref[...]
      vals = [case_ref[...] for case_ref in case_refs]
      o_ref[...] = lax.select_n(pred_, *vals)
    out_ref = lax.select_n(pred, *cases)
    if scalar_pred:
      args = cases
    else:
      args = [pred, *cases]
    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct(out_ref.shape, out_ref.dtype),
    )(*args)
    if out.dtype == jnp.bfloat16:
      out, out_ref = out.astype(jnp.float32), out_ref.astype(jnp.float32)
    np.testing.assert_allclose(out, out_ref)

  @parameterized.named_parameters(
      (name, name, func, strategy)
      for name, func, strategy in UNARY_FUNCTIONS
  )
  @hp.given(hps.data())
  @jtu.skip_if_mosaic_gpu_exceeds_shared_memory(device_patterns="RTX PRO 6000 Blackwell")
  def test_unary_primitives(self, name, func, shape_dtype_strategy, data):
    if name in ["abs", "log1p", "pow2", "reciprocal", "relu", "sin", "sqrt"]:
      self.skip_if_mosaic_gpu()

    if self.INTERPRET:
      self.skipTest("This hypothesis test is slow, even more so in interpret mode.")
    # We want exact equality here to match how JAX lowers to XLA
    tol = 0.
    if jtu.test_device_matches(["tpu"]):
      if name == "exp2":
        tol = 1e-6
    if jtu.test_device_matches(["gpu"]):
      if func == jnp.round or func == jnp.rint:
        self.skipTest("TODO: not implemented on GPU")
      if name == "tanh":
        tol = 1e-6
      elif name == "exp2":
        tol = 1e-6

    def kernel(x_ref, y_ref):
      y_ref[...] = func(x_ref[...])
    x_shape_dtype = data.draw(shape_dtype_strategy)

    sut_is_mosaic_gpu = jtu.test_device_matches(["gpu"]) and use_mosaic_gpu
    if sut_is_mosaic_gpu:
      hp.assume(math.prod(x_shape_dtype.shape) % 128 == 0)
      hp.assume(x_shape_dtype.shape[-1] >= 16)

    key = random.key(0)
    x = _random_value(key, x_shape_dtype)
    out = self.pallas_call(kernel, out_shape=x_shape_dtype)(x)
    self.assertAllClose(out, func(x), atol=tol, rtol=tol)

  @parameterized.product(from_dtype=_DTYPES_32BIT, to_dtype=_DTYPES)
  @hp.given(hps.data())
  @hp.settings(suppress_health_check=[hp.HealthCheck.too_slow])  # ASAN is slow
  def test_cast_from_32bit(self, from_dtype, to_dtype, data):
    sut_is_mosaic_gpu = jtu.test_device_matches(["gpu"]) and use_mosaic_gpu
    if to_dtype in {"float8_e4m3b11fnuz", "float8_e5m2", "float8_e4m3fn"}:
      if not jtu.test_device_matches(["tpu"]):
        self.skipTest("Not supported on this hardware")
      if jtu.get_tpu_version() >= 5 and not jtu.if_cloud_tpu_at_least(
          2025, 3, 8
      ):
        self.skipTest("Test requires libtpu from 2025/3/8 or later")
      if jtu.get_tpu_version() < 5 and not jtu.if_cloud_tpu_at_least(
          2025, 5, 15
      ):
        self.skipTest("Test requires libtpu from 2025/5/15 or later")
    if from_dtype in {"int2", "uint2"} or to_dtype in {"int2", "uint2"}:
      if jtu.test_device_matches(["tpu"]) and not jtu.if_cloud_tpu_at_least(
          2025, 4, 1
      ):
        self.skipTest("Test requires libtpu from 2025/4/1 or later")
    if from_dtype == to_dtype:
      self.skipTest("Unnecessary test")
    if jtu.is_device_tpu(version=4):
      if to_dtype in {"int2", "uint2"}:
        self.skipTest("Not supported on this TPU generation")
      if to_dtype in {"int16", "uint16"} and not jtu.if_cloud_tpu_at_least(2025, 1, 18):
        self.skipTest("Test requires libtpu from 2025/1/18 or later")
      if to_dtype in {
          "int4",
          "uint4",
          "int8",
          "uint8",
      } and not jtu.if_cloud_tpu_at_least(2025, 5, 15):
        self.skipTest("Test requires libtpu from 2025/5/15 or later")
    if jtu.test_device_matches(["tpu"]) and jtu.get_tpu_version() < 4:
      # Currently only casts between 32-bit types and to bf16 are supported.
      if to_dtype not in {"int32", "uint32", "float32", "bfloat16"}:
        self.skipTest("Not supported on this TPU generation")
    if jtu.test_device_matches(["gpu"]) and to_dtype in {
        "int4",
        "uint4",
        "int2",
        "uint2",
    }:
      self.skipTest("sub-byte casts are buggy on GPU")  # b/391292861
    if to_dtype == "float16" and not sut_is_mosaic_gpu:
      self.skipTest("float16 is only supported with Mosaic GPU")
    if sut_is_mosaic_gpu and to_dtype == "bool":
      self.skipTest("Sub-byte types are not yet supported with Mosaic GPU")

    # XLA does not specify the float->int conversion result for NaNs.
    elements = dict(allow_nan=not jnp.issubdtype(to_dtype, jnp.integer))
    shape = (8, 128)
    if to_dtype in {"int2", "uint2"}:
      # Make sure #rows is a least the packing factor of int2.
      # TODO(b/343490729): XLA convert(f32[16, 128]) fails on v5p.
      shape = (32, 128)
    x = data.draw(hnp.arrays(from_dtype, shape, elements=elements))
    x = jnp.asarray(x)
    def kernel(x_ref, y_ref):
      x = x_ref[...]
      y = x.astype(to_dtype)
      if to_dtype == jnp.bool:
        y = y.astype(jnp.int32)
      y_ref[...] = y
    y_dtype = jnp.int32 if to_dtype == jnp.bool else to_dtype
    try:
      y = self.pallas_call(
          kernel, out_shape=jax.ShapeDtypeStruct(x.shape, y_dtype))(x)
    except Exception as e:
      if "Unsupported cast" in e.args[0]:
        self.skipTest("Unsupported cast")
      raise
    if to_dtype == jnp.bool:
      y = y.astype(jnp.bool)
    y_ref = x.astype(to_dtype)
    if jnp.dtype(to_dtype) in map(jnp.dtype, (jnp.bfloat16, jnp.int4, jnp.uint4)):
      y, y_ref = y.astype(np.float32), y_ref.astype(np.float32)
    np.testing.assert_allclose(y, y_ref, atol=0., rtol=0.)

  # Types narrower than 32-bit have few values so we test them exhaustively.
  # We also take one more pass with random data just to ensure that we don't
  # miss bugs that would be hidden due to exhaustive enumeration being in order.
  @parameterized.product(from_dtype=_DTYPES_SUB_32BIT, to_dtype=_DTYPES, randomize=(False, True))
  def test_cast_from_sub_32bit(self, from_dtype, to_dtype, randomize):
    sut_is_mosaic_gpu = jtu.test_device_matches(["gpu"]) and use_mosaic_gpu

    if from_dtype == to_dtype:
      self.skipTest("Unnecessary test")
    if from_dtype in {"int2", "uint2"} or to_dtype in {"int2", "uint2"}:
      if jtu.test_device_matches(["tpu"]) and not jtu.if_cloud_tpu_at_least(
          2025, 4, 1
      ):
        self.skipTest("Test requires libtpu from 2025/4/1 or later")
    if jtu.is_device_tpu(version=4):
      allowed_v4_cats = {("int16", "int32"): (2025, 1, 18)}
      if (
          from_dtype in {"int2", "uint2"} or to_dtype in {"int2", "uint2"}
      ) and (from_dtype, to_dtype) not in allowed_v4_cats:
        self.skipTest("Not supported on this TPU generation")
      if minimum_libtpu_date := allowed_v4_cats.get((from_dtype, to_dtype), None):
        if not jtu.if_cloud_tpu_at_least(*minimum_libtpu_date):
          self.skipTest("Test requires a newer libtpu")
      if to_dtype in {"int16", "uint16"} and not jtu.if_cloud_tpu_at_least(2025, 1, 18):
        self.skipTest("Test requires libtpu from 2025/1/18 or later")
      if (
          to_dtype in {"int4", "uint4", "int8", "uint8"}
          and from_dtype in {"int4", "uint4", "int8", "uint8"}
          and not jtu.if_cloud_tpu_at_least(2025, 5, 15)
      ):
        self.skipTest("Test requires libtpu from 2025/5/15 or later")
    if jtu.test_device_matches(["tpu"]) and jtu.get_tpu_version() < 4:
      self.skipTest("Not supported on this TPU generation")
    if jtu.test_device_matches(["gpu"]) and (
        to_dtype
        in {
            "int4",
            "uint4",
            "int2",
            "uint2",
        }
        or from_dtype in {"int2", "uint2"}
    ):
      self.skipTest("sub-byte casts are buggy on GPU")  # b/391292861
    if from_dtype == "float16" or to_dtype == "float16" and not sut_is_mosaic_gpu:
      self.skipTest("float16 is only supported with Mosaic GPU")
    if sut_is_mosaic_gpu:
      unsupported_types = {"bool", "int4", "uint4", "int2", "uint2"}
      if to_dtype in unsupported_types or from_dtype in unsupported_types:
        self.skipTest("Sub-byte types are not yet supported with Mosaic GPU")
      if not randomize:
        # TODO(bchetioui): rework the test shapes to make this work.
        self.skipTest("Exhaustive tests may run out of SMEM with Mosaic GPU")
    if from_dtype in {
        "float8_e4m3b11fnuz",
        "float8_e5m2",
        "float8_e4m3fn",
    } or to_dtype in {"float8_e4m3b11fnuz", "float8_e5m2", "float8_e4m3fn"}:
      if not jtu.test_device_matches(["tpu"]):
        self.skipTest("Not supported on this hardware")
      if jtu.get_tpu_version() >= 5 and not jtu.if_cloud_tpu_at_least(
          2025, 3, 9
      ):
        self.skipTest("Test requires libtpu from 2025/3/9 or later")
      if jtu.get_tpu_version() < 5 and not jtu.if_cloud_tpu_at_least(
          2025, 5, 15
      ):
        self.skipTest("Test requires libtpu from 2025/5/15 or later")
    if from_dtype in ("uint2", "int2") and to_dtype == "bool":
      self.skipTest(
          "TODO(b/343490729): XLA compare(s2, s2) yields wrong results"
      )
    if not randomize:
      if from_dtype in {"int2", "uint2"}:
        # TODO(b/343490729): XLA doesn't work well with int2.
        # iota 1D is unsupported, and XLA tends to select an unsupported
        # layout too when passing constants created in numpy. Thankfully,
        # there is randomize=True for the test coverage.
        self.skipTest("XLA tends to select an unsupported layout for int2")
      if to_dtype in {"int2", "uint2"}:
        self.skipTest(
            "TODO(b/401624977): Mask on int2 is not yet supported in Mosaic"
        )

    from_int = np.issubdtype(np.dtype(from_dtype), np.integer)
    to_int = np.issubdtype(np.dtype(to_dtype), np.integer)
    if (
        from_int and to_int and np.dtype(from_dtype).itemsize != 4
        and not jtu.if_cloud_tpu_at_least(2025, 1, 12)
    ):
      self.skipTest("trunc from non-32 bit only implemented recently")

    # TODO(sharadmv,apaszke): add support for the following casts
    if from_dtype == "bool" and to_dtype in {
        "int16",
        "int8",
        "int4",
        "uint16",
        "uint8",
        "uint4",
        "int2",
        "uint2",
    }:
      self.skipTest("Not supported: cannot extend to sub-32 bit types")

    def bitwidth(dtype):
      if jnp.issubdtype(dtype, jnp.integer):
        return jnp.iinfo(dtype).bits
      elif jnp.issubdtype(dtype, jnp.floating):
        return jnp.finfo(dtype).bits
      else:
        raise ValueError(f"Unsupported dtype: {dtype}")

    if from_dtype != "bool":
      from_bitwidth = bitwidth(from_dtype)
      from_int_dtype = getattr(jnp, "uint" + str(from_bitwidth))
      if randomize:
        # randint has no support for 4 bit integers.
        shape = (128, 128)
        rand_int_dtype = getattr(jnp, "uint" + str(max(8, from_bitwidth)))
        x = random.randint(
            random.key(1234), shape, 0, 1 << from_bitwidth, rand_int_dtype
        ).astype(from_int_dtype)
        x = lax.bitcast_convert_type(x, from_dtype)
      else:
        x = jax.lax.bitcast_convert_type(
            jnp.arange(1 << from_bitwidth, dtype=from_int_dtype), from_dtype
        )
        if sut_is_mosaic_gpu:
          # TMA loads only support max 256 elements per dimension, so we make
          # sure that all the dimensions don't exceed that.
          if x.shape[0] > 256:
            x = x.reshape(256, -1)
        else:
          x = x.reshape(8, -1)
    else:
      if randomize:
        x = random.randint(random.key(234), (16, 16), 0, 1, jnp.int32) != 0
      else:
        x = jnp.tile(
            jnp.asarray([[False, True], [True, False]], dtype="bool"), (8, 8)
        )
    assert x.dtype == jnp.dtype(from_dtype)
    # XLA does not specify the float->int conversion result for NaNs.
    if jnp.issubdtype(from_dtype, jnp.floating):
      x = x.at[jnp.isnan(x)].set(0)
    if from_dtype == jnp.bool:
      x = x.astype(jnp.int32)
    def kernel(x_ref, y_ref):
      x = x_ref[...]
      if from_dtype == jnp.bool:
        x = x.astype(jnp.bool)
      y = x.astype(to_dtype)
      if to_dtype == jnp.bool:
        y = y.astype(jnp.int32)
      y_ref[...] = y
    y_dtype = jnp.int32 if to_dtype == jnp.bool else to_dtype
    try:
      y = self.pallas_call(
          kernel, out_shape=jax.ShapeDtypeStruct(x.shape, y_dtype))(x)
    except Exception as e:
      if "Unsupported cast" in e.args[0]:
        self.skipTest("Unsupported cast")
      raise
    if to_dtype == jnp.bool:
      y = y.astype(jnp.bool)
    y_ref = x.astype(to_dtype)
    if jnp.dtype(to_dtype) in map(
        jnp.dtype,
        (jnp.bfloat16, jnp.int4, jnp.uint4, dtypes.int2, dtypes.uint2),
    ):
      y, y_ref = y.astype(np.float32), y_ref.astype(np.float32)
    np.testing.assert_allclose(y, y_ref, atol=0., rtol=0.)

  @parameterized.parameters(
      jnp.bfloat16,
      jnp.float8_e5m2,
      jnp.float8_e4m3fn,
  )
  @jtu.skip_on_devices("gpu")
  def test_scalar_downcast_float32(self, dtype):

    def kernel(x_ref, o_ref):
      o_ref[0, 0] = x_ref[:][0, 0].astype(dtype)

    x = jax.random.normal(jax.random.key(0), (8, 128), dtype=jnp.float32)
    result = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec((8, 128), lambda *_: (0, 0)),
        ],
        out_specs=pl.BlockSpec((1, 1), memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct([1, 1], dtype),
        grid=(1,),
    )(x)

    np.testing.assert_array_equal(result[0, 0], x[0, 0].astype(dtype))

  @parameterized.product(
      shape=((64,), (8, 8)),
      dtype=(jnp.int32, jnp.int16, jnp.int8),
  )
  def test_scalar_map(self, shape, dtype):
    self.skip_if_mosaic_gpu()
    if pltpu is None:
      self.skipTest("No TPU module available.")
    if dtype != jnp.int32 and len(shape) < 2:
      # TODO(b/299280718): Implement this.
      self.skipTest(
          "Loads and stores not implemented for 1D arrays of non-32bit types"
      )
    def kernel(x_ref, y_ref):
      for idx in np.ndindex(shape):
        x = x_ref[idx].astype(jnp.int32)
        y_ref[idx] = (x * x).astype(y_ref.dtype)
    f = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.SMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=jax.ShapeDtypeStruct(shape, dtype),
    )
    x = jnp.arange(np.prod(shape), dtype=dtype).reshape(shape)
    self.assertAllClose(f(x), x * x)

  @jtu.skip_on_devices("gpu")  # TODO: not implemented
  def test_extract_scalar(self):
    if pltpu is None:
      self.skipTest("No TPU module available.")
    def kernel(x_ref, y_ref):
      y_ref[0, 0] = x_ref[:][0, 0]
    f = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((1, 1), jnp.float32),
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
    )
    x = np.arange(1024, dtype=jnp.float32).reshape(8, 128) + 10
    self.assertAllClose(f(x).item(), 10.0)

  def test_concat_constant(self):
    self.skip_if_mosaic_gpu()
    if pltpu is None:
      self.skipTest("No TPU module available.")
    axis = 0
    num_arrays = 16
    if jtu.test_device_matches(["gpu"]) and not self.INTERPRET:
      # Triton only supports concatenation along the last dimension.
      num_arrays = 2
      axis = -1
    def kernel(out):
      result = []
      for i in range(num_arrays):
        result.append(jnp.full((1, 128), i, jnp.float32))
      out[:] = jnp.stack(result, axis=axis).reshape(num_arrays, 128)

    def run(interpret=False):
      return pl.pallas_call(
          kernel,
          out_shape=jax.ShapeDtypeStruct((num_arrays, 128), jnp.float32),
          out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
          interpret=interpret,
      )()
    expected = run(True)
    if not self.INTERPRET:
      actual = run(False)
      self.assertAllClose(actual, expected)

  @parameterized.named_parameters(
      (f"{dtype.__name__}_{value}", dtype, value)
      for dtypes, values in (
          ((jnp.uint16, jnp.uint32, jnp.uint64), (0, 5)),
          ((jnp.int16, jnp.int32, jnp.int64), (-3, 0, 5)),
          (
              (jnp.bfloat16, jnp.float16, jnp.float32, jnp.float64),
              (-3.2, -0., 0., 5.1, jnp.nan, jnp.inf, -jnp.inf),
          ),
      )
      for dtype in dtypes
      for value in values
  )
  def test_sign(self, dtype, value):
    self.skip_if_mosaic_gpu()

    if not jax.config.x64_enabled and jnp.dtype(dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    if jtu.test_device_matches(["tpu"]) and jnp.dtype(dtype).itemsize == 2:
      self.skipTest("16-bit types are not supported on TPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 128), dtype),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = jnp.sign(x_ref[...])

    x = jnp.full((8, 128,), value, dtype=dtype)
    out = kernel(x)
    expected = jnp.sign(x)

    # `.astype(jnp.float32)` is a workaround for dtype=bfloat16 and value=nan,
    # see https://github.com/jax-ml/ml_dtypes/issues/206
    np.testing.assert_array_equal(
        out.astype(jnp.float32),
        expected.astype(jnp.float32),
    )

  # TODO(twsung): Add more types once lowering is implemented.
  @parameterized.parameters(
      jnp.float32,
      jnp.bfloat16,
      jnp.int32,
  )
  def test_add_constant(self, dtype):
    self.skip_if_mosaic_gpu()

    shape = (256, 256)

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(shape, dtype),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = x_ref[...] + 1

    np.testing.assert_array_equal(
        kernel(jnp.zeros(shape, dtype=dtype)),
        jnp.ones(shape, dtype=dtype),
    )

  @parameterized.parameters(
      -3.2, -1.0, -0.999517, -0.4, 0., 0.72, 0.999517, 1.0, 2.4,
  )
  def test_erf_inv(self, value):
    self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 128), floatx),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = lax.erf_inv(x_ref[...])

    x = jnp.full((8, 128), value, dtype=floatx)
    out = kernel(x)
    expected = lax.erf_inv(x)
    np.testing.assert_array_equal(out, expected)

  IS_FINITE_TEST_VALUES = [
      -0.2, jnp.inf, -jnp.inf, jnp.nan, 0.0, 1.0, -1.0, 0.5,
  ]

  def test_is_finite(self):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("Not supported on GPU")

    size = len(self.IS_FINITE_TEST_VALUES)

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((size,), jnp.bool_),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = lax.is_finite(x_ref[...])

    x = jnp.array(self.IS_FINITE_TEST_VALUES, dtype=jnp.float32)
    out = kernel(x)
    expected = lax.is_finite(x)
    self.assertArraysEqual(out, expected)

  def test_is_finite_scalar(self):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("Not supported on GPU")

    size = len(self.IS_FINITE_TEST_VALUES)

    @functools.partial(
        self.pallas_call,
        in_specs=(pl.BlockSpec(memory_space=smem_on_tpu()),),
        out_specs=pl.BlockSpec(memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct((size,), jnp.bool_),
    )
    def kernel(x_ref, o_ref):
      for i in range(8):
        o_ref[i] = jnp.isfinite(x_ref[i])

    x = jnp.array(self.IS_FINITE_TEST_VALUES, dtype=jnp.float32)
    out = kernel(x)
    expected = lax.is_finite(x)
    self.assertArraysEqual(out, expected)

  ELEMENTWISE_OPS = [
      (
          [jnp.abs, jnp.negative],
          [
              "int16",
              "int32",
              "int64",
              "bfloat16",
              "float16",
              "float32",
              "float64",
          ],
      ),
      ([jnp.ceil, jnp.floor], ["bfloat16", "float32", "float64", "int32"]),
      (
          [jnp.exp, jnp.exp2, jnp.sin, jnp.cos, jnp.log, jnp.sqrt],
          ["bfloat16", "float16", "float32", "float64"],
      ),
      (
          # fmt: off
          [jnp.expm1, jnp.log1p, jnp.cbrt, lax.rsqrt, jnp.tan, jnp.asin,
          jnp.acos, jnp.atan, jnp.sinh, jnp.cosh, jnp.tanh, jnp.asinh,
          jnp.acosh, jnp.atanh],
          # fmt: on
          ["bfloat16", "float32", "float64"],
      ),
      ([lax.population_count, lax.clz, jnp.invert], ["int32", "int64"]),
      ([jnp.logical_not], ["bool"]),
  ]

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype}", fn, dtype)
      for args in ELEMENTWISE_OPS
      for fn, dtype in itertools.product(*args)
  )
  def test_elementwise(self, fn, dtype):
    self.skip_if_mosaic_gpu()

    if not jax.config.x64_enabled and jnp.dtype(dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    if jtu.test_device_matches(["tpu"]):
      if dtype in ("int16", "float16"):
        self.skipTest("int16 and float16 are not supported on TPU")
      if (
          fn in (jnp.ceil, jnp.floor, jnp.negative, jnp.exp, jnp.exp2, jnp.log,
                jnp.sqrt, lax.rsqrt)
          and dtype == "bfloat16"
          and not jtu.is_device_tpu_at_least(6)
      ):
        self.skipTest(f"bfloat16 {fn.__name__} is only supported on TPU v6+")
      if (
          fn in (jnp.sin, jnp.cos, jnp.tan, jnp.tanh, jnp.log1p)
          and dtype == "bfloat16"
      ):
        self.skipTest(f"bfloat16 {fn.__name__} is not supported on TPU")
      # TODO(b/370578663): implement these lowerings on TPU
      if fn in (
          jnp.acos, jnp.acosh, jnp.asin, jnp.asinh, jnp.atan, jnp.atanh,
          jnp.cbrt, jnp.cosh, jnp.expm1, jnp.sinh,
      ):
        self.skipTest(f"{fn.__name__} not implemented on TPU")
      # TODO(apaszke): Remove after 12 weeks have passed.
      if not jtu.if_cloud_tpu_at_least(2024, 12, 19):
        self.skipTest("Requires libtpu built at least on 2024-12-19")
      if fn == jnp.exp2 and dtype == "bfloat16" and not jtu.if_cloud_tpu_at_least(2025, 1, 31):
        self.skipTest("Test requires newer libtpu")

    if (
        jtu.test_device_matches(["gpu"])
        and fn
        in (jnp.ceil, jnp.floor, jnp.expm1, jnp.log1p, jnp.cbrt, lax.rsqrt,
            jnp.tan, jnp.asin, jnp.acos, jnp.atan, jnp.sinh, jnp.cosh, jnp.tanh,
            jnp.asinh, jnp.acosh, jnp.atanh)
        and dtype == "bfloat16"
    ):
      self.skipTest(f"bfloat16 {fn.__name__} is not supported on GPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 128), dtype),
    )
    def kernel(x_ref, o_ref):
      o_ref[:] = fn(x_ref[...])

    # create an array with shape (8, 128)
    if fn in (jnp.exp, jnp.exp2) and dtype == "bfloat16":
      x = jnp.array([0.42, 1.26] * (8 * 128 // 2)).reshape(8, 128).astype(dtype)
      rtol = 2e-3
    else:
      x = jnp.array([0.42, 2.4] * (8 * 128 // 2)).reshape(8, 128).astype(dtype)
      rtol = 1e-6
    self.assertAllClose(kernel(x), fn(x), rtol=rtol)

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype}", fn, dtype)
      for args in ELEMENTWISE_OPS
      for fn, dtype in itertools.product(*args)
  )
  def test_elementwise_scalar(self, fn, dtype):
    self.skip_if_mosaic_gpu()

    if not jax.config.x64_enabled and jnp.dtype(dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    if jtu.test_device_matches(["tpu"]) and jnp.dtype(dtype).itemsize == 2:
      self.skipTest("16-bit types are not supported on TPU")

    if (
        jtu.test_device_matches(["gpu"])
        and fn
        in (jnp.ceil, jnp.floor, jnp.expm1, jnp.log1p, jnp.cbrt, lax.rsqrt,
            jnp.tan, jnp.asin, jnp.acos, jnp.atan, jnp.sinh, jnp.cosh, jnp.tanh,
            jnp.asinh, jnp.acosh, jnp.atanh)
        and dtype == "bfloat16"
    ):
      self.skipTest(f"bfloat16 {fn.__name__} is not supported on GPU")

    if (
        jtu.test_device_matches(["tpu"])
        and fn == lax.population_count
        and not self.INTERPRET
    ):
      self.skipTest(
          "Scalar population count on TPU is only supported in interpret mode"
      )

    # TODO(b/370578663): implement these lowerings on TPU
    if jtu.test_device_matches(["tpu"]) and fn in (
        jnp.acos, jnp.acosh, jnp.asin, jnp.asinh, jnp.atan,
        jnp.atanh, jnp.cbrt, jnp.cosh, jnp.expm1,
        jnp.sinh,
    ):
      self.skipTest(f"{fn.__name__} not implemented on TPU")

    @functools.partial(
        self.pallas_call,
        in_specs=(pl.BlockSpec(memory_space=smem_on_tpu()),),
        out_specs=pl.BlockSpec(memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct((2,), dtype),
    )
    def kernel(x_ref, o_ref):
      o_ref[0] = fn(x_ref[0])
      o_ref[1] = fn(x_ref[1])

    x = jnp.array([0.42, 1.4]).astype(dtype)
    self.assertAllClose(kernel(x), fn(x), rtol=1e-6)

  def test_abs_weak_type(self):
    self.skip_if_mosaic_gpu()

    # see https://github.com/jax-ml/jax/issues/23191
    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((4, 4), floatx),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = jnp.abs(x_ref[...])

    x = jnp.broadcast_to(-3.2, (4, 4))  # sets `weak_type` to `True`
    np.testing.assert_allclose(kernel(x), jnp.abs(x), rtol=1e-6)

  @parameterized.parameters(
      ("float32", "int32"),
      ("float64", "int32"),
      ("float32", "float32"),
      ("float64", "float64"),
  )
  def test_pow(self, x_dtype, y_dtype):
    self.skip_if_mosaic_gpu()

    if not jax.config.x64_enabled and jnp.dtype(x_dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((4,), x_dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[:] = lax.pow(x_ref[...], y_ref[...])

    if not jax.config.x64_enabled and jnp.dtype(x_dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    x = jnp.array([1, 2, 3, 4]).astype(x_dtype)
    y = jnp.array([1, 2, 3, 4]).astype(y_dtype)
    np.testing.assert_allclose(kernel(x, y), lax.pow(x, y))

  @parameterized.parameters(0, 1, 2, 3, 4, 5, -1, -2, -3)
  def test_integer_pow(self, y):
    self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((4,), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      o_ref[:] = lax.integer_pow(x_ref[...], y)

    x = jnp.array([1, 2, 3, 4]).astype(jnp.float32) / 10
    np.testing.assert_allclose(kernel(x), lax.integer_pow(x, y))

  _NEXTAFTER_VALUES = (-3.2, -0., 0., 5.1, jnp.nan, jnp.inf, -jnp.inf)

  @parameterized.named_parameters(
      (f"{dtype.__name__} ({x=}, {y=})", dtype, x, y)
      for dtype, x, y in itertools.product(
          (jnp.float32, jnp.float64), _NEXTAFTER_VALUES, _NEXTAFTER_VALUES,
      )
  )
  def test_nextafter(self, dtype, x, y):
    self.skip_if_mosaic_gpu()

    if not jax.config.x64_enabled and jnp.dtype(dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((4,), dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[...] = jnp.nextafter(x_ref[...], y_ref[...])

    x = jnp.full((4,), x, dtype=dtype)
    y = jnp.full((4,), y, dtype=dtype)
    out = kernel(x, y)
    expected = jnp.nextafter(x, y)

    # `nextafter` requires exact equality
    self.assertArraysEqual(out, expected)

  COMPARISON_OPS = [
      jnp.equal,
      jnp.not_equal,
      jnp.less,
      jnp.less_equal,
      jnp.greater,
      jnp.greater_equal,
  ]

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype.__name__}", fn, dtype)
      for fn, dtype in itertools.product(
          COMPARISON_OPS,
          (jnp.int32, jnp.uint32, jnp.float16, jnp.float32, jnp.bool_),
      )
  )
  def test_comparison(self, fn, dtype):

    if jtu.test_device_matches(["gpu"]) and dtype == jnp.bool_:
      self.skipTest("Not implemented on GPU.")

    if jtu.test_device_matches(["tpu"]) and dtype == jnp.float16:
      self.skipTest("float16 is not supported on TPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((128,), jnp.int32),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[:] = fn(x_ref[...], y_ref[...]).astype(jnp.int32)

    x = jnp.tile(jnp.array([0, 3, -4, -6, 0, 5, 4, -7]).astype(dtype), 16)
    y = jnp.tile(jnp.array([3, 1, -4, -5, 0, -2, 2, 4]).astype(dtype), 16)
    out = kernel(x, y)
    expected = fn(x, y)
    self.assertArraysEqual(out != 0, expected)

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype.__name__}", fn, dtype)
      for fn, dtype in itertools.product(
          COMPARISON_OPS,
          (jnp.int32, jnp.uint32, jnp.float16, jnp.float32, jnp.bool_),
      )
  )
  def test_comparison_scalar(self, fn, dtype):
    if jtu.test_device_matches(["tpu"]) and dtype == jnp.float16:
      self.skipTest("float16 is not supported on TPU")

    if (
        jtu.test_device_matches(["gpu"])
        and not jtu.is_cuda_compute_capability_at_least("8.0")
    ):
      self.skipTest("Only works on GPUs with capability >= sm80")

    if jtu.test_device_matches(["gpu"]) and dtype == jnp.bool_:
      self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call,
        in_specs=(
            pl.BlockSpec(memory_space=smem_on_tpu()),
            pl.BlockSpec(memory_space=smem_on_tpu()),
        ),
        out_specs=pl.BlockSpec(memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct((128,), jnp.int32),
    )
    def kernel(x_ref, y_ref, o_ref):
      for i in range(128):
        o_ref[i] = fn(x_ref[i], y_ref[i]).astype(jnp.int32)

    x = jnp.tile(jnp.array([0, 3, -4, -6, 0, 5, 4, -7]).astype(dtype), 16)
    y = jnp.tile(jnp.array([3, 1, -4, -5, 0, -2, 2, 4]).astype(dtype), 16)
    out = kernel(x, y)
    expected = fn(x, y)
    self.assertArraysEqual(out != 0, expected)

  def test_isnan(self):
    self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((8,), jnp.bool_),
    )
    def isnan(x_ref, o_ref):
      o_ref[:] = jnp.isnan(x_ref[...])

    x = jnp.arange(8.)
    x = x.at[3].set(jnp.nan)
    np.testing.assert_allclose(isnan(x), jnp.isnan(x))

  def test_jnp_einsum_grad_y_pallas(self):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("This test ooms on gpu")

    x = jnp.arange(128 * 256, dtype=jnp.float32).reshape((128, 256))
    y = jnp.arange(256 * 128, dtype=jnp.float32).reshape((128, 256))

    def kernel(x_ref, y_ref, out_ref):
      # grad_y side of grouped matmul
      out_ref[...] = jnp.einsum('mk,mn->kn', x_ref[...], y_ref[...])

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((256, 256), jnp.float32)
    )(x, y)
    np.testing.assert_array_equal(out, jnp.einsum('mk,mn->kn', x, y))

  @parameterized.product(
      dtype=[jnp.float32, jnp.bfloat16],
  )
  def test_dot_dims_batched_simple_dot_general(self, dtype):
    """This test is only meant to exercise a simple batch lowering of dot_general.

    It is not meant to be a comprehensive test of dot_general lowering, for that
    see pallas_test.py.
    """
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("TPU only test")

    if jtu.test_device_matches(["tpu"]):
      if dtype == jnp.bfloat16:
        if not jtu.if_cloud_tpu_at_least(2025, 7, 25):
          self.skipTest("Requires libtpu built after 2025-07-25")
        if not jtu.is_device_tpu_at_least(version=4):
          self.skipTest("Requires TPUv4+")

    k1, k2 = random.split(jax.random.key(0))
    x = random.normal(k1, (11, 16, 256), dtype=dtype)
    y = random.normal(k2, (11, 256, 128), dtype=dtype)

    def kernel(x_ref, y_ref, out_ref):
      out_ref[...] = jax.lax.dot_general(
          x_ref[...],
          y_ref[...],
          dimension_numbers=(([2], [1]), ([0], [0])),
          preferred_element_type=jnp.float32,
      )

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((11, 16, 128), jnp.float32)
    )(x, y)
    np.testing.assert_allclose(
        out,
        jax.lax.dot_general(
            x,
            y,
            dimension_numbers=(([2], [1]), ([0], [0])),
            preferred_element_type=jnp.float32,
        ),
        rtol=5e-3 if dtype == jnp.bfloat16 else 1e-7,
    )

  @parameterized.product(
      batch_size=(None, 1, 2),
      # dims_numbers is without batch dims
      shapes_and_dims_numbers=(
          # leading lhs non contracting dims.
          ((8, 8, 256), (256, 128), ([2], [0])),
          ((3, 4, 128), (256, 128), ([2], [1])),
          # trailing lhs non contracting dims.
          ((256, 8, 128), (256, 128), ([0], [0])),
          ((128, 8, 128), (256, 128), ([0], [1])),
          # leading rhs non contracting dims.
          ((8, 128), (8, 128, 128), ([1], [2])),
          ((128, 8), (8, 128, 128), ([0], [2])),
          # trailing rhs non contracting dims.
          ((8, 128), (128, 8, 128), ([1], [0])),
          ((128, 8), (128, 8, 128), ([0], [0])),
          # leading lhs and rhs non contracting dims.
          ((8, 8, 128), (8, 128, 128), ([2], [2])),
          # leading lhs and trailing rhs non contracting dims.
          ((8, 8, 128), (128, 8, 128), ([2], [0])),
          # trailing lhs and leading rhs non contracting dims.
          ((32, 8, 128), (8, 128, 32), ([0], [2])),
          # trailing lhs and trailing rhs non contracting dims.
          ((8, 8, 128), (8, 8, 128), ([0], [0])),
      ),
  )
  def test_dot_general_multiple_non_contracting_dims(
      self, batch_size, shapes_and_dims_numbers
  ):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("TPU only test")

    if jtu.test_device_matches(["tpu"]) and not jtu.if_cloud_tpu_at_least(
        2025, 7, 19
    ):
      self.skipTest("Requires libtpu built after 2025-07-19")

    x_shape, y_shape, dims_numbers = shapes_and_dims_numbers
    if batch_size is not None:
      x_shape = (batch_size,) + x_shape
      y_shape = (batch_size,) + y_shape

      # Batch size is always the first dimension so we need to offset
      # dims_numbers by 1.
      def offset_by_one(x):
        return [a + 1 for a in x]

      dims_numbers = (
          (offset_by_one(dims_numbers[0]), offset_by_one(dims_numbers[1])),
          ([0], [0]),
      )
    else:
      dims_numbers = (
          (dims_numbers[0], dims_numbers[1]),
          ([], []),
      )

    k1, k2 = random.split(jax.random.key(0))
    x = jax.random.normal(k1, x_shape, dtype=jnp.float32)
    y = jax.random.normal(k2, y_shape, dtype=jnp.float32)

    # Just infer shape from jax.
    expected = jax.lax.dot_general(x, y, dimension_numbers=dims_numbers)

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(expected.shape, jnp.float32),
    )
    def kernel(x_ref, y_ref, out_ref):
      out_ref[...] = jax.lax.dot_general(
          x_ref[...],
          y_ref[...],
          dimension_numbers=dims_numbers,
      )

    np.testing.assert_allclose(
        kernel(x, y),
        expected,
    )

  @parameterized.parameters(
      ("int32", "float32"),
      ("float32", "float32"),
      ("bfloat16", "bfloat16"),
  )
  def test_true_divide(self, dtype, out_dtype):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      if out_dtype == "bfloat16" and not jtu.is_device_tpu_at_least(6):
        self.skipTest("bfloat16 is not supported on older TPU generations")
      if not jtu.if_cloud_tpu_at_least(2025, 1, 9):
        self.skipTest("Requires libtpu built after 2025-01-09")
    elif jtu.test_device_matches(["gpu"]):
      if dtype == "bfloat16":
        self.skipTest("bfloat16 not supported")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 8), out_dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[...] = jnp.true_divide(x_ref[...], y_ref[...])

    x = jnp.array([1, 3, -4, -6, 2, 5, 4, -7]).astype(dtype)
    y = jnp.array([3, 1, -4, -5, 2, -2, 2, 4]).astype(dtype)
    x = jnp.repeat(x, 8, axis=0).reshape(8, 8)
    y = jnp.tile(y, 8).reshape(8, 8)
    rtol = 8e-3 if dtype == "bfloat16" else 1e-6
    np.testing.assert_allclose(
        jnp.true_divide(x, y).astype(jnp.float32),
        kernel(x, y).astype(jnp.float32),
        rtol=rtol,
    )

  @parameterized.parameters("float16", "bfloat16")
  def test_true_divide_unsupported(self, dtype):
    self.skip_if_mosaic_gpu()

    if self.INTERPRET:
      self.skipTest("No lowering in interpret mode")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[...] = jnp.true_divide(x_ref[...], y_ref[...])

    x = jnp.array([2.4, 4.2]).astype(dtype)
    y = jnp.array([4.2, 2.4]).astype(dtype)
    with self.assertRaises(Exception):
      kernel(x, y)

  BINARY_OPS = [
      ([jnp.floor_divide], ["int32", "uint32"]),
      (
          [jnp.add, jnp.subtract, jnp.multiply],
          ["int16", "int32", "uint32", "float16", "float32"],
      ),
      ([jnp.remainder], ["int32", "uint32", "float32"]),
      (
          # fmt: off
          [jnp.bitwise_and, jnp.bitwise_or, jnp.bitwise_xor,
          jnp.bitwise_left_shift, jnp.bitwise_right_shift],
          # fmt: on
          ["int32", "uint32"],
      ),
  ]

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype}", fn, dtype)
      for args in BINARY_OPS
      for fn, dtype in itertools.product(*args)
  )
  def test_binary(self, f, dtype):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]) and jnp.dtype(dtype).itemsize == 2:
      self.skipTest("16-bit types are not supported on TPU")

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((8,), dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[...] = f(x_ref[...], y_ref[...])

    x = jnp.array([1, 3, -4, -6, 2, 5, 4, -7]).astype(dtype)
    if f == jnp.bitwise_left_shift:
      y = jnp.array([3, 1, 4, 5, 2, 2, 2, 4]).astype(dtype)
    else:
      y = jnp.array([3, 1, -4, -5, 2, -2, 2, 4]).astype(dtype)

    np.testing.assert_allclose(f(x, y), kernel(x, y))

  @parameterized.named_parameters(
      (f"{fn.__name__}_{dtype}", fn, dtype)
      for args in BINARY_OPS
      for fn, dtype in itertools.product(*args)
  )
  def test_binary_scalar(self, f, dtype):
    self.skip_if_mosaic_gpu()

    if not jtu.test_device_matches(["tpu"]):
      self.skipTest("Test only supported on TPU.")
    if jtu.test_device_matches(["tpu"]) and jnp.dtype(dtype).itemsize == 2:
      self.skipTest("16-bit types are not supported on TPU")

    @functools.partial(
        self.pallas_call,
        in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM),
                  pl.BlockSpec(memory_space=pltpu.SMEM),
                  ],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=jax.ShapeDtypeStruct((1,), dtype),
    )
    def kernel(x_ref, y_ref, o_ref):
      o_ref[0] = f(x_ref[0], y_ref[0])

    x = jnp.array([1,]).astype(dtype)
    y = jnp.array([18,]).astype(dtype)

    np.testing.assert_allclose(f(x, y), kernel(x, y))

  @parameterized.parameters(
      ((32,), jnp.int32, 0),
      ((8, 4), jnp.int32, 0),
      ((8, 16), jnp.float32, 1),
      ((8, 16, 2), jnp.int8, 1),
  )
  def test_iota(self, shape, dtype, dimension):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]) and dtype != jnp.int32:
      self.skipTest("Only 32-bit integer iota supported")

    f = lambda: jax.lax.broadcasted_iota(dtype, shape, dimension)

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct(shape, dtype),
    )
    def kernel(o_ref):
      o_ref[...] = f()

    np.testing.assert_allclose(f(), kernel())

  @parameterized.parameters("float16", "bfloat16", "float32")
  def test_approx_tanh(self, dtype):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    if self.INTERPRET:
      self.skipTest("approx_tanh is not supported in interpret mode")

    if (dtype == "bfloat16" and
        not jtu.is_cuda_compute_capability_at_least("9.0")):
      self.skipTest("tanh.approx.bf16 requires a GPU with capability >= sm90")

    @functools.partial(
        self.pallas_call, out_shape=jax.ShapeDtypeStruct((4,), dtype),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = plgpu_triton.approx_tanh(x_ref[...])

    x = jnp.asarray([-1, 0.42, 0.24, 1]).astype(dtype)
    # We upcast to float32 because NumPy <2.0 does not handle custom dtypes
    # properly. See https://github.com/jax-ml/jax/issues/11014.
    np.testing.assert_allclose(
        kernel(x).astype(jnp.float32),
        jnp.tanh(x).astype(jnp.float32),
        atol=5e-3,
        rtol=5e-3,
    )

  def test_elementwise_inline_asm(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented: elementwise_inline_asm_p")

    if self.INTERPRET:
      self.skipTest(
          "elementwise_inline_asm is not supported in interpret mode"
      )

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((256,), jnp.float16),
    )
    def kernel(x_ref, o_ref):
      [o_ref[...]] = plgpu_triton.elementwise_inline_asm(
          "tanh.approx.f16x2 $0, $1;",
          args=[x_ref[...]],
          constraints="=r,r",
          pack=2,
          result_shape_dtypes=[jax.ShapeDtypeStruct(x_ref.shape, x_ref.dtype)],
      )

    x = jnp.arange(256).astype(jnp.float16)
    np.testing.assert_allclose(kernel(x), jnp.tanh(x), atol=5e-3, rtol=5e-3)

  def test_debug_barrier(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented: debug_barrier_p")

    if self.INTERPRET:
      self.skipTest("debug_barrier is not supported in interpret mode")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = x_ref[...]
      plgpu_triton.debug_barrier()

    x = jnp.array([4.2, 2.4]).astype(jnp.float32)
    np.testing.assert_array_equal(kernel(x), x)

  @unittest.skipIf(
      sys.platform == "win32",
      "plgpu_triton.CompilerParams unavailable on Windows",
  )
  def test_debug_print(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Test for TPU is covered in tpu_pallas_test.py")

    # TODO: this test flakes on gpu
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("This test flakes on gpu")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
        compiler_params=plgpu_triton.CompilerParams(
            num_warps=1, num_stages=1
        ),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print("It works!")

    x = jnp.array([4.2, 2.4]).astype(jnp.float32)
    with jtu.capture_stdout() as output:
      jax.block_until_ready(kernel(x))
      jax.effects_barrier()

    self.assertIn("It works!", output())

  @unittest.skipIf(
      sys.platform == "win32",
      "plgpu_triton.CompilerParams unavailable on Windows",
  )
  def test_debug_print_with_values(self):
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Test for TPU is covered in tpu_pallas_test.py")

    # TODO: this test flakes on gpu
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("This test flakes on gpu")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
        compiler_params=plgpu_triton.CompilerParams(
            num_warps=1, num_stages=1
        ),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print("x[0] =", x_ref[0])

    x = jnp.array([4.2, 2.4]).astype(jnp.float32)
    with jtu.capture_stdout() as output:
      jax.block_until_ready(kernel(x))
      jax.effects_barrier()

    self.assertIn("x[0] = 4.2", output())

  @parameterized.parameters(
      ((2, 4), (8,)),
      ((2, 4), (8, 1)),
      ((2, 4), (1, 8)),
      ((64,), (32, 2)),
  )
  def test_reshape(self, in_shape, out_shape):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on TPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(out_shape, jnp.float32),
    )
    def f(x_ref, o_ref):
      o_ref[...] = x_ref[...].reshape(out_shape)

    x = jnp.arange(int(np.prod(in_shape)), dtype=jnp.float32).reshape(in_shape)
    expected = x.reshape(out_shape)
    np.testing.assert_allclose(f(x), expected)

  @parameterized.parameters(
      # fmt: off
      ((), (1,)),
      ((), (1, 1)),
      ((2, 4), (2, 4)),
      ((2, 4), (2, 4, 1)),
      ((2, 4, 1), (2, 4)),
      ((2, 4), (1, 2, 4)),
      ((1, 2, 4), (2, 4)),
      ((2, 4), (2, 1, 4)),
      ((1, 2, 1, 4, 1), (2, 4)),
      ((2, 4,), (1, 2, 1, 4)),
      ((2, 4,), (1, 2, 4, 1)),
      ((1, 2, 4, 1), (1, 2, 1, 4, 1)),
      # fmt: on
  )
  def test_reshape_noop_or_singleton_dims(self, in_shape, out_shape):
    self.skip_if_mosaic_gpu()

    # Unsupported implicit dim change: from "32,{0,0},(2,128),-1" to none
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on TPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(out_shape, jnp.float32),
    )
    def f(x_ref, o_ref):
      o_ref[...] = x_ref[...].reshape(out_shape)

    x = jnp.arange(int(np.prod(in_shape)), dtype=jnp.float32).reshape(in_shape)
    expected = x.reshape(out_shape)
    np.testing.assert_allclose(f(x), expected)

  def test_reshape_to_scalar(self):
    self.skip_if_mosaic_gpu()
    # Test reshapes from (1, 1) to ().
    # Because TPUs distinguish between VREGs/SREGs this tests an implicit
    # copy from VREG -> SREG that must be inserted by Pallas.
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.int32),
    )
    def f(x_ref, o_ref):
      o_ref[...] = jnp.zeros_like(o_ref)
      vector_val = x_ref[1:2, 0:1]
      scalar_val = jnp.reshape(vector_val, ())
      o_ref[scalar_val] = jnp.ones_like(o_ref[0]) * scalar_val

    in_shape = (4, 4)
    x = jnp.arange(int(np.prod(in_shape)), dtype=jnp.int32).reshape(in_shape)
    expected = jnp.zeros((8, 128), jnp.int32)
    expected = expected.at[x[1, 0]].set(x[1, 0])
    np.testing.assert_allclose(f(x), expected)

  def test_num_programs(self):
    self.skip_if_mosaic_gpu()

    @functools.partial(
        self.pallas_call,
        out_specs=pl.BlockSpec(memory_space=smem_on_tpu()),
        out_shape=jax.ShapeDtypeStruct((4,), intx),
        grid=4,
    )
    def kernel(o_ref):
      o_ref[pl.program_id(0)] = pl.num_programs(0)

    np.testing.assert_array_equal(
        kernel(), jnp.array([4, 4, 4, 4], dtype=intx)
    )

  def test_where_broadcasting(self):
    self.skip_if_mosaic_gpu()

    # The Pallas TPU lowering currently supports only blocks of rank >= 1
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on TPU")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((4, 2, 2), floatx),
    )
    def copyitem(x_ref, in_idx_ref, out_idx_ref, o_ref):
      mask = (jnp.arange(o_ref.shape[0]) == out_idx_ref[()])[:, None, None]
      o_ref[...] = jnp.where(mask, x_ref[in_idx_ref[()]], 0)

    x = jnp.arange(7 * 2 * 2.0).reshape(7, 2, 2)
    for ii in range(7):
      for oi in range(4):
        out = copyitem(x, ii, oi)
        self.assertEqual((4, 2, 2), out.shape)
        np.testing.assert_allclose(out[:oi], jnp.zeros_like(out[:oi]))
        np.testing.assert_allclose(out[oi], x[ii])
        np.testing.assert_allclose(out[oi + 1 :], jnp.zeros_like(out[oi + 1 :]))

  @parameterized.product(
      shape_spec=[
          ((), (2,), ()),
          ((1,), (2,), (0,)),
          ((1, 128), (8, 128), (0, 1)),  # row broadcasting
          ((), (2, 2), ()),
      ],
      dtype=[jnp.int32, jnp.int16, jnp.int8, jnp.bool_],
  )
  def test_broadcast_in_dim(self, shape_spec, dtype):
    self.skip_if_mosaic_gpu()

    in_shape, out_shape, dims = shape_spec
    if jtu.test_device_matches(["tpu"]):
      if not in_shape:
        self.skipTest(
            "The Pallas TPU lowering currently supports only blocks of rank"
            " >= 1"
        )
      if dtype is jnp.bool_ and not jtu.if_cloud_tpu_at_least(2025, 6, 5):
        self.skipTest("Requires libtpu built after 2025-06-05")
      if (
          len(in_shape) == 1
          and len(out_shape) == 1
          and dtype not in {jnp.int32, jnp.bool_}
      ):
        self.skipTest("Unsupported tiling")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(out_shape, dtype),
    )
    def f(x_ref, o_ref):
      x = x_ref[...]
      o_ref[...] = jax.lax.broadcast_in_dim(x, out_shape, dims)

    x = (
        jnp.arange(math.prod(in_shape), dtype=jnp.int32)
        .reshape(in_shape)
        .astype(dtype)
    )
    expected = jax.lax.broadcast_in_dim(x, out_shape, dims)
    np.testing.assert_array_equal(f(x), expected)

  @parameterized.product(
      lhs_and_rhs_shape=[
          ((16, 16), (16, 16)),
          ((32, 32), (32, 32)),
          ((64, 64), (64, 64)),
          ((128, 128), (128, 128)),
          ((256, 256), (256, 256)),
          ((8, 128), (128, 256)),
          ((8, 128), (256, 128)),
          ((8, 256), (256, 128)),
          ((16, 128), (128, 256)),
          ((16, 128), (256, 128)),
          ((16, 256), (256, 128)),
          ((24, 128), (128, 256)),
          ((24, 128), (256, 128)),
          ((24, 256), (256, 128)),
          ((128, 8), (128, 256)),
          ((128, 8), (256, 128)),
          ((256, 8), (256, 128)),
          ((128, 16), (128, 256)),
          ((128, 16), (256, 128)),
          ((256, 16), (256, 128)),
          ((128, 24), (128, 256)),
          ((128, 24), (256, 128)),
          ((256, 24), (256, 128)),
      ],
      dtype=[jnp.float32, jnp.float16, jnp.bfloat16],
      trans_x=[False, True],
      trans_y=[False, True],
  )
  @jtu.skip_if_triton_exceeds_shared_memory(device_patterns="RTX PRO 6000 Blackwell")
  @jtu.skip_if_mosaic_gpu_exceeds_shared_memory(device_patterns="RTX PRO 6000 Blackwell")
  def test_dot(self, lhs_and_rhs_shape, dtype, trans_x, trans_y):
    self.skip_if_mosaic_gpu()

    # TODO(apaszke): Remove after 12 weeks have passed.
    if not jtu.if_cloud_tpu_at_least(2024, 12, 19):
      self.skipTest("Requires libtpu built after 2024-12-19")
    lhs_shape, rhs_shape = lhs_and_rhs_shape

    final_lhs_shape = lhs_shape[::-1] if trans_x else lhs_shape
    final_rhs_shape = rhs_shape[::-1] if trans_y else rhs_shape
    if final_lhs_shape[1] != final_rhs_shape[0]:
      self.skipTest("Contraction dimensions do not match")

    out_shape = (final_lhs_shape[0], final_rhs_shape[1])

    if jtu.test_device_matches(["tpu"]):
      if dtype == jnp.float16:
        self.skipTest("float16 type is not supported on TPU")
      if dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4):
        self.skipTest("bfloat16 matmul is supported on TPUv4+")
      if trans_x:
        self.skipTest("Not implemented: Transposed LHS")

    if jtu.test_device_matches(["gpu"]):
      if dtype == jnp.bfloat16:
        self.skipTest("bfloat16 type are not supported on GPU")
      if (
          math.prod(lhs_shape) + math.prod(rhs_shape) + math.prod(out_shape)
          > (256 * 256) * 2
      ):
        self.skipTest("Shared memory size limit exceeded")
      if (jax.local_devices()[0].device_kind == "NVIDIA L4" and
          dtype == jnp.float32 and
          lhs_and_rhs_shape in [
            ((128, 16), (128, 256)),
            ((16, 128), (128, 256)),
            ((16, 256), (256, 128)),
            ((256, 16), (256, 128)),
          ]):
        self.skipTest("Shared memory size limit exceeded")
      if min(*lhs_shape, *rhs_shape) < 16:
        self.skipTest("All dimensions of lhs and rhs must be >= 16")
      if any(not is_power_of_two(x) for x in lhs_shape + rhs_shape):
        self.skipTest("All dimensions of lhs and rhs must be power of two")

    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(out_shape, dtype),
    )
    def dot(x_ref, y_ref, o_ref):
      x = x_ref[:, :]
      y = y_ref[:, :]
      o_ref[:, :] = pl.dot(x, y, trans_x, trans_y).astype(o_ref.dtype)

    k1, k2 = random.split(random.key(0))
    x = random.normal(k1, lhs_shape, dtype=dtype)
    y = random.normal(k2, rhs_shape, dtype=dtype)
    out = dot(x, y)
    # Pallas always accumulates in FP32, so we are explicit about
    # preferred_element_type here.
    expected = jnp.dot(x.T if trans_x else x, y.T if trans_y else y,
                      preferred_element_type=jnp.float32).astype(dtype)
    np.testing.assert_allclose(
        out.astype(jnp.float32),
        expected.astype(jnp.float32),
        atol=0.05,
        rtol=0.05,
    )

  def test_strided_load(self):
    self.skip_if_mosaic_gpu()

    # Reproducer from https://github.com/jax-ml/jax/issues/20895.
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((4, 4), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      o_ref[...] = x_ref[::4]

    x = jnp.arange(64, dtype=jnp.float32).reshape((16, 4))
    np.testing.assert_array_equal(kernel(x), x[::4])

  def test_broadcasted_load_store(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Unimplemented primitive: broadcast_to")

    m, n = 16, 32

    @functools.partial(
        self.pallas_call,
        out_shape=(jax.ShapeDtypeStruct((m, n), floatx)),
    )
    def load(x_ref, o_ref):
      idx = (jnp.arange(m)[:, None], jnp.arange(n)[None, :])
      o_ref[idx] = x_ref[idx] + 1.0

    key = random.key(0)
    x = random.normal(key, (m, n))
    np.testing.assert_allclose(load(x), x + 1.0, atol=1e-5, rtol=1e-5)

  def test_swap(self):
    self.skip_if_mosaic_gpu()

    # TODO: skipped due to https://github.com/jax-ml/jax/issues/24023
    if jtu.test_device_matches(["tpu"]) and not self.INTERPRET:
      self.skipTest("On TPU this is only supported in interpret mode")

    m, n = 16, 32

    @functools.partial(
        self.pallas_call,
        out_shape=(jax.ShapeDtypeStruct((m, n), floatx),) * 2,
        input_output_aliases={0: 0, 1: 1},
    )
    def swap(_, _2, x_ref, y_ref):
      x = x_ref[:]
      y = pl.swap(y_ref, (slice(None),), x)
      x_ref[:] = y

    x = random.normal(random.key(0), (m, n))
    y = random.normal(random.key(1), (m, n))
    out = swap(x, y)
    np.testing.assert_array_equal(out[0], y)
    np.testing.assert_array_equal(out[1], x)

  def test_masked_swap(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    m, n = 16, 32

    @functools.partial(
        self.pallas_call,
        out_shape=(jax.ShapeDtypeStruct((m, n), floatx),) * 2,
        input_output_aliases={0: 0, 1: 1},
    )
    def masked_swap(_, _2, mask_ref, x_ref, y_ref):
      x = x_ref[:]
      y = pl.swap(y_ref, (slice(None),), x, mask=mask_ref[:])
      x_ref[:] = y

    x = random.normal(random.key(0), (m, n))
    y = random.normal(random.key(1), (m, n))
    mask = random.bernoulli(random.key(2), shape=(m, n))
    out = masked_swap(x, y, mask)
    np.testing.assert_array_equal(out[0], jnp.where(mask, y, x))
    np.testing.assert_array_equal(out[1], jnp.where(mask, x, y))

  def test_masked_oob_swap_slice(self):
    self.skip_if_mosaic_gpu()

    # The Pallas TPU lowering currently supports only blocks of rank >= 1
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on TPU")

    m, n = 32, 16

    @functools.partial(
        self.pallas_call,
        out_shape=(jax.ShapeDtypeStruct((n,), floatx),
                  jax.ShapeDtypeStruct((m,), floatx)),
        input_output_aliases={0: 0, 1: 1},
    )
    def masked_oob_swap_slice(_, _2, mask_ref, start_idx_ref, x_ref, y_ref):
      x, mask = x_ref[:], mask_ref[:]
      y = pl.swap(y_ref, (pl.dslice(start_idx_ref[()], n)), x, mask=mask)
      x_ref[:] = y

    x = random.normal(random.key(0), (n,))
    y = random.normal(random.key(1), (m,))
    slice_start = random.randint(random.key(2), (), m-n+1, m)
    indices = jnp.arange(n) + slice_start
    mask = indices < m
    out = masked_oob_swap_slice(x, y, mask, slice_start)

    # the unjittable masked indexing equivalent
    unmasked_idx = indices[mask]
    x_new = x.at[mask].set(y[unmasked_idx])
    y_new = y.at[unmasked_idx].set(x[mask])
    np.testing.assert_array_equal(out[0], x_new)
    np.testing.assert_array_equal(out[1], y_new)

  def test_reduce_only_dim(self):
    self.skip_if_mosaic_gpu()

    # The Pallas TPU lowering currently supports only blocks of rank >= 1
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on TPU")

    m = 32
    x = random.normal(random.key(0), (m,), dtype=jnp.float32)
    out_shape = jax.ShapeDtypeStruct((), x.dtype)

    @functools.partial(self.pallas_call, out_shape=out_shape)
    def reduce(x_ref, y_ref):
      y_ref[...] = jnp.sum(x_ref[jnp.arange(m)], axis=-1)

    y = reduce(x)
    y_ref = jnp.sum(x, axis=-1)
    np.testing.assert_allclose(y, y_ref, atol=1e-2, rtol=1e-2)

  @parameterized.named_parameters(*[
      (f"{op_name}_{dtype}_{axis}", op, dtype, axis)
      for op_name, op in [
          ("add", jnp.sum),
          ("max", jnp.max),
          ("min", jnp.min),
          ("argmax", jnp.argmax),
          ("argmin", jnp.argmin),
      ]
      for axis in [0, 1, (1,), (0, 1)]
      for dtype in [
          "float16",
          "bfloat16",
          "float32",
          "float64",
          "int32",
          "int64",
          "uint32",
          "uint64",
      ]
  ])
  def test_array_reduce(self, op, dtype, axis):
    self.skip_if_mosaic_gpu()

    if not isinstance(axis, int):
      self.skipTest("TODO: tuple axes are not yet supported")

    if not jax.config.x64_enabled and jnp.dtype(dtype).itemsize == 8:
      self.skipTest("64-bit types require x64_enabled")

    # The Pallas TPU lowering currently supports only blocks of rank >= 1
    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    # Skip argmin/argmax on GPU in 64-bit mode because Pallas expects
    # `index_type` to be i32
    if (
        jax.config.x64_enabled
        and jtu.test_device_matches(["gpu"])
        and op in (jnp.argmin, jnp.argmax)
    ):
      self.skipTest("Not supported on GPU in 64-bit mode")

    m, n = 32, 8

    def make_x(key):
      if jnp.issubdtype(dtype, jnp.integer):
        return random.permutation(
            key, jnp.arange(m * n, dtype=dtype), independent=True
        ).reshape(m, n)
      else:
        return random.normal(key, (m, n), dtype=dtype)

    # deduct `out_dtype` by executing the op on a single element
    out_dtype = op(jnp.arange(1, dtype=dtype)).dtype
    out_shape = jax.ShapeDtypeStruct(
        op(make_x(random.key(0)), axis=axis).shape, out_dtype)
    if isinstance(axis, int):
      grid = tuple(a for i, a in enumerate((m, n)) if i != axis)
    else:
      grid = tuple(a for i, a in enumerate((m, n)) if i not in axis)

    @functools.partial(self.pallas_call, out_shape=out_shape, grid=grid)
    def reduce(x_ref, y_ref):
      x = x_ref[
          jnp.arange(m, dtype=jnp.int32)[:, None],
          jnp.arange(n, dtype=jnp.int32)[None],
      ]
      y = op(x, axis=axis)
      y_ref[tuple(jnp.arange(d, dtype=jnp.int32) for d in y.shape)] = y

    for i, key in enumerate(random.split(random.key(0), 20)):
      x = make_x(key)
      y = reduce(x)
      y_ref = op(x, axis=axis)
      self.assertAllClose(y, y_ref, atol=1e-2, rtol=1e-2, err_msg=i)

  @parameterized.product(
      axis=[0, 1],
      dtype=["float16", "float32", "int32", "uint32"],
  )
  def test_cumsum(self, dtype, axis):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    m, n = 32, 8
    out_dtype = dtype

    def make_x(key):
      if jnp.issubdtype(dtype, jnp.integer):
        return random.permutation(
            key, jnp.arange(m * n, dtype=dtype), independent=True
        ).reshape(m, n)
      else:
        return random.normal(key, (m, n), dtype=dtype)

    out_shape = jax.ShapeDtypeStruct((m, n), out_dtype)
    grid = ()

    @functools.partial(self.pallas_call, out_shape=out_shape, grid=grid)
    def reduce(x_ref, y_ref):
      x = x_ref[...]
      y_ref[...] = jnp.cumsum(x, axis=axis)

    for i, key in enumerate(random.split(random.key(0), 20)):
      x = make_x(key)
      y = reduce(x)
      y_ref = jnp.cumsum(x, axis=axis)
      np.testing.assert_allclose(y, y_ref, atol=1e-2, rtol=1e-2, err_msg=i)

  @parameterized.parameters(
      (0, jnp.float32),
      (0, jnp.bfloat16),
      (1, jnp.float32),
      (1, jnp.bfloat16),
      (-1, jnp.float32),
      (-1, jnp.bfloat16),
  )
  def test_triu(self, k, dtype):
    self.skip_if_mosaic_gpu()

    if dtype == jnp.bfloat16 and jtu.test_device_matches(["tpu"]):
      # TODO(mvoz): b/376330700
      raise unittest.SkipTest('NYI - bf16 select')

    x = jnp.arange(128 * 256, dtype=dtype).reshape((128, 256))

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.triu(x_ref[...], k=k)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((128, 256), dtype)
    )(x)
    np.testing.assert_array_equal(out, np.triu(x, k=k))

  @parameterized.parameters(
      (jnp.float16, jnp.float16),  # Noop
      (jnp.int16, jnp.bfloat16),
      (jnp.int16, jnp.float16),
      (jnp.uint16, jnp.float16),
      (jnp.float32, jnp.int32),
      (jnp.float32, jnp.uint32),
      (jnp.uint32, jnp.int32),
      (jnp.int32, jnp.uint32),
  )
  def test_bitcast_convert_type(self, in_dtype, out_dtype):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    m, n = 4, 4
    out_shape = jax.ShapeDtypeStruct((m, n), out_dtype)
    grid = ()

    @functools.partial(self.pallas_call, out_shape=out_shape, grid=grid)
    def convert(x_ref, y_ref):
      y_ref[...] = jax.lax.bitcast_convert_type(x_ref[...], out_shape)

    x = jnp.arange(m * n, dtype=in_dtype).reshape((m, n))
    y = convert(x)
    y_ref = jax.lax.bitcast_convert_type(x, out_dtype)
    np.testing.assert_array_equal(y, y_ref)

  def test_bitcast_convert_type_scalar(self):
    self.skip_if_mosaic_gpu()

    if jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on TPU")

    x = jnp.int32(42)
    out_dtype = jnp.float32
    out_shape = jax.ShapeDtypeStruct(x.shape, out_dtype)
    grid = ()

    @functools.partial(self.pallas_call, out_shape=out_shape, grid=grid)
    def convert(x_ref, y_ref):
      y_ref[...] = jax.lax.bitcast_convert_type(x_ref[...], out_dtype)

    y = convert(x)
    y_ref = jax.lax.bitcast_convert_type(x, out_dtype)
    np.testing.assert_array_equal(y, y_ref)

  @parameterized.product(
      array_shapes=[(4, 128), (10, 100), (8, 128), (17, 257)],
      padding=[
          ((5, 8), (0, 0)),
          ((0, 0), (5, 100)),
          ((1, 1), (1, 1)),
          ((0, 0), (0, 0)),
      ],
      pad_type=["constant", "wrap"],
      dtype=(
          jnp.float32,
          jnp.bfloat16,
      ),
  )
  def test_arbitrary_padding_jnp_pad(
      self, array_shapes, padding, pad_type, dtype
  ):
    if jtu.test_device_matches(["gpu"]):
      self.skipTest("Not implemented on GPU")
    # TODO(apaszke): Remove after 12 weeks have passed.
    if not jtu.if_cloud_tpu_at_least(2024, 12, 19):
      self.skipTest("Requires libtpu built after 2024-12-19")

    x = jnp.arange(np.prod(array_shapes), dtype=dtype).reshape(array_shapes)

    def kernel(x_ref, o_ref):
      o_ref[...] = jnp.pad(x_ref[...], padding, mode=pad_type)

    ref = jnp.pad(x, padding, mode=pad_type)

    out_shape = jax.ShapeDtypeStruct(ref.shape, x.dtype)
    try:
      out = self.pallas_call(
          kernel,
          out_shape=out_shape,
      )(x)
      np.testing.assert_array_equal(out, jnp.pad(x, padding, mode=pad_type))
    except Exception as e:
      self.assertEqual(
          dtype,
          jnp.bfloat16,
          "some bfloat16 combinations can fail with not implemented",
      )
      # The first two options are expected to fail due to current limitations
      # in the Pallas TPU lowering. However, the last one is unexpected, and
      # should be fixed, it is a pjrt bug.
      # b/379787665
      acceptable_errors = (
          "Only 32-bit types supported" in str(e)
          or "Not implemented" in str(e)
          or "Expected mask vector type" in str(e)
      )
      self.assertTrue(acceptable_errors, "Failed with error: " + str(e))

  @parameterized.parameters((128, 128), (256, 256))
  def test_jnp_diagonal_pallas(self, n, m):
    if jtu.test_device_matches(["gpu"]):
      # TODO(mvoz): platform_index_p on GPU
      self.skipTest("Not implemented on GPU")
    x = jnp.arange(n * m, dtype=jnp.float32).reshape((n, m))

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.diagonal(x_ref[...])

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((n,), jnp.float32)
    )(x)
    np.testing.assert_array_equal(out, np.diagonal(x))

  @parameterized.product(
      # Skip some steps to just run less cases
      # TODO(mvoz): Hypothesis?
      x_dim_size=tuple(8 * i for i in range(1, 5)),
      y_dim_size=tuple(8 * i for i in range(1, 5)),
      z_dim_size=tuple(128 * i for i in range(1, 3)),
      dtype=(jnp.float32,),
  )
  def test_jnp_swapaxes_major_minor(
      self, x_dim_size, y_dim_size, z_dim_size, dtype
  ):
    if jtu.test_device_matches(["gpu"]):
      if any(
          not is_power_of_two(x) for x in [x_dim_size, y_dim_size, z_dim_size]
      ):
        self.skipTest(
            "the Pallas Triton lowering currently requires that all operations"
            " have array arguments and results whose size is a power of 2."
            f" Encountered an array of shape ({x_dim_size}, {y_dim_size},"
            f" {z_dim_size})"
        )
      if x_dim_size * y_dim_size * z_dim_size * 4 > 32768:
        self.skipTest(
            "Mosaic GPU kernel exceeds available shared memory"
            f" smem_bytes={x_dim_size * y_dim_size * z_dim_size * 4} > 32768"
        )
    self.skip_if_mosaic_gpu()
    if not jtu.if_cloud_tpu_at_least(2025, 5, 22):
      self.skipTest("Requires libtpu built after 2025-5-22")

    x = jnp.arange(x_dim_size * y_dim_size * z_dim_size, dtype=dtype).reshape(
        (x_dim_size, y_dim_size, z_dim_size)
    )

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.swapaxes(x_ref[...], 0, 1)

    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct(
            (y_dim_size, x_dim_size, z_dim_size), dtype
        ),
    )(x)
    expected = jnp.swapaxes(x, 0, 1)
    np.testing.assert_array_equal(out, expected)

  @hp.given(batch_size=hps.integers(1, 16))
  def test_8bit_gather(self, batch_size):
    if not jtu.test_device_matches(["tpu"]):
      self.skipTest("Not supported on this hardware")
    if jtu.get_tpu_version() < 6:
      self.skipTest("Requires TPUv6 or newer")
    if not jtu.if_cloud_tpu_at_least(2025, 9, 22):
      self.skipTest("Requires libtpu built after 2025-9-22")

    dtype = jnp.int8
    xspec = pl.BlockSpec((32, 128), lambda i: (i, 0))
    lspec = pl.BlockSpec((32, 128), lambda i: (0, 0))

    data = jax.random.randint(
        key=jax.random.key(1234),
        shape=(32 * batch_size, 128),
        minval=0,
        maxval=32,
        dtype=jnp.int8,
    )
    lut = jax.random.randint(
        key=jax.random.key(1234),
        shape=(32, 128),
        minval=-128,
        maxval=127,
        dtype=jnp.int8,
    )

    def kernel(data_ref, lut_ref, output_ref):
      data_chunk = data_ref[...]
      lut_chunk = lut_ref[...]
      data_chunk = data_chunk.reshape((32, 128, 1))
      output = lax.gather(
          lut_chunk,
          data_chunk,
          dimension_numbers=lax.GatherDimensionNumbers(
              offset_dims=(),
              start_index_map=(0,),
              operand_batching_dims=(1,),
              start_indices_batching_dims=(1,),
              collapsed_slice_dims=(0,)
          ),
          slice_sizes=(1, 1),
          mode="promise_in_bounds",
      )
      output_ref[...] = output

    deq_call = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct(data.shape, dtype),
        grid=(batch_size,),
        out_specs=xspec,
        in_specs=[xspec, lspec],
    )
    result = deq_call(data, lut)
    expected = jnp.take_along_axis(
        lut, data, axis=0, mode="promise_in_bounds"
    )
    np.testing.assert_array_equal(result, expected)


class OpsInterpretTest(OpsTest):
  INTERPRET = True

  def test_debug_print(self):
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((2,), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      jax.debug.print("x = {}", x_ref)

    x = jnp.array([4.2, 2.4]).astype(jnp.float32)
    with jtu.capture_stdout() as output:
      jax.block_until_ready(kernel(x))
      jax.effects_barrier()

    self.assertIn("x = [4.2 2.4]", output())


class PallasPrimitivesTest(PallasBaseTest):

  @parameterized.parameters(*[
    (lambda: (pl.dslice(0, 4), slice(None), slice(None)), "<- a[:,:,:]"),
    (lambda: (pl.dslice(0, 3), slice(None), slice(None)), "<- a[:3,:,:]"),
    (lambda: (pl.dslice(1, 3), slice(None), pl.dslice(0, 4)), "<- a[1:,:,:4]"),
    (lambda: (jnp.arange(5), slice(None), pl.dslice(0, 4)), "<- a[b,:,:4]"),
    (lambda: (jnp.arange(5)[:, None], jnp.arange(3)[None], pl.ds(4)), "<- a[f,g,:4]"),
  ])
  def test_load_pretty_print(self, expr, expected):
    def body(x_ref):
      with jtu.ignore_warning(
          category=DeprecationWarning, message="pl.load is deprecated"
      ):
        x = pl.load(x_ref, expr())
      return [x]
    jaxpr, _ , _ = pe.trace_to_jaxpr_dynamic(
        wrap_init(body, 1), [state.shaped_array_ref((4, 3, 2), jnp.int32)])
    self.assertIn(expected, jaxpr.pretty_print(use_color=False))

  @parameterized.parameters(*[
    (lambda: (pl.dslice(0, 4), slice(None), slice(None)), "a[:,:,:] <-"),
    (lambda: (pl.dslice(0, 3), slice(None), slice(None)), "a[:3,:,:] <-"),
    (lambda: (pl.dslice(1, 3), slice(None), pl.dslice(0, 4)), "a[1:,:,:4] <-"),
    (lambda: (jnp.arange(5), slice(None), pl.dslice(0, 4)), "a[b,:,:4] <-"),
    (lambda: (jnp.arange(5)[:, None], jnp.arange(3)[None], pl.dslice(4)), "a[m,n,:4] <-"),
  ])
  def test_store_pretty_print(self, expr, expected):
    def body(x_ref):
      with jtu.ignore_warning(
          category=DeprecationWarning, message="pl.(load|store) is deprecated"
      ):
        pl.store(x_ref, expr(), pl.load(x_ref, expr()))
      return []
    jaxpr, _ , _ = pe.trace_to_jaxpr_dynamic(
        wrap_init(body, 1), [state.shaped_array_ref((4, 3, 2), jnp.int32)])
    self.assertIn(expected, jaxpr.pretty_print(use_color=False))

  @parameterized.parameters(*[
    (lambda: (pl.dslice(0, 4), slice(None), slice(None)),
    "c:i32[4,3,2], a[:,:,:] <-"),
    (lambda: (pl.dslice(0, 3), slice(None), slice(None)),
    "c:i32[3,3,2], a[:3,:,:] <-"),
    (lambda: (pl.dslice(1, 3), slice(None), pl.dslice(0, 4)),
    "c:i32[3,3,4], a[1:,:,:4] <-"),
    (lambda: (jnp.arange(5), slice(None), pl.dslice(0, 4)),
    "e:i32[5,3,4], a[b,:,:4] <-"),
    (lambda: (jnp.arange(5)[:, None], jnp.arange(3)[None], pl.dslice(4)),
    "o:i32[5,3,4], a[m,n,:4] <-"),
  ])
  def test_swap_pretty_print(self, expr, expected):
    def body(x_ref):
      with jtu.ignore_warning(
          category=DeprecationWarning, message="pl.(load|swap) is deprecated"
      ):
        x = pl.swap(x_ref, expr(), pl.load(x_ref, expr()))
      return [x]
    jaxpr, _ , _ = pe.trace_to_jaxpr_dynamic(
        wrap_init(body, 1), [state.shaped_array_ref((4, 3, 2), jnp.int32)])
    self.assertIn(expected, jaxpr.pretty_print(use_color=False))

  @parameterized.product(approx=[False, True])
  def test_reciprocal(self, approx):
    if not jtu.test_device_matches(["tpu"]):
      self.skipTest("Not implemented on non-TPU devices")
    if not jtu.if_cloud_tpu_at_least(2025, 3, 8):
      self.skipTest("Test requires libtpu from 2025/3/8 or later")
    shape = (32, 256)
    x = jnp.arange(np.prod(shape), dtype=jnp.float32).reshape(shape)

    def kernel(x_ref, o_ref):
      o_ref[...] = pl.reciprocal(x_ref[...], approx=approx)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct(shape, jnp.float32)
    )(x)
    kwargs = {}
    if approx:
      kwargs.update(dict(atol=2e-5, rtol=2e-5))
    np.testing.assert_allclose(out, jax.lax.reciprocal(x), **kwargs)


class PallasPrimitivesInterpretTest(PallasPrimitivesTest):
  INTERPRET = True


if __name__ == "__main__":
  absltest.main()
