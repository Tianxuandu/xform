// This file was modified from sputnik to implement batch support for
// sddmm directly in the kernels
// Copyright 2020 The Sputnik Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <algorithm>
#include <cmath>

#include "sputnik/barrier.h"
#include "sputnik/common.h"
#include "sputnik/cuda_utils.h"
#include "sputnik/load_store.h"
#include "sputnik/sddmm/all_reduce.h"
#include "sputnik/sddmm/compute_utils.h"
#include "sputnik/sddmm/cuda_sddmm.h"
#include "sputnik/sddmm/dense_to_reg.h"
#include "sputnik/sddmm/dense_to_shared.h"
#include "sputnik/sddmm/output_tile.h"
#include "sputnik/tiling_utils.h"

#include <ATen/ATen.h>
#include <torch/types.h>

#include <ATen/cuda/CUDAContext.h>
#include <c10/cuda/CUDAGuard.h>

namespace sputnik {

namespace {

template <
    typename LoadType,
    int kBlockItemsY,
    int kBlockItemsK,
    int kBlockItemsX,
    int kBlockWidth,
    int kPredicateK = true>
__global__ void __launch_bounds__(kBlockItemsY* kBlockWidth) CudaSddmmKernel2(
    int m,
    int k,
    int n,
    const int* __restrict__ row_indices,
    const int* __restrict__ row_offsets,
    const int* __restrict__ column_indices,
    const float* __restrict__ lhs_matrix,
    const float* __restrict__ rhs_matrix,
    float* __restrict__ output_values,
    int nnz) {
  static_assert(
      (kBlockItemsY * kBlockWidth) % 32 == 0,
      "The thread-block size must be divisible by the warp size.");
  static_assert(
      (kBlockItemsY * kBlockWidth) > 0,
      "The thread-block size must be nonzero.");
  static_assert(
      kBlockItemsK >= kBlockWidth, "k-dimension tile must be >= block width.");
  static_assert(
      kBlockItemsK % kBlockWidth == 0,
      "k-dimension tile size must be divisible by block width.");
  static_assert(
      kBlockItemsX >= kBlockWidth,
      "n-dimension tile size must be >= block width.");
  static_assert(
      kBlockItemsX % kBlockWidth == 0,
      "n-dimension tile size must be divisible by block width.");
  typedef TilingUtils<kBlockItemsY, kBlockItemsK, kBlockItemsX> Tiling;
  // Calculate this thread block's indices into the M and N dimensions.
  int m_index = Tiling::IndexM(), n_index = Tiling::IndexN();

  // Threads that work on different m-dim indices are independent. If we're
  // out of bounds in the m-dimension we can just return.
  if (m_index >= m)
    return;
  m_index = Load(row_indices + m_index);

  // Load the row offset and calculate the number of non-zeros in the row.
  int row_offset = __ldg(row_offsets + m_index);
  int nonzeros = __ldg(row_offsets + m_index + 1) - row_offset;

  // If this thread block has no nonzeros in the row to process, exit early.
  if (n_index >= nonzeros)
    return;

  // Calculate the number of nonzeros that this thread block processes and
  // substract the x-dim thread index to simplify loop bounds checks.
  nonzeros = Min(nonzeros - n_index, kBlockItemsX) - threadIdx.x;

  // Shared memory tile for the lhs dense matrix values.
  float lhs_fragment[kBlockItemsK / kBlockWidth];

  // Shared memory tile for the output column indices.
  __shared__ int column_indices_tile_array[kBlockItemsX * kBlockItemsY];

  int* column_indices_tile =
      TilingUtils<kBlockItemsY, kBlockItemsK, kBlockItemsX>::MaybeOffset(
          column_indices_tile_array, kBlockItemsK * threadIdx.y);

  // Create a dense-to-shared loader for the lhs matrix.
  DenseToShared<LoadType, kBlockItemsK, kBlockWidth> lhs_tile_loader(
      k, m_index, lhs_matrix + blockIdx.z * m * k, lhs_fragment);

  // Register file fragment for the rhs dense matrix values.
  float rhs_fragment[kBlockItemsK * kBlockItemsX / kBlockWidth];

  // Create a dense-to-register loader for the rhs matrix.
  DenseToReg<LoadType, kBlockItemsK, kBlockItemsX, kBlockWidth> rhs_tile_loader(
      k,
      row_offset,
      n_index,
      column_indices,
      rhs_matrix + blockIdx.z * n * k,
      column_indices_tile,
      rhs_fragment);

  // Accumulator registers for the partial results. Initialize the
  // registers to zero s.t. we can always accumulate in-place.
  float accumulator_fragment[kBlockItemsX] = {};

  // Helper for managing syncronization between collaborating threads.
  Barrier<kBlockItemsY, kBlockWidth> barrier(threadIdx.y);

  // Helper for computing tile-level partial matmuls.
  ComputeUtils<kBlockItemsK, kBlockItemsX, kBlockWidth> computer(
      lhs_fragment, rhs_fragment, accumulator_fragment);

  // Registers for the final reduced outputs.
  float output_fragment[kBlockItemsX / kBlockWidth];

  // Helper to reduce the partial accumulators prior to writing.
  AllReduce<LoadType, kBlockItemsX, kBlockWidth> all_reduce(
      barrier.ThreadMask(), accumulator_fragment, output_fragment);

  // Helper for storing the results to the output.
  OutputTile<kBlockItemsX, kBlockWidth> output_tile_storer(
      row_offset, n_index, output_fragment, output_values + blockIdx.z * nnz);

  //
  /// Begin kernel main loop.
  //

  // Load the column indices for this n-dimension tile.
  rhs_tile_loader.LoadColumnIndices(nonzeros);
  barrier.Sync();

#pragma nounroll
  for (; k >= kBlockItemsK; k -= kBlockItemsK) {
    // Load a tile from the dense lhs matrix into smem and sync.
    lhs_tile_loader.Load();

    // Load a tile from the dense rhs matrix into registers.
    rhs_tile_loader.Load();

    // Multiply the tiles and accumulate the results.
    computer.TileMAC();
  }

  //
  /// Begin k-dimension residue computation.
  //

  if (kPredicateK) {
    // Update the residue size to simplify loop bounds checking. Note
    // that `k` is guaranteed to be a multiple of `kValuesPerLoad`.
    constexpr int kValuesPerLoad = sizeof(LoadType) / sizeof(float);
    k -= threadIdx.x * kValuesPerLoad;

    // Load a partial tile from the lhs matrix and sync.
    lhs_tile_loader.Residue(k);

    // Load a tile from the rhs matrix and compute immediately.
    rhs_tile_loader.ResidueAndCompute(k, lhs_fragment, accumulator_fragment);
  }

  //
  /// Cleanup the partial sums across the (sub)warp.
  //
  all_reduce.Reduce();

  //
  ///  Write the results to the output.
  //
  output_tile_storer.Store(nonzeros);
}

} // namespace
} // namespace sputnik

template <
    typename LoadType,
    int kBlockItemsY,
    int kBlockItemsK,
    int kBlockItemsX,
    int kBlockWidth,
    int kPredicateK>
cudaError_t CudaSddmmEx2(
    int m,
    int k,
    int n,
    int nonzeros,
    const int* __restrict__ row_indices,
    const int* __restrict__ row_offsets,
    const int* __restrict__ column_indices,
    const float* __restrict__ lhs_matrix,
    const float* __restrict__ rhs_matrix,
    float* __restrict__ output_values,
    cudaStream_t stream,
    int batch_size) {
  dim3 grid_dim(
      std::ceil(static_cast<float>(m) / kBlockItemsY),
      std::ceil(static_cast<float>(n) / kBlockItemsX),
      batch_size);
  dim3 block_dim(kBlockWidth, kBlockItemsY, 1);

  sputnik::CudaSddmmKernel2<
      LoadType,
      kBlockItemsY,
      kBlockItemsK,
      kBlockItemsX,
      kBlockWidth,
      kPredicateK><<<grid_dim, block_dim, 0, stream>>>(
      m,
      k,
      n,
      row_indices,
      row_offsets,
      column_indices,
      lhs_matrix,
      rhs_matrix,
      output_values,
      nonzeros);
  return cudaGetLastError();
}

cudaError_t CudaSddmm2(
    int m,
    int k,
    int n,
    int nonzeros,
    const int* __restrict__ row_indices,
    const int* __restrict__ row_offsets,
    const int* __restrict__ column_indices,
    const float* __restrict__ lhs_matrix,
    const float* __restrict__ rhs_matrix,
    float* __restrict__ output_values,
    cudaStream_t stream,
    int batch_size) {
  // If possible, launch a variant that does not include the k-dimension
  // residue handling code.
  if ((k % 4) == 0) {
    if ((k % 32) == 0) {
      return CudaSddmmEx2<float4, 4, 32, 32, 8, false>(
          m,
          k,
          n,
          nonzeros,
          row_indices,
          row_offsets,
          column_indices,
          lhs_matrix,
          rhs_matrix,
          output_values,
          stream,
          batch_size);
    } else {
      return CudaSddmmEx2<float4, 4, 32, 32, 8, true>(
          m,
          k,
          n,
          nonzeros,
          row_indices,
          row_offsets,
          column_indices,
          lhs_matrix,
          rhs_matrix,
          output_values,
          stream,
          batch_size);
    }
  } else if ((k % 2) == 0) {
    return CudaSddmmEx2<float2, 2, 32, 32, 16, true>(
        m,
        k,
        n,
        nonzeros,
        row_indices,
        row_offsets,
        column_indices,
        lhs_matrix,
        rhs_matrix,
        output_values,
        stream,
        batch_size);
  } else {
    // Scalar kernel.
    return CudaSddmmEx2<float, 1, 32, 32, 32, true>(
        m,
        k,
        n,
        nonzeros,
        row_indices,
        row_offsets,
        column_indices,
        lhs_matrix,
        rhs_matrix,
        output_values,
        stream,
        batch_size);
  }
}

at::Tensor sddmm_sputnik(
    const at::Tensor& a,
    const at::Tensor& b,
    const at::Tensor& row_indices,
    const at::Tensor& row_offsets,
    const at::Tensor& column_indices) {
  cudaStream_t stream = at::cuda::getCurrentCUDAStream();
  int batch = a.size(0);
  int m = a.size(1);
  int k = a.size(2);
  int n = b.size(1);

  int nonzeros = column_indices.size(0);

  at::Tensor output = at::empty({batch, nonzeros}, a.options());

  AT_CUDA_CHECK(CudaSddmm2(
      m,
      k,
      n,
      nonzeros,
      row_indices.data_ptr<int>(),
      row_offsets.data_ptr<int>(),
      column_indices.data_ptr<int>(),
      a.data_ptr<float>(),
      b.data_ptr<float>(),
      output.data_ptr<float>(),
      stream,
      batch));

  return output;
}

TORCH_LIBRARY_IMPL(xformers, CUDA, m) {
  m.impl(
      TORCH_SELECTIVE_NAME("xformers::sddmm_sputnik"), TORCH_FN(sddmm_sputnik));
}
