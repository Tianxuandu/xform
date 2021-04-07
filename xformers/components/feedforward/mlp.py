from dataclasses import dataclass

import torch
import torch.nn as nn

from xformers.components.feedforward import Activations, Feedforward, FeedforwardConfig

from . import register_feedforward


@dataclass(init=False)
class MlpConfig(FeedforwardConfig):
    hidden_layer_multiplier: int


@register_feedforward("MLP")
class MLP(Feedforward):
    def __init__(
        self,
        dim_latent: int,
        dropout: float,
        activation: Activations,
        hidden_layer_multiplier: int,
        *args,
        **kwargs
    ):
        super().__init__()

        activation_layer: nn.Module = {
            Activations.ReLU: nn.ReLU,
            Activations.GeLU: nn.GELU,
        }[activation]()

        self.mlp = nn.Sequential(
            nn.Linear(dim_latent, hidden_layer_multiplier * dim_latent),
            activation_layer,
            nn.Linear(hidden_layer_multiplier * dim_latent, dim_latent),
            nn.Dropout(dropout),
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.mlp(inputs)
