from typing import Dict, Optional, Union

import torch
from doremi_context import DoReMiContext
from nanotron import distributed as dist
from nanotron.config import ParallelismArgs
from nanotron.models import NanotronModel
from nanotron.models.fast.llama import LlamaModel
from nanotron.nn.layer_norm import TritonRMSNorm
from nanotron.parallel import ParallelContext
from nanotron.parallel.parameters import NanotronParameter
from nanotron.parallel.pipeline_parallel.block import (
    PipelineBlock,
    TensorPointer,
)
from nanotron.parallel.tensor_parallel.functional import sharded_cross_entropy
from nanotron.parallel.tensor_parallel.nn import (
    TensorParallelColumnLinear,
    TensorParallelEmbedding,
    TensorParallelRowLinear,
)
from nanotron.random import RandomStates
from torch import nn
from transformers import LlamaConfig


def normalize_domain_weights(weights: torch.Tensor, smoothing_param: float = 1e-3) -> torch.Tensor:
    """
    Renormalize and smooth domain weights.
    alpha_t = (1 - c) * (alpha_t' / sum(i=1 to k of alpha_t'[i])) + c * u
    Algorithm 1 DoReMi domain reweighting (Step 2).
    """
    NUM_DOMAINS = weights.shape[0]
    uniform_weights = torch.ones(NUM_DOMAINS, device=weights.device) / NUM_DOMAINS
    normalized_weight = (1 - smoothing_param) * weights / weights.sum() + (smoothing_param * uniform_weights)
    return normalized_weight


class LLaMaForInference(NanotronModel):
    def __init__(
        self,
        config: LlamaConfig,
        parallel_context: ParallelContext,
        parallel_config: Optional[ParallelismArgs],
    ):
        super().__init__()
        self.model = LlamaModel(config=config, parallel_context=parallel_context, parallel_config=parallel_config)
        self.parallel_context = parallel_context
        self.config = config
        self.parallel_config = parallel_config

    def forward(
        self,
        input_ids: Union[torch.Tensor, TensorPointer],
        input_mask: Union[torch.Tensor, TensorPointer],
        label_ids: Union[torch.Tensor, TensorPointer],
        label_mask: Union[torch.Tensor, TensorPointer],
    ) -> Dict[str, Union[torch.Tensor, TensorPointer]]:
        sharded_logits = self.model(
            input_ids=input_ids,
            input_mask=input_mask,
        )
        sharded_logits = sharded_logits.transpose(0, 1).contiguous()  # [batch size, seq_length, vocab_size]
        logprobs = sharded_cross_entropy(
            sharded_logits,
            label_ids.contiguous(),
            group=self.dpg.tp_pg,
            dtype=torch.float,
        )
        # TODO(xrsrke): recheck if this is correct
        losses = (logprobs * label_mask).sum(dim=-1) / label_mask.sum(dim=-1)
        return {"losses": losses}

    @torch.no_grad()
    def init_model_randomly(self, init_method, scaled_init_method):
        """Initialize model parameters randomly.
        Args:
            init_method (callable): Used for embedding/position/qkv weight in attention/first layer weight of mlp/ /lm_head/
            scaled_init_method (callable): Used for o weight in attention/second layer weight of mlp/

        Note:
            Layernorm weight all 0 or 1 depending on `apply_layernorm_1p`
        """
        model = self
        initialized_parameters = set()
        # Handle tensor parallelism
        module_id_to_prefix = {id(module): f"{module_name}." for module_name, module in model.named_modules()}
        # Fix the root_model
        module_id_to_prefix[id(model)] = ""

        for module_name, module in model.named_modules():
            if isinstance(module, TensorParallelColumnLinear):
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()} or {"weight"} == {
                    name for name, _ in module.named_parameters()
                }
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        init_method(param)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TensorParallelRowLinear):
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()} or {"weight"} == {
                    name for name, _ in module.named_parameters()
                }
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        scaled_init_method(param)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TritonRMSNorm):
                assert {"weight"} == {name for name, _ in module.named_parameters()}
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        # TODO @thomasw21: Sometimes we actually want 0
                        param.fill_(1)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TensorParallelEmbedding):
                # TODO @thomasw21: Handle tied embeddings
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()}

                assert isinstance(module.weight, NanotronParameter)
                if module.weight.is_tied:
                    tied_info = module.weight.get_tied_info()
                    full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                        module_id_to_prefix=module_id_to_prefix
                    )
                else:
                    full_param_name = f"{module_name}.weight"

                if full_param_name in initialized_parameters:
                    # Already initialized
                    continue

                init_method(module.weight)
                assert full_param_name not in initialized_parameters
                initialized_parameters.add(full_param_name)

        assert initialized_parameters == {
            param.get_tied_info().get_full_name_from_module_id_to_prefix(module_id_to_prefix=module_id_to_prefix)
            if param.is_tied
            else name
            for name, param in model.named_parameters()
        }, f"Somehow the initialized set of parameters don't match:\n - Expected: { {name for name, _ in model.named_parameters()} }\n - Got: {initialized_parameters}"

    def get_block_compute_costs(self):
        """Computes the compute cost of each block in the model so that we can do a better job of load balancing."""
        return self.model.get_block_compute_costs()

    def get_flops_per_sec(self, iteration_time_in_sec, sequence_length, global_batch_size):
        """Get flops per second for a given model"""
        return self.model.get_flops_per_sec(iteration_time_in_sec, sequence_length, global_batch_size)


class DoReMiLoss(nn.Module):
    def __init__(self, tp_pg: dist.ProcessGroup):
        super().__init__()
        self.tp_pg = tp_pg

    def forward(
        self,
        sharded_logits: torch.Tensor,  # [seq_length, batch_size, logits]
        label_ids: torch.Tensor,  # [batch_size, seq_length]
        label_mask: torch.Tensor,  # [batch_size, seq_length]
        domain_idx: torch.Tensor,
        ref_losses: torch.Tensor,
        doremi_context: DoReMiContext,
    ) -> Dict[str, torch.Tensor]:
        # Megatron by defaults cast everything in fp32. `--f16-lm-cross-entropy` is an option you can use to keep current precision.
        # https://github.com/NVIDIA/Megatron-LM/blob/f267e6186eae1d6e2055b412b00e2e545a8e896a/megatron/model/gpt_model.py#L38
        logprobs = sharded_cross_entropy(
            sharded_logits, label_ids.transpose(0, 1).contiguous(), group=self.tp_pg, dtype=torch.float
        ).transpose(0, 1)
        # TODO @thomasw21: It's unclear what kind of normalization we want to do.
        losses = (logprobs * label_mask).sum(dim=-1) / label_mask.sum(dim=-1)
        excess_loss = (losses - ref_losses).clamp(min=0)

        # TODO(xrsrke): flatten it in the dataloader
        domain_idx = domain_idx.view(-1)

        # NOTE: Calculate total loss per domain
        domain_losses = torch.zeros(domain_idx.max() + 1, device="cuda")
        for i in range(len(excess_loss)):
            domain_losses[domain_idx[i]] += excess_loss[i]

        tokens_per_domain = torch.bincount(domain_idx, minlength=domain_idx.max() + 1)

        normalized_domain_losses = domain_losses / tokens_per_domain
        updated_domain_weights = doremi_context.domain_weights * torch.exp(
            doremi_context.step_size * normalized_domain_losses
        )
        normalized_domain_weights = normalize_domain_weights(
            updated_domain_weights, smoothing_param=doremi_context.smoothing_param
        )

        doremi_context.domain_weights = (
            normalized_domain_weights * torch.randn_like(normalized_domain_weights)
        ).detach()

        # Sync the loss
        # I think indexing causes a sync we don't actually want
        # loss = loss[label_mask].sum()

        return {"loss": normalized_domain_weights.sum(dim=-1)}


class LlamaForDoReMiTraining(NanotronModel):
    def __init__(
        self,
        config: LlamaConfig,
        parallel_context: ParallelContext,
        parallel_config: Optional[ParallelismArgs],
        random_states: Optional[RandomStates] = None,
        doremi_context: Optional[DoReMiContext] = None,
    ):
        super().__init__()
        self.model = LlamaModel(config=config, parallel_context=parallel_context, parallel_config=parallel_config)
        self.loss = PipelineBlock(
            p2p=self.model.p2p,
            module_builder=DoReMiLoss,
            module_kwargs={"tp_pg": parallel_context.tp_pg},
            module_input_keys={
                "sharded_logits",
                "label_ids",
                "label_mask",
                "domain_idx",
                "ref_losses",
                "doremi_context",
            },
            module_output_keys={"loss"},
        )
        self.parallel_context = parallel_context
        self.config = config
        self.parallel_config = parallel_config

    def forward(
        self,
        input_ids: Union[torch.Tensor, TensorPointer],
        input_mask: Union[torch.Tensor, TensorPointer],
        label_ids: Union[torch.Tensor, TensorPointer],
        label_mask: Union[torch.Tensor, TensorPointer],
        # TODO(xrsrke): change to plural
        domain_idx: Optional[Union[torch.Tensor, TensorPointer]],
        ref_losses: Optional[Union[torch.Tensor, TensorPointer]],
    ) -> Dict[str, Union[torch.Tensor, TensorPointer]]:
        sharded_logits = self.model(
            input_ids=input_ids,
            input_mask=input_mask,
        )
        loss = self.loss(
            sharded_logits=sharded_logits,
            label_ids=label_ids,
            label_mask=label_mask,
            ref_losses=ref_losses,
            domain_idx=domain_idx,
            doremi_context=self.doremi_context,
        )["loss"]
        return {"loss": loss}

    @torch.no_grad()
    def init_model_randomly(self, init_method, scaled_init_method):
        """Initialize model parameters randomly.
        Args:
            init_method (callable): Used for embedding/position/qkv weight in attention/first layer weight of mlp/ /lm_head/
            scaled_init_method (callable): Used for o weight in attention/second layer weight of mlp/

        Note:
            Layernorm weight all 0 or 1 depending on `apply_layernorm_1p`
        """
        model = self
        initialized_parameters = set()
        # Handle tensor parallelism
        module_id_to_prefix = {id(module): f"{module_name}." for module_name, module in model.named_modules()}
        # Fix the root_model
        module_id_to_prefix[id(model)] = ""

        for module_name, module in model.named_modules():
            if isinstance(module, TensorParallelColumnLinear):
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()} or {"weight"} == {
                    name for name, _ in module.named_parameters()
                }
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        init_method(param)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TensorParallelRowLinear):
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()} or {"weight"} == {
                    name for name, _ in module.named_parameters()
                }
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        scaled_init_method(param)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TritonRMSNorm):
                assert {"weight"} == {name for name, _ in module.named_parameters()}
                for param_name, param in module.named_parameters():
                    assert isinstance(param, NanotronParameter)
                    if param.is_tied:
                        tied_info = param.get_tied_info()
                        full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                            module_id_to_prefix=module_id_to_prefix
                        )
                    else:
                        full_param_name = f"{module_name}.{param_name}"

                    if full_param_name in initialized_parameters:
                        # Already initialized
                        continue

                    if "weight" == param_name:
                        # TODO @thomasw21: Sometimes we actually want 0
                        param.fill_(1)
                    elif "bias" == param_name:
                        param.zero_()
                    else:
                        raise ValueError(f"Who the fuck is {param_name}?")

                    assert full_param_name not in initialized_parameters
                    initialized_parameters.add(full_param_name)
            elif isinstance(module, TensorParallelEmbedding):
                # TODO @thomasw21: Handle tied embeddings
                # Somehow Megatron-LM does something super complicated, https://github.com/NVIDIA/Megatron-LM/blob/2360d732a399dd818d40cbe32828f65b260dee11/megatron/core/tensor_parallel/layers.py#L96
                # What it does:
                #  - instantiate a buffer of the `full size` in fp32
                #  - run init method on it
                #  - shard result to get only a specific shard
                # Instead I'm lazy and just going to run init_method, since they are scalar independent
                assert {"weight"} == {name for name, _ in module.named_parameters()}

                assert isinstance(module.weight, NanotronParameter)
                if module.weight.is_tied:
                    tied_info = module.weight.get_tied_info()
                    full_param_name = tied_info.get_full_name_from_module_id_to_prefix(
                        module_id_to_prefix=module_id_to_prefix
                    )
                else:
                    full_param_name = f"{module_name}.weight"

                if full_param_name in initialized_parameters:
                    # Already initialized
                    continue

                init_method(module.weight)
                assert full_param_name not in initialized_parameters
                initialized_parameters.add(full_param_name)

        assert initialized_parameters == {
            param.get_tied_info().get_full_name_from_module_id_to_prefix(module_id_to_prefix=module_id_to_prefix)
            if param.is_tied
            else name
            for name, param in model.named_parameters()
        }, f"Somehow the initialized set of parameters don't match:\n - Expected: { {name for name, _ in model.named_parameters()} }\n - Got: {initialized_parameters}"

    def get_block_compute_costs(self):
        """Computes the compute cost of each block in the model so that we can do a better job of load balancing."""
        return self.model.get_block_compute_costs()

    def get_flops_per_sec(self, iteration_time_in_sec, sequence_length, global_batch_size):
        """Get flops per second for a given model"""
        return self.model.get_flops_per_sec(iteration_time_in_sec, sequence_length, global_batch_size)
