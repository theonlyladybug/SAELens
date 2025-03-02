from dataclasses import dataclass
from typing import Any, Optional, cast

import torch

import wandb


@dataclass
class LanguageModelSAERunnerConfig:
    """
    Configuration for training a sparse autoencoder on a language model.
    """

    # Data Generating Function (Model + Training Distibuion)
    model_name: str = "gelu-2l"
    hook_point: str = "blocks.{layer}.hook_mlp_out"
    hook_point_layer: int | list[int] = 0
    hook_point_head_index: Optional[int] = None
    dataset_path: str = "NeelNanda/c4-tokenized-2b"
    is_dataset_tokenized: bool = True
    context_size: int = 128
    use_cached_activations: bool = False
    cached_activations_path: Optional[str] = (
        None  # Defaults to "activations/{dataset}/{model}/{full_hook_name}_{hook_point_head_index}"
    )

    # SAE Parameters
    d_in: int = 512

    # Activation Store Parameters
    n_batches_in_buffer: int = 20
    total_training_tokens: int = 2_000_000
    store_batch_size: int = 32
    train_batch_size: int = 4096

    # Misc
    device: str | torch.device = "cpu"
    seed: int = 42
    dtype: torch.dtype = torch.float32
    prepend_bos: bool = True

    # SAE Parameters
    b_dec_init_method: str = "geometric_median"
    expansion_factor: int | list[int] = 4
    from_pretrained_path: Optional[str] = None
    d_sae: Optional[int] = None

    # Training Parameters
    l1_coefficient: float | list[float] = 1e-3
    lp_norm: float | list[float] = 1
    lr: float | list[float] = 3e-4
    lr_scheduler_name: str | list[str] = (
        "constant"  # constant, cosineannealing, cosineannealingwarmrestarts
    )
    lr_warm_up_steps: int | list[int] = 500
    lr_end: float | list[float] | None = (
        None  # only used for cosine annealing, default is lr / 10
    )
    lr_decay_steps: int | list[int] = 0
    n_restart_cycles: int | list[int] = 1  # used only for cosineannealingwarmrestarts
    train_batch_size: int = 4096

    # Resampling protocol args
    use_ghost_grads: bool | list[bool] = (
        False  # want to change this to true on some timeline.
    )
    feature_sampling_window: int = 2000
    dead_feature_window: int = 1000  # unless this window is larger feature sampling,

    dead_feature_threshold: float = 1e-8

    # WANDB
    log_to_wandb: bool = True
    wandb_project: str = "mats_sae_training_language_model"
    run_name: Optional[str] = None
    wandb_entity: Optional[str] = None
    wandb_log_frequency: int = 10

    # Misc
    n_checkpoints: int = 0
    checkpoint_path: str = "checkpoints"
    verbose: bool = True

    def __post_init__(self):
        if self.use_cached_activations and self.cached_activations_path is None:
            self.cached_activations_path = _default_cached_activations_path(
                self.dataset_path,
                self.model_name,
                self.hook_point,
                self.hook_point_head_index,
            )

        if not isinstance(self.expansion_factor, list):
            self.d_sae = self.d_in * self.expansion_factor
        self.tokens_per_buffer = (
            self.train_batch_size * self.context_size * self.n_batches_in_buffer
        )

        if self.run_name is None:
            self.run_name = f"{self.d_sae}-L1-{self.l1_coefficient}-LR-{self.lr}-Tokens-{self.total_training_tokens:3.3e}"

        if self.b_dec_init_method not in ["geometric_median", "mean", "zeros"]:
            raise ValueError(
                f"b_dec_init_method must be geometric_median, mean, or zeros. Got {self.b_dec_init_method}"
            )
        if self.b_dec_init_method == "zeros":
            print(
                "Warning: We are initializing b_dec to zeros. This is probably not what you want."
            )

        self.device: str | torch.device = torch.device(self.device)

        if self.lr_end is None:
            if isinstance(self.lr, list):
                self.lr_end = [lr / 10 for lr in self.lr]
            else:
                self.lr_end = self.lr / 10

        unique_id = cast(
            Any, wandb
        ).util.generate_id()  # not sure why this type is erroring
        self.checkpoint_path = f"{self.checkpoint_path}/{unique_id}"

        if self.verbose:
            print(
                f"Run name: {self.d_sae}-L1-{self.l1_coefficient}-LR-{self.lr}-Tokens-{self.total_training_tokens:3.3e}"
            )
            # Print out some useful info:
            n_tokens_per_buffer = (
                self.store_batch_size * self.context_size * self.n_batches_in_buffer
            )
            print(f"n_tokens_per_buffer (millions): {n_tokens_per_buffer / 10 **6}")
            n_contexts_per_buffer = self.store_batch_size * self.n_batches_in_buffer
            print(
                f"Lower bound: n_contexts_per_buffer (millions): {n_contexts_per_buffer / 10 **6}"
            )

            total_training_steps = self.total_training_tokens // self.train_batch_size
            print(f"Total training steps: {total_training_steps}")

            total_wandb_updates = total_training_steps // self.wandb_log_frequency
            print(f"Total wandb updates: {total_wandb_updates}")

            # how many times will we sample dead neurons?
            # assert self.dead_feature_window <= self.feature_sampling_window, "dead_feature_window must be smaller than feature_sampling_window"
            n_feature_window_samples = (
                total_training_steps // self.feature_sampling_window
            )
            print(
                f"n_tokens_per_feature_sampling_window (millions): {(self.feature_sampling_window * self.context_size * self.train_batch_size) / 10 **6}"
            )
            print(
                f"n_tokens_per_dead_feature_window (millions): {(self.dead_feature_window * self.context_size * self.train_batch_size) / 10 **6}"
            )
            print(
                f"We will reset the sparsity calculation {n_feature_window_samples} times."
            )
            # print("Number tokens in dead feature calculation window: ", self.dead_feature_window * self.train_batch_size)
            print(
                f"Number tokens in sparsity calculation window: {self.feature_sampling_window * self.train_batch_size:.2e}"
            )

        if not isinstance(self.use_ghost_grads, list) and self.use_ghost_grads:
            print("Using Ghost Grads.")


@dataclass
class CacheActivationsRunnerConfig:
    """
    Configuration for caching activations of an LLM.
    """

    # Data Generating Function (Model + Training Distibuion)
    model_name: str = "gelu-2l"
    hook_point: str = "blocks.{layer}.hook_mlp_out"
    hook_point_layer: int | list[int] = 0
    hook_point_head_index: Optional[int] = None
    dataset_path: str = "NeelNanda/c4-tokenized-2b"
    is_dataset_tokenized: bool = True
    context_size: int = 128
    cached_activations_path: Optional[str] = (
        None  # Defaults to "activations/{dataset}/{model}/{full_hook_name}_{hook_point_head_index}"
    )

    # SAE Parameters
    d_in: int = 512

    # Activation Store Parameters
    n_batches_in_buffer: int = 20
    total_training_tokens: int = 2_000_000
    store_batch_size: int = 32
    train_batch_size: int = 4096

    # Misc
    device: str | torch.device = "cpu"
    seed: int = 42
    dtype: torch.dtype = torch.float32
    prepend_bos: bool = True

    # Activation caching stuff
    shuffle_every_n_buffers: int = 10
    n_shuffles_with_last_section: int = 10
    n_shuffles_in_entire_dir: int = 10
    n_shuffles_final: int = 100

    def __post_init__(self):
        # Autofill cached_activations_path unless the user overrode it
        if self.cached_activations_path is None:
            self.cached_activations_path = _default_cached_activations_path(
                self.dataset_path,
                self.model_name,
                self.hook_point,
                self.hook_point_head_index,
            )


def _default_cached_activations_path(
    dataset_path: str,
    model_name: str,
    hook_point: str,
    hook_point_head_index: int | None,
) -> str:
    path = f"activations/{dataset_path.replace('/', '_')}/{model_name.replace('/', '_')}/{hook_point}"
    if hook_point_head_index is not None:
        path += f"_{hook_point_head_index}"
    return path
