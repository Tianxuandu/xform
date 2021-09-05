import logging

import torch
import triton
import triton.language as tl

# CREDITS: This is essentially the vanilla Triton example from https://openai.com/blog/triton/
# and https://triton-lang.org/getting-started/tutorials/02-fused-softmax.html


_triton_register_overflow = False


def next_power_of_2(n):
    """Return the smallest power of 2 greater than or equal to n"""
    assert n < 2 ** 16, "Depths beyond 2^16 are not yet handled by this softmax kernel"

    n -= 1
    n |= n >> 1
    n |= n >> 2
    n |= n >> 4
    n |= n >> 8
    n |= n >> 16
    n += 1
    return n


kernel_configs = [
    triton.Config({}, num_warps=1),
    triton.Config({}, num_warps=2),
    triton.Config({}, num_warps=4),
]


def _get_depth(*args, **kwargs):
    return next_power_of_2(args[-1])


def _get_fp16(*args, **kwargs):
    return args[0].dtype == torch.float16


# autotune: Triton will test out these configurations, and automatically pick the fastest one.
# heuristic: add arguments to the kernal call automatically given some heuristics. These arguments are passed in "meta"
@triton.autotune(
    configs=kernel_configs,
    key=["K"],
)
@triton.heuristics(values={"DEPTH": _get_depth, "is_fp16": _get_fp16})
@triton.jit
def _softmax(
    Y,
    stride_ym,
    stride_yn,
    stride_yk,
    X,
    stride_xm,
    stride_xn,
    stride_xk,
    K,
    **meta,  # extra parameters which can be automatically filled in given some heuristics
):
    """
    Fused softmax kernel over a 3d tensor.
    The softmax is applied over the last dimension, meaning that this is equivalent to torch.softmax(tensor, dim=-1)
    """
    m = tl.program_id(0)
    n = tl.program_id(1)

    # col indices
    k = tl.arange(0, meta["DEPTH"])

    # the memory address of all the elements that we want to load can be computed as follows
    X = X + m * stride_xm + n * stride_xn + k * stride_xk

    # load input data; pad out-of-bounds elements with 0
    x = tl.load(X, mask=k < K, other=float("-inf"))

    # compute numerically-stable softmax
    z = x - tl.max(x, axis=0)

    if meta["is_fp16"]:
        # tl.exp() crashes on fp16 values
        # See https://github.com/openai/triton/issues/241
        z = z.to(tl.float32)

    num = tl.exp(z)

    if meta["is_fp16"]:
        num = num.to(tl.float16)

    denom = tl.sum(num, axis=0)
    y = num / denom

    # write back to Y.
    # we only write once, hence the "fused" softmax naming
    Y = Y + m * stride_ym + n * stride_yn + k * stride_yk
    tl.store(Y, y, mask=k < K)


@triton.jit
def _softmax_backward(
    B,
    stride_bm,
    stride_bn,
    stride_bk,
    G,
    stride_gm,
    stride_gn,
    stride_gk,
    Out,
    stride_om,
    stride_on,
    stride_ok,
    K,
    **meta,
):
    """
    Compute the softmax gradients.
    ..Note: Not autotuning for now because this would lead to broken accumulated gradients
    """

    m = tl.program_id(0)
    n = tl.program_id(1)

    # col indices
    k = tl.arange(0, meta["DEPTH"])

    # the memory address of all the elements that we want to load can be computed as follows
    G = G + m * stride_gm + n * stride_gn + k * stride_gk
    Out = Out + m * stride_om + n * stride_on + k * stride_ok

    # load input data; pad out-of-bounds elements with 0
    g = tl.load(G, mask=k < K, other=float(0))
    o = tl.load(Out, mask=k < K, other=float(0))

    # Step 1: Compute the intermediate sum used for the gradient
    s = tl.sum(g * o, 0)

    # Step 2: Compute the gradients
    b = o * (g - s)

    # write back to B.
    # we only write once, hence the "fused" softmax naming
    B = B + m * stride_bm + n * stride_bn + k * stride_bk
    tl.store(B, b, mask=k < K)


# Helper to handle the SMPD launch grid and error cases
class _softmax_triton(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        """
        Fused softmax implementation, using the Triton programming model.
        This only supports a reduction over the last dimension for now
        """

        assert x.ndim == 3, "This implementation only supports 3-dim tensors"

        y = torch.empty_like(x)

        # SPMD launch grid
        grid_2d = (
            x.shape[0],
            x.shape[1],
        )

        # enqueue GPU kernel
        _softmax[grid_2d](
            y,
            y.stride(0),
            y.stride(1),
            y.stride(2),
            x,
            x.stride(0),
            x.stride(1),
            x.stride(2),
            x.shape[2],
        )

        ctx.save_for_backward(y)
        return y

    @staticmethod
    def backward(ctx, grad):
        (out,) = ctx.saved_tensors

        assert out.ndim == 3, "This implementation only supports 3-dim tensors"

        # SPMD launch grid
        grid_2d = (
            grad.shape[0],
            grad.shape[1],
        )

        DEPTH = next_power_of_2(out.shape[2])

        num_warps = 4
        if DEPTH >= 2048:
            num_warps = 8
        if DEPTH >= 4096:
            num_warps = 16

        # enqueue GPU kernel
        ga = torch.empty_like(out)
        _softmax_backward[grid_2d](
            ga,
            ga.stride(0),
            ga.stride(1),
            ga.stride(2),
            grad,
            grad.stride(0),
            grad.stride(1),
            grad.stride(2),
            out,
            out.stride(0),
            out.stride(1),
            out.stride(2),
            out.shape[2],
            DEPTH=DEPTH,
            num_warps=num_warps,
        )
        return ga


def softmax(x: torch.Tensor) -> torch.Tensor:
    # Triton is used if
    # - CUDA
    # - there's enough data to make it faster than pytorch. This could change over time, Triton is improving
    # - there was no previous failure

    global _triton_register_overflow

    if (
        torch.cuda.is_available()
        and x.is_cuda
        and x.numel()
        and not _triton_register_overflow
    ):
        try:
            return _softmax_triton.apply(x)
        except triton.OutOfResources:
            # Catch cases where the current GPU does not have enough registers to hold a full tensor line
            # fallback to PyTorch's implementation, which streams the tensor in and out
            _triton_register_overflow = True
            logging.warning(
                "Triton softmax kernel register spillover caught."
                "Deactivating this kernel, please file an issue int the xFormers repository"
            )

            pass

    return torch.softmax(x, dim=-1)
