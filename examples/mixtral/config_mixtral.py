""" Example python script to generate a YAML config file which can be used to run a training with nanotron. Refer to "examples" section in the `/README.md` for more information.

Usage:
```
python config_tiny_mixtral.py
```
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class MixtralConfig:
    """Configuration for a MIXTRAL model

    Be careful on having a coherent typing as we use it to reconstruct the model from yaml
    """

    attn_pdrop: float = 0.0
    bos_token_id: int = 1
    eos_token_id: int = 2
    hidden_act: str = "silu"
    hidden_size: int = 4096
    initializer_range: float = 0.02
    intermediate_size: int = 14336
    is_mixtral_config: bool = True  # We use this help differentiate models in yaml/python conversion
    max_position_embeddings: int = 32768
    num_attention_heads: int = 32
    num_hidden_layers: int = 32
    num_key_value_heads: Optional[int] = 8
    pad_token_id: Optional[int] = None
    pretraining_tp: int = 1
    rms_norm_eps: float = 1e-05
    rope_theta: float = 10000.0
    sliding_window_size: int = 4096
    tie_word_embeddings: bool = False
    use_cache: bool = True
    vocab_size: int = 32000

    ## MoE specific
    # Number of experts per Sparse MLP layer.
    moe_num_experts: int = 1
    # the number of experts to root per-token, can be also interpreted as the `top-p` routing parameter
    num_experts_per_tok: int = 2
    moe_capacity_factor: int = 1

    def __post_init__(self):
        # for backward compatibility
        if self.num_key_value_heads is None:
            self.num_key_value_heads = self.num_attention_heads

        assert (
            self.num_experts_per_tok <= self.moe_num_experts
        ), f"num_experts_per_tok ({self.num_experts_per_tok}) must be <= moe_num_experts ({self.moe_num_experts})"

    @classmethod
    def from_hf_config(cls, model_name: str) -> "MixtralConfig":
        from transformers import MixtralConfig as MixtralConfigHF

        config = MixtralConfigHF.from_pretrained(model_name)
        return cls(
            attn_pdrop=config.attention_dropout,
            bos_token_id=config.bos_token_id,
            eos_token_id=config.eos_token_id,
            hidden_act=config.hidden_act,
            hidden_size=config.hidden_size,
            initializer_range=config.initializer_range,
            intermediate_size=config.intermediate_size,
            max_position_embeddings=config.max_position_embeddings,
            num_attention_heads=config.num_attention_heads,
            num_hidden_layers=config.num_hidden_layers,
            num_key_value_heads=config.num_key_value_heads,
            pad_token_id=config.pad_token_id,
            rms_norm_eps=config.rms_norm_eps,
            rope_theta=config.rope_theta,
            sliding_window_size=config.sliding_window,
            tie_word_embeddings=config.tie_word_embeddings,
            use_cache=config.use_cache,
            vocab_size=config.vocab_size,
            # MoE specific config
            num_experts_per_tok=config.num_experts_per_tok,
            moe_num_experts=config.num_local_experts,
            # router_aux_loss_coef=config.router_aux_loss_coef, #TODO @nouamane: do we need this?
            # moe_capacity_factor=config.moe_capacity_factor,  #TODO @nouamane: transformers doesn't have this
        )


def get_num_params(model_config: MixtralConfig) -> int:
    num_params = model_config.vocab_size * model_config.hidden_size * 2 + model_config.num_hidden_layers * (
        3 * model_config.hidden_size * model_config.intermediate_size
        + 2 * model_config.hidden_size * model_config.hidden_size
        + 2
        * model_config.hidden_size
        * (model_config.hidden_size / (model_config.num_attention_heads / model_config.num_key_value_heads))
    )
    return num_params
