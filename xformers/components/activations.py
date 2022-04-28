# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from enum import Enum
from typing import Optional

import torch
from torch import nn


class Activation(str, Enum):
    SquaredReLU = "squared_relu"
    GeLU = "gelu"
    LeakyReLU = "leaky_relu"
    ReLU = "relu"
    SmeLU = "smelu"


# For unit testing / parity comparisons, probably not the fastest way
class SquaredReLU(nn.Module):
    def __init__(self) -> None:
        super().__init__()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_ = torch.nn.functional.relu(x)
        return x_ * x_


class SmeLU(nn.Module):
    def __init__(self, beta: float = 2.0) -> None:
        super().__init__()
        self.register_buffer("beta", torch.tensor(beta))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        relu = torch.where(
            x >= self.beta,
            x,
            torch.tensor([0.0], device=x.device, dtype=x.dtype),
        )
        return torch.where(
            torch.abs(x) <= self.beta,
            ((x + self.beta) ** 2) / (4.0 * self.beta),
            relu,
            device=x.device,
            dtype=x.dtype,
        )


class Passthrough(nn.Module):
    def __init__(self) -> None:
        super().__init__()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x


def build_activation(activation: Optional[Activation]):
    if not activation:
        return Passthrough()

    return {
        Activation.ReLU: nn.ReLU,
        Activation.GeLU: nn.GELU,
        Activation.LeakyReLU: nn.LeakyReLU,
        Activation.SquaredReLU: SquaredReLU,
        Activation.SmeLU: SmeLU,
    }[activation]()
