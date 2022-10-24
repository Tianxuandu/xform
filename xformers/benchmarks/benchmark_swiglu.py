# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


import itertools
from functools import partial

import torch
from torch.utils import benchmark
from utils import benchmark_main_helper

import xformers.ops.swiglu as xsw
from xformers.ops import unbind as xunbind

min_run_time = 0.5
device = torch.device("cuda")

SHAPES = [
    # Format: [inp.shape[0], inp.shape[1], hidden.shape[1]]
    # ViT-Giant
    (9456, 1536, 4096),
    (4440, 1536, 4096),
    (4728, 1536, 4096),
    # Some smaller shapes as well
    (4728, 1536, 1024),
]


# OP = xsw._SwiGLUDecomposedOp
OP = xsw._SwiGLUFusedOp


def product_dict(**kwargs):
    keys = kwargs.keys()
    vals = kwargs.values()
    for instance in itertools.product(*vals):
        yield dict(zip(keys, instance))


CASES = list(
    product_dict(
        shape=SHAPES,
        dtype=[torch.half],
    )
)


def benchmark_swiglu(shape, dtype):
    inp_dtype, model_dtype, autocast = dtype, dtype, False

    x = torch.randn(shape[:2], device=device, dtype=inp_dtype)
    module = (
        xsw._SwiGLUModule(in_features=shape[1], hidden_features=shape[2])
        .to(device)
        .to(model_dtype)
    )

    dtype_str = {
        torch.bfloat16: "b16",
        torch.half: "f16",
        torch.float: "f32",
    }.get(dtype, dtype)
    sub_label = f"{dtype_str} B={shape[0]}, I={shape[1]}, H={shape[2]}"

    assert not autocast

    params = module._ordered_params_for_op()
    # w1w2 = torch.cat([params[0], params[2]], dim=0).view([2, *params[0].shape])
    # params[0], params[2] = w1w2.unbind(dim=0)

    yield benchmark.Timer(
        stmt="fn(x, *args)",
        globals={
            "x": x,
            "args": params,
            "fn": partial(xsw.functional_swiglu, op=OP),
        },
        label="swiglu_fw",
        description=OP.NAME,
        sub_label=sub_label,
    )
    yield benchmark.Timer(
        stmt="fn(x)",
        globals={
            "x": x,
            "fn": module,
        },
        label="swiglu_fw",
        description="eager",
        sub_label=sub_label,
    )


def benchmark_swiglu_bw(shape, dtype):
    inp_dtype, model_dtype, autocast = dtype, dtype, False

    x = torch.randn(shape[:2], device=device, dtype=inp_dtype)
    x.requires_grad_()
    module = (
        xsw._SwiGLUModule(in_features=shape[1], hidden_features=shape[2])
        .to(device)
        .to(model_dtype)
    )

    dtype_str = {
        torch.bfloat16: "b16",
        torch.half: "f16",
        torch.float: "f32",
    }.get(dtype, dtype)
    sub_label = f"{dtype_str} B={shape[0]}, I={shape[1]}, H={shape[2]}"

    assert not autocast
    params = module._ordered_params_for_op()
    w1w2 = torch.cat([params[0], params[2]], dim=0).view([2, *params[0].shape]).detach()
    w1w2.requires_grad_()
    params[0], params[2] = xunbind(w1w2, dim=0)
    out = xsw.functional_swiglu(x, *params, op=OP)
    grad = torch.zeros_like(out)
    yield benchmark.Timer(
        stmt="out.backward(grad, retain_graph=True)",
        globals={
            "out": out,
            "grad": grad,
        },
        label="swiglu_bw",
        description=OP.NAME,
        sub_label=sub_label,
    )
    del out

    yield benchmark.Timer(
        stmt="out.backward(grad, retain_graph=True)",
        globals={
            "out": module(x),
            "grad": grad,
        },
        label="swiglu_bw",
        description="eager",
        sub_label=sub_label,
    )


benchmark_main_helper(benchmark_swiglu, CASES, min_run_time=min_run_time)
benchmark_main_helper(benchmark_swiglu_bw, CASES, min_run_time=min_run_time)
