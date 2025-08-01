# Copyright 2018 The JAX Authors.
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

# pytype: skip-file
from __future__ import annotations

from collections import namedtuple
from collections.abc import Callable, Sequence, Hashable
import contextlib
from dataclasses import dataclass
from functools import partial
import itertools as it
import operator as op
from typing import Any, NamedTuple, Union
from weakref import finalize, ref, ReferenceType, WeakValueDictionary

from jax._src import ad_util
from jax._src import api_util
from jax._src import config
from jax._src import core
from jax._src import dtypes
from jax._src import effects
from jax._src import linear_util as lu
from jax._src import profiler
from jax._src import source_info_util
from jax._src import compute_on
from jax._src import xla_metadata_lib
from jax._src.core import (Trace, Tracer, TraceTag, Jaxpr, Literal, get_aval,
                           AbstractValue, ClosedJaxpr, new_jaxpr_eqn,
                           Var, DropVar, Atom,
                           JaxprEqn, Primitive, ShapedArray, DShapedArray,
                           mapped_aval, unmapped_aval, DBIdx, InDBIdx, OutDBIdx,
                           InputType, OutputType, get_referent, JaxprEqnContext)
from jax._src.source_info_util import SourceInfo
from jax._src.state.types import AbstractRef, ReadEffect
from jax._src.tree_util import PyTreeDef, treedef_tuple, register_static
from jax._src.util import (unzip2, safe_zip, safe_map, toposort, split_list,
                           merge_lists, partition_list, OrderedSet,
                           as_hashable_function, weakref_lru_cache, subs_list,
                           HashableFunction, foreach, cache)


map, unsafe_map = safe_map, map
zip, unsafe_zip = safe_zip, zip
def identity(x): return x

TracerId = int
AvalId = int
ConstId = int

AttrKind = Any
PyTree = Any

def _update_annotation_known(
    f: lu.WrappedFun,
    orig_type: InputType | None,
    in_knowns: list[bool]
  ) -> lu.WrappedFun:
  if orig_type is None: return f
  # orig_type might contain DBIdx, but we're tossing out some args so we have to
  # re-index. moreover some of the implicit args may not be needed anymore.
  # so we basically just re-infer the lambda input type
  if (all(e for _, e in orig_type) and
      not any(type(d) is DBIdx for a, _ in orig_type for d in a.shape
              if type(a) is DShapedArray)):
    new_type = [ty for ty, known in zip(orig_type, in_knowns) if known]
    return lu.annotate(f, tuple(new_type))

  # Replace DBIdx with names, prune down to explicit only.
  class Name:
    def __init__(self, a): self.a = a
  names = [Name(a) for a, _  in orig_type]
  avals = [a.update(shape=tuple(names[d.val] if type(d) is DBIdx else d
                                for d in a.shape))
           if type(a) is DShapedArray else a for a, e in orig_type if e]
  avals = [a for a, known in zip(avals, in_knowns) if known]
  # Figure out the implicit part: names which aren't explicit and known.
  expl_names = [o for o, (_, e) in zip(names, orig_type) if e]
  expl_names = [o for o, k in zip(expl_names, in_knowns) if k]
  expl_names_ = set(expl_names)
  impl_names = {d for a in avals if type(a) is DShapedArray for d in a.shape
                if type(d) is Name and d not in expl_names_}
  impl_part = [(n.a, False) for n in impl_names]  # type: ignore
  # Figure out the explicit part: known explicit avals, replacing names w/ dbidx
  name_map = {n: DBIdx(i) for i, n in enumerate((*impl_names, *expl_names))}
  expl_part = [(a.update(shape=tuple(name_map.get(d, d) for d in a.shape))
                if type(a) is DShapedArray else a, True) for a in avals]
  return lu.annotate(f, (*impl_part, *expl_part))

class PartialVal(tuple):
  """Partial value: either a known value or an unknown (abstract) value.

  Represented as a pair `(aval_opt, const)` of one of two kinds:
  * `(None, <Constant>)` indicates a known value, where the constant is either a
    Tracer or satisfies `core.valid_jaxtype(const)`;
  * `(<AbstractValue>, None)` indicates an unknown value characterized by an
    abstract value.
  """
  def __new__(cls, xs: tuple[AbstractValue | None, core.Value]):
    pv, const = xs
    if config.enable_checks.value:
      # type checks
      assert isinstance(pv, (AbstractValue, type(None))), xs
      assert (const is None or isinstance(const, core.Tracer) or
              core.valid_jaxtype(const)), const
      # invariant checks
      assert (pv is None) ^ (const is None)
    return tuple.__new__(cls, xs)

  @classmethod
  def known(cls, const: core.Value) -> PartialVal:
    return PartialVal((None, const))

  @classmethod
  def unknown(cls, aval: AbstractValue) -> PartialVal:
    return PartialVal((aval, None))

  def is_known(self) -> bool:
    return self[0] is None

  def get_known(self) -> core.Value | None:
    """Get the known value, if known, else None."""
    return self[1] if self[0] is None else None

  def get_aval(self) -> AbstractValue:
    """Get AbstractValue directly (if unknown) or from the constant (known)."""
    known = self.get_known()
    if known is not None:
      return get_aval(known)
    else:
      return self[0]

@dataclass(frozen=True)
class EffectHandle:
  parents : list[Tracer]
  recipe : JaxprEqnRecipe

class JaxprTrace(Trace['JaxprTracer']):

  def __init__(self, parent_trace:Trace, name_stack: source_info_util.NameStack, tag:TraceTag):
    super().__init__()
    self.name_stack = name_stack
    self.tag = tag
    self.parent_trace = parent_trace
    self.requires_low = False
    self.effect_handles : list[EffectHandle] = []
    self.counter = it.count()

  def to_jaxpr_tracer(self, x):
    if isinstance(x, JaxprTracer) and x._trace.tag is self.tag:
      if x._trace is self:
        return x
      else:
        return JaxprTracer(self, x.pval, FreeVar(x))
    else:
      return self.new_const(x)

  def new_const(self, val) -> JaxprTracer:
    return JaxprTracer(self, PartialVal.known(val), None)

  def new_instantiated_literal(self, val) -> JaxprTracer:
    aval = get_aval(val)
    return JaxprTracer(self, PartialVal.unknown(aval), Literal(val, aval))

  def new_instantiated_const(self, val) -> JaxprTracer:
    aval = get_aval(val)
    return JaxprTracer(self, PartialVal.unknown(aval), ConstVar(val))

  def new_arg(self, pval: PartialVal) -> JaxprTracer:
    const = pval.get_known()
    # XXX: Think twice before changing this constant argument pruning!
    # This has really important consequences for partial_eval_jaxpr.
    # Most importantly, this guarantees that the unknown jaxpr never uses
    # known inputs (if it needs them, then they get passed through residuals).
    if const is None:
      aval = pval.get_aval()
      if type(aval) is DShapedArray:
        # TODO(dougalm): Fix the type error and remove the pytype pragmas.
        # pytype: disable=attribute-error
        shape = [self.new_instantiated_const(d)
                 if isinstance(d, Tracer) and d._trace.level < self.level else d
                 for d in aval.shape]
        # pytype: enable=attribute-error
        aval = aval.update(shape=tuple(shape))
      return JaxprTracer(self, PartialVal.unknown(aval), LambdaBinding())
    else:
      return self.new_const(const)

  def instantiate_const(self, tracer: JaxprTracer) -> JaxprTracer:
    const = tracer.pval.get_known()
    if const is None:
      return tracer
    else:
      if core.is_literalable(const):
        return self.new_instantiated_literal(const)
      else:
        return self.new_instantiated_const(const)

  def cur_qdd(self, x):
    const = self.to_jaxpr_tracer(x).pval.get_known()
    if const is None:
      assert False # TODO: track tangent QDDs
    else:
      with core.set_current_trace(self.parent_trace):
        return core.cur_qdd(const)

  def process_primitive(self, primitive, tracers, params):
    with core.set_current_trace(self.parent_trace):
      if primitive in custom_partial_eval_rules:
        tracers = map(self.to_jaxpr_tracer, tracers)
        return custom_partial_eval_rules[primitive](self, *tracers, **params)
      else:
        return self.default_process_primitive(primitive, tracers, params)

  def default_process_primitive(self, primitive, tracers, params):
    # By default, if all the input tracers are known, then bind the primitive
    # and consider all outputs known. Otherwise, stage the application into the
    # jaxpr and consider all outputs unknown.
    tracers = map(self.to_jaxpr_tracer, tracers)
    consts = [t.pval.get_known() for t in tracers]
    if all(c is not None for c in consts):
      return primitive.bind_with_trace(self.parent_trace, consts, params)
    tracers = map(self.instantiate_const, tracers)
    avals = [t.aval for t in tracers]
    out_aval, effs = primitive.abstract_eval(*avals, **params)
    name_stack = self._current_truncated_name_stack()
    source = source_info_util.current().replace(name_stack=name_stack)
    if primitive.multiple_results:
      out_tracers = [JaxprTracer(self, PartialVal.unknown(aval), None)
                     for aval in out_aval]
      eqn = new_eqn_recipe(self, tracers, out_tracers, primitive, params, effs,
                           source)
      if effects.partial_eval_kept_effects.filter_in(effs):
        self.effect_handles.append(EffectHandle(tracers, eqn))
      for t in out_tracers: t.recipe = eqn
      return out_tracers
    else:
      out_tracer = JaxprTracer(self, PartialVal.unknown(out_aval), None)
      eqn = new_eqn_recipe(self, tracers, [out_tracer], primitive,
                           params, effs, source)
      if effects.partial_eval_kept_effects.filter_in(effs):
        self.effect_handles.append(EffectHandle(tracers, eqn))
      out_tracer.recipe = eqn
      return out_tracer

  def process_call(self, primitive, f: lu.WrappedFun, tracers, params):
    tracers = map(self.to_jaxpr_tracer, tracers)
    rule = call_partial_eval_rules.get(primitive)
    if rule:
      return rule(self, primitive, f, tracers, params)

    update_params = call_param_updaters.get(primitive) or (lambda p, _, __: p)
    in_knowns, in_avals, in_consts = partition_pvals([t.pval for t in tracers])
    # TODO(mattjj): check in_avals are consistent with f.in_type

    # We want to partially evaluate this call into two calls: one evaluated now
    # taking known values (in_consts) as inputs and producing known values
    # (out_consts) as outputs, and the other staged out as an eqn into the jaxpr
    # being built. The latter takes as input residuals (res) produced as outputs
    # of the first call, shared closed-over values (env), and explicit arguments
    # which were unknown to the first call (corresponding to in_avals).

    # Wrap f to perform the partial evaluation and plumb out aux data.
    f_ = trace_to_subjaxpr_nounits_fwd(f, self.tag, f.debug_info, False)
    f_, aux = partial_eval_wrapper_nounits(f_, tuple(in_knowns), tuple(in_avals))

    # Adjust parameters (e.g. donated_invars) for the call to be evaluated now.
    const_params = update_params(params, in_knowns, 0)

    # Run the call, getting known out vals and aux data used for staged-out call
    fun_and_args = (_update_annotation_known(f_, f.in_type, in_knowns),) + tuple(in_consts)
    out = primitive.bind_with_trace(self.parent_trace, fun_and_args, const_params)
    fwds, out_knowns, out_type, jaxpr, env = aux()
    # Split apart known outputs from the original call and non-fwded residuals.
    out_consts, non_fwd_res = split_list(out, [sum(out_knowns)])

    # Form the complete list of residuals by forwarding some inputs.
    if config.dynamic_shapes.value:
      # With dynamic shapes, we may need to forward implicit arguments.
      assert f.in_type is not None, "f must be annotated with lu.annotate()"
      in_consts_, in_knowns_ = iter(in_consts), iter(in_knowns)
      in_consts_full = [None] * len(f.in_type)
      for idx, (aval, explicit) in enumerate(f.in_type):
        if explicit and next(in_knowns_):
          c = in_consts_full[idx] = next(in_consts_)
          if aval.shape:
            for d1, d2 in zip(aval.shape, c.shape):
              if type(d1) is DBIdx:
                in_consts_full[d1.val] = d2
    else:
      in_consts_full = in_consts
    res = subs_list(fwds, in_consts_full, non_fwd_res)

    # Create the input tracers for the staged-out (unknown-value) call.
    res_tracers = map(self.instantiate_const, map(self.new_const, res))
    env_tracers = map(self.to_jaxpr_tracer, env)
    unknown_arg_tracers = [t for t in tracers if not t.is_known()]
    # Adjust parameters (e.g. donated_invars) for the staged-out call's args.
    num_new_args = len(res_tracers) + len(env_tracers)
    new_jaxpr = convert_constvars_jaxpr(jaxpr)
    if isinstance(primitive, core.ClosedCallPrimitive):
      new_jaxpr = close_jaxpr(new_jaxpr)  # type: ignore
    staged_params = dict(params, call_jaxpr=new_jaxpr)
    staged_params = update_params(staged_params, map(op.not_, in_knowns),
                                  num_new_args)
    # The outputs of the staged-out call are Tracers with the new eqn as recipe.
    if config.dynamic_shapes.value:
      # With dynamic shapes, we may need to substitute Tracers into avals.
      out_tracers = []
      for aval, _ in out_type:
        if type(aval) is DShapedArray:
          shape = [[*res_tracers, *env_tracers, *unknown_arg_tracers][d.val]
                  if type(d) is InDBIdx else d for d in aval.shape]
          aval = aval.update(shape=tuple(shape))
        out_tracers.append(JaxprTracer(self, PartialVal.unknown(aval), None))
    else:
      out_tracers = [JaxprTracer(self, PartialVal.unknown(a), None)
                     for a in out_type]
    name_stack = self._current_truncated_name_stack()
    source = source_info_util.current().replace(name_stack=name_stack)
    eqn = new_eqn_recipe(self, (*res_tracers, *env_tracers, *unknown_arg_tracers),
                         out_tracers, primitive, staged_params, jaxpr.effects,
                         source)
    for t in out_tracers: t.recipe = eqn
    return merge_lists(out_knowns, out_tracers, out_consts)

  def process_map(self, primitive, f: lu.WrappedFun, tracers, params):
    tracers = map(self.to_jaxpr_tracer, tracers)
    update_params = call_param_updaters.get(primitive) or (lambda p, _, __: p)
    in_knowns, in_avals, in_consts = partition_pvals([t.pval for t in tracers])

    # This method is like process_call above, except:
    #   1. we delete an axis from mapped-over input avals' shapes, and
    #      analogously add an axis to mapped-over output avals' shapes;
    #   2. we update the in_axes and out_axes/out_axes_thunk parameters to
    #      reflect the inputs and outputs pruned from the unknown/known sides.

    # Map (delete an axis from) unknown inputs' avals as dictated by in_axes.
    unk_in_axes, const_in_axes = partition_list(in_knowns, params['in_axes'])
    in_avals_mapped = [mapped_aval(params['axis_size'], ax, aval)
                       for ax, aval in zip(unk_in_axes, in_avals)]

    # Wrap f to perform partial evaluation and plumb out aux data.
    f = trace_to_subjaxpr_nounits2(f, self.tag, f.debug_info, False)
    f, aux = partial_eval_wrapper_nounits(f, tuple(in_knowns),
                                          tuple(in_avals_mapped))
    # Adjust params for knowns (e.g. donated_invars, in_axes, out_axes_thunk)
    const_params = update_params(params, in_knowns, 0)  # handles donated_invars
    out_axes_thunk = params['out_axes_thunk']
    @as_hashable_function(closure=out_axes_thunk)
    def const_out_axes_thunk():
      out_knowns, _, jaxpr, _ = aux()
      _, out_axes = partition_list(out_knowns, out_axes_thunk())
      return tuple(out_axes) + (0,) * len(jaxpr.constvars)  # res mapped axis 0
    const_params = dict(const_params, in_axes=tuple(const_in_axes),
                        out_axes_thunk=const_out_axes_thunk)

    # Run the map, getting known out vals and aux data used for staged-out map.
    out = primitive.bind_with_trace(self.parent_trace, (f, *in_consts), const_params)
    out_knowns, out_avals_mapped, jaxpr, env = aux()
    # Split apart known outputs from the original call and residuals.
    out_consts, res = split_list(out, [len(out) - len(jaxpr.constvars)])

    # We can only check_jaxpr with the dynamic axis environment extended:
    with core.extend_axis_env_nd([(params['axis_name'], params['axis_size'])]):
      call_jaxpr = convert_constvars_jaxpr(jaxpr)

    # Compute staged and const out_axes, taking into account residuals.
    out_axes = params['out_axes_thunk']()
    staged_out_axes, _ = partition_list(out_knowns, out_axes)
    staged_in_axes = (0,) * len(res) + (None,) * len(env) + (*unk_in_axes,)

    # Create the input tracers for the staged-out (unknown-value) call.
    const_tracers = map(self.new_instantiated_const, res)
    env_tracers = map(self.to_jaxpr_tracer, env)
    unknown_arg_tracers = [t for t in tracers if not t.is_known()]
    # Adjust params for staged-out call on unknown values.
    num_new_args = len(const_tracers) + len(env_tracers)
    staged_params = update_params(params, map(op.not_, in_knowns), num_new_args)
    staged_params = dict(staged_params, in_axes=staged_in_axes,
                         out_axes=tuple(staged_out_axes), call_jaxpr=call_jaxpr)
    del staged_params['out_axes_thunk']
    # The outputs of the staged-out call are Tracers with the new eqn as recipe.
    out_avals = [unmapped_aval(params['axis_size'], ax, a)
                 for ax, a in zip(staged_out_axes, out_avals_mapped)]
    out_tracers = [JaxprTracer(self, PartialVal.unknown(a), None)
                   for a in out_avals]
    effs = core.filter_named_axis_effects(jaxpr.effects, {params['axis_name']})
    src_info = source_info_util.current()
    eqn = new_eqn_recipe(self, (*const_tracers, *env_tracers, *unknown_arg_tracers),
                         out_tracers, primitive, staged_params, effs, src_info)
    for t in out_tracers: t.recipe = eqn

    return merge_lists(out_knowns, out_tracers, out_consts)

  def _current_truncated_name_stack(self):
    return source_info_util.current_name_stack()[len(self.name_stack):]

  def process_custom_jvp_call(self, prim, fun, jvp, tracers, symbolic_zeros):
    tracers = map(self.to_jaxpr_tracer, tracers)
    if all(t.is_known() for t in tracers):
      with core.set_current_trace(self.parent_trace):
        vals = [t.pval[1] for t in tracers]
        return prim.bind(fun, jvp, *vals, symbolic_zeros=symbolic_zeros)
    # We assume non-trivial partial evaluation is only performed to build linear
    # functions, and hence we don't need to keep the custom JVP rule around.
    del jvp, symbolic_zeros
    with core.set_current_trace(self):
      return fun.call_wrapped(*tracers)

  def process_custom_transpose(self, prim, call, tracers, **params):
    tracers = map(self.to_jaxpr_tracer, tracers)
    res_ts, lin_ts = split_list(tracers, [params['res_tree'].num_leaves])
    assert all(t.is_known()     for t in res_ts)
    lin_all_known   = all(t.is_known()     for t in lin_ts)
    if lin_all_known:
      res_cvals = [t.pval[1] for t in res_ts]
      lin_cvals = [t.pval[1] for t in lin_ts]
      return prim.bind(call, *res_cvals, *lin_cvals, **params)
    else:
      out_tracers = [JaxprTracer(self, PartialVal.unknown(aval), None)
                     for aval in params['out_types']]
      in_tracers = map(self.instantiate_const, tracers)
      new_params = dict(params, call=call)
      eqn = new_eqn_recipe(self, in_tracers, out_tracers, prim, new_params,
          core.no_effects, source_info_util.current())
      for t in out_tracers: t.recipe = eqn
      return out_tracers

  def process_custom_vjp_call(self, prim, f, fwd, bwd, tracers, out_trees, symbolic_zeros):
    tracers = map(self.to_jaxpr_tracer, tracers)
    if all(t.is_known() for t in tracers):
      vals = [t.pval[1] for t in tracers]
      with core.set_current_trace(self.parent_trace):
        return prim.bind(f, fwd, bwd, *vals, out_trees=out_trees,
                         symbolic_zeros=symbolic_zeros)

    tracers = map(self.instantiate_const, tracers)
    in_knowns = (False,) * len(tracers)
    in_avals = tuple(t.aval for t in tracers)
    f_ = trace_to_subjaxpr_nounits2(f, self.tag, f.debug_info, True)
    f_, aux = partial_eval_wrapper_nounits(f_, in_knowns, in_avals)
    params = dict(out_trees=out_trees, symbolic_zeros=symbolic_zeros)
    res = prim.bind_with_trace(self.parent_trace, (f_, fwd, bwd), params)
    out_knowns, out_avals, jaxpr, env = aux()
    assert not any(out_knowns)
    res_tracers = map(self.instantiate_const, map(self.new_const, res))
    env_tracers = map(self.to_jaxpr_tracer, env)
    out_tracers = [JaxprTracer(self, PartialVal.unknown(a), None)
                   for a in out_avals]
    closed_jaxpr = close_jaxpr(convert_constvars_jaxpr(jaxpr))

    @partial(lu.wrap_init, debug_info=fwd.debug_info)
    @_memoize
    def fwd_jaxpr_thunk(*zeros):
      fwd_ = _interleave_fun(fwd, zeros)
      fwd_jaxpr, _, consts = trace_to_jaxpr_dynamic(fwd_, in_avals)
      return fwd_jaxpr, consts

    name_stack = self._current_truncated_name_stack()
    source = source_info_util.current().replace(name_stack=name_stack)
    params = dict(
        call_jaxpr=closed_jaxpr,
        fwd_jaxpr_thunk=fwd_jaxpr_thunk,
        num_consts=len(res) + len(env),
        bwd=bwd,
        out_trees=out_trees,
        symbolic_zeros=symbolic_zeros
    )
    eqn = new_eqn_recipe(self, (*res_tracers, *env_tracers, *tracers),
                         out_tracers, prim, params, jaxpr.effects, source)
    for t in out_tracers: t.recipe = eqn
    return out_tracers

def partition_pvals(
    pvals: list[PartialVal]
  ) -> tuple[list[bool], list[AbstractValue], list[Any]]:
  knowns = [pval.is_known()  for pval in pvals                       ]
  avals  = [pval.get_aval()  for pval in pvals if not pval.is_known()]
  consts = [pval.get_known() for pval in pvals if     pval.is_known()]
  return knowns, avals, consts

@lu.transformation_with_aux2
def partial_eval_wrapper_nounits(
    f: Callable,
    store: lu.Store,
    in_knowns: Sequence[bool],
    in_avals: Sequence[AbstractValue],
    *in_consts: Any):
  in_avals_, in_consts_ = iter(in_avals), iter(in_consts)
  in_pvals = [PartialVal.known(next(in_consts_)) if known else
              PartialVal.unknown(next(in_avals_)) for known in in_knowns]
  sentinel = object()
  assert next(in_avals_, sentinel) is next(in_consts_, sentinel) is sentinel
  jaxpr, (*maybe_fwds, out_pvals, res, env) = f(in_pvals)
  out_knowns, out_avals, out_consts = partition_pvals(out_pvals)
  store.store((*maybe_fwds, out_knowns, out_avals, jaxpr, env))
  return (*out_consts, *res)

@lu.transformation_with_aux2
def partial_eval_wrapper_nounits2(
    f: Callable,
    store: lu.Store,
    in_knowns: Sequence[bool],
    in_avals: Sequence[AbstractValue],
    *in_consts: Any):
  in_avals_, in_consts_ = iter(in_avals), iter(in_consts)
  in_pvals = [PartialVal.known(next(in_consts_)) if known else
              PartialVal.unknown(next(in_avals_)) for known in in_knowns]
  sentinel = object()
  assert next(in_avals_, sentinel) is next(in_consts_, sentinel) is sentinel
  jaxpr, (*maybe_fwds, out_pvals, res, env) = f(in_pvals)
  out_knowns, _, out_consts = partition_pvals(out_pvals)
  res_avals = [core.typeof(r) for r in res]
  store.store((*maybe_fwds, out_knowns, res_avals, jaxpr, env))
  return (*out_consts, *res)

custom_partial_eval_rules: dict[Primitive, Callable] = {}
call_partial_eval_rules: dict[Primitive, Callable] = {}
call_param_updaters: dict[Primitive, Callable] = {}

def abstract_eval_fun(fun: Callable, *avals,
                      debug_info: core.DebugInfo, **params):
  _, avals_out, _ = trace_to_jaxpr_dynamic(
      lu.wrap_init(fun, params, debug_info=debug_info), avals)
  assert all(isinstance(aval, AbstractValue) for aval in avals_out)
  return avals_out


JaxprTracerRecipe = Union[
    'JaxprEqnRecipe', 'LambdaBinding', 'FreeVar', 'ConstVar', Literal,
]

class JaxprTracer(Tracer):
  __slots__ = ['pval', 'recipe']

  def __init__(self, trace: JaxprTrace, pval: PartialVal,
               recipe: JaxprTracerRecipe | None):
    assert isinstance(pval, PartialVal)
    pv, const = pval
    self._trace = trace
    self.pval = pval
    self.recipe = recipe

  def __repr__(self):
    return f'Traced<{self.aval}:{self._trace}>'

  @property
  def aval(self) -> AbstractValue:
    return self.pval.get_aval()

  @property
  def parents(self) -> Sequence[JaxprTracer]:
    if isinstance(self.recipe, JaxprEqnRecipe):
      # TODO broadcast_in_dim can create a new tracer...
      return self.recipe.in_tracers
    elif isinstance(self.aval, DShapedArray):
      return [d for d in self.aval.shape if isinstance(d, JaxprTracer)]
    else:
      return []

  def full_lower(self):
    known = self.pval.get_known()
    if known is not None:
      return core.full_lower(known)
    else:
      return self

  def is_known(self):
    return self.pval.is_known()

  def get_referent(self):
    if self.pval.is_known():
      return get_referent(self.pval.get_known())
    elif isinstance(self.recipe, (FreeVar, ConstVar, Literal)):
      return get_referent(self.recipe.val)  # pytype: disable=attribute-error
    else:
      return self


@profiler.annotate_function
def trace_to_jaxpr_nounits(
    fun: lu.WrappedFun, pvals: Sequence[PartialVal],
    instantiate: bool | Sequence[bool] = False,
  ) -> tuple[Jaxpr, list[PartialVal], list[core.Value]]:
  current_name_stack = source_info_util.current_name_stack()
  with core.take_current_trace() as parent_trace:
    trace = JaxprTrace(parent_trace, current_name_stack, TraceTag())
    with core.ensure_no_leaks(trace):
      fun = trace_to_subjaxpr_nounits(fun, trace, instantiate, fun.debug_info)
      with core.set_current_trace(trace):
        jaxpr, (out_pvals, consts, env) = fun.call_wrapped(pvals)
        assert not env
      del trace, fun
      return jaxpr, out_pvals, consts

# TODO(mattjj): superfluous wrapper...?
@lu.transformation2
def trace_to_subjaxpr_nounits(
    f: Callable,
    trace: JaxprTrace,
    instantiate: Sequence[bool] | bool,
    debug_info: core.DebugInfo,
    in_pvals: Sequence[PartialVal]):
  assert all(isinstance(pv, PartialVal) for pv in in_pvals), in_pvals
  out_tracers, jaxpr, out_consts, env = _trace_to_subjaxpr_nounits(
      f, trace, instantiate, in_pvals, debug_info)
  out_pvals = [t.pval for t in out_tracers]
  del out_tracers
  return jaxpr, (out_pvals, out_consts, env)

@lu.transformation2
def trace_to_subjaxpr_nounits2(
    f: Callable,
    tag: TraceTag,
    debug_info: core.DebugInfo,
    instantiate: bool | Sequence[bool],
    in_pvals: Sequence[PartialVal]):
  assert isinstance(tag, TraceTag)
  assert all(isinstance(pv, PartialVal) for pv in in_pvals), in_pvals
  current_name_stack = source_info_util.current_name_stack()
  with core.take_current_trace() as parent_trace:
    trace = JaxprTrace(parent_trace, current_name_stack, tag)
    out_tracers, jaxpr, out_consts, env = _trace_to_subjaxpr_nounits(
        f, trace, instantiate, in_pvals, debug_info)
    out_pvals = [t.pval for t in out_tracers]
    del out_tracers
  return jaxpr, (out_pvals, out_consts, env)

def _trace_to_subjaxpr_nounits(f: Callable, trace: JaxprTrace,
                               instantiate: Sequence[bool] | bool,
                               in_pvals: Sequence[PartialVal],
                               debug_info: core.DebugInfo):
  in_knowns  = [pval.is_known()     for pval in in_pvals]
  in_consts  = [pval.get_known()    for pval in in_pvals if     pval.is_known()]
  in_tracers = [trace.new_arg(pval) for pval in in_pvals if not pval.is_known()]
  in_args = merge_lists(in_knowns, in_tracers, in_consts)
  with core.set_current_trace(trace):
    ans = f(*in_args)
  assert isinstance(ans, (list, tuple)), (
      f"Got unexpected return type when tracing function to jaxpr: {ans}")
  assert all(isinstance(x, core.Tracer) or core.valid_jaxtype(x) for x in ans), (
      f"Got unexpected return type when tracing function to jaxpr: {ans}")
  if isinstance(instantiate, bool):
    instantiate = [instantiate] * len(ans)
  out_tracers = map(trace.to_jaxpr_tracer, ans)
  out_tracers = [trace.instantiate_const(t) if inst else t
                 for inst, t in zip(instantiate, out_tracers)]
  out_tracers_ = [t for t in out_tracers if not t.is_known()]
  jaxpr, out_consts, env = tracers_to_jaxpr(in_tracers, out_tracers_, trace.effect_handles, debug_info)
  return out_tracers, jaxpr, out_consts, env

# The below variant implements an optimization where residuals which are also
# inputs are indicated in auxiliary data rather than passed as outputs.
# TODO(mattjj): update all callers to use this version, delete other version.
@lu.transformation2
def trace_to_subjaxpr_nounits_fwd(
    f: Callable,
    tag: TraceTag,
    debug_info: core.DebugInfo,
    instantiate: bool | Sequence[bool],
    in_pvals: Sequence[PartialVal]):
  assert all(isinstance(pv, PartialVal) for pv in in_pvals), in_pvals
  current_name_stack = source_info_util.current_name_stack()
  with core.take_current_trace() as parent_trace:
    trace = JaxprTrace(parent_trace, current_name_stack, tag)
    with core.set_current_trace(trace):
      out_tracers, jaxpr, out_consts, env = _trace_to_subjaxpr_nounits(
          f, trace, instantiate, in_pvals, debug_info)
    out_pvals = [t.pval for t in out_tracers]

    # Which out_consts (aka residuals) are just forwarded inputs? Check obj id.
    in_consts  = [pval.get_known()    for pval in in_pvals if     pval.is_known()]
    id_map = {id(c): i for i, c in enumerate(in_consts)}
    fwds: list[int | None] = [id_map.get(id(c)) for c in out_consts]
    pruned_consts = [c for c, fwd in zip(out_consts, fwds) if fwd is None]

    del out_tracers
  return jaxpr, (fwds, out_pvals, pruned_consts, env)

# The below variant implements two optimizations:
#  1. residuals that are also primal inputs are indicated in aux data rather
#     than passed as outputs;
#  2. residuals that are also primal outputs are indicated in aux data rather
#     than passed as redundant outputs.
@lu.transformation2
def trace_to_subjaxpr_nounits_fwd2(
    f: Callable,
    tag: TraceTag,
    debug_info: core.DebugInfo,
    instantiate: bool | Sequence[bool],
    in_pvals: Sequence[PartialVal]):
  assert all(isinstance(pv, PartialVal) for pv in in_pvals), in_pvals
  current_name_stack = source_info_util.current_name_stack()
  with core.take_current_trace() as parent_trace:
    trace = JaxprTrace(parent_trace, current_name_stack, tag)
    out_tracers, jaxpr, consts, env = _trace_to_subjaxpr_nounits(
        f, trace, instantiate, in_pvals, debug_info)
    out_pvals = [t.pval for t in out_tracers]

  # Which consts (aka residuals) are just forwarded inputs? Check obj id.
  in_consts  = [pval.get_known()    for pval in  in_pvals if    pval.is_known()]
  id_map = {id(c): i for i, c in enumerate(in_consts)}
  input_fwds: list[int | None] = [id_map.get(id(c)) for c in consts]

  # Which consts (aka residuals) are already primal outputs? Check obj id.
  out_consts = [pval.get_known()    for pval in out_pvals if    pval.is_known()]
  id_map = {id(c): i for i, c in enumerate(out_consts)}
  output_fwds: list[int | None] = [id_map.get(id(c)) for c in consts]

  pruned_consts = [c for c, f1, f2 in zip(consts, input_fwds, output_fwds)
                   if f1 is None and f2 is None]

  del out_tracers
  return jaxpr, (input_fwds, output_fwds, out_pvals, pruned_consts, env)


FreeVar = namedtuple('FreeVar', ['val'])
ConstVar = namedtuple('ConstVar', ['val'])
LambdaBinding = namedtuple('LambdaBinding', [])
class JaxprEqnRecipe(NamedTuple):
  eqn_id: Any
  in_tracers: Sequence[JaxprTracer]
  out_tracer_refs: Sequence[ref[JaxprTracer]]
  out_avals: Sequence[core.AbstractValue]
  primitive: Primitive
  params: dict[str, Any]
  effects: core.Effects
  source_info: source_info_util.SourceInfo
  ctx: JaxprEqnContext

def new_eqn_recipe(trace: JaxprTrace,
                   in_tracers: Sequence[JaxprTracer],
                   out_tracers: Sequence[JaxprTracer],
                   primitive: Primitive,
                   params: dict[str, Any],
                   effects: core.Effects,
                   source_info: source_info_util.SourceInfo,
                   ctx: JaxprEqnContext | None = None) -> JaxprEqnRecipe:
  # TODO(necula): move these checks to core.check_jaxpr, and call in more places
  if primitive.call_primitive or primitive.map_primitive:
    assert "call_jaxpr" in params
    assert ("donated_invars" not in params or
            len(params["donated_invars"]) == len(params["call_jaxpr"].invars))
  if primitive.map_primitive:
    assert ("in_axes" in params and
            len(params["in_axes"]) == len(params["call_jaxpr"].invars))
    assert ("donated_invars" in params and
            len(params["donated_invars"]) == len(params["call_jaxpr"].invars))
  out_avals = [t.aval for t in out_tracers]
  ctx = ctx or JaxprEqnContext(
      compute_on.current_compute_type(),
      config.threefry_partitionable.value,
      xla_metadata_lib.current_xla_metadata(),
  )
  return JaxprEqnRecipe(next(trace.counter), tuple(in_tracers), map(ref, out_tracers),
                        out_avals, primitive, params, effects, source_info,
                        ctx)


def recipe_to_eqn(getvar: Callable[[JaxprTracer], Atom],
                  recipe: JaxprEqnRecipe) -> core.JaxprEqn:
  (_, in_tracers, out_tracer_refs, out_avals, prim, params, eff, src,
   ctx) = recipe
  invars  = [getvar(t) for t in in_tracers]
  out_tracers = [t_ref() for t_ref in out_tracer_refs]
  outvars = [DropVar(a) if t is None else getvar(t)
             for a, t in zip(out_avals, out_tracers)]
  return new_jaxpr_eqn(invars, outvars, prim, params, eff, src, ctx)

def tracers_to_jaxpr(
  in_tracers: Sequence[JaxprTracer],
  out_tracers: Sequence[JaxprTracer],
  effect_handles: Sequence[Any],
  debug_info: core.DebugInfo,
  ) -> tuple[Jaxpr, tuple[Any, ...], tuple[Any, ...]]:
  """Constructs Jaxpr given tracers for inputs and outputs.

  Params:
    in_tracers: the tracers that were created for the function inputs
    out_tracers: the tracers that were output by the function.
    debug_info: the debug info for the function.

  Returns: a triple of a `Jaxpr`, a list of constant values corresponding to
    the `constvars` in the returned Jaxps, and a list of environment values.
    The vars for the environment values have been prepended to the Jaxpr's
    `invars`.
  """
  gensym = core.gensym()

  t_to_var: dict[TracerId, Var] = {}
  consts: dict[Var, Any] = {}
  env: dict[Var, JaxprTracer] = {}
  constid_to_var: dict[ConstId, Var] = {}  # for deduplication

  def get_atom(t: JaxprTracer) -> Atom:
    return t.recipe if type(t.recipe) is Literal else t_to_var[id(t)]

  def newvar(t: JaxprTracer | None) -> Var:
    assert t is not None
    var = gensym(type_substitute(t.aval))
    var_ = t_to_var.setdefault(id(t), var)
    assert var is var_
    return var

  def type_substitute(aval: AbstractValue) -> AbstractValue:
    if isinstance(aval, DShapedArray):
      # Replace any Tracers in aval.shape with Vars or Literal values
      shape = [get_atom(d) if type(d) is JaxprTracer else d for d in aval.shape]
      shape = [d.val if type(d) is Literal else d for d in shape]
      aval = aval.update(shape=tuple(shape))
    return aval

  processed_eqn_ids = set()
  eqns: list[core.JaxprEqn] = []

  reachable = toposort
  tracers = reachable((*in_tracers, *out_tracers, *effect_handles))
  def sort_key(t):
    r = t.recipe
    return r.eqn_id if isinstance(r, JaxprEqnRecipe) else -1
  tracers = sorted(tracers, key=sort_key)

  for t in tracers:
    r = t.recipe
    if isinstance(r, JaxprEqnRecipe):
      # TODO broadcast_in_dim can create a new tracer, not present in parents
      if r.eqn_id not in processed_eqn_ids:
        in_atoms = map(get_atom, r.in_tracers)
        outvars = [DropVar(type_substitute(a)) if rf() is None else newvar(rf())
                   for a, rf in zip(r.out_avals, r.out_tracer_refs)]
        eqns.append(new_jaxpr_eqn(in_atoms, outvars, r.primitive, r.params,
                                  r.effects, r.source_info, r.ctx))
        processed_eqn_ids.add(r.eqn_id)
    elif isinstance(r, LambdaBinding):
      if not any(t is in_tracer for in_tracer in in_tracers):
        raise core.escaped_tracer_error(t, f"Tracer not in input tracers: {t}")
      newvar(t)
    elif isinstance(r, ConstVar):
      var = constid_to_var.get(id(r.val))
      if var is None:
        var = constid_to_var[id(r.val)] = newvar(t)
        consts[var] = r.val
      t_to_var[id(t)] = var
    elif isinstance(r, FreeVar):
      env[newvar(t)] = r.val
    elif isinstance(r, Literal):
      pass
    elif r is None:
      assert False
    else:
      raise TypeError(r)

  env_vars, env_vals = unzip2(env.items())
  invars = [*env_vars, *map(get_atom, in_tracers)]
  const_vars, const_vals = unzip2(consts.items())
  outvars = map(get_atom, out_tracers)  # type: ignore[arg-type]
  jaxpr_effects = make_jaxpr_effects(const_vars, invars, outvars, eqns)
  jaxpr = Jaxpr(const_vars, invars,  # type: ignore[arg-type]
                outvars, eqns, jaxpr_effects,
                debug_info)
  config.enable_checks.value and core.check_jaxpr(jaxpr)
  # del getvar  # needed to avoid cyclic-reference closure, apparently!
  return jaxpr, const_vals, env_vals

@weakref_lru_cache
def move_envvars(jaxpr: Jaxpr, which: tuple[bool, ...]) -> Jaxpr:
  constvars, envvars = partition_list(which, jaxpr.constvars)
  return jaxpr.replace(constvars=constvars, invars=[*envvars, *jaxpr.invars])

@weakref_lru_cache
def convert_constvars_jaxpr(jaxpr: Jaxpr) -> Jaxpr:
  """Moves the constvars to the start of invars."""
  config.enable_checks.value and core.check_jaxpr(jaxpr)
  dbg = jaxpr.debug_info._replace(
      arg_names=("",) * len(jaxpr.constvars) + (*jaxpr.debug_info.arg_names,))
  lifted_jaxpr = jaxpr.replace(
      constvars=(), invars=jaxpr.constvars + jaxpr.invars, debug_info=dbg)
  config.enable_checks.value and core.check_jaxpr(lifted_jaxpr)
  return lifted_jaxpr

@weakref_lru_cache
def convert_invars_to_constvars(jaxpr: Jaxpr, n: int) -> Jaxpr:
  """Move n invars to constvars. Like an inverse of convert_constvars_jaxpr."""
  if n == 0:
    return jaxpr.replace()  # 'return jaxpr' would create cache reference cycle
  config.enable_checks.value and core.check_jaxpr(jaxpr)
  constvars, invars = split_list(jaxpr.invars, [n])
  dbg = jaxpr.debug_info._replace(
      arg_names=jaxpr.debug_info.arg_names[n:])
  lifted_jaxpr = jaxpr.replace(constvars=tuple(constvars), invars=invars,
                               debug_info=dbg)
  config.enable_checks.value and core.check_jaxpr(lifted_jaxpr)
  return lifted_jaxpr

def convert_envvars_to_constvars(jaxpr: Jaxpr, num_env_vars: int) -> Jaxpr:
  if any(isinstance(eff, effects.JaxprInputEffect) for eff in jaxpr.effects):
    raise NotImplementedError
  config.enable_checks.value and core.check_jaxpr(jaxpr)
  env_vars, invars = split_list(jaxpr.invars, [num_env_vars])
  converted_jaxpr = jaxpr.replace(constvars=jaxpr.constvars + env_vars,
                                  invars=invars)
  config.enable_checks.value and core.check_jaxpr(converted_jaxpr)
  return converted_jaxpr


def partial_eval_jaxpr_nounits(
    jaxpr: ClosedJaxpr, unknowns: Sequence[bool],
    instantiate: bool | Sequence[bool],
  ) -> tuple[ClosedJaxpr, ClosedJaxpr, list[bool], list[AbstractValue]]:
  """Unzip a jaxpr in two by data dependence into 'known' and 'unknown' parts.

  That is, given a jaxpr and a sequence of booleans indicating which jaxpr
  inputs (i.e. invars) are considered unknown, produce two jaxprs, a list of
  booleans representing which of the original jaxpr's outputs are unknown (i.e.
  have a data dependence on an unknown input), and a list of abstract values
  representing residuals (part of the first jaxpr's output and the second
  jaxpr's input). The two jaxprs result from partitioning the original jaxpr's
  first-order primitive applications based on whether all the inputs to the
  application are known (in which case the application is represented in the
  'known' jaxpr and its result is considered known) or whether any inputs to the
  application are unknown (in which case the application is represented in the
  'unknown' jaxpr and its result is considered unknown). Higher-order primitives
  are recursively unzipped in two.

  The `instantiate` argument can be used to ensure some outputs are lifted into
  the 'unknown' jaxpr.

  For example, give an input jaxpr:

    { lambda ; a:f32[] b:f32[]. let
        c:f32[] = cos a
        d:f32[] = sin a
        e:f32[] = neg d
        f:f32[] = mul e b
      in (c, f) }

  then applying this function with `unknowns=[False, True]` and
  `instantiate=False` produces as an output triple:

    # jaxpr_known
    { lambda ; a:f32[]. let
       b:f32[] = cos a
       c:f32[] = sin a
       d:f32[] = neg c
     in (b, d) }

    # jaxpr_unknown
    { lambda ; a:f32[] b:f32[]. let c:f32[] = mul b a in (c,) }

    # out_unknowns
    [False, True]

  Notice in particular that the first output (jaxpr_known) contains all the
  primitive applications which do not have a data dependence on an unknown
  input. Also notice the input and output types: the input type of the first
  jaxpr produced represents the type of the known inputs of the original jaxpr,
  and the output type of the second jaxpr produced represents the type of the
  unknown outputs of the original jaxpr.

  In the above example, the output of jaxpr_known named `d` is a _residual_
  output, and corresponds to the input named `a` in jaxpr_unknown. In general,
  jaxpr_known will produce extra outputs (at the end of its output list)
  corresponding to intermediate values of the original jaxpr which must be
  passed to jaxpr_unknown (as leading inputs).
  """
  instantiate = tuple(instantiate) if isinstance(instantiate, list) else instantiate
  return _partial_eval_jaxpr_nounits(jaxpr, tuple(unknowns), instantiate, False)[:-1]

def partial_eval_jaxpr_nounits_fwd(
    jaxpr: ClosedJaxpr, unknowns: Sequence[bool],
    instantiate: bool | Sequence[bool],
    fwd: bool | Sequence[bool] = True,
) -> tuple[ClosedJaxpr, ClosedJaxpr, list[bool], list[AbstractValue], list[int | None]]:
  instantiate = tuple(instantiate) if isinstance(instantiate, list) else instantiate
  fwd = tuple(fwd) if isinstance(fwd, list) else fwd
  return _partial_eval_jaxpr_nounits(jaxpr, tuple(unknowns), instantiate, fwd)

@weakref_lru_cache
def _partial_eval_jaxpr_nounits(
    jaxpr: ClosedJaxpr, in_unknowns: Sequence[bool],
    instantiate: bool | Sequence[bool], fwd: bool | Sequence[bool]):
  f = lu.wrap_init(core.jaxpr_as_fun(jaxpr), debug_info=jaxpr.jaxpr.debug_info)

  cell = []
  def fun(*known_vals_in):
    known_vals_in_ = iter(known_vals_in)
    unknown_avals = (a for a, uk in zip(jaxpr.in_avals, in_unknowns) if uk)
    in_pvals = [PartialVal.unknown(next(unknown_avals)) if uk
                else PartialVal.known(next(known_vals_in_)) for uk in in_unknowns]
    assert next(known_vals_in_, None) is next(unknown_avals, None) is None
    jaxpr_unknown_, (fwds, out_pvals, residuals, ()) = trace_to_subjaxpr_nounits_fwd(
        f, TraceTag(), jaxpr.jaxpr.debug_info, instantiate).call_wrapped(in_pvals)
    jaxpr_unknown = convert_constvars_jaxpr(jaxpr_unknown_)
    out_unknowns = [not pval.is_known() for pval in out_pvals]
    if type(fwd) is bool and not fwd:
      residuals_ = iter(residuals)
      residuals = [next(residuals_) if f is None else known_vals_in[f]
                   for f in fwds]
      assert next(residuals_, None) is None
      fwds = [None] * len(fwds)
    else:
      if type(fwd) is tuple:
        fwd_ = [f for f, uk in zip(fwd, in_unknowns) if not uk]
        residuals_, residuals = iter(residuals), []
        fwds = [residuals.append(next(residuals_)) if f is None else
                residuals.append(known_vals_in[f]) if not fwd_[f] else
                f for f in fwds]
      fwds, residuals = _include_consts_in_fwds(jaxpr.consts, fwds, residuals)
    res_avals = [core.get_aval(r) for r in residuals]
    cell.append((out_unknowns, jaxpr_unknown, res_avals, fwds))
    known_vals_out = [pval.get_known() for pval in out_pvals if pval.is_known()]
    return [*known_vals_out, *residuals]

  known_avals = [a for a, uk in zip(jaxpr.in_aval_qdds, in_unknowns) if not uk]
  jaxpr_known, _, consts_known = trace_to_jaxpr_dynamic(
      lu.wrap_init(fun, debug_info=f.debug_info), known_avals)
  (out_unknowns, jaxpr_unknown, res_avals, fwds), = cell  # pytype: disable=bad-unpacking

  if config.enable_checks.value:
    core.check_jaxpr(jaxpr_known)
    core.check_jaxpr(jaxpr_unknown)

  closed_jaxpr_known = ClosedJaxpr(jaxpr_known, consts_known)
  closed_jaxpr_unknown = ClosedJaxpr(jaxpr_unknown, ())
  return closed_jaxpr_known, closed_jaxpr_unknown, out_unknowns, res_avals, fwds

def _include_consts_in_fwds(consts, fwds, residuals):
  if all(f is None for f in fwds):
    return fwds, residuals
  dummys = [object() for _ in range(max(f for f in fwds if f is not None) + 1)]
  residuals_ = iter(residuals)
  residuals = [next(residuals_) if f is None else dummys[f] for f in fwds]
  assert next(residuals_, None) is None
  idxs = {id(x): i for i, x in enumerate((*consts, *dummys))}
  fwds = [idxs.get(id(r)) for r in residuals]
  residuals = [r for r in residuals if id(r) not in idxs]
  return fwds, residuals


def partial_eval_jaxpr_custom(
    jaxpr: Jaxpr,
    in_unknowns: Sequence[bool],
    in_inst: bool | Sequence[bool],
    ensure_out_unknowns: bool | Sequence[bool],
    ensure_out_inst: bool | Sequence[bool],
    saveable: Callable[..., RematCases_],
  ) -> tuple[Jaxpr, Jaxpr, list[bool], list[bool], int]:
  *outs, num_res_ref = partial_eval_jaxpr_stateful(
      jaxpr, in_unknowns, in_inst, ensure_out_unknowns, ensure_out_inst, saveable)
  if num_res_ref:
    raise ValueError("Cannot use `partial_eval_jaxpr_custom` with stateful jaxprs.")
  return *outs,  # type: ignore

def partial_eval_jaxpr_stateful(
    jaxpr: Jaxpr,
    in_unknowns: Sequence[bool],
    in_inst: bool | Sequence[bool],
    ensure_out_unknowns: bool | Sequence[bool],
    ensure_out_inst: bool | Sequence[bool],
    saveable: Callable[..., RematCases_] | None,
  ) -> tuple[Jaxpr, Jaxpr, list[bool], list[bool], int, int]:
  if type(in_inst) is bool:
    in_inst = (in_inst,) * len(jaxpr.invars)
  if type(ensure_out_unknowns) is bool:
    ensure_out_unknowns = (ensure_out_unknowns,) * len(jaxpr.outvars)
  if type(ensure_out_inst) is bool:
    ensure_out_inst = (ensure_out_inst,) * len(jaxpr.outvars)
  if saveable is None:
    saveable = everything_saveable
  jaxpr_known, jaxpr_staged, out_unknowns, out_inst, num_res, num_res_ref = \
      _partial_eval_jaxpr_custom_cached(
          jaxpr, tuple(in_unknowns), tuple(in_inst), tuple(ensure_out_unknowns),
          tuple(ensure_out_inst), saveable)
  return jaxpr_known, jaxpr_staged, out_unknowns, out_inst, num_res, num_res_ref

everything_saveable = lambda *_, **__: True

@weakref_lru_cache
def _partial_eval_jaxpr_custom_cached(
    jaxpr: Jaxpr,
    in_unknowns: tuple[bool, ...],
    in_inst: tuple[bool, ...],
    ensure_out_unknowns: tuple[bool, ...],
    ensure_out_inst: tuple[bool, ...],
    saveable: Callable[..., RematCases_],
  ) -> tuple[Jaxpr, Jaxpr, list[bool], list[bool], int, int]:
  env: dict[Var, tuple[bool, bool]] = {}
  residuals: OrderedSet[Var] = OrderedSet()
  residual_refs: OrderedSet[Var] = OrderedSet()

  def read(x: Atom) -> tuple[bool, bool]:
    if type(x) is Var:
      return env[x]
    return (False, True)

  def write(unk: bool, inst: bool, v: Var) -> None:
    assert (unk, inst) != (True, False)
    env[v] = (unk, inst)

  def ensure_instantiated(inst: bool, x: Atom) -> Atom:
    if type(x) is Var and not inst:
      residuals.add(x)
    return x

  def has_effects(effects) -> bool:
    return bool({e for e in effects if not isinstance(e, core.NamedAxisEffect)})

  known_eqns, staged_eqns = [], []
  foreach(write, in_unknowns, in_inst, jaxpr.invars)
  foreach(partial(write, False, True), jaxpr.constvars)
  for eqn in jaxpr.eqns:
    unks_in, inst_in = unzip2(map(read, eqn.invars))
    rule = partial_eval_jaxpr_custom_rules.get(eqn.primitive)
    if rule:
      eqn1, eqn2, unks_out, inst_out, res = rule(saveable, unks_in, inst_in, eqn)
      eqn1 and known_eqns.append(eqn1); eqn2 and staged_eqns.append(eqn2)  # type: ignore
      for r in res:
        if isinstance(r.aval, AbstractRef):
          residual_refs.add(r)
        else:
          residuals.add(r)
      foreach(write, unks_out, inst_out, eqn.outvars)
    elif any(unks_in):
      inputs = map(ensure_instantiated, inst_in, eqn.invars)
      staged_eqns.append(eqn.replace(invars=inputs))
      foreach(partial(write, True, True), eqn.outvars)
    else:
      known_eqns.append(eqn)
      # If it's an effectful primitive, we always to run and avoid staging it.
      policy = ensure_enum(saveable(
          eqn.primitive, *[x.aval for x in eqn.invars], **eqn.params))
      if has_effects(eqn.effects) or isinstance(policy, SaveableType):
        foreach(partial(write, False, False), eqn.outvars)
      elif isinstance(policy, Offloadable):
        # TODO(slebedev): This is a legit error which requires a BUILD fix.
        from jax._src.dispatch import device_put_p, TransferToMemoryKind, CopySemantics  # pytype: disable=import-error
        resvars = [Var(v.aval) for v in eqn.outvars]
        outvars_copy = list[Atom](eqn.outvars)
        offload_eqn = core.JaxprEqn(
            outvars_copy, resvars, device_put_p,
            dict(
                devices=(TransferToMemoryKind(policy.dst),) * len(outvars_copy),
                srcs=(None,),
                copy_semantics=(CopySemantics.COPY,),
            ),
            set(), source_info_util.new_source_info(),
            JaxprEqnContext(None, False))
        known_eqns.append(offload_eqn)
        # resvars are known and available in the backward jaxpr.
        foreach(partial(write, False, True), resvars)
        residuals.update(resvars)
        reload_eqn = core.JaxprEqn(
            resvars, eqn.outvars, device_put_p,
            dict(
              devices=(TransferToMemoryKind(policy.src),) * len(resvars),
              srcs=(None,),
              copy_semantics=(CopySemantics.COPY,)
            ),
            set(), source_info_util.new_source_info(),
            JaxprEqnContext(None, False))
        staged_eqns.append(reload_eqn)
        # outvars are known and available in the backward jaxpr.
        foreach(partial(write, False, True), eqn.outvars)
      else:
        assert isinstance(policy, RecomputeType)
        inputs = map(ensure_instantiated, inst_in, eqn.invars)
        staged_eqns.append(eqn.replace(invars=inputs))
        foreach(partial(write, False, True), eqn.outvars)
  unzipped = unzip2(map(read, jaxpr.outvars))
  out_unknowns, out_inst = list(unzipped[0]), list(unzipped[1])
  assert all(type(v) is Var for v in residuals), residuals

  for x, inst, ensure_inst in zip(jaxpr.outvars, out_inst, ensure_out_inst):
    if ensure_inst: ensure_instantiated(inst, x)
  out_unknowns = map(op.or_, out_unknowns, ensure_out_unknowns)
  out_inst     = map(op.or_, out_inst,     ensure_out_inst)


  ins_known, _ = partition_list(in_unknowns, jaxpr.invars)
  outs_known, _ = partition_list(out_unknowns, jaxpr.outvars)
  ref_res_is_input = [r in ins_known for r in residual_refs]
  non_input_res_refs, _ = partition_list(ref_res_is_input, list(residual_refs))
  ins_known_and_ref_res = [*ins_known, *non_input_res_refs]
  known_outvars = [*outs_known, *residuals]
  known_effects = make_jaxpr_effects(jaxpr.constvars, ins_known_and_ref_res,
                                     known_outvars, known_eqns)

  # TODO(mattjj,necula): debug info should be updated here
  jaxpr_known = jaxpr.replace(
      invars=ins_known_and_ref_res, outvars=known_outvars,
      eqns=known_eqns, effects=known_effects)
  config.enable_checks.value and core.check_jaxpr(jaxpr_known)

  _, ins_staged = partition_list(in_inst, jaxpr.invars)
  _, outs_staged = partition_list(out_inst, jaxpr.outvars)
  staged_invars = [*residuals, *non_input_res_refs, *ins_staged]
  staged_effects = make_jaxpr_effects(jaxpr.constvars, staged_invars,
                                      outs_staged, staged_eqns)
  # TODO(mattjj,necula): debug info should be updated here
  jaxpr_staged = jaxpr.replace(
      invars=staged_invars, outvars=outs_staged, eqns=staged_eqns,
      effects=staged_effects)
  config.enable_checks.value and core.check_jaxpr(jaxpr_staged)

  return (jaxpr_known, jaxpr_staged, out_unknowns, out_inst, len(residuals),
          len(non_input_res_refs))


MemoryKind = str

class RecomputeType: pass
Recompute = RecomputeType()

class SaveableType: pass
Saveable = SaveableType()

class Offloadable(NamedTuple):
  src: MemoryKind
  dst: MemoryKind

RematCases = Union[RecomputeType, SaveableType, Offloadable]
RematCases_ = Union[RematCases, bool]

def ensure_enum(case: bool | RematCases) -> RematCases:
  if isinstance(case, bool):
    return Saveable if case else Recompute
  return case

# A primitive rule for policy-driven partial evaluation returns a 5-tuple
# with the components representing, respectively:
#  * the JaxprEqn for the 'known' side (or None if there is no known component),
#  * the JaxprEqn for the 'unknown' side (or None),
#  * a list of booleans indicating which of the original outputs are unknown,
#  * a list of booleans indicating which of the original outputs are
#    instantiated (i.e. available) in the 'unknown' side,
#  * a list of Var instances representing residuals to be added (i.e. to be
#    plumbed as outputs of the 'known' side jaxpr and added as input binders to
#    the 'unknown' jaxpr).
PartialEvalCustomResult = tuple[Union[JaxprEqn, None], Union[JaxprEqn, None],
                                Sequence[bool], Sequence[bool], list[Var]]
PartialEvalCustomRule = Callable[
    [Callable[..., RematCases_], Sequence[bool], Sequence[bool], JaxprEqn],
    PartialEvalCustomResult]
partial_eval_jaxpr_custom_rules: dict[Primitive, PartialEvalCustomRule] = {}

def partial_eval_jaxpr_custom_rule_not_implemented(
    name: str, saveable: Callable[..., RematCases_], unks_in: Sequence[bool],
    inst_in: Sequence[bool], eqn: JaxprEqn) -> PartialEvalCustomResult:
  msg = (f'custom-policy remat rule not implemented for {name}, '
         'open a feature request at https://github.com/jax-ml/jax/issues!')
  raise NotImplementedError(msg)


ParamsUpdater = Callable[[Sequence[bool], Sequence[bool], Sequence[bool],
                          Sequence[bool], int, dict, dict],
                         tuple[dict, dict]]
ResAvalUpdater = Callable[[dict[str, Any], AbstractValue], AbstractValue]
def _default_res_aval_updater(
    params: dict[str, Any], aval: AbstractValue) -> AbstractValue:
  return aval


def call_partial_eval_custom_rule(
    jaxpr_param_name: str, params_updater: ParamsUpdater,
    saveable: Callable[..., RematCases_], unks_in: list[bool], inst_in: list[bool],
    eqn: JaxprEqn, *, res_aval: ResAvalUpdater = _default_res_aval_updater,
    ctx = contextlib.nullcontext,
  ) -> tuple[JaxprEqn, JaxprEqn, Sequence[bool], Sequence[bool], list[Var]]:
  jaxpr = eqn.params[jaxpr_param_name]
  with ctx(eqn.params):
    jaxpr_known, jaxpr_staged, unks_out, inst_out, num_res = \
        partial_eval_jaxpr_custom(jaxpr, unks_in, inst_in, False, False, saveable)
  ins_known, _ = partition_list(unks_in, eqn.invars)
  out_binders_known, _ = partition_list(unks_out, eqn.outvars)
  _, ins_staged = partition_list(inst_in, eqn.invars)
  _, out_binders_staged = partition_list(inst_out, eqn.outvars)
  params_known = {**eqn.params, jaxpr_param_name: jaxpr_known}
  params_staged = {**eqn.params, jaxpr_param_name: jaxpr_staged}
  params_known, params_staged = params_updater(
      unks_in, inst_in, map(op.not_, unks_out), inst_out, num_res, params_known,
      params_staged)
  residuals = [Var(res_aval(params_known, var.aval))
               for var in jaxpr_staged.invars[:num_res]]
  eqn_known = new_jaxpr_eqn(ins_known, [*out_binders_known, *residuals],
                            eqn.primitive, params_known, jaxpr_known.effects,
                            eqn.source_info, eqn.ctx)
  eqn_staged = new_jaxpr_eqn([*residuals, *ins_staged], out_binders_staged,
                             eqn.primitive, params_staged,
                             jaxpr_staged.effects, eqn.source_info, eqn.ctx)
  assert len(eqn_staged.invars) == len(jaxpr_staged.invars)
  new_inst = [x for x, inst in zip(eqn.invars, inst_in)
              if type(x) is Var and not inst]
  return eqn_known, eqn_staged, unks_out, inst_out, new_inst + residuals

# TODO(mattjj): unify with ParamsUpdater (this one takes an extra int)
ParamsUpdater2 = Callable[[Sequence[bool], Sequence[bool], Sequence[bool],
                           Sequence[bool], int, int, dict, dict],
                          tuple[dict, dict]]

def closed_call_partial_eval_custom_rule(
    jaxpr_param_name: str, params_updater: ParamsUpdater2,
    saveable: Callable[..., RematCases_], unks_in: list[bool], inst_in: list[bool],
    eqn: JaxprEqn, *, res_aval: ResAvalUpdater = _default_res_aval_updater,
  ) -> tuple[JaxprEqn, JaxprEqn, Sequence[bool], Sequence[bool], list[Var]]:
  # TODO(sharadmv,mattjj): dedup this rule with call_partial_eval_custom_rule.
  dropvars = tuple(isinstance(v, DropVar) for v in eqn.outvars)
  jaxpr_known, jaxpr_staged, unks_out, inst_out, num_res_ref, num_res_val, out_fwd = \
      _closed_jaxpr_partial_eval_custom_cached(
          eqn.params[jaxpr_param_name], (*unks_in,), (*inst_in,), dropvars, saveable)
  num_res = num_res_ref + num_res_val
  out_binders_known, _ = partition_list(unks_out, eqn.outvars)
  ins_known, _ = partition_list(unks_in, eqn.invars)
  _, ins_staged = partition_list(inst_in, eqn.invars)
  _, out_binders_staged = partition_list(inst_out, eqn.outvars)
  params_known = {**eqn.params, jaxpr_param_name: jaxpr_known}
  params_staged = {**eqn.params, jaxpr_param_name: jaxpr_staged}
  params_known, params_staged = params_updater(
      unks_in, inst_in, map(op.not_, unks_out), inst_out,
      sum(f is None for f in out_fwd), num_res, params_known, params_staged)
  res_val_binders, res_ref_binders = split_list(
      [Var(res_aval(params_known, v))
       for v in jaxpr_staged.in_avals[:num_res]], [num_res_val])
  res_val_binders = [v for v, f in zip(res_val_binders, out_fwd) if f is None]
  res_val_vars = subs_list(out_fwd, out_binders_known, res_val_binders)
  eqn_known = new_jaxpr_eqn([*ins_known, *res_ref_binders],
                            [*out_binders_known, *res_val_binders],
                            eqn.primitive, params_known, jaxpr_known.effects,
                            eqn.source_info, eqn.ctx)
  eqn_staged = new_jaxpr_eqn([*res_val_vars, *res_ref_binders, *ins_staged],
                             out_binders_staged,
                             eqn.primitive, params_staged, jaxpr_staged.effects,
                             eqn.source_info, eqn.ctx)
  assert len(eqn_staged.invars) == len(jaxpr_staged.in_avals)
  assert len(ins_known) + len(res_ref_binders) == len(jaxpr_known.jaxpr.invars)
  assert len(ins_staged) + len(res_ref_binders) + len(res_val_vars) == len(jaxpr_staged.jaxpr.invars)
  assert len(out_binders_known) + len(res_val_binders) == len(jaxpr_known.jaxpr.outvars)
  new_inst = [x for x, inst in zip(eqn.invars, inst_in)
              if type(x) is Var and not inst]
  new_vars = [*new_inst, *res_val_vars, *res_ref_binders]
  return eqn_known, eqn_staged, unks_out, inst_out, new_vars

@weakref_lru_cache
def _closed_jaxpr_partial_eval_custom_cached(
    jaxpr: ClosedJaxpr, unks_in: tuple[bool, ...], inst_in: tuple[bool, ...],
    dropvars: tuple[bool, ...], saveable: Callable
    ) -> tuple[ClosedJaxpr, ClosedJaxpr, Sequence[bool], Sequence[bool],
               int, int, Sequence[int | None]]:
  jaxpr_known_, jaxpr_staged_, unks_out, inst_out, num_res_val, num_res_ref = \
      partial_eval_jaxpr_stateful(jaxpr.jaxpr, unks_in, inst_in,
                                  False, False, saveable)

  # Compute which residual value outputs are also *undropped* primal outputs.
  num_out_primals = len(jaxpr_known_.outvars) - num_res_val
  out_vars, res_vars = split_list(jaxpr_known_.outvars, [num_out_primals])
  out_dropvars_known, _ = partition_list(unks_out, dropvars)
  idx_map = {id(v): i for i, (v, b) in enumerate(zip(out_vars, out_dropvars_known))
             if not b}
  out_fwd = [idx_map.get(id(v)) for v in res_vars]

  # Prune jaxpr_known_ outputs by removing forwards.
  jaxpr_known_ = prune_jaxpr_outputs(
      jaxpr_known_, [True] * num_out_primals + [f is None for f in out_fwd])

  jaxpr_known = core.ClosedJaxpr(jaxpr_known_, jaxpr.consts)
  jaxpr_staged = core.ClosedJaxpr(jaxpr_staged_, jaxpr.consts)
  return jaxpr_known, jaxpr_staged, unks_out, inst_out, num_res_ref, num_res_val, out_fwd


partial_eval_jaxpr_custom_rules[core.call_p] = \
    partial(call_partial_eval_custom_rule, 'call_jaxpr',
            lambda _, __, ___, ____, _____, x, y: (x, y))
partial_eval_jaxpr_custom_rules[core.closed_call_p] = \
    partial(closed_call_partial_eval_custom_rule, 'call_jaxpr',
            lambda _, __, ___, ____, _____, ______, x, y: (x, y))


def _jaxpr_forwarding(jaxpr: Jaxpr) -> list[int | None]:
  # Compute which inputs are just forwarded to outputs.
  fwds: dict[Var, Atom] = dict(zip(jaxpr.invars, jaxpr.invars))
  for eqn in jaxpr.eqns:
    if eqn.primitive in forwarding_rules:
      eqn = eqn.replace(invars=[a if type(a) is Literal else fwds.get(a, a)  # type: ignore
                                for a in eqn.invars])
      fwd_idx, _ = forwarding_rules[eqn.primitive](eqn)
      for v_orig, idx in zip(eqn.outvars, fwd_idx):
        if idx is not None:
          fwds[v_orig] = eqn.invars[idx]
  idxs: dict[Var, int] = {v: i for i, v in enumerate(jaxpr.invars)}
  return [None if type(v) is Literal else idxs.get(fwds.get(v))  # type: ignore
          for v in jaxpr.outvars]


def prune_jaxpr_outputs(jaxpr: Jaxpr, used_outputs: Sequence[bool]) -> Jaxpr:
  return _prune_jaxpr_outputs_cached(jaxpr, tuple(used_outputs))

def _prune_jaxpr_outputs(jaxpr: Jaxpr, used_outputs: tuple[bool, ...]) -> Jaxpr:
  outvars = [v for v, b in zip(jaxpr.outvars, used_outputs) if b]
  dbg = core.DebugInfo(
      jaxpr.debug_info.traced_for, jaxpr.debug_info.func_src_info,
      jaxpr.debug_info.arg_names,
      jaxpr.debug_info.filter_result_paths(used_outputs))
  new_jaxpr = jaxpr.replace(outvars=outvars, debug_info=dbg)
  config.enable_checks.value and core.check_jaxpr(new_jaxpr)
  return new_jaxpr
_prune_jaxpr_outputs_cached = weakref_lru_cache(_prune_jaxpr_outputs)

def prune_closed_jaxpr_outputs(
    jaxpr: ClosedJaxpr, used_outputs: Sequence[bool]
) -> ClosedJaxpr:
  return _prune_closed_jaxpr_outputs(jaxpr, tuple(used_outputs))

@partial(weakref_lru_cache, trace_context_in_key=False)
def _prune_closed_jaxpr_outputs(
    jaxpr: ClosedJaxpr, used_outputs: tuple[bool, ...]
) -> ClosedJaxpr:
  return ClosedJaxpr(_prune_jaxpr_outputs(jaxpr.jaxpr, used_outputs),
                     jaxpr.consts)


def dce_jaxpr(jaxpr: Jaxpr, used_outputs: Sequence[bool],
              instantiate: bool | Sequence[bool] = False,
              ) -> tuple[Jaxpr, list[bool]]:
  """Runs dead-code elementation on a given jaxpr.

  Args:
    jaxpr: The jaxpr to DCE.
    used_outputs: A list of bools indicating which outputs are used.
    instantiate: A bool or a list of bools indicating which inputs should be
      considered used, regardless of whether they are actually used in a jaxpr.
      If a bool, the same value is used for all inputs.

  Returns:
    A tuple of ``(new_jaxpr, used_inputs)``.
  """
  if type(instantiate) is bool:
    instantiate = (instantiate,) * len(jaxpr.invars)
  return _dce_jaxpr(jaxpr, tuple(used_outputs), tuple(instantiate))


def dce_jaxpr_consts(jaxpr: Jaxpr, used_outputs: Sequence[bool],
                     instantiate: bool | Sequence[bool] = False,
                     ) -> tuple[Jaxpr, list[bool], list[bool]]:
  jaxpr_ = convert_constvars_jaxpr(jaxpr)
  new_jaxpr, used_inputs_ = dce_jaxpr(jaxpr_, used_outputs, instantiate)
  used_consts, used_inputs = split_list(used_inputs_, [len(jaxpr.constvars)])
  if sum(used_consts):
    new_jaxpr = convert_invars_to_constvars(new_jaxpr, sum(used_consts))
  return new_jaxpr, used_consts, used_inputs


def has_effects(eqn: JaxprEqn) -> bool:
  effs = {e for e in eqn.effects if not isinstance(e, core.NamedAxisEffect)
          and not isinstance(e, ReadEffect)}
  return bool(effs)


@weakref_lru_cache
def _dce_jaxpr(jaxpr: Jaxpr, used_outputs: tuple[bool, ...],
               instantiate: tuple[bool, ...]
               ) -> tuple[Jaxpr, list[bool]]:
  env: dict[Var, bool] = {}

  def read(v: Var) -> bool:
    return env.get(v, False)

  def write(x: Atom, b: bool) -> None:
    if type(x) is Var:
      env[x] = read(x) or b

  new_eqns = []
  foreach(write, jaxpr.outvars, used_outputs)
  for eqn in jaxpr.eqns[::-1]:
    used_outs = map(read, eqn.outvars)
    rule = dce_rules.get(eqn.primitive, _default_dce_rule)
    used_ins, new_eqn = rule(used_outs, eqn)
    if new_eqn is not None:
      new_eqns.append(new_eqn)
    foreach(write, eqn.invars, used_ins)
  used_inputs = map(read, jaxpr.invars)
  used_inputs = map(op.or_, instantiate, used_inputs)

  invars = [v for v, b in zip(jaxpr.invars, used_inputs)   if b]
  outvars = [v for v, b in zip(jaxpr.outvars, used_outputs) if b]
  eqns = new_eqns[::-1]
  jaxpr_effects = make_jaxpr_effects(jaxpr.constvars, invars, outvars, eqns)

  dbg = core.DebugInfo(
      jaxpr.debug_info.traced_for, jaxpr.debug_info.func_src_info,
      jaxpr.debug_info.filter_arg_names(used_inputs),
      jaxpr.debug_info.filter_result_paths(used_outputs))
  new_jaxpr = jaxpr.replace(invars=invars, outvars=outvars, eqns=eqns,
                            effects=jaxpr_effects, debug_info=dbg)
  config.enable_checks.value and core.check_jaxpr(new_jaxpr)

  return new_jaxpr, used_inputs

DCERule = Callable[[list[bool], JaxprEqn],
                   tuple[list[bool], Union[JaxprEqn, None]]]

def _default_dce_rule(
    used_outs: list[bool], eqn: JaxprEqn
  ) -> tuple[list[bool], JaxprEqn | None]:
  if not any(used_outs) and not has_effects(eqn):
    return [False] * len(eqn.invars), None
  return [True] * len(eqn.invars), eqn

dce_rules: dict[Primitive, DCERule] = {}


def dce_jaxpr_call_rule(used_outputs: list[bool], eqn: JaxprEqn
                        ) -> tuple[list[bool], JaxprEqn | None]:
  if not any(used_outputs) and not has_effects(eqn):
    return [False] * len(eqn.invars), None
  new_jaxpr, used_inputs = dce_jaxpr(eqn.params['call_jaxpr'], used_outputs)
  new_params = dict(eqn.params, call_jaxpr=new_jaxpr)
  update_params = call_param_updaters.get(eqn.primitive)
  if update_params:
    new_params = update_params(new_params, used_inputs, 0)
  if not any(used_inputs) and not any(used_outputs) and not new_jaxpr.effects:
    return used_inputs, None
  else:
    new_eqn = new_jaxpr_eqn(
        [v for v, used in zip(eqn.invars, used_inputs) if used],
        [v for v, used in zip(eqn.outvars, used_outputs) if used],
        eqn.primitive, new_params, new_jaxpr.effects, eqn.source_info, eqn.ctx)
    return used_inputs, new_eqn

dce_rules[core.call_p] = dce_jaxpr_call_rule


@weakref_lru_cache
def _cached_closed_call_dce(jaxpr_, used_outputs: tuple[bool, ...]
                            ) -> tuple[core.ClosedJaxpr, list[bool]]:
  jaxpr, consts = jaxpr_.jaxpr, jaxpr_.consts
  new_jaxpr, used_inputs = dce_jaxpr(jaxpr, used_outputs)
  return core.ClosedJaxpr(new_jaxpr, consts), used_inputs

def dce_jaxpr_closed_call_rule(used_outputs: list[bool], eqn: JaxprEqn
                               ) -> tuple[list[bool], JaxprEqn | None]:
  # TODO(mattjj): de-duplicate with above rule?
  if not any(used_outputs) and not has_effects(eqn):
    return [False] * len(eqn.invars), None
  jaxpr_ = eqn.params['call_jaxpr']
  closed_jaxpr, used_inputs = _cached_closed_call_dce(jaxpr_, tuple(used_outputs))
  new_params = dict(eqn.params, call_jaxpr=closed_jaxpr)
  new_eqn = new_jaxpr_eqn(
      [v for v, used in zip(eqn.invars, used_inputs) if used],
      [v for v, used in zip(eqn.outvars, used_outputs) if used],
      eqn.primitive, new_params, closed_jaxpr.effects, eqn.source_info, eqn.ctx)
  return used_inputs, new_eqn
dce_rules[core.closed_call_p] = dce_jaxpr_closed_call_rule

@weakref_lru_cache
def close_jaxpr(jaxpr: Jaxpr) -> ClosedJaxpr:
  # The `jaxpr.replace()` is making a copy of the Jaxpr, without which
  # the cache value would have a strong reference to the same Jaxpr as
  # the key, and we would never gc the cache entry. This works because
  # Jaxpr is hashed by id, and the cache entry is dead is the key is dead.
  return ClosedJaxpr(jaxpr.replace(), ())

def move_invars_right(jaxpr: ClosedJaxpr, to_move: Sequence[bool]):
  return _move_invars_right(jaxpr, tuple(to_move))

@weakref_lru_cache
def _move_invars_right(jaxpr: ClosedJaxpr, to_move: tuple[bool, ...]):
  invars, rest = split_list(jaxpr.jaxpr.invars, [len(to_move)])
  left_invars, right_invars = partition_list(to_move, invars)
  new_invars = [*left_invars, *right_invars, *rest]
  new_effs = _renumber_effects(
      (*jaxpr.jaxpr.constvars, *new_invars),
      (*jaxpr.jaxpr.constvars, *jaxpr.jaxpr.invars),
      jaxpr.jaxpr.effects)
  return jaxpr.replace(jaxpr=jaxpr.jaxpr.replace(invars=new_invars, effects=new_effs))

def move_binders_to_front(closed_jaxpr: ClosedJaxpr, to_move: Sequence[bool]
                          ) -> ClosedJaxpr:
  """Reorder `invars` by moving those indicated in `to_move` to the front."""
  return _move_binders_to_front(closed_jaxpr, tuple(to_move))

@weakref_lru_cache
def _move_binders_to_front(jaxpr: ClosedJaxpr, to_move: tuple[bool, ...]
                           ) -> ClosedJaxpr:
  assert len(jaxpr.in_avals) == len(to_move)
  constvars, invars = jaxpr.jaxpr.constvars, jaxpr.jaxpr.invars
  new_invars = _move_to_front(invars, to_move)
  new_effs = _renumber_effects(
      (*constvars, *new_invars), (*constvars, *invars), jaxpr.jaxpr.effects)
  arg_names = jaxpr.jaxpr.debug_info.safe_arg_names(len(jaxpr.in_avals))
  new_arg_names = tuple(_move_to_front(arg_names, to_move))
  dbg = jaxpr.jaxpr.debug_info._replace(arg_names=new_arg_names)
  new_jaxpr = jaxpr.jaxpr.replace(
      constvars=constvars, invars=new_invars, effects=new_effs, debug_info=dbg)
  return core.ClosedJaxpr(new_jaxpr, jaxpr.consts)

def _renumber_effects(new_vars, old_vars, effs):
  newvar_idxs = {id(v): i for i, v in enumerate(new_vars)}
  old_to_new = {i: newvar_idxs[id(v)] for i, v in enumerate(old_vars)}
  return {e.replace(input_index=old_to_new[e.input_index])
          if isinstance(e, effects.JaxprInputEffect) else e for e in effs}

def _move_to_front(lst: Sequence, to_move: Sequence[bool]) -> Sequence:
  return ([elt for elt, move in zip(lst, to_move) if move] +
          [elt for elt, move in zip(lst, to_move) if not move])

def move_binders_to_back(closed_jaxpr: ClosedJaxpr, to_move: Sequence[bool]
                         ) -> ClosedJaxpr:
  """Reorder `invars` by moving those indicated in `to_move` to the back."""
  return move_binders_to_front(closed_jaxpr, map(op.not_, to_move))

def move_outvars_to_back(jaxpr: ClosedJaxpr, to_move: Sequence[bool]) -> ClosedJaxpr:
  return _move_outvars_to_back(jaxpr, tuple(to_move))

@weakref_lru_cache
def _move_outvars_to_back(jaxpr, to_move):
  new_outvars = ([e for e, m in zip(jaxpr.jaxpr.outvars, to_move) if not m] +
                 [e for e, m in zip(jaxpr.jaxpr.outvars, to_move) if     m])
  return jaxpr.replace(jaxpr=jaxpr.jaxpr.replace(outvars=new_outvars))


class DynamicJaxprTracer(core.Tracer):
  __slots__ = ['aval', 'val', 'mutable_qdd', 'parent', '_debug_info']

  def __init__(self, trace: DynamicJaxprTrace,
               aval: core.AbstractValue | core.AvalQDD,
               val : Atom,
               line_info: source_info_util.SourceInfo | None = None,
               parent : TracingEqn | None = None):
    # TODO(dougalm): Remove aval. It's redundant now that we have val.
    if isinstance(aval, core.AvalQDD):
      assert aval.qdd is not None
      aval, qdd = aval.aval, aval.qdd
    else:
      assert not aval.has_qdd
      qdd = None
    self._trace = trace
    self._line_info = line_info
    self._debug_info = self._trace.frame.debug_info  # for UnexpectedTracerError
    self.aval = aval  # type: ignore[misc]
    self.val = val
    self.mutable_qdd = core.MutableQuasiDynamicData(qdd)
    self.parent = parent

  def _short_repr(self):
    return f"JitTracer<{self.aval}>"

  @property
  def aval_mutable_qdd(self):
    aval = self.aval
    if aval.has_qdd:
      return core.AvalMutableQDD(aval, self.mutable_qdd)
    else:
      return aval

  def full_lower(self):
    atom = self.val
    if isinstance(atom, Literal):
      return self.val.val
    else:
      maybe_const = self._trace.frame.constvar_to_val.get(atom)
      if maybe_const is None:
        return self
      else:
        return core.full_lower(maybe_const)

  def _contents(self):
    return ()

  def _origin_msg(self):
    invar_pos, progenitor_eqns = self._trace.frame.find_progenitors(self)
    dbg = self._debug_info
    if dbg is None:
      return ""

    origin = ("The error occurred while tracing the function "
              f"{dbg.func_src_info} for {dbg.traced_for}. ")
    if invar_pos:
      try:
        arg_names = [dbg.arg_names[i] for i in invar_pos]
      except IndexError:
        return ""  # TODO(mattjj): figure out when not (invar_pos < len(arg_info))
      if len(arg_names) == 1:
        arg_info_str = f"the argument {arg_names[0]}"
      elif len(arg_names) == 2:
        arg_info_str = f"the arguments {arg_names[0]} and {arg_names[1]}"
      else:
        *rest, last = arg_names
        arg_info_str = f"the arguments {', '.join(rest)}, and {last}"
      origin += ("This concrete value was not available in Python because it "
                 f"depends on the value{'s' if len(invar_pos) > 1 else ''} "
                 f"of {arg_info_str}.")
    elif progenitor_eqns:
      msts = ["  operation "
              f"{core.pp_eqn(eqn, core.JaxprPpContext(), core.JaxprPpSettings(print_shapes=True))}\n"
              f"    from line {source_info_util.summarize(eqn.source_info)}"
              for eqn in progenitor_eqns[:5]]  # show at most 5
      origin += ("This value became a tracer due to JAX operations on these lines:"
                 "\n\n" + "\n\n".join(msts))
      if len(progenitor_eqns) > 5:
        origin += "\n\n(Additional originating lines are not shown.)"
    return "\n" + origin

  def get_const(self):
    return self._trace.get_const(self)

  def get_referent(self):
    frame = self._trace.frame
    atom = self.val
    val = frame.constvar_to_val.get(atom) if isinstance(atom, Var) else None
    return self if val is None else get_referent(val)

core.pytype_aval_mappings[DynamicJaxprTracer] = lambda x: x.aval

def make_jaxpr_effects(constvars, invars, outvars, eqns) -> effects.Effects:
  sentinel = object()
  jaxpr_effects = set()
  all_vars = {v: i for i, v in enumerate(it.chain(constvars, invars))}
  mut_arrays = set()
  for eqn in eqns:
    if eqn.primitive is core.mutable_array_p:
      outvar, = eqn.outvars
      all_vars[outvar] = None  # type: ignore
      mut_arrays.add(outvar)
    for eff in eqn.effects:
      if isinstance(eff, effects.JaxprInputEffect):
        if eff.input_index >= len(eqn.invars):
          raise ValueError(
              f"`JaxprInputEffect` {eff} is invalid."
              f"\n Equation: {eqn}\n"
              "\n Jaxpr: "
              f"{core.Jaxpr(constvars, invars, outvars, eqns, set())}")
        eqn_invar = eqn.invars[eff.input_index]
        if type(eqn_invar) is core.Literal or eqn_invar in mut_arrays:
          continue
        if (input_index := all_vars.get(eqn_invar, sentinel)) is sentinel:
          # TODO(mattjj): ask for forgiveness
          dbg = type('Fake', (), {'resolve_result_paths': lambda _: None})()
          raise ValueError(
                f"`JaxprInputEffect` {eff} does not have "
                f"corresponding jaxpr input: {eqn_invar=}."
                f"\n Equation: {eqn}\n"
                f"\n Effects: {eqn.effects}\n"
                "\n Jaxpr: "
                f"{core.Jaxpr(constvars, invars, outvars, eqns, set(), dbg)}")  # type: ignore
        eff = eff.replace(input_index=input_index)
      jaxpr_effects.add(eff)
  return jaxpr_effects


class JaxprStackFrame:
  gensym: Callable[[AbstractValue], Var]
  constid_to_tracer: WeakValueDictionary[ConstId, DynamicJaxprTracer]
  constvar_to_val: dict[Var, Any]
  tracing_eqns: list[Union[ReferenceType[TracingEqn], Callable[[], TracingEqn]]]
  invars: list[Var]
  effects: core.Effects
  debug_info: core.DebugInfo
  is_high: bool
  mutable_qdds: list[tuple[Var, core.MutableQuasiDynamicData]]
  auto_dce: bool

  def __init__(self, debug_info: core.DebugInfo, auto_dce: bool):
    self.gensym = core.gensym()
    self.constid_to_tracer = WeakValueDictionary()
    self.constvar_to_val = {}
    self.tracing_eqns = []      # cleared when we pop frame from main
    self.invars = []
    self.effects = set()
    self.debug_info = debug_info
    self.is_high = False
    self.mutable_qdds = []
    self.auto_dce = auto_dce

  def add_eqn(self, eqn: core.TracingEqn):
    assert isinstance(eqn, TracingEqn)
    r = (lambda: eqn) if (eqn.effects or not self.auto_dce) else ref(eqn)
    self.tracing_eqns.append(r)

  def get_eqns(self):
    eqns = []
    for tracing_eqn in self.tracing_eqns:
      e = tracing_eqn()
      if e is None: continue
      eqns.append(JaxprEqn(
          [t.val for t in e.in_tracers],
          e.outvars, e.primitive, e.params, e.effects, e.source_info, e.ctx))
    return eqns

  def to_jaxpr(
      self, trace: DynamicJaxprTrace,
      out_tracers: Sequence[Tracer],
      debug_info: core.DebugInfo,
      source_info: SourceInfo,
    ) -> tuple[Jaxpr, list[Any]]:
    eqns = self.get_eqns()
    outvars = [t.val for t in out_tracers]
    constvars, constvals = unzip2(self.constvar_to_val.copy().items())
    constvars, constvals = _drop_unused_vars(constvars, constvals, eqns, outvars)
    effs = make_jaxpr_effects(constvars, self.invars, outvars, eqns)

    # TODO(dougalm): handle qdd for consts
    for v, qdd in self.mutable_qdds:
      v.final_qdd = qdd.cur_val

    jaxpr = Jaxpr(constvars, self.invars, outvars, eqns, effs, debug_info,
                  self.is_high)
    return jaxpr, list(constvals)

  def to_jaxpr2(self, out_tracers: Sequence[core.Tracer],
                debug_info: core.DebugInfo):
    eqns = self.get_eqns()
    outvars = [t.val for t in out_tracers]
    constvars, constvals = unzip2(self.constvar_to_val.copy().items())
    constvars, constvals = _drop_unused_vars(constvars, constvals, eqns, outvars)
    effs = make_jaxpr_effects(constvars, self.invars, outvars, eqns)
    jaxpr = Jaxpr(constvars, self.invars, outvars, eqns, effs, debug_info)
    jaxpr, out_type = _add_implicit_outputs(jaxpr)
    config.enable_checks.value and core.check_jaxpr(jaxpr)
    return jaxpr, out_type, constvals

  def newvar(self, aval):
    if isinstance(aval, DShapedArray):
      # this aval may have tracers in it, so we replace those with variables
      new_shape = [d.val if isinstance(d, Tracer) else d for d in aval.shape]
      new_shape = [d.val if isinstance(d, Literal) else d for d in new_shape]
      aval = aval.update(shape=tuple(new_shape))
    if isinstance(aval, core.AvalQDD):
       return self.gensym(aval.aval, initial_qdd=aval.qdd)
    else:
       return self.gensym(aval)

  def find_progenitors(self, tracer):
    eqns = self.get_eqns()
    var = tracer.val
    if not var or isinstance(var, Literal):
      return None, None
    active_vars = {var}
    for eqn in eqns[::-1]:
      produced = set(eqn.outvars) & active_vars
      if produced:
        active_vars.difference_update(produced)
        active_vars.update({v for v in eqn.invars if type(v) is Var})
    invar_positions = [i for i, v in enumerate(self.invars) if v in active_vars]
    constvars = active_vars & set(self.constvar_to_val.copy())
    const_eqns = [eqn for eqn in eqns if any(
        v in constvars if type(v) is Var else type(v) is Literal
        for v in eqn.invars)]
    return invar_positions, const_eqns


ConstFoldRule = Callable[
    [list[Union[Any, None]], Any, list[AbstractValue]],
    tuple[list[Union[Any, None]], Union[JaxprEqn, None]],
]
const_fold_rules: dict[Primitive, ConstFoldRule] = {}

ForwardingRule = Callable[
    [JaxprEqn],
    tuple[list[Union[int, None]], Union[JaxprEqn, None]]
]
forwarding_rules: dict[Primitive, ForwardingRule] = {}


def _drop_unused_vars(constvars, constvals, eqns, outvars
                      ) -> tuple[list[Var], list[Any]]:
  # modifies eqns in-place!
  def vars(atom: Atom) -> list[Var]:
    if isinstance(atom, Literal):
      return []
    aval = atom.aval
    if isinstance(aval, DShapedArray):
      return [atom] + [d for d in aval.shape if isinstance(d, Var)]
    return [atom]
  used: set[Var] = {v for atom in outvars for v in vars(atom)}
  for eqn in eqns[::-1]:
    eqn.outvars = [v if v in used else DropVar(v.aval) for v in eqn.outvars]
    used.update(v for atom in eqn.invars for v in vars(atom))
  constvars, constvals = unzip2(
      (v, val) for v, val in zip(constvars, constvals) if v in used)
  return constvars, constvals


@cache()
def _cached_abstract_eval(primitive: core.Primitive, *aval_qdds, **params):
  return primitive.abstract_eval(*aval_qdds, **params)


def _verify_params_are_hashable(
    primitive: core.Primitive, params: dict[str, Any]) -> None:
  for k, v in params.items():
    try:
      hash(v)
    except TypeError as e:
      raise TypeError(
        "As of JAX v0.7, parameters to jaxpr equations must have __hash__ and "
        f"__eq__ methods. In a call to primitive {primitive}, the value of "
        f"parameter {k} was not hashable: {v}") from e

# We use TracingEqn instead JaxprEqn during tracing to allow automatic
# on-the-fly DCE based on Python refcounting. DynamicJaxprTracers point to
# TracingEqns which point to DynamicJaxprTracers and unreachable constants can
# be freed.

@dataclass
class TracingEqn:
  in_tracers: list[DynamicJaxprTracer]
  outvars: list[Var]
  primitive: Primitive
  params: dict[str, Any]
  effects: core.Effects
  source_info: source_info_util.SourceInfo
  ctx: JaxprEqnContext

  # Allow TracingEqn to duck-type JaxpeEqn because some of the forwarding
  # rules need to work with both. TODO(dougalm): remove this once we fix
  # forwarding.
  @property
  def invars(self):
    return self.in_tracers

class DynamicJaxprTrace(core.Trace):
  __slots__ = ("frame", "tag", "parent_trace")

  def __init__(self, debug_info: core.DebugInfo, parent_trace=None, lower=False,
               auto_dce=False):
    super().__init__()
    self.requires_low = lower
    self.frame = JaxprStackFrame(debug_info, auto_dce)
    self.parent_trace = parent_trace

  def invalidate(self):
    # TODO(mattjj): exposed existing tracer leaks; fix them and re-enable!
    # super().invalidate()

    # avoid cyclic refs
    self.frame.tracing_eqns = []  # thunk -> eqn -> in_tracers -> trace ->
                                  # -> frame -> tracing_eqns -> thunk

    # TODO(dougalm): we might be able to remove these given refcounting dce
    self.frame.constid_to_tracer = {}
    self.frame.constvar_to_val = {}

  def to_jaxpr_tracer(self, x, source_info: SourceInfo):
    if isinstance(x, DynamicJaxprTracer) and x._trace is self:
      return x
    else:
      if hasattr(x, "dimension_as_value"):  # Used for shape_poly._DimExpr
        with core.set_current_trace(self):
          x = x.dimension_as_value()
        return self.to_jaxpr_tracer(x, source_info)
      else:
        return self.new_const(x, source_info)

  def var_to_tracer(self, var, source_info, parent=None):
    aval = var.aval
    if aval.has_qdd:
      aval = core.AvalQDD(aval, var.initial_qdd)
    return DynamicJaxprTracer(self, aval, var, source_info, parent)

  def new_arg(self, aval, source_info: SourceInfo):
    var = self.frame.newvar(aval)
    tracer = DynamicJaxprTracer(self, aval, var, source_info)
    self.frame.invars.append(var)
    self.frame.mutable_qdds.append((var, tracer.mutable_qdd))
    return tracer

  def make_eqn(self, in_tracers, out_avals, primitive, params,
               effects, source_info=None, ctx = None):
    source_info = source_info or source_info_util.new_source_info()
    ctx = ctx or JaxprEqnContext(
        compute_on.current_compute_type(),
        config.threefry_partitionable.value,
        xla_metadata_lib.current_xla_metadata())
    outvars = map(self.frame.newvar, out_avals)
    if config.enable_checks.value:
      assert all(isinstance(x, DynamicJaxprTracer) for x in in_tracers)
      assert all(isinstance(v,  Var)               for v in outvars)
    eqn = TracingEqn(in_tracers, outvars, primitive, params, effects, source_info, ctx)
    out_tracers = [self.var_to_tracer(v, source_info, eqn) for v in outvars]
    return eqn, out_tracers

  def emit_eqn(self, in_tracers, out_avals, primitive, params, effects, source_info=None, ctx=None):
    eqn, out_tracers = self.make_eqn(in_tracers, out_avals, primitive, params, effects, source_info, ctx)
    self.frame.add_eqn(eqn)
    return out_tracers

  def new_const(self, c, source_info: SourceInfo):
    # TODO(mattjj): for ints, or hashable consts, don't rely on id
    tracer = self.frame.constid_to_tracer.get(id(c))
    if tracer is None:
      aval = get_aval(c)
      if aval.has_qdd:
        with core.set_current_trace(self.parent_trace):
          aval = core.AvalQDD(aval, core.cur_qdd(c))
      aval = self._lift_tracers_in_aval(aval, source_info)
      tracer = self._new_const(aval, c, source_info)
    return tracer

  pure = lift = new_const

  def _new_const(self, aval, c, source_info: SourceInfo) -> DynamicJaxprTracer:
    if core.is_literalable(c):
      val = Literal(c, aval)
      return DynamicJaxprTracer(self, aval, val, source_info)
    else:
      var = self.frame.newvar(aval)
      tracer = DynamicJaxprTracer(self, aval, var, source_info)
      self.frame.constid_to_tracer[id(c)] = tracer
      if isinstance(aval, core.AvalQDD):
        self.frame.mutable_qdds.append((var, tracer.mutable_qdd))
      self.frame.constvar_to_val[var] = c
      finalize(tracer, self.finalize_const, var, id(c))
      return tracer

  def finalize_const(self, var, constid):
    self.frame.constvar_to_val.pop(var, None)

  def get_const(self, tracer) -> Any:
    atom = tracer.val
    if isinstance(atom, Literal):
      return atom.val
    else:
      return self.frame.constvar_to_val.get(atom)

  def _lift_tracers_in_aval(self, aval, source_info: SourceInfo):
    if (not isinstance(aval, DShapedArray) or
        not any(isinstance(d, Tracer) for d in aval.shape)):
      return aval
    shape = [self.to_jaxpr_tracer(d, source_info) if isinstance(d, Tracer) else d
             for d in aval.shape]
    return aval.update(shape=tuple(shape))

  def cur_qdd(self, x):
    source_info = source_info_util.current()
    return self.to_jaxpr_tracer(x, source_info=source_info).mutable_qdd.cur_val

  def process_primitive(self, primitive, tracers, params):
    self.frame.is_high |= primitive.is_high(**params)
    if config.eager_constant_folding.value and not any(isinstance(x, Tracer) for x in tracers):
      return primitive.bind_with_trace(core.eval_trace, tracers, params)
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    jaxpr_tracers = map(to_jaxpr_tracer, tracers)
    if primitive in custom_staging_rules:
      return custom_staging_rules[primitive](self, source_info, *jaxpr_tracers,
                                             **params)
    return self.default_process_primitive(
        primitive, jaxpr_tracers, params, source_info)

  def default_process_primitive(self, primitive, tracers, params,
                                source_info=None):
    aval_qdds = [t.aval_mutable_qdd for t in tracers]
    # TODO(mattjj): make custom_lin have hashable params.
    # TODO(dougalm): add an attribute to primitives to mark primitives with
    # effectful abstract_eval rules.
    if (primitive.name == "custom_lin" or config.dynamic_shapes.value or
        primitive.is_effectful and primitive.is_effectful(params)):
      out_avals, effs = primitive.abstract_eval(*aval_qdds, **params)
    else:
      try:
        out_avals, effs = _cached_abstract_eval(primitive, *aval_qdds, **params)
      except Exception as e:
        # TODO(phawkins): remove this 3 months after the release of JAX v0.7.
        _verify_params_are_hashable(primitive, params)
        raise

    if isinstance(out_avals, (tuple, list)) != primitive.multiple_results:
      raise ValueError(f"{primitive}.abstract_eval() method should return "
                       f"a tuple or a list iff {primitive}.multiple_results.")
    out_avals = [out_avals] if not primitive.multiple_results else out_avals
    source_info = source_info or source_info_util.current()

    maybe_consts_out = try_constant_folding(primitive, tracers, params, out_avals)
    if maybe_consts_out is not None:
      eqn = None
      out_tracers = map(partial(self.new_const, source_info=source_info), maybe_consts_out)
    else:
      eqn, out_tracers = self.make_eqn(tracers, out_avals, primitive, params,
                                       effs, source_info=source_info)
    # Input-to-output tracer forwarding
    no_input_effects = not any(isinstance(e, effects.JaxprInputEffect) for e in effs)
    if eqn is not None and no_input_effects and primitive in forwarding_rules:
      in_fwd, eqn = forwarding_rules[primitive](eqn)
      for out_idx, in_idx in enumerate(in_fwd):
        if in_idx is not None:
          out_tracers[out_idx] = tracers[in_idx]

    if eqn is not None:
      self.frame.add_eqn(eqn)
    return out_tracers if primitive.multiple_results else out_tracers.pop()

  def process_call(self, call_primitive, f: lu.WrappedFun, explicit_tracers, params):
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    if f.in_type is None:
      f = lu.annotate(f, tuple((get_aval(t), True) for t in explicit_tracers))
    assert f.in_type is not None
    implicit_tracers = _extract_implicit_args(self, f.in_type, explicit_tracers,
                                              source_info)
    in_tracers = map(to_jaxpr_tracer, [*implicit_tracers, *explicit_tracers])
    # TODO(mattjj): check in_tracers are consistent with f.in_type annotation
    jaxpr, out_type, consts = trace_to_jaxpr_dynamic2(f)
    if params.get('inline', False):
      return core.eval_jaxpr(jaxpr, consts, *in_tracers,
                             propagate_source_info=False)

    out_avals = [aval for aval, _ in out_type]
    new_jaxpr = convert_constvars_jaxpr(jaxpr)
    if isinstance(call_primitive, core.ClosedCallPrimitive):
      new_jaxpr = close_jaxpr(new_jaxpr)  # type: ignore
    new_params = dict(params, call_jaxpr=new_jaxpr)
    update_params = call_param_updaters.get(call_primitive)
    if update_params:
      new_params = update_params(new_params, [True] * len(explicit_tracers),
                                 len(consts) + len(implicit_tracers))
    const_tracers = map(to_jaxpr_tracer, consts)
    out_tracers = self.emit_eqn(
        [*const_tracers, *in_tracers], out_avals, call_primitive,
        new_params, new_params['call_jaxpr'].effects, source_info=source_info)
    return [t for t, (_, keep) in zip(out_tracers, out_type) if keep]

  def process_map(self, map_primitive, f: lu.WrappedFun, tracers, params):
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    tracers = map(to_jaxpr_tracer, tracers)
    in_avals = [t.aval for t in tracers]
    axis_name, axis_size = params['axis_name'], params['axis_size']
    reduced_in_avals = [core.mapped_aval(axis_size, in_axis, a)
                        if in_axis is not None else a
                        for a, in_axis in zip(in_avals, params['in_axes'])]

    with core.extend_axis_env_nd([(axis_name, params["global_axis_size"])]):
      jaxpr, reduced_out_avals, consts = trace_to_jaxpr_dynamic(
          f, reduced_in_avals)
      jaxpr, consts = _linearize_of_pmap_hack(f, jaxpr, consts)
      ordered_effects = effects.ordered_effects.filter_in(jaxpr.effects)
      if ordered_effects:
        raise ValueError("Ordered effects not supported for "
                         f"map primitives: {ordered_effects}")
      out_axes = params['out_axes_thunk']()
      out_avals = [core.unmapped_aval(axis_size, out_axis, a)
                  if out_axis is not None else a
                  for a, out_axis in zip(reduced_out_avals, out_axes)]
      const_tracers = map(to_jaxpr_tracer, consts)
      new_in_axes = (None,) * len(consts) + params['in_axes']
      new_params = dict(params, in_axes=new_in_axes, out_axes=out_axes,
                        call_jaxpr=convert_constvars_jaxpr(jaxpr))
      del new_params['out_axes_thunk']
      update_params = call_param_updaters.get(map_primitive)
      if update_params:
        new_params = update_params(new_params, [True] * len(tracers), len(consts))
      effs = core.filter_named_axis_effects(jaxpr.effects, {axis_name})
      out_tracers = self.emit_eqn(
          [*const_tracers, *tracers], out_avals, map_primitive, new_params, effs, source_info=source_info)
    return out_tracers

  def process_custom_jvp_call(self, prim, fun: lu.WrappedFun,
                              jvp: lu.WrappedFun, tracers,
                              symbolic_zeros: bool):
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    tracers = map(to_jaxpr_tracer, tracers)
    in_avals = [t.aval for t in tracers]
    in_tangent_avals = [t.to_tangent_aval() for t in in_avals]
    fun_jaxpr, out_avals, consts = trace_to_jaxpr_dynamic(fun, in_avals)
    closed_fun_jaxpr = core.ClosedJaxpr(convert_constvars_jaxpr(fun_jaxpr), ())

    @partial(lu.wrap_init, debug_info=jvp.debug_info)
    @_memoize
    def jvp_jaxpr_thunk(*in_zeros):
      for store in jvp.stores: store and store.reset()
      nz_tangent_avals, zero_avals = partition_list(in_zeros, in_tangent_avals)
      jvp_, out_zeros = _jvp_jaxpr_zeros(jvp, in_zeros, tuple(zero_avals))
      in_avals_ = (*in_avals, *nz_tangent_avals)
      jaxpr, _, out_consts = trace_to_jaxpr_dynamic(jvp_, in_avals_)
      return jaxpr, out_consts, out_zeros()

    const_tracers = map(to_jaxpr_tracer, consts)
    return self.emit_eqn(
        [*const_tracers, *tracers], out_avals, prim,
        dict(call_jaxpr=closed_fun_jaxpr,
             jvp_jaxpr_fun=jvp_jaxpr_thunk,
             num_consts=len(consts),
             symbolic_zeros=symbolic_zeros),
        fun_jaxpr.effects,
        source_info=source_info)

  def process_custom_vjp_call(self, prim: core.Primitive,
                              fun: lu.WrappedFun,
                              fwd: lu.WrappedFun, bwd: lu.WrappedFun, tracers,
                              out_trees: Callable[[], tuple[PyTreeDef, PyTreeDef, list[int | None]]],
                              symbolic_zeros: bool):
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    tracers = map(to_jaxpr_tracer, tracers)
    in_avals = [t.aval for t in tracers]
    fun_jaxpr, out_avals, consts = trace_to_jaxpr_dynamic(fun, in_avals)
    num_consts = len(consts)
    closed_fun_jaxpr = core.ClosedJaxpr(convert_constvars_jaxpr(fun_jaxpr), ())

    @partial(lu.wrap_init, debug_info=fwd.debug_info)
    @_memoize
    def fwd_jaxpr_from_zeros(*zeros):
      for store in fwd.stores: store and store.reset()
      fwd_ = _interleave_fun(fwd, zeros)
      jaxpr, _, consts = trace_to_jaxpr_dynamic(fwd_, in_avals)
      return jaxpr, consts

    def out_trees_():
      out_tree, res_tree, input_fwds = out_trees()
      input_fwds = [f if f is None else f + num_consts for f in input_fwds]
      return out_tree, res_tree, input_fwds

    const_tracers = map(to_jaxpr_tracer, consts)
    return self.emit_eqn(
        [*const_tracers, *tracers], out_avals, prim,
        dict(call_jaxpr=closed_fun_jaxpr,
             fwd_jaxpr_thunk=fwd_jaxpr_from_zeros,
             num_consts=num_consts,
             bwd=bwd, out_trees=out_trees_,
             symbolic_zeros=symbolic_zeros),
        fun_jaxpr.effects,
        source_info=source_info)

  def process_custom_transpose(self, prim: core.Primitive,  # type: ignore[override]
                               call: lu.WrappedFun, tracers, *,
                               transpose: lu.WrappedFun,
                               out_types,
                               lin_tree: PyTreeDef,
                               res_tree: PyTreeDef, out_tree: PyTreeDef):
    source_info = source_info_util.current()
    to_jaxpr_tracer = partial(self.to_jaxpr_tracer, source_info=source_info)
    tracers = map(to_jaxpr_tracer, tracers)
    tracers_res, tracers_lin = split_list(tracers, [res_tree.num_leaves])

    in_avals_p = [t.aval for t in tracers]
    in_avals_t = [*[t.aval for t in tracers_res], *out_types]

    call_jaxpr, out_avals, call_consts = trace_to_jaxpr_dynamic(call, in_avals_p)
    closed_call_jaxpr = core.ClosedJaxpr(
        convert_constvars_jaxpr(call_jaxpr), ())

    transpose_flat, in_tree2 = api_util.flatten_fun_nokwargs(
        transpose, treedef_tuple((res_tree, out_tree)))

    # the following thunk evaluates to a pair: transpose_jaxpr, transpose_consts
    @_memoize
    def transpose_jaxpr_thunk():
      for store in transpose_flat.stores: store.reset()
      jaxpr, _, consts = trace_to_jaxpr_dynamic(transpose_flat, in_avals_t)
      return jaxpr, consts

    const_tracers = map(to_jaxpr_tracer, call_consts)
    return self.emit_eqn(
        [*const_tracers, *tracers], out_avals, prim,
        dict(call_jaxpr=closed_call_jaxpr,
             transpose_jaxpr_thunk=transpose_jaxpr_thunk,
             out_types=out_types, res_tree=res_tree,
             lin_tree=lin_tree, out_tree=out_tree),
        closed_call_jaxpr.effects,
        source_info=source_info)

  def to_jaxpr(self, out_tracers: Sequence[Tracer],
               debug_info: core.DebugInfo, source_info: SourceInfo):
    return self.frame.to_jaxpr(self, out_tracers, debug_info, source_info)


custom_staging_rules: dict[Primitive, Callable] = {}

@lu.transformation2
def _interleave_fun(f, every_others, *args, **kwargs):
  args_ = [x for pair in zip(args, every_others) for x in pair]
  return f(*args_, **kwargs)

# TODO: consider renaming to "lazy_thunk"
def _memoize(fn):
  cells = {}
  sentinel = object()
  def memoized(*args):
    out = cells.get(args, sentinel)
    if out is sentinel:
      with core.set_current_trace(None):
        out = cells[args] = fn(*args)
    return out
  return memoized

@lu.transformation_with_aux2
def _jvp_jaxpr_zeros(f, store, in_zeros, zero_avals, *primal_tangent_avals):
  in_primals, nz_in_tangents = split_list(primal_tangent_avals, [len(in_zeros)])
  symbolic_zeros = map(ad_util.SymbolicZero, zero_avals)
  tangents = merge_lists(in_zeros, nz_in_tangents, symbolic_zeros)
  out = f(*in_primals, *tangents)
  n, ragged = divmod(len(out), 2)
  assert not ragged
  out_primals, out_tangents = out[:n], out[n:]
  out_zeros = [type(t) is ad_util.SymbolicZero for t in out_tangents]
  out_nz_tangents, _ = partition_list(out_zeros, out_tangents)
  store.store(out_zeros)
  return [*out_primals, *out_nz_tangents]


@profiler.annotate_function
def trace_to_jaxpr_dynamic(
    fun: lu.WrappedFun,
    in_avals: Sequence[AbstractValue],
    *,
    keep_inputs: list[bool] | None = None,
    lower: bool = False,
    auto_dce: bool = False,
) -> tuple[Jaxpr, list[AbstractValue], list[Any]]:
  keep_inputs = [True] * len(in_avals) if keep_inputs is None else keep_inputs
  parent_trace = core.trace_ctx.trace
  trace = DynamicJaxprTrace(fun.debug_info, parent_trace=parent_trace,
                            lower=lower, auto_dce=auto_dce)
  # Name stacks are reset because the name stacks on jaxpr equations should be
  # rooted at the enclosing jaxpr.
  with core.ensure_no_leaks(trace), source_info_util.reset_name_stack():
    source_info = source_info_util.current()
    in_tracers = _input_type_to_tracers(
        partial(trace.new_arg, source_info=source_info), in_avals)
    in_tracers = [t for t, keep in zip(in_tracers, keep_inputs) if keep]

    with core.set_current_trace(trace):
      ans = fun.call_wrapped(*in_tracers)
    _check_returned_jaxtypes(fun.debug_info, ans)
    out_tracers = map(partial(trace.to_jaxpr_tracer, source_info=source_info), ans)
    _check_no_returned_refs(fun.debug_info, out_tracers)
    jaxpr, consts = trace.frame.to_jaxpr(trace, out_tracers, fun.debug_info,
                                         source_info)
    del trace, fun, in_tracers, out_tracers, ans

  config.enable_checks.value and core.check_jaxpr(jaxpr)
  return jaxpr, [v.aval for v in jaxpr.outvars], consts

def _check_returned_jaxtypes(dbg, out_tracers):
  for i, x in enumerate(out_tracers):
    try:
      core.typeof(x)
    except TypeError:
      if (dbg and len(paths := dbg.resolve_result_paths()) > i and
          (p := paths[i].removeprefix('result'))):
        extra = f' at output component {p}'
      else:
        extra = ''
      raise TypeError(
      f"function {dbg.func_src_info} traced for {dbg.traced_for} returned a "
      f"value of type {type(x)}{extra}, which is not a valid JAX type") from None

def _check_no_returned_refs(
    dbg: core.DebugInfo,
    out_tracers: Sequence[DynamicJaxprTracer]
) -> None:
  if not config.mutable_array_checks.value: return
  for i, t in enumerate(out_tracers):
    a = t.aval
    if isinstance(a, AbstractRef):
      result_paths = dbg.resolve_result_paths().safe_result_paths(len(out_tracers))
      loc = result_paths[i] and f' at output tree path {result_paths[i]}'
      frame = t._trace.frame
      v = t.val
      eqns = frame.get_eqns()
      # TODO(dougalm): something more efficient
      eqn = next((e for e in eqns if v in e.outvars), None)
      if eqn:
        assert eqn.primitive is core.mutable_array_p
        origin_info = ('\n\nThe returned mutable array was created on line '
                       f'{source_info_util.summarize(eqn.source_info)}.')
      elif v in frame.invars:
        arg_name = dbg.safe_arg_names(len(frame.invars))[frame.invars.index(v)]
        origin_info = ('\n\nThe returned mutable array was passed in as the '
                       f'argument {arg_name}.')
      else:
        origin_info = ''
      raise ValueError(
          f"function {dbg.func_src_info} traced for {dbg.traced_for} returned "
          f"a mutable array reference of type {a.str_short()}{loc}, but "
          f"mutable array references cannot be returned.{origin_info}")

@profiler.annotate_function
def trace_to_jaxpr_dynamic2(
    fun: lu.WrappedFun,
  ) -> tuple[Jaxpr, OutputType, list[Any]]:
  assert fun.in_type is not None, "fun must be annotated with lu.annotate()"

  parent_trace = core.trace_ctx.trace
  trace = DynamicJaxprTrace(fun.debug_info, parent_trace=parent_trace)
  with core.ensure_no_leaks(trace), source_info_util.reset_name_stack():
    source_info = source_info_util.current()
    in_avals, keep_inputs = unzip2(fun.in_type)
    in_tracers = _input_type_to_tracers(
        partial(trace.new_arg, source_info=source_info), in_avals)
    in_tracers = [t for t, keep in zip(in_tracers, keep_inputs) if keep]
    with core.set_current_trace(trace):
      ans = fun.call_wrapped(*in_tracers)
    out_tracers = map(partial(trace.to_jaxpr_tracer, source_info=source_info), ans)
    jaxpr = trace.frame.to_jaxpr2(out_tracers, fun.debug_info)
    del trace, in_tracers, out_tracers, ans

  return jaxpr

AbstractedAxisName = Hashable
AbstractedAxesSpec = Union[
    dict[int, AbstractedAxisName],
    tuple[AbstractedAxisName, ...],
]

@register_static
class DoesNotExist: ...
dne_sentinel = DoesNotExist()


def infer_lambda_input_type(
    axes_specs: Sequence[AbstractedAxesSpec] | None,
    args: Sequence[Any]
  ) -> InputType:
  ndims = [getattr(get_aval(x), 'ndim', 0) for x in args]
  partial_specs = _canonicalize_specs(ndims, axes_specs)
  specs = _complete_specs(args, partial_specs)
  idxs, implicit_types = _collect_implicit(args, specs)
  implicit_sig = [(ty, False) for ty in implicit_types]
  explicit_sig = [(_arg_type(idxs, x, s), True) for x, s in zip(args, specs)]
  input_type = (*implicit_sig, *explicit_sig)
  lu._check_input_type(input_type)
  return input_type

def _spec_to_dict(spec: AbstractedAxesSpec) -> dict[int, AbstractedAxisName]:
  if isinstance(spec, tuple):
    return {i: d for i, d in enumerate(spec) if d is not None}
  else:
    return spec

def _canonicalize_specs(
    ndims: Sequence[int], specs: Sequence[AbstractedAxesSpec] | None
  ) -> list[dict[int, AbstractedAxisName]]:
  if specs is None:
    return [{}] * len(ndims)
  else:
    return [_spec_to_dict(s) for n, s in zip(ndims, specs)]

def _complete_specs(
    args: Sequence[Any], partial_specs: list[dict[int, AbstractedAxisName]]
  ) -> list[dict[int, AbstractedAxisName]]:
  # The abstracted axes specification in `partial_specs` is partial in the sense
  # that there could be additional axis abstraction represented in `args` due to
  # Tracers existing in the shapes of elements of `args`. The purpose of this
  # function is to produce a full specification, for each argument mapping any
  # abstracted axis positions to a name, introducing new names as needed for
  # Tracers in axis sizes which don't already correspond to abstracted axis
  # names (with one new name per unique Tracer object id).

  # Identify each user-supplied name in partial_specs with a size.
  sizes: dict[AbstractedAxisName, int | DynamicJaxprTracer] = {}
  for x, spec in zip(args, partial_specs):
    for i, name in spec.items():
      d = sizes.setdefault(name, x.shape[i])
      if d is not x.shape[i] and d != x.shape[i]:
        raise TypeError(f"Provided size {d} for {name} does not match prior associated name for {name} : {x.shape[i]}")

  # Introduce new names as needed for Tracers in shapes.
  named_tracers: dict[TracerId, AbstractedAxisName] = {
      id(d): name for name, d in sizes.items() if isinstance(d, Tracer)}
  specs: list[dict[int, AbstractedAxisName]] = []
  for x, spec in zip(args, partial_specs):
    if isinstance(get_aval(x), DShapedArray):
      spec = dict(spec)
      for i, d in enumerate(x.shape):
        if isinstance(d, Tracer):
          spec[i] = named_tracers.get(id(d), TracerAsName(d))
    specs.append(spec)

  # Assert that `specs` is now complete in the sense that there are no Tracers
  # which don't correspond to an AbstractedAxisName.
  assert all(not spec or not any(isinstance(d, Tracer) and i not in spec
                                 for i, d in enumerate(x.shape))
             for x, spec in zip(args, specs))
  return specs


def _collect_implicit(
    args: Sequence[Any], specs: list[dict[int, AbstractedAxisName]]
  ) -> tuple[dict[AbstractedAxisName, DBIdx], list[AbstractValue]]:
  # Given an explicit argument list and a specification of abstracted axes, we
  # want to produce an InputType by identifying AbstractedAxisNames with DBIdxs
  # and figuring out which AbstractedAxisNames correspond to implicit arguments.

  idxs: dict[AbstractedAxisName, DBIdx] = {}
  implicit_types: list[AbstractValue] = []
  explicit_tracers: dict[TracerId, int] = {}
  counter = it.count()

  # Add implicit arguments to idxs.
  for explicit_idx, (x, spec) in enumerate(zip(args, specs)):
    for i, name in spec.items():
      if name not in idxs and id(x.shape[i]) not in explicit_tracers:
        idxs[name] = DBIdx(next(counter))
        implicit_types.append(get_aval(x.shape[i]))
    if isinstance(x, Tracer):
      explicit_tracers.setdefault(id(x), explicit_idx)  # use the first

  # Now that we know the implicit args, add explicit args to idxs.
  offset = len(implicit_types)
  for x, spec in zip(args, specs):
    for i, name in spec.items():
      if id(x.shape[i]) in explicit_tracers:
        idxs.setdefault(name, DBIdx(offset + explicit_tracers[id(x.shape[i])]))

  return idxs, implicit_types

def _arg_type(
    idxs: dict[AbstractedAxisName, DBIdx], x: Any,
    spec: dict[int, AbstractedAxisName]
  ) -> AbstractValue:
  # Produce an AbstractValue by substituting DBIdxs for AbstractedAxisNames.
  aval = get_aval(x)  # aval.shape could contain Tracers
  if not spec: return aval
  shape: list[int | DBIdx] = [idxs[spec[i]] if i in spec else d
                                    for i, d in enumerate(aval.shape)]
  assert not any(isinstance(d, Tracer) for d in shape)
  return DShapedArray(tuple(shape), aval.dtype, False)

def _add_implicit_outputs(jaxpr: Jaxpr) -> tuple[Jaxpr, OutputType]:
  invars = [*jaxpr.constvars, *jaxpr.invars]
  expl_outvars = jaxpr.outvars

  # First do a pass to collect implicit outputs, meaning variables which occur
  # in explicit_outvars types but not in invars or to the left in outvars.
  seen: set[Var] = set(invars)
  impl_outvars = [seen.add(d) or d for x in expl_outvars if type(x) is Var and  # type: ignore
                  (seen.add(x) or type(x.aval) is DShapedArray)  # type: ignore
                  for d in x.aval.shape if type(d) is Var and d not in seen]
  outvars = [*impl_outvars, *expl_outvars]

  # Now assemble an OutputType by mapping vars in shapes to InDBIdx/OutDBIdx.
  in_map : dict[Var,  InDBIdx] = {v:  InDBIdx(i) for i, v in enumerate( invars)}
  out_map: dict[Var, OutDBIdx] = {x: OutDBIdx(i) for i, x in enumerate(outvars)
                                  if type(x) is Var}
  out_avals_ = (x.aval for x in outvars)
  out_avals = [a.update(shape=tuple(in_map.get(d, out_map.get(d))
                                    if type(d) is Var else d for d in a.shape))
               if type(a) is DShapedArray else a for a in out_avals_]
  kept_outs = [False] * len(impl_outvars) + [True] * len(expl_outvars)
  out_type = tuple(zip(out_avals, kept_outs))

  new_jaxpr = jaxpr.replace(outvars=outvars)
  config.enable_checks.value and core.check_jaxpr(jaxpr)
  return new_jaxpr, out_type


class TracerAsName:
  ref: Any
  def __init__(self, tracer):
    self.ref = core.get_referent(tracer)
  def __eq__(self, other):
    return isinstance(other, TracerAsName) and self.ref is other.ref
  def __hash__(self):
    return id(self.ref)

def _extract_implicit_args(
    trace: DynamicJaxprTrace, in_type: Sequence[tuple[AbstractValue, bool]],
    explicit_tracers: Sequence[DynamicJaxprTracer], source_info: SourceInfo,
  ) -> Sequence[DynamicJaxprTracer]:
  # First, construct a list to represent the full argument list, leaving the
  # implicit arguments as Nones for now.
  explicit_tracers_ = iter(explicit_tracers)
  tracers = [next(explicit_tracers_) if expl else None for _, expl in in_type]
  assert next(explicit_tracers_, None) is None
  del explicit_tracers_

  # Next, populate the implicit arguments using DBIdxs in in_type.
  for i, (aval, explicit) in enumerate(in_type):
    if not explicit or not isinstance(aval, DShapedArray):
      continue  # can't populate an implicit argument
    tracer = tracers[i]
    assert tracer is not None
    for d1, d2 in zip(aval.shape, tracer.aval.shape):
      if isinstance(d1, DBIdx):
        if tracers[d1.val] is None:
          tracers[d1.val] = trace.to_jaxpr_tracer(d2, source_info)
        assert tracers[d1.val] is trace.to_jaxpr_tracer(d2, source_info)
  assert all(t is not None for t in tracers)
  return [t for t, (_, e) in zip(tracers, in_type) if not e]  # type: ignore

def _input_type_to_tracers(
    new_arg: Callable[[AbstractValue], Tracer],
    in_avals: Sequence[AbstractValue]
  )  -> Sequence[Tracer]:
  # Create input Tracers given input AbstractValues, each of which can contain
  # DeBruijn indices which refer to positions in the input argument list. That
  # is, each element `a` of `in_avals` can have DBIdx instances in its shape,
  # which must refer to positions left of `a`'s.
  in_tracers: list[Tracer] = []

  def _substitute_tracers_in_aval(a: AbstractValue) -> AbstractValue:
    if isinstance(a, DShapedArray) and any(type(d) is DBIdx for d in a.shape):
      shape = [in_tracers[d.val] if type(d) is DBIdx else d for d in a.shape]
      return a.update(shape=tuple(shape))
    return a

  for a in in_avals:
    in_tracers.append(new_arg(_substitute_tracers_in_aval(a)))
  return in_tracers

Const = Any
Val = Any

def pad_jaxpr(jaxpr: Jaxpr, consts: Sequence[Const]
              ) -> tuple[Jaxpr, list[Const]]:
  bounds = {v: v.aval.dtype.bound for v in jaxpr.invars
            if isinstance(v.aval, core.UnshapedArray) and
            type(v.aval.dtype) is core.bint and not v.aval.shape}
  idxs = {v: DBIdx(i) for i, v in enumerate(jaxpr.invars)}

  def substitute(aval: AbstractValue) -> AbstractValue:
    if (isinstance(aval, core.UnshapedArray) and type(aval.dtype) is core.bint
        and not aval.shape):
      return ShapedArray((), dtypes._scalar_type_to_dtype(int))
    elif isinstance(aval, DShapedArray):
      shape = [bounds.get(d, idxs.get(d, d)) for d in aval.shape]  # type: ignore
      typ = ShapedArray if all(type(d) is int for d in shape) else DShapedArray
      return typ(tuple(shape), aval.dtype, aval.weak_type)
    else:
      return aval

  in_avals = [substitute(v.aval) for v in jaxpr.invars]
  eval_padded = lu.wrap_init(partial(_eval_jaxpr_padded, jaxpr, consts),
                             debug_info=jaxpr.debug_info)
  padded_jaxpr, _, padded_consts = trace_to_jaxpr_dynamic(eval_padded, in_avals)
  return padded_jaxpr, padded_consts

class BoundedAxisSize(NamedTuple):
  val: int | DynamicJaxprTracer
  bound: int

def _eval_jaxpr_padded(
    jaxpr: Jaxpr, consts: Sequence[Const], *args: DynamicJaxprTracer
  ) -> list[Const | DynamicJaxprTracer]:
  env: dict[Var, Val] = {}

  def read(x):
    return x.val if type(x) is Literal else env[x]

  def write(v, val) -> None:
    env[v] = val

  foreach(write, jaxpr.constvars, consts)
  foreach(write, jaxpr.invars, args)
  last_used = core.last_used(jaxpr)
  for eqn in jaxpr.eqns:
    in_avals  = [_substitute_axis_sizes(env, v.aval) for v in eqn.invars]
    out_avals = [_substitute_axis_sizes(env, v.aval) for v in eqn.outvars]
    rule = padding_rules[eqn.primitive]
    outs = rule(in_avals, out_avals, *map(read, eqn.invars), **eqn.params)
    foreach(write, eqn.outvars, outs)
    core.clean_up_dead_vars(eqn, env, last_used)
  return map(read, jaxpr.outvars)

def _substitute_axis_sizes(env: dict, aval: AbstractValue) -> AbstractValue:
  if isinstance(aval, DShapedArray):
    shp = []
    for d in aval.shape:
      if isinstance(d, core.DArray):
        assert not d.shape and type(d.dtype) is core.bint
        shp.append(BoundedAxisSize(int(d._data), int(d.dtype.bound)))
      elif (type(d) is core.Var and isinstance(d.aval, core.DShapedArray) and
            type(d.aval.dtype) is core.bint):
        assert not d.aval.shape
        shp.append(BoundedAxisSize(env[d], d.aval.dtype.bound))
      else:
        shp.append(env.get(d, d))
    return DShapedArray(tuple(shp), aval.dtype, aval.weak_type)
  else:
    return aval

def _is_bint_axis_size(d: int | core.DArray | core.Var) -> bool:
  if isinstance(d, core.DArray):
    assert not d.shape                 # pytype: disable=attribute-error
    return type(d.dtype) is core.bint  # pytype: disable=attribute-error
  elif isinstance(d, core.Var):
    return (isinstance(d.aval, core.DShapedArray) and  # pytype: disable=attribute-error
            type(d.aval.dtype) is core.bint)           # pytype: disable=attribute-error
  return False


padding_rules: dict[Primitive, Callable] = {}

def def_trivial_padding(prim: Primitive) -> None:
  if prim.multiple_results:
    padding_rules[prim] = partial(_trivial_padding_rule_multi, prim)
  else:
    padding_rules[prim] = partial(_trivial_padding_rule, prim)

def _trivial_padding_rule(prim, _, __, *args, **params):
  return [prim.bind(*args, **params)]

def _trivial_padding_rule_multi(prim, _, __, *args, **params):
  return prim.bind(*args, **params)

def call_padding_rule(prim, in_avals, out_avals, *args, call_jaxpr, **params):
  if call_jaxpr.constvars: raise NotImplementedError
  padded_jaxpr, padded_consts = pad_jaxpr(call_jaxpr, ())
  if padded_consts: raise NotImplementedError
  new_params = dict(params, call_jaxpr=padded_jaxpr)
  subfuns, bind_params = prim.get_bind_params(new_params)
  return prim.bind(*subfuns, *args, **bind_params)


def instantiate_const_at(trace: JaxprTrace, instantiate: bool, tracer):
  if instantiate:
    return trace.instantiate_const(tracer)
  else:
    return tracer

def inline_jaxpr_into_trace(
    trace: DynamicJaxprTrace, src: SourceInfo, jaxpr: Jaxpr,
    consts: Sequence[Any], *arg_tracers: DynamicJaxprTracer) -> list[Any]:
  # This function is conceptually the same thing as just calling eval_jaxpr,
  const_tracers = map(partial(trace.new_const, source_info=src), consts)
  env: dict[Var, DynamicJaxprTracer] = dict(
      zip([*jaxpr.constvars, *jaxpr.invars],
          [*const_tracers, *arg_tracers]))

  def inline_atom(src_, x):
    if isinstance(x, Literal):
      return DynamicJaxprTracer(trace, x.aval, x, src_)
    else:
      return env[x]

  for eqn in jaxpr.eqns:
    src_ = (src if not eqn.source_info.name_stack else
            src.replace(name_stack=src.name_stack + eqn.source_info.name_stack))
    in_tracers = map(partial(inline_atom, src_), eqn.invars)
    out_avals = [v.aval for v in eqn.outvars]

    maybe_consts = try_constant_folding(eqn.primitive, in_tracers, eqn.params, out_avals)
    if maybe_consts is not None:
      out_tracers = map(partial(trace.new_const, source_info=src_), maybe_consts)
    else:
      out_tracers = trace.emit_eqn(in_tracers, out_avals, eqn.primitive,
                                   eqn.params, eqn.effects, src_, eqn.ctx)
    foreach(env.setdefault, eqn.outvars, out_tracers)

  return map(partial(inline_atom, src), jaxpr.outvars)


def try_constant_folding(primitive, tracers, params, out_avals):
  if primitive in const_fold_rules:
    consts_in = [t.get_const() for t in tracers]
    if any(c is not None for c in consts_in):
      return const_fold_rules[primitive](consts_in, params, out_avals)
  return None

# TODO(mattjj,dougalm): this special handling is to avoid round-tripping the
# jaxpr when we do grad-of-pmap. The tag is set by LinearizeTrace.process_call's
# handling of pmap. Remove when we replace the pmap implementation.
def _linearize_of_pmap_hack(f: lu.WrappedFun, jaxpr, consts) -> tuple[Jaxpr, list]:
  if (not f.transforms and type(f.f) is HashableFunction and
      getattr(f.f, '_pmap_tag', None)):
    _, jaxpr = f.f.closure
    return convert_constvars_jaxpr(jaxpr), []
  return jaxpr, consts


@weakref_lru_cache
def lower_jaxpr(hi_jaxpr):
  lo_avals = [lo_ty for aval in hi_jaxpr.in_aval_qdds for lo_ty in aval.lo_ty()]
  f = lu.wrap_init(partial(lower_traceable, hi_jaxpr),
                   debug_info=hi_jaxpr.jaxpr.debug_info)
  lo_jaxpr, _, lo_consts = trace_to_jaxpr_dynamic(f, lo_avals, lower=True)
  return core.ClosedJaxpr(lo_jaxpr, lo_consts)

def lower_traceable(jaxpr, *lo_args):
  lo_args_ = iter(lo_args)
  hi_args = [aval.raise_val(*it.islice(lo_args_, len(aval.lo_ty())))
             if not aval.has_qdd else
             aval.new_from_loval(*it.islice(lo_args_, len(aval.lo_ty())))
             for aval in jaxpr.in_aval_qdds]
  assert (problem := next(lo_args_, None)) is None
  hi_outs = core.jaxpr_as_fun(jaxpr)(*hi_args)
  mut_outs = [lo_val for aval, hi_arg in zip(jaxpr.final_aval_qdds, hi_args) if aval.has_qdd
              for lo_val in aval.read_loval(hi_arg)]
  lo_outs = [lo_val for v, hi_val in zip(jaxpr.jaxpr.outvars, hi_outs)
             for lo_val in v.aval.lower_val(hi_val)]
  return mut_outs + lo_outs

def convert_const_himutables(jaxpr):
  move = [core.typeof(c).has_qdd for c in jaxpr.consts]
  constvals, in_mutables = partition_list(move, jaxpr.consts)
  constvars, boxvars = partition_list(move, jaxpr.jaxpr.constvars)
  invars = *boxvars, *jaxpr.jaxpr.invars
  effects = make_jaxpr_effects(constvars, invars, jaxpr.jaxpr.outvars,
                               jaxpr.jaxpr.eqns)
  new_jaxpr = jaxpr.jaxpr.replace(constvars=constvars, invars=invars,
                                  effects=effects)
  return jaxpr.replace(jaxpr=new_jaxpr, consts=constvals), in_mutables
