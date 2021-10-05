from dataclasses import dataclass
from typing import Optional
from functools import partial, reduce
from inspect import isfunction
from operator import mul

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from xformers.components.attention import ATTENTION_REGISTRY, Attention, AttentionConfig, register_attention
from transformers.models.reformer.modeling_reformer import LSHSelfAttention, ReformerConfig, ReverseSort

"""
using implementation from huggingface transformers. Make LSH attention compatible with global tokens
"""

@dataclass
class LSHGlobalSelfAttentionConfig(AttentionConfig):
    num_hash: int
    num_heads: int
    dim_model: int
    seq_len: int
    num_buckets: int
    chunk_length: int

@register_attention("lsh_global", LSHGlobalSelfAttentionConfig)
class LSHGlobalAttention(Attention):

    def __init__(
        self,
        num_heads: int,
        dim_model: int,
        num_hash: int = 4,
        seq_len: int = 4096,
        dropout: float = 0.0,
        num_buckets: int = None,
        chunk_length: int = None,
        *args,
        **kwargs,
    ):
        super().__init__()
        attn_config = ReformerConfig()
        attn_config.num_attention_heads = num_heads
        attn_config.attn_layers = ["lsh"]
        attn_config.is_decoder = False
        attn_config.num_hashes = num_hash
        attn_config.max_position_embeddings = seq_len
        attn_config.attention_head_size = dim_model // num_heads
        # attn_config.feed_forward_size = 1
        if chunk_length:
            attn_config.lsh_attn_chunk_length = chunk_length
        attn_config.num_buckets = num_buckets
        self.attn = ReformerAttention(attn_config)

    def forward(
        self, 
        q: torch.Tensor, 
        k: torch.Tensor, 
        v: torch.Tensor, 
        att_mask: Optional[torch.Tensor] = None, 
        key_padding_mask: Optional[Tensor] = None,
        *args, **kwargs
    ):
        return self.attn(q, k, v, att_mask, key_padding_mask)


class ReformerAttention(LSHSelfAttention):
    def __init__(self, config):
        super().__init__(config)
        # self.query_key = None
        # self.value = None
        del self.query_key
        del self.value
        self.num_heads = self.num_attention_heads

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        att_mask: Optional[torch.Tensor] = None,
        key_padding_mask: Optional[Tensor] = None,
        *args,
        **kwargs,
    ):
        orig_sequence_length, head_dim = q.shape[1], q.shape[2]

        # padding to factors of self.chunk_length

        # needs centain sequence length to make the block wise local attention work
        def _pad_to_window_size(x, window_size):
            seq_len = x.size(-2)
            pad_len = (window_size - seq_len % window_size) % window_size
            return F.pad(x, (0,0,0,pad_len), value=0), pad_len
        q, _ = _pad_to_window_size(q, self.chunk_length)
        v, _ = _pad_to_window_size(v, self.chunk_length)
        if key_padding_mask.shape[1] % self.chunk_length != 0:
            pad_len = (self.chunk_length - key_padding_mask.shape[1] % self.chunk_length) % self.chunk_length
            # key padding mask: 1 means padding tokens
            key_padding_mask = torch.cat([key_padding_mask, key_padding_mask.new_ones(key_padding_mask.size(0), pad_len).to(key_padding_mask)], dim=1) 
        sequence_length = q.shape[1]

        batch_size = q.shape[0] // self.num_attention_heads

        # num hashes can optionally be overwritten by user
        num_hashes = self.num_hashes

        # @xwhan reformer needs the key and query vectors to be the same
        # project hidden_states to query_key and value
        query_key_vectors = q.view(batch_size, self.num_attention_heads, sequence_length, head_dim)
        value_vectors = v.view(batch_size, self.num_attention_heads, sequence_length, head_dim)

        do_standard_self_attention = (sequence_length <= self.chunk_length)

        # xformer format, att_mask: B x seq_len x seq_len #TODO: better solution?
        if key_padding_mask is not None:
            attention_mask = key_padding_mask == 0
        elif att_mask is not None:
            assert len(att_mask.shape) == 3
            att_mask = att_mask.reshape(batch_size, self.num_heads, -1, sequence_length)[:,0,:,:]
            attention_mask = att_mask.sum(1) > 0
        else:
            attention_mask = None

        # LSH attention only makes sense if chunked attention should be performed
        if not do_standard_self_attention:
            # set `num_buckets` on the fly, recommended way to do it
            if self.num_buckets is None:
                self._set_num_buckets(sequence_length)

            # hash query key vectors into buckets
            buckets = self._hash_vectors(query_key_vectors, num_hashes, attention_mask)

            assert (
                int(buckets.shape[-1]) == num_hashes * sequence_length
            ), f"last dim of buckets is {buckets.shape[-1]}, but should be {num_hashes * sequence_length}"

            sorted_bucket_idx, undo_sorted_bucket_idx = self._get_sorted_bucket_idx_and_undo_sorted_bucket_idx(
                sequence_length, buckets, num_hashes
            )

            # make sure bucket idx is not longer then sequence length
            sorted_bucket_idx_per_hash = sorted_bucket_idx % sequence_length

            # cluster query key value vectors according to hashed buckets
            query_key_vectors = self._gather_by_expansion(query_key_vectors, sorted_bucket_idx_per_hash, num_hashes)
            value_vectors = self._gather_by_expansion(value_vectors, sorted_bucket_idx_per_hash, num_hashes)
            query_key_vectors = self._split_seq_length_dim_to(
                query_key_vectors,
                -1,
                self.chunk_length,
                self.num_attention_heads,
                self.attention_head_size,
            )
            value_vectors = self._split_seq_length_dim_to(
                value_vectors,
                -1,
                self.chunk_length,
                self.num_attention_heads,
                self.attention_head_size,
            )

            if self.chunk_length is None:
                assert (
                    self.num_chunks_before == 0 and self.num_chunks_after == 0
                ), "If `config.chunk_length` is `None`, make sure `config.num_chunks_after` and `config.num_chunks_before` are set to 0."

        else:
            # get sequence length indices
            sorted_bucket_idx_per_hash = torch.arange(sequence_length, device=query_key_vectors.device).repeat(
                batch_size, self.num_attention_heads, 1
            )

        # scale key vectors
        key_vectors = self._len_and_dim_norm(query_key_vectors)

        # set query_vectors to query key vectors if LSH self attention
        query_vectors = query_key_vectors

        # free memory
        del query_key_vectors

        # get attention probs
        out_vectors, logits, attention_probs = self.attend(
            query_vectors=query_vectors,
            key_vectors=key_vectors,
            value_vectors=value_vectors,
            sorted_bucket_idx_per_hash=sorted_bucket_idx_per_hash,
            attention_mask=attention_mask,
            head_mask=None,
            do_standard_self_attention=do_standard_self_attention,
            do_cached_attention=False
        )

        # free memory
        del key_vectors, value_vectors

        # re-order out_vectors and logits
        if not do_standard_self_attention:
            # sort clusters back to correct ordering
            out_vectors, logits = ReverseSort.apply(out_vectors, logits, sorted_bucket_idx, undo_sorted_bucket_idx)

        if not do_standard_self_attention:
            # sum up all hash rounds
            if num_hashes > 1:
                out_vectors = self._split_seq_length_dim_to(
                    out_vectors,
                    num_hashes,
                    sequence_length,
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                logits = self._split_seq_length_dim_to(
                    logits,
                    num_hashes,
                    sequence_length,
                    self.num_attention_heads,
                    self.attention_head_size,
                ).unsqueeze(-1)

                probs_vectors = torch.exp(logits - torch.logsumexp(logits, dim=2, keepdim=True))
                out_vectors = torch.sum(out_vectors * probs_vectors, dim=2)
                # free memory
                del probs_vectors

            # free memory
            del logits

        assert out_vectors.shape == (
            batch_size,
            self.num_attention_heads,
            sequence_length,
            self.attention_head_size,
        ), "out_vectors have be of shape `[batch_size, config.num_attention_heads, sequence_length, config.attention_head_size]`."

    
        return out_vectors.view(-1, sequence_length, head_dim)[:,:orig_sequence_length,:].contiguous()


    def attend(
        self,
        query_vectors,
        key_vectors,
        value_vectors,
        sorted_bucket_idx_per_hash,
        attention_mask,
        head_mask,
        do_standard_self_attention,
        do_cached_attention,
    ):
        # look at previous and following chunks if chunked attention
        if not do_standard_self_attention:
            key_vectors = self._look_adjacent(key_vectors, self.num_chunks_before, self.num_chunks_after)
            value_vectors = self._look_adjacent(value_vectors, self.num_chunks_before, self.num_chunks_after)


        # get logits and dots
        # (BS, NumAttn, NumHash x NumChunk, Chunk_L x Hidden),(BS, NumAttn, NumHash x NumChunk, Chunk_L * (Num_bef + Num_aft + 1) x Hidden) -> (BS, NumAttn, NumHash x NumChunk, Chunk_L, Chunk_L * (1 + Num_bef + Num_aft))
        query_key_dots = torch.matmul(query_vectors, key_vectors.transpose(-1, -2))

        # free memory
        del query_vectors, key_vectors

        # if chunked attention split bucket idxs to query and key
        if not do_standard_self_attention:
            query_bucket_idx = self._split_seq_length_dim_to(
                sorted_bucket_idx_per_hash, -1, self.chunk_length, self.num_attention_heads
            )
            key_value_bucket_idx = self._look_adjacent(query_bucket_idx, self.num_chunks_before, self.num_chunks_after)
        elif do_cached_attention and query_key_dots.ndim > 4:
            key_value_bucket_idx = sorted_bucket_idx_per_hash
            query_bucket_idx = (
                key_value_bucket_idx.new_ones(key_value_bucket_idx.shape[:-1] + (1,)) * key_value_bucket_idx.max()
            )
        elif do_cached_attention and query_key_dots.ndim <= 4:
            query_bucket_idx = (query_key_dots.shape[-1] - 1) * torch.ones_like(query_key_dots)[:, :, :, -1]
            key_value_bucket_idx = torch.arange(
                query_key_dots.shape[-1], dtype=torch.long, device=query_key_dots.device
            )[None, None, :].expand(query_bucket_idx.shape[:2] + (-1,))
        else:
            query_bucket_idx = key_value_bucket_idx = sorted_bucket_idx_per_hash

        # get correct mask values depending on precision
        if query_key_dots.dtype == torch.float16:
            self_mask_value = self.self_mask_value_float16.half()
            mask_value = self.mask_value_float16.half()
        else:
            self_mask_value = self.self_mask_value_float32
            mask_value = self.mask_value_float32

        if not do_cached_attention:
            mask = self._compute_attn_mask(
                query_bucket_idx,
                key_value_bucket_idx,
                attention_mask,
                query_key_dots.shape,
                do_standard_self_attention,
            )

            if mask is not None:
                query_key_dots = torch.where(mask, query_key_dots, mask_value)

            # free memory
            del mask

        # Self mask is ALWAYS applied.
        # From the reformer paper (https://arxiv.org/pdf/2001.04451.pdf):
        # " While attention to the future is not allowed, typical implementations of the
        # Transformer do allow a position to attend to itself.
        # Such behavior is undesirable in a shared-QK formulation because the dot-product
        # of a query vector with itself will almost always be greater than the dot product of a
        # query vector with a vector at another position. We therefore modify the masking
        # to forbid a token from attending to itself, except in situations
        # where a token has no other valid attention targets (e.g. the first token in a sequence) "

        breakpoint()

        self_mask = torch.ne(query_bucket_idx.unsqueeze(-1), key_value_bucket_idx.unsqueeze(-2)).to(
            query_bucket_idx.device
        )

        # apply self_mask
        query_key_dots = torch.where(self_mask, query_key_dots, self_mask_value)

        # free memory
        del self_mask

        logits = torch.logsumexp(query_key_dots, dim=-1, keepdim=True)
        # dots shape is `[batch_size, num_attn_heads, num_hashes * seq_len // chunk_length, chunk_length, chunk_length * (1 + num_chunks_before + num_chunks_after)]`
        attention_probs = torch.exp(query_key_dots - logits)

        # free memory
        del query_key_dots

        # dropout
        attention_probs = nn.functional.dropout(attention_probs, p=self.dropout, training=self.training)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        # breakpoint()

        # attend values
        out_vectors = torch.matmul(attention_probs, value_vectors)

        # free memory
        del value_vectors

        # merge chunk length
        if out_vectors.ndim > 4:
            logits = logits.flatten(start_dim=2, end_dim=3).squeeze(-1)
            out_vectors = out_vectors.flatten(start_dim=2, end_dim=3)

        return out_vectors, logits, attention_probs
