# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional, Set, Tuple

import torch

from ... import _is_triton_available

if TYPE_CHECKING or _is_triton_available():
    from ..._flash_attn.flash_attn_triton import (
        _flash_attn_backward,
        _flash_attn_forward,
    )

    triton_flash_backward = _flash_attn_backward
    triton_flash_forward = _flash_attn_forward
else:
    triton_flash_backward = None
    triton_flash_forward = None

from .common import (
    AttentionBwOpBase,
    AttentionFwOpBase,
    Context,
    Gradients,
    Inputs,
    LowerTriangularMask,
)


def _prepare_inputs(inp: Inputs) -> Inputs:
    attn_bias = inp.attn_bias
    if isinstance(attn_bias, torch.Tensor) and attn_bias.ndim == 3:
        B = inp.query.shape[0]
        h = attn_bias.shape[0] // B
        attn_bias = attn_bias.reshape(B, h, attn_bias.shape[1], attn_bias.shape[2])

    # Make sure that the last dimension is contiguous
    query, key, value = [
        x if x.stride(-1) == 1 else x.contiguous()
        for x in [inp.query, inp.key, inp.value]
    ]
    return replace(inp, attn_bias=attn_bias, query=query, key=key, value=value)


class FwOp(AttentionFwOpBase):
    OPERATOR = triton_flash_forward
    SUPPORTED_DEVICES = {"cuda"}
    SUPPORTED_DTYPES = {torch.half, torch.bfloat16}
    SUPPORTED_MAX_K = 128
    SUPPORTED_ATTN_BIAS_TYPES: Set[Any] = {
        type(None),
        LowerTriangularMask,
        # TODO: backwards accuracy is failing for a few cases, perhaps we want to disable this for now.
        # torch.Tensor,
    }
    SUPPORTS_DROPOUT = False
    SUPPORTS_CUSTOM_SCALE = True
    NAME = "tritonflashattF"

    @classmethod
    def info(cls):
        if cls.OPERATOR is None:
            return "not built"
        return "available"

    @classmethod
    def supports(cls, d: "Inputs") -> bool:
        if cls.OPERATOR is None:
            return False
        device_capability = torch.cuda.get_device_capability(d.device)
        if not device_capability >= (7, 5):
            return False
        return super(FwOp, cls).supports(d)

    @classmethod
    def apply(
        cls, inp: Inputs, needs_gradient: bool
    ) -> Tuple[torch.Tensor, Optional[Context]]:
        inp = _prepare_inputs(inp)

        out, lse, softmax_scale = triton_flash_forward(
            q=inp.query,
            k=inp.key,
            v=inp.value,
            bias=inp.attn_bias if isinstance(inp.attn_bias, torch.Tensor) else None,
            softmax_scale=inp.scale_float,
            causal=isinstance(inp.attn_bias, LowerTriangularMask),
        )
        return out, Context(lse=lse, out=out)


class BwOp(AttentionBwOpBase):
    OPERATOR = triton_flash_backward
    SUPPORTED_DEVICES = FwOp.SUPPORTED_DEVICES
    SUPPORTED_DTYPES = FwOp.SUPPORTED_DTYPES
    SUPPORTED_MAX_K = FwOp.SUPPORTED_MAX_K
    SUPPORTED_ATTN_BIAS_TYPES = FwOp.SUPPORTED_ATTN_BIAS_TYPES
    SUPPORTS_DROPOUT = FwOp.SUPPORTS_DROPOUT
    SUPPORTS_CUSTOM_SCALE = FwOp.SUPPORTS_CUSTOM_SCALE
    SUPPORTS_DIFFERENT_VALUE_EMBED = FwOp.SUPPORTS_DIFFERENT_VALUE_EMBED
    NAME = "tritonflashattB"

    @classmethod
    def supports(cls, d: "Inputs") -> bool:
        return cls.OPERATOR is not None and FwOp.supports(d)

    @classmethod
    def apply(cls, ctx: Context, inp: Inputs, grad: torch.Tensor) -> Gradients:
        inp = _prepare_inputs(inp)

        # Triton's autotune causes the Tensor._version to change, and so Pytorch autograd
        # does a memcpy. To avoid this we run in inference_mode, which doesn't track the version.
        with torch.inference_mode():
            grads = Gradients(
                dq=torch.empty_like(inp.query),
                dk=torch.empty_like(inp.key),
                dv=torch.empty_like(inp.value),
            )
            cls.OPERATOR(
                grad,
                inp.query,
                inp.key,
                inp.value,
                ctx.out,
                ctx.lse,
                grads.dq,
                grads.dk,
                grads.dv,
                bias=inp.attn_bias if isinstance(inp.attn_bias, torch.Tensor) else None,
                softmax_scale=inp.scale_float,
                causal=isinstance(inp.attn_bias, LowerTriangularMask),
            )
        return grads
