from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import torch
import torch.nn as nn

from xformers.components import build_multi_head_attention
from xformers.components.feedforward import (
    FEEDFORWARD_REGISTRY,
    FeedforwardConfig,
    build_feedforward,
)
from xformers.components.positional_embedding import (
    POSITION_EMBEDDING_REGISTRY,
    PositionEmbeddingConfig,
    build_positional_embedding,
)
from xformers.utils import generate_matching_config


class BlockType(str, Enum):
    Encoder = "encoder"
    Decoder = "decoder"


class LayerNormStyle(str, Enum):
    """Support different layer norm styles.
    See "On Layer Normalization in the Transformer Architecture",
    Xiong et al., https://arxiv.org/pdf/2002.04745v1.pdf
    """

    Pre = "pre"
    Post = "post"


# Credits: the following is inspired by FastAI's Transformer implementation
class Residual(nn.Module):
    """Object-oriented handling of the residual path"""

    def __init__(self, layer: nn.Module):
        super().__init__()
        self.layer = layer

    def forward(self, inputs: Union[torch.Tensor, List[torch.Tensor]], *args, **kwargs):
        if not isinstance(inputs, list):
            inputs = [inputs]

        return inputs[0] + self.layer(*inputs, *args, **kwargs)


class PreNorm(nn.Module):
    """Adds LayerNorm before computing attention

    ..Note: If a list of inputs is passed, all of them get normalized"""

    def __init__(self, d_model: int, sublayer: nn.Module):
        super().__init__()
        self.norm = nn.LayerNorm(d_model)
        self.sublayer = sublayer

    def forward(self, inputs: Union[torch.Tensor, List[torch.Tensor]], *args, **kwargs):
        if not isinstance(inputs, list):
            inputs = [inputs]

        x_norm = [self.norm(x_) for x_ in inputs]
        return self.sublayer(*x_norm, *args, **kwargs)


class PostNorm(nn.Module):
    """Adds LayerNorm after computing attention"""

    def __init__(self, d_model: int, sublayer: nn.Module):
        super().__init__()
        self.norm = nn.LayerNorm(d_model)
        self.sublayer = sublayer

    def forward(self, inputs: Union[torch.Tensor, List[torch.Tensor]], *args, **kwargs):
        if not isinstance(inputs, list):
            inputs = [inputs]

        x = self.sublayer(*inputs, *args, **kwargs)
        return self.norm(x)


def _get_ln_factory(d_model: int, layer_norm_style: Optional[LayerNormStyle]):
    def get_layer_norm(
        d_model: int, sublayer: nn.Module, layer_norm_style: Optional[LayerNormStyle]
    ):
        return (
            PreNorm(d_model, sublayer)
            if layer_norm_style == LayerNormStyle.Pre
            else PostNorm(d_model, sublayer)
        )

    def ln_factory(sublayer: nn.Module):
        return get_layer_norm(d_model, sublayer, layer_norm_style)

    return ln_factory


@dataclass(init=False)  # handle constructors explicitly to force type changes
class xFormerBlockConfig:
    dim_model: int
    num_layers: int
    feedforward_config: FeedforwardConfig
    position_encoding_config: Optional[PositionEmbeddingConfig]
    block_type: BlockType
    layer_norm_style: LayerNormStyle

    def __init__(
        self,
        dim_model: int,
        feedforward_config: Dict[str, Any],
        position_encoding_config: Optional[Dict[str, Any]],
        block_type: BlockType,
        num_layers: int = 1,
        layer_norm_style: LayerNormStyle = LayerNormStyle("post"),
    ):
        self.dim_model = dim_model
        self.num_layers = num_layers
        self.block_type = block_type
        self.layer_norm_style = layer_norm_style

        # Fill in possible gaps in the config for subparts of the block
        self.feedforward_config = generate_matching_config(
            feedforward_config,
            FEEDFORWARD_REGISTRY[feedforward_config["name"]].config,
        )

        self.position_encoding_config = (
            generate_matching_config(
                position_encoding_config,
                POSITION_EMBEDDING_REGISTRY[position_encoding_config["name"]].config,
            )
            if position_encoding_config is not None
            else None
        )


@dataclass(init=False)
class xFormerEncoderConfig(xFormerBlockConfig):
    multi_head_config: Dict[str, Any]

    def __init__(
        self,
        dim_model: int,
        feedforward_config: Dict[str, Any],
        position_encoding_config: Optional[Dict[str, Any]],
        multi_head_config: Dict[str, Any],
        num_layers: int = 1,
        layer_norm_style: str = "post",
        *args,
        **kwargs,
    ):
        super().__init__(
            dim_model=dim_model,
            feedforward_config=feedforward_config,
            position_encoding_config=position_encoding_config,
            layer_norm_style=LayerNormStyle(layer_norm_style),
            num_layers=num_layers,
            block_type=BlockType("encoder"),
        )

        self.multi_head_config = multi_head_config


@dataclass(init=False)
class xFormerDecoderConfig(xFormerBlockConfig):
    multi_head_config_masked: Dict[str, Any]  # prior to encoder output
    multi_head_config_cross: Dict[str, Any]  # cross attention, takes encoder output

    def __init__(
        self,
        dim_model: int,
        feedforward_config: Dict[str, Any],
        position_encoding_config: Optional[Dict[str, Any]],
        multi_head_config_masked: Dict[str, Any],
        multi_head_config_cross: Dict[str, Any],
        num_layers: int = 1,
        layer_norm_style: str = "post",
        *args,
        **kwargs,
    ):
        super().__init__(
            dim_model=dim_model,
            feedforward_config=feedforward_config,
            position_encoding_config=position_encoding_config,
            layer_norm_style=LayerNormStyle(layer_norm_style),
            num_layers=num_layers,
            block_type=BlockType("encoder"),
        )

        self.multi_head_config_masked = multi_head_config_masked
        self.multi_head_config_cross = multi_head_config_cross


class xFormerEncoderBlock(nn.Module):
    r""" A vanilla Transformer Encoder block """

    def __init__(self, config: xFormerEncoderConfig):
        super().__init__()
        self.pose_encoding = (
            build_positional_embedding(asdict(config.position_encoding_config))
            if config.position_encoding_config
            else None
        )

        # mini helper, builds a LayerNorm with the right Pre/Post config and the right dimensions
        ln_factory = _get_ln_factory(config.dim_model, config.layer_norm_style)

        self.mha = build_multi_head_attention(config.multi_head_config)
        self.feedforward = build_feedforward(asdict(config.feedforward_config))

        if config.layer_norm_style == LayerNormStyle.Pre:
            # Attention is computed on normalized inputs, the residual path stays un-normalized
            self.layer_norm_att = ln_factory(self.mha)
            self.layer_norm_feedforward = ln_factory(self.feedforward)

            self.wrap_att = Residual(self.layer_norm_att)
            self.wrap_ff = PostNorm(
                config.dim_model, Residual(self.layer_norm_feedforward)
            )
        else:
            # Attention and residual path are applied on the raw sigal, the normalization happens last
            self.residual_att = Residual(self.mha)
            self.residual_ff = Residual(self.feedforward)

            self.wrap_att = ln_factory(self.residual_att)
            self.wrap_ff = ln_factory(self.residual_ff)

    @classmethod
    def from_config(cls, config: xFormerEncoderConfig):
        return cls(config)

    def forward(
        self,
        x: torch.Tensor,
        att_mask: Optional[torch.Tensor] = None,
        input_mask: Optional[torch.Tensor] = None,
    ):
        if self.pose_encoding:
            x = self.pose_encoding(x)

        # Handle the optional input masking, differs on Q, K, V
        if input_mask is not None:
            q = x
            k = x * input_mask.unsqueeze(-1)
            v = k
        else:
            q, k, v = x, x, x

        # Pre/Post norms and residual paths are already handled
        x = self.wrap_att(q, k, v, att_mask=att_mask)
        x = self.wrap_ff(x)

        return x


class xFormerDecoderBlock(nn.Module):
    r""" A vanilla Transformer Decoder block """

    def __init__(self, config: xFormerDecoderConfig):
        super().__init__()
        self.linear1 = nn.Linear(config.dim_model, config.feedforward_config.dim_model)
        self.linear2 = nn.Linear(config.dim_model, config.feedforward_config.dim_model)

        self.pose_encoding = (
            build_positional_embedding(config.position_encoding_config)
            if config.position_encoding_config
            else None
        )

        # mini helper, builds a LayerNorm with the right Pre/Post config and the right dimensions
        ln_factory = _get_ln_factory(config.dim_model, config.layer_norm_style)

        self.mha = build_multi_head_attention(config.multi_head_config_masked)
        self.cross_mha = build_multi_head_attention(config.multi_head_config_cross)
        self.feedforward = build_feedforward(config.feedforward_config)

        if config.layer_norm_style == LayerNormStyle.Pre:
            # Attention is computed on normalized inputs, the residual path stays un-normalized
            self.layer_norm_att = ln_factory(self.mha)
            self.layer_norm_cross = ln_factory(self.cross_mha)
            self.layer_norm_feedforward = ln_factory(self.feedforward)

            self.wrap_att = Residual(self.layer_norm_att)
            self.wrap_cross = Residual(self.layer_norm_cross)
            self.wrap_ff = PostNorm(
                config.dim_model, Residual(self.layer_norm_feedforward)
            )
        else:
            # Attention and residual path are applied on the raw sigal, the normalization happens last
            self.residual_att = Residual(self.mha)
            self.residual_cross = Residual(self.cross_mha)
            self.residual_ff = Residual(self.feedforward)

            self.wrap_att = ln_factory(self.residual_att)
            self.wrap_cross = Residual(self.cross_mha)
            self.wrap_ff = ln_factory(self.residual_ff)

    @classmethod
    def from_config(cls, config: xFormerDecoderConfig):
        return cls(config)

    def forward(
        self,
        target: torch.Tensor,
        memory: torch.Tensor,
        encoder_att_mask: Optional[torch.Tensor] = None,
        decoder_att_mask: Optional[torch.Tensor] = None,
        input_mask: Optional[torch.Tensor] = None,
    ):
        if self.pose_encoding:
            target = self.pose_encoding(target)

        # Handle the optional input masking, differs on Q, K, V
        if input_mask is not None:
            target_q = target
            target_k = target * input_mask.unsqueeze(-1)
            target_v = target_k
        else:
            target_q, target_k, target_v = target, target, target

        x = self.wrap_att([target_q, target_k, target_v], att_mask=decoder_att_mask)
        x = self.wrap_cross([memory, memory, x], att_mask=encoder_att_mask)
        x = self.wrap_ff(x)

        return x
