from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn as nn

from xformers.components.attention import (
    _DENSITY_THRESHOLD,
    Attention,
    AttentionConfig,
    register_attention,
)
from xformers.components.attention.core import scaled_dot_product_attention


@dataclass(init=False)
class LocalAttentionConfig(AttentionConfig):
    causal: bool
    window_size: int


@register_attention("local")
class LocalAttention(Attention):
    def __init__(
        self,
        dropout: float = 0.0,
        causal: bool = False,
        window_size: int = 5,
        *args,
        **kwargs,
    ):

        r"""
        An implementation of a sliding window attention, as proposed in RoutingTransformer_, LongFormer_ or BigBird_


        Args:
            dropout (float): the probability of an output to be randomly dropped at training time
            causal (bool): apply a causal mask, in that the attention cannot be applied to the future
            window_size (int): the overall window size for local attention.
                Odd number is expected if the mask is not causal, as the window size will be evenly
                distributed on both sides of each query


        _RoutingTransformer: "Efficient Content-Based Sparse Attention with Routing Transformers", A. Roy et al.
        https://arxiv.org/pdf/2003.05997.pdf

        _BigBird: "Big Bird: Transformers for Longer Sequences" M. Zaheer et al
        https://arxiv.org/pdf/2007.14062.pdf

        _Longformer: "Longformer: The Long-Document Transformer.", I. Beltagy et al
        https://arxiv.org/pdf/2004.05150.pdf
        """
        super().__init__()

        self.attn_drop = nn.Dropout(dropout, inplace=True)
        self.causal = causal

        if not self.causal:
            assert (
                window_size % 2 == 1
            ), "The window size is assumed to be odd (counts self-attention + 2 wings)"

        self.window_size = window_size
        self.mask: Optional[torch.Tensor] = None

    def _get_local_mask(self, shape: torch.Size) -> torch.Tensor:
        if self.causal:
            mask = torch.tril(torch.ones(shape[1], shape[1])).to(dtype=torch.bool)
            mask &= ~torch.tril(
                torch.ones(shape[1], shape[1]), diagonal=-self.window_size - 1
            ).to(dtype=torch.bool)
        else:
            h_win_size = self.window_size // 2
            mask = torch.tril(torch.ones(shape[1], shape[1]), diagonal=h_win_size).to(
                dtype=torch.bool
            )
            mask &= ~torch.tril(
                torch.ones(shape[1], shape[1]), diagonal=-(h_win_size + 1)
            ).to(dtype=torch.bool)

        # Take the batch dimension into account
        # FIXME: not needed with https://github.com/fairinternal/xformers/issues/42
        mask = mask.expand(shape[0], shape[1], shape[1])

        # Sparsify if that makes sense
        if torch.count_nonzero(mask).item() / mask.numel() < _DENSITY_THRESHOLD:
            mask = mask.to_sparse()

        return mask

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        att_mask: Optional[torch.Tensor] = None,
        *args,
        **kwargs,
    ):
        # Local window attention masking
        if self.mask is None or self.mask.shape[1] != q.shape[1]:
            self.mask = self._get_local_mask(q.shape).to(q.device)

        # Take into account the optional user mask
        mask = self.mask if att_mask is None else self.mask & att_mask

        return scaled_dot_product_attention(q, k, v, mask, dropout=self.attn_drop)

    @classmethod
    def from_config(cls, config: AttentionConfig) -> "Attention":
        return cls(**LocalAttentionConfig.as_patchy_dict(config))
