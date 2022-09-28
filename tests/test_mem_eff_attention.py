# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import math

import pytest
import torch
from scipy.stats import binom_test

import xformers.ops

torch.backends.cuda.matmul.allow_tf32 = False
cuda_only = pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA")
_devices = ["cpu", "cuda"] if torch.cuda.is_available() else ["cpu"]


def _generate_op_device_dtype_B_Mq_Mkv_K():
    for op in [
        xformers.ops.MemoryEfficientAttentionOp,
        xformers.ops.MemoryEfficientAttentionCutlassOp,
        xformers.ops.MemoryEfficientAttentionFlashAttentionOp,
    ]:
        for shape in op.generate_test_shapes_B_Mq_Mkv_K():
            for device in _devices:
                if device not in op.SUPPORTED_DEVICES:
                    continue
                for dtype in op.SUPPORTED_DTYPES:
                    yield (op, device, dtype, *shape)


_op_device_dtype_B_Mq_Mkv_K = list(_generate_op_device_dtype_B_Mq_Mkv_K())


def assert_allclose(
    out: torch.Tensor,
    ref: torch.Tensor,
    msg: str = "failed",
    atol: float = 1e-8,
    rtol: float = 1e-5,
) -> None:
    assert out.shape == ref.shape
    flatten_diff = ((out - ref).abs() - atol - ref.abs() * rtol).flatten()
    max_pos = flatten_diff.argmax()
    assert torch.allclose(out, ref, rtol=rtol, atol=atol), (
        f"{msg}: "
        f"out={out.flatten()[max_pos]} and ref={ref.flatten()[max_pos]} (diff={flatten_diff[max_pos]} > 0)"
        f"/ atol={atol}, rtol={rtol}"
    )


def ref_attention(q, k, v, attn_bias=None, drop_mask=None, p=0.0):
    q = q.float()
    k = k.float()
    v = v.float()

    q = q * (1 / q.shape[-1] ** 0.5)
    attn = q @ k.transpose(-2, -1)
    if attn_bias is not None:
        if isinstance(attn_bias, xformers.ops.AttentionMask):
            attn_bias = attn_bias.to_tensor()
        attn = attn + attn_bias.float()
    attn = attn.softmax(-1)
    if drop_mask is not None:
        attn = attn * (drop_mask / (1 - p))
    return attn @ v


def create_attn_bias(
    bias_type, batch_size: int, q_len: int, kv_len: int, device, dtype
):
    if bias_type is None:
        return None
    if bias_type is torch.Tensor:
        attn_bias = torch.randn((batch_size, 1, kv_len), device=device, dtype=dtype) * 3
        return attn_bias.expand(batch_size, q_len, kv_len)
    if bias_type is xformers.ops.LowerTriangularMask:
        return bias_type([batch_size, q_len, kv_len], dtype=dtype, device=device)
    assert False, f"Unsupported bias type: {bias_type}"


@pytest.mark.parametrize(
    "attn_bias_type", [None, xformers.ops.LowerTriangularMask, torch.Tensor]
)
@pytest.mark.parametrize(
    "op,device,dtype,batch_size,q_len,kv_len,k_len", _op_device_dtype_B_Mq_Mkv_K
)
def test_forward(
    device,
    q_len,
    kv_len,
    batch_size,
    k_len,
    attn_bias_type,
    dtype,
    op: xformers.ops.MemoryEfficientAttentionOp,
):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device, dtype=dtype) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale
    attn_bias = None
    if attn_bias_type is not None:
        attn_bias = create_attn_bias(
            attn_bias_type,
            batch_size=batch_size,
            q_len=q_len,
            kv_len=kv_len,
            dtype=dtype,
            device=device,
        )

    if not op.supports(
        xformers.ops.AttentionOpDispatch.from_arguments(
            query=query, key=key, value=value, attn_bias=attn_bias
        )
    ):
        pytest.skip("unsupported configuration")

    out = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, op=op
    ).float()
    ref = ref_attention(query, key, value, attn_bias)

    assert_allclose(
        out,
        ref,
        atol=op.FORWARD_ERROR_ATOL[dtype],
        rtol=op.FORWARD_ERROR_RTOL.get(dtype, 1e-5),
    )


@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 4])
@pytest.mark.parametrize("kv_len", [128, 512])
@pytest.mark.parametrize("q_len", [128, 512])
@pytest.mark.parametrize("device", _devices)
def test_key_query_all_ones(device, q_len, kv_len, batch_size, k_len):
    scale = 3
    query = torch.ones((batch_size, q_len, k_len), device=device)
    key = torch.ones((batch_size, kv_len, k_len), device=device)
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    out = xformers.ops.memory_efficient_attention(query, key, value)
    # this should be equivalent to the average over value
    ref = value.mean(1, keepdim=True).expand_as(query)

    assert_allclose(out, ref, atol=1e-5)


@pytest.mark.parametrize(
    "op,device,dtype,batch_size,q_len,kv_len,k_len", _op_device_dtype_B_Mq_Mkv_K
)
def test_logsumexp(
    device,
    q_len,
    kv_len,
    batch_size,
    k_len,
    dtype,
    op: xformers.ops.MemoryEfficientAttentionOp,
):
    if op.FORWARD_OPERATOR is None:
        return

    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device, dtype=dtype) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale

    if not op.supports(
        xformers.ops.AttentionOpDispatch.from_arguments(
            query=query, key=key, value=value
        )
    ):
        pytest.skip("unsupported configuration")

    if op is xformers.ops.MemoryEfficientAttentionCutlassOp:
        _, lse = op.FORWARD_OPERATOR(
            query.unsqueeze(2),
            key.unsqueeze(2),
            value.unsqueeze(2),
            max_seqlen_q=None,
            cu_seqlens_q=None,
            cu_seqlens_k=None,
            compute_logsumexp=True,
            causal=False,
        )
        lse = lse[:, 0, :]
    else:
        _, lse, _, _ = op.FORWARD_OPERATOR(query, key, value, True, None, 0.0)
    ref_lse = (
        (query.float() / k_len**0.5) @ key.float().transpose(-2, -1)
    ).logsumexp(-1)

    assert_allclose(lse[:, : ref_lse.shape[1]], ref_lse, atol=2e-4)


@pytest.mark.parametrize(
    "attn_bias_type", [None, xformers.ops.LowerTriangularMask, torch.Tensor]
)
@pytest.mark.parametrize("grad_out_contiguous", [False, True])
@pytest.mark.parametrize(
    "op,device,dtype,batch_size,q_len,kv_len,k_len", _op_device_dtype_B_Mq_Mkv_K
)
def test_backward(
    device,
    q_len,
    kv_len,
    batch_size,
    k_len,
    grad_out_contiguous,
    attn_bias_type,
    dtype,
    op: xformers.ops.MemoryEfficientAttentionOp,
):
    torch.manual_seed(batch_size * q_len + kv_len * k_len)
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device, dtype=dtype) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale

    attn_bias = None
    if attn_bias_type is not None:
        attn_bias = create_attn_bias(
            attn_bias_type,
            batch_size=batch_size,
            q_len=q_len,
            kv_len=kv_len,
            dtype=dtype,
            device=device,
        )

    if not op.supports(
        xformers.ops.AttentionOpDispatch.from_arguments(
            query=query, key=key, value=value, attn_bias=attn_bias
        )
    ):
        pytest.skip("unsupported configuration")

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)
    if grad_out_contiguous is False:
        grad_out = torch.tensor([1.0], device=device)[None, None, :].expand_as(query)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, op=op)
    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)

    atol = 2e-4 + 2e-6 * k_len * kv_len * math.sqrt(q_len)
    rtol = 1e-4
    if dtype is torch.half:
        atol = 4e-2
        rtol = 2e-2
        # Longer sequences mean we iterate more and errors accumulate
        atol *= (127 + q_len) // 128
    if dtype is torch.bfloat16:
        # I've seen (out=0.04 / ref=-0.4 with flash)
        atol = 0.5
        rtol = 0.1

    # (for mypy)
    assert isinstance(query.grad, torch.Tensor)
    assert isinstance(key.grad, torch.Tensor)
    assert isinstance(value.grad, torch.Tensor)

    for name, calc_grad, ref_grad in [
        ("query", grad_q, query.grad),
        ("key", grad_k, key.grad),
        ("value", grad_v, value.grad),
    ]:
        assert_allclose(calc_grad, ref_grad, name, atol=atol, rtol=rtol)


def _vec_binom_test(x, n, p):
    """
    vectorized implementation of scipy.stats.binom_test
    this makes our tests much faster
    reference: https://github.com/scipy/scipy/blob/v1.8.0/scipy/stats/_morestats.py#L2609-L2702
    """
    import numpy as np
    from scipy.stats import distributions

    x = np.atleast_1d(x)
    d = distributions.binom.pmf(x, n, p)[:, None]
    rerr = 1 + 1e-7
    # x < p * n case
    i = np.arange(np.ceil(p * n), n + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval1 = distributions.binom.cdf(x, n, p) + distributions.binom.sf(n - y, n, p)

    # other case
    i = np.arange(np.floor(p * n) + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval2 = distributions.binom.cdf(y - 1, n, p) + distributions.binom.sf(x - 1, n, p)

    pval = np.where(x < p * n, pval1, pval2)
    pval = np.minimum(1.0, pval)
    return pval


@cuda_only
@pytest.mark.parametrize("seed", [42, 124])
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
@pytest.mark.parametrize("device", ["cuda"])
def test_dropout(device, q_len, kv_len, batch_size, k_len, p, seed):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    attn_bias = None

    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    torch.manual_seed(seed)
    out2 = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    assert_allclose(out, out2)

    mask = torch.empty((batch_size, q_len, kv_len), device=device)

    torch.manual_seed(seed)
    mask = torch.ops.xformers._temp_dropout(mask, p)

    ref = ref_attention(query, key, value, attn_bias, mask, p)
    assert_allclose(out, ref, atol=2e-4), f"{(out - ref).abs().max()}"

    num_trials = 1000
    p_val_tol = 0.0001
    keep_prob = 1 - p
    masks = []
    for i in range(num_trials):
        mask = torch.ops.xformers._temp_dropout(mask, p)
        masks.append(mask.clone().cpu())
    masks = torch.stack(masks, dim=0)
    p_value = binom_test(masks.sum(), masks.numel(), p=keep_prob)
    assert p_value > p_val_tol, p_value
    masks = masks.sum(0).flatten()
    p_values = _vec_binom_test(masks, num_trials, p=keep_prob)
    assert all(p_values > p_val_tol)


@cuda_only
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
@pytest.mark.parametrize("device", ["cuda"])
def test_dropout_backward(device, q_len, kv_len, batch_size, k_len, p):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    attn_bias = None

    seed = 42
    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias, p)

    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    mask = torch.empty((batch_size, q_len, kv_len), device=device)

    torch.manual_seed(seed)
    mask = torch.ops.xformers._temp_dropout(mask, p)

    ref = ref_attention(query, key, value, attn_bias, mask, p)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)


@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1])
@pytest.mark.parametrize("kv_len", [3 * 32])
@pytest.mark.parametrize("q_len", [3 * 32])
@pytest.mark.parametrize("device", _devices)
def test_memory_efficient_attention_full_block_masked(
    device, q_len, kv_len, batch_size, k_len
):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    # in this case, most of the blocks in a row get masked
    attn_bias = torch.full((3, 32), float("-inf"), device=device)
    attn_bias[:2, :4] = 0
    attn_bias = attn_bias.flatten()[None, None, :].expand(1, q_len, -1)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    ref = ref_attention(query, key, value, attn_bias)

    assert_allclose(out, ref, atol=2e-4)

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)
