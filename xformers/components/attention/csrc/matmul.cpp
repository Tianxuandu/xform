#include <torch/types.h>
#include <limits>
#include "matmul.h"

namespace {

at::Tensor matmul_with_mask_kernel(const at::Tensor& a, const at::Tensor& b, const at::Tensor& mask) {
    auto result = at::matmul(a, b);
    result = result.masked_fill(mask.logical_not(), -std::numeric_limits<float>::infinity());
    return result;
}

}  // namespace

at::Tensor matmul_with_mask(const at::Tensor& a, const at::Tensor& b, const at::Tensor& mask) {
    static auto op = c10::Dispatcher::singleton()
                         .findSchemaOrThrow("xformers::matmul_with_mask", "")
                         .typed<decltype(matmul_with_mask)>();
    auto result = op.call(a, b, mask);
    return result;
}

TORCH_LIBRARY_FRAGMENT(xformers, m) {
  m.def(TORCH_SELECTIVE_SCHEMA(
      "xformers::matmul_with_mask(Tensor a, Tensor b, Tensor mask) -> Tensor"));
}

TORCH_LIBRARY_IMPL(xformers, CPU, m) {
  m.impl(TORCH_SELECTIVE_NAME("xformers::matmul_with_mask"), TORCH_FN(matmul_with_mask_kernel));
}

TORCH_LIBRARY_IMPL(xformers, CUDA, m) {
  m.impl(TORCH_SELECTIVE_NAME("xformers::matmul_with_mask"), TORCH_FN(matmul_with_mask_kernel));
}
