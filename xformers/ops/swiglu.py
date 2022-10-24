# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional

import torch
import torch.nn.functional as F
from torch import nn

from .unbind import efficient_stack_or_none


class _SwiGLUModule(nn.Module):
    """
    Reference implementation of a SwiGLU module
    """

    def __init__(
        self,
        in_features: int,
        hidden_features: Optional[int] = None,
        out_features: Optional[int] = None,
        align_as: int = 8,
    ) -> None:
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        swiglu_hidden_features = int(2 * hidden_features / 3)
        swiglu_hidden_features = (
            (swiglu_hidden_features + align_as - 1) // align_as * align_as
        )

        self.w1 = nn.Linear(in_features, swiglu_hidden_features)
        self.w2 = nn.Linear(in_features, swiglu_hidden_features)
        self.w3 = nn.Linear(swiglu_hidden_features, out_features)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x1 = self.w1(x)
        x2 = self.w2(x)
        hidden = F.silu(x1) * x2
        return self.w3(hidden)

    def _ordered_params_for_op(self):
        """Used for testing - returns ordered arguments for operators"""
        return [
            self.w1.weight,
            self.w1.bias,
            self.w2.weight,
            self.w2.bias,
            self.w3.weight,
            self.w3.bias,
        ]


class _SwiGLUDecomposedOp(torch.autograd.Function):
    """
    This is just an example implementation with all
    operations explicited. This implementation is worse
    than pytorch, because pytorch is able to fuse some operations
    (eg the linear forward ...) that are decomposed here.

    The time measurements were made on the ViT-Giant setting:
    - A100/f16
    - input: [4440, 1536]
    - hidden: [4440, 4096]
    """

    NAME = "decomposed"
    FORCE_BW_F32 = False

    def _silu_backward(dy, x):
        # https://github.com/pytorch/pytorch/blob/563b065f5a4b4055fa6b025c2514b566d5fd9439/aten/src/ATen/native/Activation.cpp#L483
        sigm = 1 / (1 + torch.exp(-x.float()))
        return (dy.float() * sigm * (1 + x.float() * (1 - sigm))).to(x.dtype)

    # 952us
    @classmethod
    def forward(cls, ctx, x, w1, b1, w2, b2, w3, b3):
        x1 = x @ w1.transpose(-2, -1) + b1  # 275us
        x2 = x @ w2.transpose(-2, -1) + b2  # 275us
        x3 = F.silu(x1)  # 62us
        x4 = x3 * x2  # 90us
        x5 = x4 @ w3.transpose(-2, -1) + b3  # 250us

        ctx.save_for_backward(x, w1, b1, w2, b2, w3, b3, x1, x2, x3, x4, x5)
        return x5

    # 1900us
    @classmethod
    def backward(cls, ctx, dx5):
        saved_tensors = ctx.saved_tensors
        if cls.FORCE_BW_F32:
            dx5 = dx5.float()
            saved_tensors = [t.float() for t in ctx.saved_tensors]
        x, w1, b1, w2, b2, w3, b3, x1, x2, x3, x4, x5 = saved_tensors
        dx4 = dx5 @ w3  # 255us (nn)
        dw3 = dx5.transpose(-2, -1) @ x4  # 247us (nt)
        db3 = dx5.sum(0)  # 25us
        dx3 = dx4 * x2  # 88us
        dx2 = dx4 * x3  # 88us
        dx1 = cls._silu_backward(dx3, x1)  # 90us
        dx = dx2 @ w2  # 260us (nn)
        dw2 = dx2.transpose(-2, -1) @ x  # 245us (nt)
        db2 = dx2.sum(0)  # 50us
        dx += dx1 @ w1  # 260us (nn)
        dw1 = dx1.transpose(-2, -1) @ x  # 245us (nt)
        db1 = dx1.sum(0)  # 50us
        return (dx, dw1, db1, dw2, db2, dw3, db3)


class _SwiGLUFusedOp(torch.autograd.Function):
    NAME = "fused"

    @classmethod
    def forward(cls, ctx, x, w1, b1, w2, b2, w3, b3):
        x1, x2, x4 = torch.ops.xformers.dual_gemm_silu_identity_mul(x, w1, b1, w2, b2)
        x5 = F.linear(x4, w3, b3)
        ctx.save_for_backward(x, w1, w2, w3, x1, x2, x4)
        return x5

    @classmethod
    def backward(cls, ctx, dx5):
        x, w1, w2, w3, x1, x2, x4 = ctx.saved_tensors
        w1w2 = efficient_stack_or_none([w1, w2], dim=0)

        dx4 = dx5 @ w3  # 255us (nn)
        dw3 = dx5.transpose(-2, -1) @ x4  # 247us (nt)
        del x4
        db3 = dx5.sum(0)  # 25us
        del dx5
        dx1, dx2 = torch.ops.xformers.silu_bw_fused(x1, x2, dx4)
        del x2, dx4
        if w1w2 is not None:
            dx1dx2 = efficient_stack_or_none([dx1, dx2], dim=1)
            assert dx1dx2 is not None
            assert dx1dx2.is_contiguous()
            assert w1w2.is_contiguous()
            dx1dx2 = dx1dx2.view([dx1.shape[0], dx1.shape[1] * 2])
            w1w2 = w1w2.view([w1.shape[0] * 2, w1.shape[1]])
            dx = dx1dx2 @ w1w2
            # Create [dw1,dw2] already stacked together
            dw1dw2 = torch.empty_like(w1w2).view([w1.shape[0], 2, w1.shape[1]])
            dw1, dw2 = torch.unbind(dw1dw2, dim=1)
            torch.matmul(dx2.transpose(-2, -1), x, out=dw2)
            db2 = dx2.sum(0)  # 50us
            torch.matmul(dx1.transpose(-2, -1), x, out=dw1)
            db1 = dx1.sum(0)  # 50us
        else:
            dx = dx2 @ w2  # 260us (nn)
            dx += dx1 @ w1  # 260us (nn)
            dw2 = dx2.transpose(-2, -1) @ x  # 245us (nt)
            db2 = dx2.sum(0)  # 50us
            dw1 = dx1.transpose(-2, -1) @ x  # 245us (nt)
            db1 = dx1.sum(0)  # 50us
        return (dx, dw1, db1, dw2, db2, dw3, db3)


def functional_swiglu(
    x: torch.Tensor,
    w1: torch.Tensor,
    b1: torch.Tensor,
    w2: torch.Tensor,
    b2: torch.Tensor,
    w3: torch.Tensor,
    b3: torch.Tensor,
    *,
    op=None
) -> torch.Tensor:
    if op is not None:
        return op.apply(x, w1, b1, w2, b2, w3, b3)
    x1 = F.linear(x, w1, b1)
    x2 = F.linear(x, w2, b2)
    hidden = F.silu(x1) * x2
    return F.linear(hidden, w3, b3)
