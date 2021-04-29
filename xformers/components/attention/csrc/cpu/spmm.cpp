#include <ATen/ATen.h>
#include <torch/types.h>

namespace {
// taken from
// https://github.com/google-research/google-research/blob/master/sgk/sparse/ops/cc/spmm_launcher.cc
// with slight modifications to add batch support
// Simple CPU kernel launcher.
void LaunchSpmm(
    int m,
    int k,
    int n,
    int nonzeros,
    const int* row_indices,
    const float* values,
    const int* row_offsets,
    const int* column_indices,
    const float* dense_matrix,
    float* output_matrix,
    int batch_size) {
  for (int b = 0; b < batch_size; b++) {
    for (int i = 0; i < m; ++i) {
      for (int j = 0; j < n; ++j) {
        float accumulator = 0.0f;
        for (int l = row_offsets[i]; l < row_offsets[i + 1]; ++l) {
          int column_index = column_indices[l];
          accumulator += values[b * nonzeros + l] *
              dense_matrix[b * k * n + column_index * n + j];
        }
        output_matrix[b * m * n + i * n + j] = accumulator;
      }
    }
  }
}

at::Tensor spmm_sputnik(
    const at::Tensor& b,
    const at::Tensor& row_indices,
    const at::Tensor& values,
    const at::Tensor& row_offsets,
    const at::Tensor& column_indices,
    int64_t m) {
  int batch = b.size(0);
  int k = b.size(1);
  int n = b.size(2);

  int nonzeros = column_indices.size(0);
  TORCH_CHECK(
      batch == 1 || nonzeros % 4 == 0,
      "If batch size > 1 then number of nonzeros should be a multiple of 4");

  at::Tensor output = at::empty({batch, m, n}, b.options());

  LaunchSpmm(
      m,
      k,
      n,
      nonzeros,
      row_indices.data_ptr<int>(),
      values.data_ptr<float>(),
      row_offsets.data_ptr<int>(),
      column_indices.data_ptr<int>(),
      b.data_ptr<float>(),
      output.data_ptr<float>(),
      batch);

  return output;
}

} // namespace

TORCH_LIBRARY_IMPL(xformers, CPU, m) {
  m.impl(
      TORCH_SELECTIVE_NAME("xformers::spmm_sputnik"), TORCH_FN(spmm_sputnik));
}
