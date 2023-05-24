import os
from typing import Any, Iterator

import psutil
import torch
from absl import logging
from datasets import Dataset as HFDataset
from datasets import load_dataset
from ml_collections import config_dict
from tokenizers import Tokenizer
from tokenizers.models import BPE
from tokenizers.normalizers import BertNormalizer
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.trainers import BpeTrainer
from torch import optim
from torch.nn import functional as F
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizerFast


class NoamOpt:
    def __init__(
        self,
        parameters: Iterator[torch.nn.Parameter],
        lr: float = 0,
        betas: tuple[float, float] = (0.9, 0.98),
        eps: float = 1e-9,
        embed_dim: int = 512,
        warmup_steps: int = 4000,
    ) -> None:
        """Implements the Noam learning rate schedule.

        Args:
            parameters (Iterator[torch.nn.Parameter]): the model parameters.
            lr (float, optional): initial lr. Defaults to 0.
            betas (tuple[float, float], optional): betas for Adam. Defaults to (0.9, 0.98).
            eps (float, optional): eps for Adam. Defaults to 1e-9.
            embed_dim (int, optional): model embedding dimension. Defaults to 512.
            warmup_steps (int, optional): warmup steps prior to decay. Defaults to 4000.
        """
        self.opt = optim.Adam(parameters, lr=lr, betas=betas, eps=eps)
        self.embed_dim = embed_dim
        self.warmup_steps = warmup_steps
        self._step = 0

    def zero_grad(self):
        self.opt.zero_grad()

    def step(self):
        self._step += 1
        self._update_lr()
        self.opt.step()

    def _lr(self):
        # lrate = d^−0.5 * min(step_num−0.5, step_num * warmup_steps^−1.5)
        return self.embed_dim**-0.5 * min(
            self._step**-0.5, self._step * self.warmup_steps**-1.5
        )

    def _update_lr(self):
        for param_group in self.opt.param_groups:
            param_group["lr"] = self._lr()


class Trainer:
    def __init__(self, model, device: torch.device) -> None:
        """Training class to centralize training and validation logic.

        Args:
            model: the model to train.
            device (torch.device): device to use for training.
        """
        self.model = model.to(device)
        self.opt = NoamOpt(
            model.parameters(),
            lr=0,
            betas=(0.9, 0.98),
            eps=1e-9,
            embed_dim=model.embed_dim,
            warmup_steps=4000,
        )
        self.device = device

    @staticmethod
    def loss_fn(
        model,
        batch: dict[str, torch.Tensor],
        device: torch.device,
        bypass_embedding: bool = False,
    ):
        input_ids = batch["input"].to(device)
        target_ids = batch["target"].to(device)
        logits, _ = model(input_ids, bypass_embedding=bypass_embedding)
        return F.cross_entropy(logits.transpose(1, 2), target_ids)

    def train_step(self, batch: dict[str, torch.Tensor]):
        self.model.train()
        self.opt.zero_grad()
        # perform train step
        loss = Trainer.loss_fn(self.model, batch, self.device)
        loss.backward()
        self.opt.step()
        return loss.item()

    def eval_step(self, batch: dict[str, torch.Tensor]):
        self.model.eval()
        with torch.no_grad():
            loss = Trainer.loss_fn(self.model, batch, self.device)
        return loss.item()

    def save_checkpoint(self, path: str):
        torch.save(
            {
                "model": self.model.state_dict(),
                "opt": self.opt.state_dict(),
                "step": self.opt._step,
            },
            path,
        )

    def load_checkpoint(self, path: str):
        state = torch.load(path)
        self.model.load_state_dict(state["model"])
        self.opt.load_state_dict(state["opt"])
        self.opt._step = state["step"]


def setup_device() -> torch.device:
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
        logging.info(
            f"Using CUDA device: {torch.cuda.get_device_name()} ({torch.cuda.device_count()}x)"
        )
        logging.info(
            f"Memory available: {1e-9 * torch.cuda.mem_get_info(device)[0]:.1f} GB"
        )
        return device
    elif torch.backends.mps.is_available():
        logging.info("Using MPS device")
        return torch.device("mps")

    stats = psutil.virtual_memory()  # returns a named tuple
    available = getattr(stats, "available")
    logging.info(f"available memory: {1e-6 * available} mb")
    return torch.device("cpu")


def create_train_tokenizer(
    raw_dataset: HFDataset,
    tokenizer_file_path: str,
    batch_size: int = 1000,
    config: config_dict = None,
) -> PreTrainedTokenizerFast:
    if not os.path.exists(tokenizer_file_path):
        # create the tokenizer
        tokenizer = Tokenizer(BPE(unk_token="[UNK]"))
        trainer = BpeTrainer(
            special_tokens=config.special_tokens,
        )
        tokenizer.pre_tokenizer = Whitespace()
        tokenizer.normalizer = BertNormalizer()

        # train tokenizer in batches
        def batch_iterator():
            for i in range(0, len(raw_dataset), batch_size):
                yield raw_dataset[i : i + batch_size]["text"]

        tokenizer.train_from_iterator(
            batch_iterator(), trainer=trainer, length=len(raw_dataset)
        )
        tokenizer.save(tokenizer_file_path)
    else:
        tokenizer = Tokenizer.from_file(tokenizer_file_path)

    # wrap in a fast tokenizer for reuse
    wrapped_tokenizer = PreTrainedTokenizerFast(tokenizer_object=tokenizer)
    return wrapped_tokenizer, tokenizer.get_vocab_size()


class SimpleTextDataset(Dataset):
    def __init__(
        self,
        path: str,
        name: str,
        split: str,
        config: config_dict,
        tokenizer_batch_size: int = 1000,
    ) -> None:
        raw_dataset = load_dataset(path, name, split=split)
        # remove empty rows
        raw_dataset = raw_dataset.filter(lambda x: len(x["text"]) > 0)

        self.tokenizer, self.vocab_size = create_train_tokenizer(
            raw_dataset,
            tokenizer_file_path=f"{path}-tokenizer.json",
            batch_size=tokenizer_batch_size,
            config=config,
        )

        if os.path.exists(f"{path}-{split}-tokenized.pt"):
            self.data = torch.load(f"{path}-{split}-tokenized.pt")
        else:
            encoded = raw_dataset.map(
                lambda x: self.tokenizer(x["text"]),
                batched=True,
            )
            # save as a series of tokens, discarding sentence structure
            tokens = torch.cat([torch.tensor(x) for x in encoded["input_ids"]])
            self.data = torch.tensor(tokens, dtype=torch.long)
            torch.save(self.data, f"{path}-{split}-tokenized.pt")

        self.block_size = config.hyperparams.block_size

    def encode(self, block_input: str) -> torch.Tensor:
        return self.tokenizer(block_input)["input_ids"]

    def decode(self, block_ids: torch.Tensor) -> str:
        return self.tokenizer.decode(block_ids)

    def tokenize(self, block_input: str) -> list[str]:
        return self.tokenizer.tokenize(block_input)

    def __len__(self) -> int:
        return len(self.data) - self.block_size

    def __getitem__(self, index) -> Any:
        # Consider block_size + 1 tokens from the dataset, starting from index.
        return {
            "input": self.data[index : index + self.block_size],
            "target": self.data[index + 1 : index + self.block_size + 1],
        }