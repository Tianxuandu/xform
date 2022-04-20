#include <ATen/ATen.h>
#include <torch/library.h>
#include <cmath>
#include <vector>

#include <ATen/cuda/CUDAContext.h>
#include <c10/cuda/CUDAGuard.h>
#include <ATen/cuda/Atomic.cuh>

#include "sputnik/vector_utils.h"

namespace {

template <typename integer>
constexpr __host__ __device__ inline integer ceil_div(integer n, integer m) {
  return (n + m - 1) / m;
}

template <typename scalar_t>
constexpr __host__ __device__ bool integerIsPowerOf2(scalar_t v) {
  return (v && !(v & (v - 1)));
}

template <typename scalar_t>
__device__ __forceinline__ void iMul(scalar_t x1, float4* out) {
  out[0].x *= x1;
  out[0].y *= x1;
  out[0].z *= x1;
  out[0].w *= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void iMul(scalar_t x1, float2* out) {
  out[0].x *= x1;
  out[0].y *= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void iMul(scalar_t x1, float* out) {
  out[0] *= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void iDiv(scalar_t x1, float4* out) {
  out[0].x /= x1;
  out[0].y /= x1;
  out[0].z /= x1;
  out[0].w /= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void iDiv(scalar_t x1, float2* out) {
  out[0].x /= x1;
  out[0].y /= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void iDiv(scalar_t x1, float* out) {
  out[0] /= x1;
}

template <typename scalar_t>
__device__ __forceinline__ void myGpuAtomicAdd(scalar_t* address, float4 val) {
  gpuAtomicAdd(address + 0, val.x);
  gpuAtomicAdd(address + 1, val.y);
  gpuAtomicAdd(address + 2, val.z);
  gpuAtomicAdd(address + 3, val.w);
}

template <typename scalar_t>
__device__ __forceinline__ void myGpuAtomicAdd(scalar_t* address, float2 val) {
  gpuAtomicAdd(address + 0, val.x);
  gpuAtomicAdd(address + 1, val.y);
}

template <typename scalar_t>
__device__ __forceinline__ void myGpuAtomicAdd(scalar_t* address, float val) {
  gpuAtomicAdd(address, val);
}

template <typename scalar_t, int WARP_SIZE>
__device__ __forceinline__ scalar_t warpSum(scalar_t val) {
  for (int stride = WARP_SIZE / 2; stride > 0; stride >>= 1) {
    val += __shfl_xor_sync(0xffffffff, val, stride, WARP_SIZE);
  }
  return val;
}

template <typename scalar_t, int WARP_SIZE>
__device__ __forceinline__ float2 warpSum(float2 val) {
  for (int stride = WARP_SIZE / 2; stride > 0; stride >>= 1) {
    val.x += __shfl_xor_sync(0xffffffff, val.x, stride, WARP_SIZE);
    val.y += __shfl_xor_sync(0xffffffff, val.y, stride, WARP_SIZE);
  }
  return val;
}

template <typename scalar_t, int WARP_SIZE>
__device__ __forceinline__ float4 warpSum(float4 val) {
  for (int stride = WARP_SIZE / 2; stride > 0; stride >>= 1) {
    val.x += __shfl_xor_sync(0xffffffff, val.x, stride, WARP_SIZE);
    val.y += __shfl_xor_sync(0xffffffff, val.y, stride, WARP_SIZE);
    val.z += __shfl_xor_sync(0xffffffff, val.z, stride, WARP_SIZE);
    val.w += __shfl_xor_sync(0xffffffff, val.w, stride, WARP_SIZE);
  }
  return val;
}

template <typename scalar_t, int WARP_SIZE>
__device__ __forceinline__ scalar_t warpMax(scalar_t val) {
  for (int stride = WARP_SIZE / 2; stride > 0; stride >>= 1) {
    scalar_t tmp = __shfl_xor_sync(0xffffffff, val, stride, WARP_SIZE);
    val = tmp > val ? tmp : val;
  }
  return val;
}

template <typename scalar_t, typename vec_t, int kBlockSizeK, int kBlockSizeQ>
__device__ void compute_dot(
    vec_t* queries[kBlockSizeQ],
    vec_t* keys,
    scalar_t out[kBlockSizeQ][kBlockSizeK],
    int64_t K) {
  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);
  scalar_t scale = 1.0; // / std::sqrt(scalar_t(K));
  vec_t q_i[kBlockSizeQ];
  for (int64_t k = 0; k < K / kVecSize; k += 1) {
#pragma unroll
    for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      q_i[q_item_idx] = __ldg(queries[q_item_idx] + k);
      iMul(scale, q_i + q_item_idx);
    }
#pragma unroll
    for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
      vec_t k_i = keys[k + K / kVecSize * k_item_idx];
#pragma unroll
      for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
        sputnik::VectorCompute<vec_t>::Dot(
            q_i[q_item_idx], k_i, &out[q_item_idx][k_item_idx]);
      }
    }
  }
}

template <
    typename scalar_t,
    typename vec_t,
    int kBlockSizeK,
    int kBlockSizeQ,
    int BUFFER_SIZE>
__device__ void compute_final_mult(
    vec_t* vi,
    scalar_t s_delta[kBlockSizeQ][kBlockSizeK],
    scalar_t m_delta[kBlockSizeQ],
    vec_t buffer[kBlockSizeQ][BUFFER_SIZE] /*TODO [BUFFER_SIZE limitation]*/,
    int64_t K) {
  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);

  for (int64_t k = 0; k < K / kVecSize; k += 1) {
#pragma unroll
    for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      iMul<scalar_t>(m_delta[q_item_idx], &buffer[q_item_idx][k]);
    }
#pragma unroll
    for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
      vec_t tmp2 = vi[k + K / kVecSize * k_item_idx];

#pragma unroll
      for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
        sputnik::VectorCompute<vec_t>::FMA(
            s_delta[q_item_idx][k_item_idx], tmp2, &buffer[q_item_idx][k]);
      }
    }
  }
}

template <typename scalar_t, int kBlockSizeK, int kBlockSizeQ>
__device__ __forceinline__ void compute_max(
    scalar_t a[kBlockSizeQ][kBlockSizeK],
    scalar_t b[kBlockSizeQ],
    scalar_t out[kBlockSizeQ]) {
#pragma unroll
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    out[q_item_idx] =
        a[q_item_idx][0] > b[q_item_idx] ? a[q_item_idx][0] : b[q_item_idx];
#pragma unroll
    for (int64_t k_item_idx = 1; k_item_idx < kBlockSizeK; k_item_idx++) {
      out[q_item_idx] = a[q_item_idx][k_item_idx] > out[q_item_idx]
          ? a[q_item_idx][k_item_idx]
          : out[q_item_idx];
    }
  }
}

template <typename scalar_t, int kBlockSizeK, int kBlockSizeQ>
__device__ __forceinline__ void compute_scaling_coeffs(
    scalar_t m_i[kBlockSizeQ],
    scalar_t m_prime[kBlockSizeQ],
    scalar_t si[kBlockSizeQ][kBlockSizeK],
    scalar_t m_delta[kBlockSizeQ],
    scalar_t s_delta[kBlockSizeQ][kBlockSizeK]) {
#pragma unroll
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++)
    m_delta[q_item_idx] = std::exp(m_prime[q_item_idx] - m_i[q_item_idx]);
#pragma unroll
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++)
#pragma unroll
    for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++)
      s_delta[q_item_idx][k_item_idx] =
          std::exp(si[q_item_idx][k_item_idx] - m_i[q_item_idx]);
}

template <typename scalar_t, int kBlockSizeK, int kBlockSizeQ>
__device__ __forceinline__ void update_scaling_coeffs(
    scalar_t m_delta[kBlockSizeQ],
    scalar_t m_i[kBlockSizeQ],
    scalar_t s_delta[kBlockSizeQ][kBlockSizeK],
    scalar_t m_prime[kBlockSizeQ],
    scalar_t s_prime[kBlockSizeQ]) {
#pragma unroll
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    s_prime[q_item_idx] = s_prime[q_item_idx] * m_delta[q_item_idx];
#pragma unroll
    for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++)
      s_prime[q_item_idx] += s_delta[q_item_idx][k_item_idx];

    m_prime[q_item_idx] = m_i[q_item_idx];
  }
}

template <
    typename scalar_t,
    typename vec_t,
    int kBlockSizeK,
    int kBlockSizeQ,
    int BUFFER_SIZE>
__device__ void compute_loop(
    vec_t* query_block[kBlockSizeQ],
    vec_t* key_i,
    vec_t* value_i,
    scalar_t m_prime[kBlockSizeQ],
    scalar_t s_prime[kBlockSizeQ],
    vec_t buffer[kBlockSizeQ][BUFFER_SIZE] /*TODO [BUFFER_SIZE limitation]*/,
    int64_t K) {
  scalar_t si[kBlockSizeQ][kBlockSizeK] = {0};
  compute_dot<scalar_t, vec_t, kBlockSizeK, kBlockSizeQ>(
      query_block, key_i, si, K);

  scalar_t m_i[kBlockSizeQ];
  compute_max<scalar_t, kBlockSizeK, kBlockSizeQ>(si, m_prime, m_i);

  scalar_t m_delta[kBlockSizeQ];
  scalar_t s_delta[kBlockSizeQ][kBlockSizeK];

  compute_scaling_coeffs<scalar_t, kBlockSizeK, kBlockSizeQ>(
      m_i, m_prime, si, m_delta, s_delta);

  compute_final_mult<scalar_t, vec_t, kBlockSizeK, kBlockSizeQ, BUFFER_SIZE>(
      value_i, s_delta, m_delta, buffer, K);

  update_scaling_coeffs<scalar_t, kBlockSizeK, kBlockSizeQ>(
      m_delta, m_i, s_delta, m_prime, s_prime);
}

template <
    typename scalar_t,
    typename vec_t,
    int kBlockSizeQ,
    int WARP_SIZE,
    int BUFFER_SIZE>
__device__ __forceinline__ void aggregate_coeffs(
    scalar_t m_prime[kBlockSizeQ],
    scalar_t s_prime[kBlockSizeQ],
    vec_t buffer[kBlockSizeQ][BUFFER_SIZE] /*TODO [BUFFER_SIZE limitation]*/,
    int64_t K) {
  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    scalar_t m_i = m_prime[q_item_idx];
    scalar_t s_i = s_prime[q_item_idx];
    m_prime[q_item_idx] = warpMax<scalar_t, WARP_SIZE>(m_prime[q_item_idx]);
    scalar_t m_delta = std::exp(m_i - m_prime[q_item_idx]);
    scalar_t s_delta = s_i * m_delta;
    s_delta = warpSum<scalar_t, WARP_SIZE>(s_delta);
    s_prime[q_item_idx] = s_delta;
    for (int64_t k = 0; k < K / kVecSize; k += 1) {
      vec_t tmp = buffer[q_item_idx][k];
      iMul<scalar_t>(m_delta, &tmp);
      tmp = warpSum<vec_t, WARP_SIZE>(tmp);
      buffer[q_item_idx][k] = tmp;
    }
  }
}

template <
    bool first,
    typename scalar_t,
    typename vec_t,
    int kBlockSizeK,
    int kBlockSizeQ,
    int BUFFER_SIZE,
    int WARP_SIZE>
struct UnrollLoop {
  static __device__ __forceinline__ void eval(
      vec_t* query_block[kBlockSizeQ],
      at::TensorAccessor<scalar_t, 2> key,
      at::TensorAccessor<scalar_t, 2> value,
      scalar_t m_prime[kBlockSizeQ],
      scalar_t s_prime[kBlockSizeQ],
      vec_t buffer[kBlockSizeQ][BUFFER_SIZE] /*TODO [BUFFER_SIZE limitation]*/,
      int64_t K,
      int64_t N) {
    constexpr int64_t step = kBlockSizeK * WARP_SIZE;
    int64_t l;
    if (first) {
      l = threadIdx.x * kBlockSizeK;
    } else {
      l = N - (N & (2 * step - 1)) + threadIdx.x * kBlockSizeK;
    }
    // this is equivalent to N - N % step, but faster
    // guaranteed to be the same as step is a power of 2
    int64_t end_iter = N - (N & (step - 1));
    // if (l < end_iter) {
    {
      for (; l < end_iter; l += step) {
        auto key_i = reinterpret_cast<vec_t*>(key[l].data());
        auto value_i = reinterpret_cast<vec_t*>(value[l].data());

        compute_loop<scalar_t, vec_t, kBlockSizeK, kBlockSizeQ, BUFFER_SIZE>(
            query_block, key_i, value_i, m_prime, s_prime, buffer, K);
      }
    }
    {
      UnrollLoop<
          false,
          scalar_t,
          vec_t,
          kBlockSizeK / 2,
          kBlockSizeQ,
          BUFFER_SIZE,
          WARP_SIZE>::
          eval(query_block, key, value, m_prime, s_prime, buffer, K, N);
    }
  }
};

template <
    bool first,
    typename scalar_t,
    typename vec_t,
    int kBlockSizeQ,
    int BUFFER_SIZE,
    int WARP_SIZE>
struct UnrollLoop<
    first,
    scalar_t,
    vec_t,
    0,
    kBlockSizeQ,
    BUFFER_SIZE,
    WARP_SIZE> {
  static __device__ __forceinline__ void eval(
      vec_t* query_block[kBlockSizeQ],
      at::TensorAccessor<scalar_t, 2> key,
      at::TensorAccessor<scalar_t, 2> value,
      scalar_t m_prime[kBlockSizeQ],
      scalar_t s_prime[kBlockSizeQ],
      vec_t buffer[kBlockSizeQ][BUFFER_SIZE] /*TODO [BUFFER_SIZE limitation]*/,
      int64_t K,
      int64_t N) {}
};

template <
    typename scalar_t,
    typename vec_t,
    int kBlockSizeK,
    int kBlockSizeQ,
    int WARP_SIZE,
    int BUFFER_SIZE>
__global__ void attention_kernel(
    at::PackedTensorAccessor<scalar_t, 3> output,
    at::PackedTensorAccessor<scalar_t, 3> query,
    at::PackedTensorAccessor<scalar_t, 3> key,
    at::PackedTensorAccessor<scalar_t, 3> value) {
  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);
  static_assert(
      integerIsPowerOf2(kBlockSizeK * WARP_SIZE),
      "kBlockSizeK * WARP_SIZE should be a power of 2");
  int64_t K = query.size(2);
  int64_t B = query.size(0);
  int64_t M = query.size(1);
  int64_t N = key.size(1);

  int64_t batch_idx = blockIdx.y;
  int64_t query_idx =
      blockIdx.x * (blockDim.y * kBlockSizeQ) + threadIdx.y * kBlockSizeQ;

  if (query_idx >= M)
    return;

  vec_t* query_block[kBlockSizeQ];
  vec_t* output_block[kBlockSizeQ];
  // TODO [BUFFER_SIZE limitation]: the current strategy assumes a
  // statically-known size for K. Ideally we would like to remove this
  // limitation in the future, so that any K is supported
  vec_t buffer[kBlockSizeQ][BUFFER_SIZE] = {0};
  scalar_t s_prime[kBlockSizeQ] = {0};
  scalar_t m_prime[kBlockSizeQ];
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    int64_t index = query_idx + q_item_idx;
    index = index >= M ? M - 1 : index;
    query_block[q_item_idx] =
        reinterpret_cast<vec_t*>(query[batch_idx][index].data());
    output_block[q_item_idx] =
        reinterpret_cast<vec_t*>(output[batch_idx][index].data());
    m_prime[q_item_idx] = -std::numeric_limits<scalar_t>::infinity();
  }
#if 0
  // this for now makes things slower
  UnrollLoop<true, scalar_t, vec_t, kBlockSizeK, kBlockSizeQ, BUFFER_SIZE, WARP_SIZE>::eval(query_block, key[batch_idx], value[batch_idx], m_prime, s_prime, buffer, K, N);
#else
  int64_t l = threadIdx.x * kBlockSizeK;
  constexpr int64_t step = kBlockSizeK * WARP_SIZE;
  // this is equivalent to N - N % step, but faster
  // guaranteed to be the same as step is a power of 2
  int64_t end_iter = N - (N & (step - 1));
  for (; l < end_iter; l += step) {
    auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
    auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());

    compute_loop<scalar_t, vec_t, kBlockSizeK, kBlockSizeQ, BUFFER_SIZE>(
        query_block, key_i, value_i, m_prime, s_prime, buffer, K);
  }

  {
    // TODO: unroll this in a generic manner
    l = N - (N & (step - 1)) + threadIdx.x * (kBlockSizeK / 2);
    end_iter = N - (N & (step / 2 - 1));
    for (; l < end_iter; l += step / 2) {
      auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
      auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
      compute_loop<scalar_t, vec_t, kBlockSizeK / 2, kBlockSizeQ, BUFFER_SIZE>(
          query_block, key_i, value_i, m_prime, s_prime, buffer, K);
    }

    l = N - (N & (step / 2 - 1)) + threadIdx.x * (kBlockSizeK / 4);
    end_iter = N - (N & (step / 4 - 1));
    for (; l < end_iter; l += step / 4) {
      auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
      auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
      compute_loop<scalar_t, vec_t, kBlockSizeK / 4, kBlockSizeQ, BUFFER_SIZE>(
          query_block, key_i, value_i, m_prime, s_prime, buffer, K);
    }

    l = N - (N & (step / 4 - 1)) + threadIdx.x * (kBlockSizeK / 8);
    end_iter = N - (N & (step / 8 - 1));
    for (; l < end_iter; l += step / 8) {
      auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
      auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
      compute_loop<scalar_t, vec_t, kBlockSizeK / 8, kBlockSizeQ, BUFFER_SIZE>(
          query_block, key_i, value_i, m_prime, s_prime, buffer, K);
    }

    l = N - (N & (step / 8 - 1)) + threadIdx.x * (kBlockSizeK / 16);
    end_iter = N - (N & (step / 16 - 1));
    for (; l < end_iter; l += step / 16) {
      auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
      auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
      compute_loop<scalar_t, vec_t, kBlockSizeK / 16, kBlockSizeQ, BUFFER_SIZE>(
          query_block, key_i, value_i, m_prime, s_prime, buffer, K);
    }

    l = N - (N & (step / 16 - 1)) + threadIdx.x;
    for (; l < N; l += blockDim.x) {
      auto key_i = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
      auto value_i = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
      compute_loop<scalar_t, vec_t, 1, kBlockSizeQ, BUFFER_SIZE>(
          query_block, key_i, value_i, m_prime, s_prime, buffer, K);
    }
  }
#endif

  aggregate_coeffs<scalar_t, vec_t, kBlockSizeQ, WARP_SIZE, BUFFER_SIZE>(
      m_prime, s_prime, buffer, K);

  for (int64_t k = threadIdx.x; k < K / kVecSize; k += blockDim.x) {
    vec_t tmp;

#pragma unroll
    for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      tmp = buffer[q_item_idx][k];
      iDiv<scalar_t>(s_prime[q_item_idx], &tmp);

      output_block[q_item_idx][k] = tmp;
    }
  }
}

at::Tensor attention(
    const at::Tensor& query,
    const at::Tensor& key,
    const at::Tensor& value
    // const at::Tensor& mask
) {
  TORCH_CHECK(query.dim() == key.dim());
  TORCH_CHECK(query.dim() == value.dim());
  // TORCH_CHECK(query.dim() == mask.dim());
  TORCH_CHECK(query.dim() == 3);
  TORCH_CHECK(query.size(2) == key.size(2));
  TORCH_CHECK(query.size(0) == key.size(0));

  TORCH_CHECK(query.size(0) == value.size(0));
  TORCH_CHECK(key.size(1) == value.size(1));
  TORCH_CHECK(
      query.size(2) ==
      value.size(2)); // TODO: drop this limitation in the future

  TORCH_CHECK(query.is_cuda(), "query must be a CUDA tensor");
  TORCH_CHECK(key.is_cuda(), "key must be a CUDA tensor");
  TORCH_CHECK(value.is_cuda(), "value must be a CUDA tensor");

  TORCH_CHECK(!query.is_sparse(), "query must be a dense tensor");
  TORCH_CHECK(!key.is_sparse(), "key must be a dense tensor");
  TORCH_CHECK(!value.is_sparse(), "value must be a dense tensor");

  // TODO drop this limitation in the future
  TORCH_CHECK(query.is_contiguous());
  TORCH_CHECK(key.is_contiguous());
  TORCH_CHECK(value.is_contiguous());

  // TODO: support other dtypes in the future
  TORCH_CHECK(
      query.scalar_type() == at::ScalarType::Float,
      "Only float32 type is supported for now");

  at::cuda::CUDAGuard device_guard(query.device());

  int64_t B = query.size(0);
  int64_t M = query.size(1);
  int64_t N = key.size(1);
  int64_t K = query.size(2);

  at::Tensor res = at::zeros({B, M, K}, query.options());

  cudaStream_t stream = at::cuda::getCurrentCUDAStream();

  constexpr int WARP_SIZE = 4;

  constexpr int kBlockSizeK = 32;
  constexpr int kBlockSizeQ = 2;

  constexpr int TILE_SIZE = 32;
  constexpr int BUFFER_SIZE = 8;

  dim3 grid(ceil_div(M, int64_t(TILE_SIZE)), B);
  dim3 block(WARP_SIZE, TILE_SIZE / kBlockSizeQ);

  using scalar_t = float;

  if ((K % 4) == 0) {
    TORCH_CHECK(
        K / 4 <= BUFFER_SIZE,
        "For now only a certain number of K values are supported. Let us know if you hit this and we will fix it");
    attention_kernel<
        scalar_t,
        float4,
        kBlockSizeK,
        kBlockSizeQ,
        WARP_SIZE,
        BUFFER_SIZE><<<grid, block, 0, stream>>>(
        res.packed_accessor<scalar_t, 3>(),
        query.packed_accessor<scalar_t, 3>(),
        key.packed_accessor<scalar_t, 3>(),
        value.packed_accessor<scalar_t, 3>());
  } else if ((K % 2) == 0) {
    TORCH_CHECK(
        K / 2 <= BUFFER_SIZE,
        "For now only a certain number of K values are supported. Let us know if you hit this and we will fix it");
    attention_kernel<
        scalar_t,
        float2,
        kBlockSizeK,
        kBlockSizeQ,
        WARP_SIZE,
        BUFFER_SIZE><<<grid, block, 0, stream>>>(
        res.packed_accessor<scalar_t, 3>(),
        query.packed_accessor<scalar_t, 3>(),
        key.packed_accessor<scalar_t, 3>(),
        value.packed_accessor<scalar_t, 3>());

  } else {
    TORCH_CHECK(
        K <= BUFFER_SIZE,
        "For now only a certain number of K values are supported. Let us know if you hit this and we will fix it");
    attention_kernel<
        scalar_t,
        float,
        kBlockSizeK,
        kBlockSizeQ,
        WARP_SIZE,
        BUFFER_SIZE><<<grid, block, 0, stream>>>(
        res.packed_accessor<scalar_t, 3>(),
        query.packed_accessor<scalar_t, 3>(),
        key.packed_accessor<scalar_t, 3>(),
        value.packed_accessor<scalar_t, 3>());
  }
  AT_CUDA_CHECK(cudaGetLastError());

  return res;
}

template <typename scalar_t, typename vec_t, int kBlockSizeQ, int kBlockSizeK>
__global__ void attention_backward_grad_v_kernel(
    at::PackedTensorAccessor<scalar_t, 3> grad_v,
    at::PackedTensorAccessor<scalar_t, 3> grad_out,
    at::PackedTensorAccessor<scalar_t, 3> query,
    at::PackedTensorAccessor<scalar_t, 3> key,
    at::PackedTensorAccessor<scalar_t, 3> value,
    at::PackedTensorAccessor<scalar_t, 2> tmp_sum_i,
    at::PackedTensorAccessor<scalar_t, 2> logsumexp_normalizer) {
  int64_t K = query.size(2);
  int64_t B = query.size(0);
  int64_t M = query.size(1);
  int64_t N = key.size(1);

  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);

  int64_t batch_idx = blockIdx.y;
  int64_t query_idx =
      blockIdx.x * blockDim.y * kBlockSizeQ + threadIdx.y * kBlockSizeQ;

  if (query_idx >= M)
    return;

  vec_t* query_block[kBlockSizeQ];
  vec_t* grad_out_block[kBlockSizeQ];
  scalar_t normalizer[kBlockSizeQ];

  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    int64_t index = query_idx + q_item_idx;
    index = index >= M ? M - 1 : index;
    query_block[q_item_idx] =
        reinterpret_cast<vec_t*>(query[batch_idx][index].data());
    grad_out_block[q_item_idx] =
        reinterpret_cast<vec_t*>(grad_out[batch_idx][index].data());
    normalizer[q_item_idx] = logsumexp_normalizer[batch_idx][index];
  }

  scalar_t tmp_sum[kBlockSizeQ] = {0};
  for (int64_t l = threadIdx.x * kBlockSizeK; l < N;
       l += blockDim.x * kBlockSizeK) {
    auto key_j = reinterpret_cast<vec_t*>(key[batch_idx][l].data());

    scalar_t attn_v[kBlockSizeQ][kBlockSizeK] = {0};
    compute_dot<scalar_t, vec_t, kBlockSizeK, kBlockSizeQ>(
        query_block, key_j, attn_v, K);

#pragma unroll
    for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
#pragma unroll
      for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
        attn_v[q_item_idx][k_item_idx] =
            std::exp(attn_v[q_item_idx][k_item_idx] - normalizer[q_item_idx]);
      }
    }

    // first compute the gradient for the self-attention
    // after softmax
    scalar_t grad_attn_v[kBlockSizeQ][kBlockSizeK] = {0};
    auto value_j = reinterpret_cast<vec_t*>(value[batch_idx][l].data());

    for (int64_t k = 0; k < K / kVecSize; k++) {
      vec_t temp_i[kBlockSizeQ];
#pragma unroll
      for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
        temp_i[q_item_idx] = __ldg(grad_out_block[q_item_idx] + k);
      }

#pragma unroll
      for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
        vec_t v = value_j[k + K / kVecSize * k_item_idx];
        vec_t tt = {0};
#pragma unroll
        for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
          sputnik::VectorCompute<vec_t>::Dot(
              temp_i[q_item_idx], v, &grad_attn_v[q_item_idx][k_item_idx]);
          sputnik::VectorCompute<vec_t>::FMA(
              attn_v[q_item_idx][k_item_idx], temp_i[q_item_idx], &tt);
        }
        myGpuAtomicAdd(&grad_v[batch_idx][l + k_item_idx][k * kVecSize], tt);
      }
    }

    // those are temporaries for the gradient of the softmax
#pragma unroll
    for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
#pragma unroll
      for (int64_t k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
        tmp_sum[q_item_idx] += attn_v[q_item_idx][k_item_idx] *
            grad_attn_v[q_item_idx][k_item_idx];
      }
    }
  }
#pragma unroll
  for (int64_t q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
    tmp_sum[q_item_idx] = warpSum<scalar_t, 32>(tmp_sum[q_item_idx]);
    tmp_sum_i[batch_idx][query_idx + q_item_idx] = tmp_sum[q_item_idx];
  }
}

template <
    typename scalar_t,
    typename vec_t,
    int kBlockSizeQ,
    int kBlockSizeK,
    int TILE_SIZEQ,
    int TILE_SIZEK>
__global__ void attention_backward_grad_qk_kernel(
    at::PackedTensorAccessor<scalar_t, 3> grad_q,
    at::PackedTensorAccessor<scalar_t, 3> grad_k,
    at::PackedTensorAccessor<scalar_t, 3> grad_out,
    at::PackedTensorAccessor<scalar_t, 3> query,
    at::PackedTensorAccessor<scalar_t, 3> key,
    at::PackedTensorAccessor<scalar_t, 3> value,
    at::PackedTensorAccessor<scalar_t, 2> tmp_sum_i,
    at::PackedTensorAccessor<scalar_t, 2> logsumexp_normalizer) {
  int64_t K = query.size(2);
  int64_t B = query.size(0);
  int64_t M = query.size(1);
  int64_t N = key.size(1);

  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);

  int64_t batch_idx = blockIdx.z;
  int64_t query_idx =
      blockIdx.x * blockDim.x * kBlockSizeQ + threadIdx.x * kBlockSizeQ;
  int64_t l = blockIdx.y * blockDim.y * kBlockSizeK + threadIdx.y * kBlockSizeK;

  if (query_idx >= M)
    return;

  if (l >= N)
    return;

  scalar_t normalizer[kBlockSizeQ];
  scalar_t tmp_sum[kBlockSizeQ];

  __shared__ scalar_t fact[TILE_SIZEQ][TILE_SIZEK + 1];

  auto qb = reinterpret_cast<vec_t*>(query[batch_idx][query_idx].data());
  auto kb = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
  auto vb = reinterpret_cast<vec_t*>(value[batch_idx][l].data());
  auto gb = reinterpret_cast<vec_t*>(grad_out[batch_idx][query_idx].data());

  for (int i = 0; i < kBlockSizeQ; i++) {
    normalizer[i] = logsumexp_normalizer[batch_idx][query_idx + i];
    tmp_sum[i] = tmp_sum_i[batch_idx][query_idx + i];
  }

  auto key_j = reinterpret_cast<vec_t*>(key[batch_idx][l].data());
  scalar_t attn_v[kBlockSizeQ][kBlockSizeK] = {0};
  scalar_t grad_attn_v[kBlockSizeQ][kBlockSizeK] = {0};

  for (int64_t k = 0; k < K / kVecSize; k += 1) {
#pragma unroll
    for (int k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
      vec_t kk = __ldg(kb + k + K / kVecSize * k_item_idx);
      vec_t tt = __ldg(vb + k + K / kVecSize * k_item_idx);
#pragma unroll
      for (int q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
        sputnik::VectorCompute<vec_t>::Dot(
            __ldg(qb + k + K / kVecSize * q_item_idx),
            kk,
            &attn_v[q_item_idx][k_item_idx]);
        sputnik::VectorCompute<vec_t>::Dot(
            __ldg(gb + k + K / kVecSize * q_item_idx),
            tt,
            &grad_attn_v[q_item_idx][k_item_idx]);
      }
    }
  }
#pragma unroll
  for (int k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
#pragma unroll
    for (int q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      attn_v[q_item_idx][k_item_idx] =
          std::exp(attn_v[q_item_idx][k_item_idx] - normalizer[q_item_idx]);
    }
  }

#pragma unroll
  for (int k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
#pragma unroll
    for (int q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      fact[kBlockSizeQ * threadIdx.x + q_item_idx]
          [kBlockSizeK * threadIdx.y + k_item_idx] =
              attn_v[q_item_idx][k_item_idx] *
              grad_attn_v[q_item_idx][k_item_idx] -
          attn_v[q_item_idx][k_item_idx] * tmp_sum[q_item_idx];
    }
  }
  __syncthreads();

  for (int64_t k = threadIdx.y; k < K / kVecSize; k += blockDim.y) {
    vec_t res[kBlockSizeQ] = {0};
#pragma unroll
    for (int64_t i = 0; i < TILE_SIZEK; i++) {
      vec_t kk = __ldg(kb + k + K / kVecSize * (i - kBlockSizeK * threadIdx.y));
#pragma unroll
      for (int q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
        sputnik::VectorCompute<vec_t>::FMA(
            fact[kBlockSizeQ * threadIdx.x + q_item_idx][i],
            kk,
            &res[q_item_idx]);
      }
    }
#pragma unroll
    for (int q_item_idx = 0; q_item_idx < kBlockSizeQ; q_item_idx++) {
      myGpuAtomicAdd(
          &grad_q[batch_idx][query_idx + q_item_idx][k * kVecSize],
          res[q_item_idx]);
    }
  }

  for (int64_t k = threadIdx.x; k < K / kVecSize; k += blockDim.x) {
    vec_t res[kBlockSizeK] = {0};
#pragma unroll
    for (int64_t i = 0; i < TILE_SIZEQ; i++) {
      vec_t kk = __ldg(qb + k + K / kVecSize * (i - kBlockSizeQ * threadIdx.x));
#pragma unroll
      for (int k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
        sputnik::VectorCompute<vec_t>::FMA(
            fact[i][kBlockSizeK * threadIdx.y + k_item_idx],
            kk,
            &res[k_item_idx]);
      }
    }
#pragma unroll
    for (int k_item_idx = 0; k_item_idx < kBlockSizeK; k_item_idx++) {
      myGpuAtomicAdd(
          &grad_k[batch_idx][l + k_item_idx][k * kVecSize], res[k_item_idx]);
    }
  }
}

std::tuple<at::Tensor, at::Tensor, at::Tensor> attention_backward(
    const at::Tensor& grad_out,
    const at::Tensor& query,
    const at::Tensor& key,
    const at::Tensor& value,
    const at::Tensor& logsumexp
    // const at::Tensor& mask
) {
  TORCH_CHECK(query.dim() == grad_out.dim());
  TORCH_CHECK(query.dim() == key.dim());
  TORCH_CHECK(query.dim() == value.dim());
  // TORCH_CHECK(query.dim() == mask.dim());
  TORCH_CHECK(query.dim() == 3);

  TORCH_CHECK(query.size(0) == grad_out.size(0));
  TORCH_CHECK(query.size(1) == grad_out.size(1));
  TORCH_CHECK(query.size(2) == grad_out.size(2));

  TORCH_CHECK(query.size(2) == key.size(2));
  TORCH_CHECK(query.size(0) == key.size(0));

  TORCH_CHECK(query.size(0) == value.size(0));
  TORCH_CHECK(key.size(1) == value.size(1));
  TORCH_CHECK(
      query.size(2) ==
      value.size(2)); // TODO: drop this limitation in the future

  TORCH_CHECK(query.is_cuda(), "query must be a CUDA tensor");
  TORCH_CHECK(key.is_cuda(), "key must be a CUDA tensor");
  TORCH_CHECK(value.is_cuda(), "value must be a CUDA tensor");
  TORCH_CHECK(grad_out.is_cuda(), "grad_out must be a CUDA tensor");

  TORCH_CHECK(!query.is_sparse(), "query must be a dense tensor");
  TORCH_CHECK(!key.is_sparse(), "key must be a dense tensor");
  TORCH_CHECK(!value.is_sparse(), "value must be a dense tensor");
  TORCH_CHECK(!grad_out.is_sparse(), "grad_out must be a dense tensor");

  at::cuda::CUDAGuard device_guard(query.device());

  int64_t B = query.size(0);
  int64_t M = query.size(1);
  int64_t N = key.size(1);
  int64_t K = query.size(2);

  at::Tensor grad_q = at::zeros_like(query);
  at::Tensor grad_k = at::zeros_like(key);
  at::Tensor grad_v = at::zeros_like(value);

  at::Tensor tmp_sum_i = at::empty({B, M}, query.options());

  using scalar_t = float;
  using vec_t = float4;
  // using vec_t = float;

  constexpr int TILE_SIZE = 16 * 8;
  constexpr int kVecSize = sizeof(vec_t) / sizeof(scalar_t);

  constexpr int64_t kBlockSizeQ = 16;
  constexpr int64_t kBlockSizeK = 4;

  dim3 grid(ceil_div(M, int64_t(TILE_SIZE)), B);
  dim3 block(32, TILE_SIZE / kBlockSizeQ);

  cudaStream_t stream = at::cuda::getCurrentCUDAStream();

  attention_backward_grad_v_kernel<scalar_t, vec_t, kBlockSizeQ, kBlockSizeK>
      <<<grid, block, 0, stream>>>(
          grad_v.packed_accessor<scalar_t, 3>(),
          grad_out.packed_accessor<scalar_t, 3>(),
          query.packed_accessor<scalar_t, 3>(),
          key.packed_accessor<scalar_t, 3>(),
          value.packed_accessor<scalar_t, 3>(),
          tmp_sum_i.packed_accessor<scalar_t, 2>(),
          logsumexp.packed_accessor<scalar_t, 2>());

  constexpr int TILE_SIZEQ2 = 32;
  constexpr int TILE_SIZEK2 = 32;

  constexpr int64_t kBlockSizeQ2 = 4;
  constexpr int64_t kBlockSizeK2 = 4;

  dim3 grid2(
      ceil_div(M, int64_t(TILE_SIZEQ2)), ceil_div(N, int64_t(TILE_SIZEK2)), B);
  dim3 block2(TILE_SIZEQ2 / kBlockSizeQ2, TILE_SIZEK2 / kBlockSizeK2);
  // TODO: try adding a blockDim.x to iterate over k

  attention_backward_grad_qk_kernel<
      scalar_t,
      vec_t,
      kBlockSizeQ2,
      kBlockSizeK2,
      TILE_SIZEQ2,
      TILE_SIZEK2><<<grid2, block2, 0, stream>>>(
      grad_q.packed_accessor<scalar_t, 3>(),
      grad_k.packed_accessor<scalar_t, 3>(),
      grad_out.packed_accessor<scalar_t, 3>(),
      query.packed_accessor<scalar_t, 3>(),
      key.packed_accessor<scalar_t, 3>(),
      value.packed_accessor<scalar_t, 3>(),
      tmp_sum_i.packed_accessor<scalar_t, 2>(),
      logsumexp.packed_accessor<scalar_t, 2>());

  AT_CUDA_CHECK(cudaGetLastError());

  return std::make_tuple(grad_q, grad_k, grad_v);
}

} // namespace

TORCH_LIBRARY_IMPL(xformers, CUDA, m) {
  m.impl(
      TORCH_SELECTIVE_NAME("xformers::efficient_attention"),
      TORCH_FN(attention));
  m.impl(
      TORCH_SELECTIVE_NAME("xformers::efficient_attention_backward"),
      TORCH_FN(attention_backward));
}
