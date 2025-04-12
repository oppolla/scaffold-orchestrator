from dataclasses import dataclass
from typing import Optional, Callable, Union, List, Dict
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import get_linear_schedule_with_warmup
import threading
import os
import math
import random
from collections import deque
import time
import numpy as np

@dataclass
class TrainingConfig:
    """Configuration for training parameters."""
    learning_rate: float = 2e-5
    grad_accum_steps: int = 4
    weight_decay: float = 0.01
    warmup_steps: int = 0
    total_steps: int = 100000
    max_grad_norm: float = 1.0
    use_amp: bool = True
    max_patience: int = 2
    batch_size: int = 2
    max_epochs: int = 3
    validate_every_n_steps: int = 100
    checkpoint_interval: int = 1000
    checkpoint_path: str = "checkpoints/sovl_trainer"
    scheduler_type: str = "linear"  # Options: linear, cosine, constant
    cosine_min_lr: float = 1e-6
    warmup_ratio: float = 0.1
    dropout_rate: float = 0.1
    max_seq_length: int = 512
    metrics_to_track: List[str] = None
    enable_gestation: bool = True
    enable_sleep_training: bool = True
    enable_lifecycle_weighting: bool = True
    lifecycle_capacity_factor: float = 0.01
    lifecycle_curve: str = "sigmoid_linear"
    sleep_conf_threshold: float = 0.7
    sleep_log_min: int = 10
    accumulation_steps: int = 4
    exposure_gain_eager: int = 3
    exposure_gain_default: int = 2
    dream_memory_weight: float = 0.1
    enable_dreaming: bool = True
    repetition_n: int = 3
    sigmoid_scale: float = 0.5
    sigmoid_shift: float = 5.0
    # Dream-specific parameters
    dream_noise_scale: float = 0.05
    dream_prompt_weight: float = 0.5
    dream_novelty_boost: float = 0.03
    dream_memory_decay: float = 0.95
    dream_prune_threshold: float = 0.1
    temp_melancholy_noise: float = 0.02
    enable_prompt_driven_dreams: bool = True
    dream_swing_var: float = 0.1
    dream_lifecycle_delta: float = 0.1
    dream_temperament_on: bool = True
    confidence_history_maxlen: int = 5
    temperament_history_maxlen: int = 5

    def __post_init__(self):
        if self.metrics_to_track is None:
            self.metrics_to_track = ["loss", "accuracy", "confidence"]
        assert self.learning_rate > 0, "Learning rate must be positive"
        assert self.grad_accum_steps >= 1, "Gradient accumulation steps must be at least 1"
        assert self.max_grad_norm > 0, "Max gradient norm must be positive"
        assert self.scheduler_type in ["linear", "cosine", "constant"], "Invalid scheduler type"
        assert self.lifecycle_curve in ["sigmoid_linear", "exponential"], "Invalid lifecycle curve"
        assert self.repetition_n >= 2, "Repetition check length must be at least 2"
        assert self.sigmoid_scale > 0, "Sigmoid scale must be positive"
        assert self.sigmoid_shift >= 0, "Sigmoid shift must be non-negative"
        assert self.dream_noise_scale >= 0, "Dream noise scale must be non-negative"
        assert 0 <= self.dream_prompt_weight <= 1, "Dream prompt weight must be in [0, 1]"
        assert self.dream_novelty_boost >= 0, "Dream novelty boost must be non-negative"
        assert 0 <= self.dream_memory_decay <= 1, "Dream memory decay must be in [0, 1]"
        assert 0 <= self.dream_prune_threshold <= 1, "Dream prune threshold must be in [0, 1]"
        assert self.dream_swing_var >= 0, "Dream swing variance must be non-negative"
        assert self.dream_lifecycle_delta >= 0, "Dream lifecycle delta must be non-negative"
        assert self.confidence_history_maxlen > 0, "Confidence history maxlen must be positive"
        assert self.temperament_history_maxlen > 0, "Temperament history maxlen must be positive"

def collate_batch(batch: List[dict], pad_token_id: int, max_seq_length: int, tokenizer) -> dict:
    """Collate batch of prompt-completion pairs into tensors."""
    prompts = [item["prompt"] for item in batch]
    completions = [item["completion"] for item in batch]
    full_texts = [p + c for p, c in zip(prompts, completions)]

    encodings = tokenizer(
        full_texts,
        return_tensors="pt",
        padding="max_length",
        truncation=True,
        max_length=max_seq_length
    )
    input_ids = encodings["input_ids"]
    attention_mask = encodings["attention_mask"]

    labels = input_ids.clone()
    labels[labels == pad_token_id] = -100
    prompt_encodings = tokenizer(
        prompts,
        return_tensors="pt",
        padding="max_length",
        truncation=True,
        max_length=max_seq_length
    )
    prompt_mask = prompt_encodings["attention_mask"]
    labels = torch.where(prompt_mask.bool(), -100, input_ids)

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
        "prompt": prompts
    }

class SOVLTrainer:
    def __init__(
        self,
        model: torch.nn.Module,
        config: TrainingConfig,
        device: torch.device,
        loss_fn: Callable,
        logger: Optional[Callable] = None,
        memory_lock: Optional[threading.Lock] = None,
        tokenizer: Optional[Callable] = None,
        state: Optional[object] = None
    ):
        self.model = model.to(device)
        self.config = config
        self.device = device
        self.loss_fn = loss_fn
        self.logger = logger or (lambda x: None)
        self.memory_lock = memory_lock or threading.Lock()
        self.tokenizer = tokenizer
        self.state = state
        self.global_step = 0
        self.best_valid_loss = float("inf")
        self.patience = 0
        self.loss_weight_fn = None
        self.memory_check = None
        self.data_exposure = 0
        self.lora_capacity = sum(p.numel() for p in model.parameters() if p.requires_grad) * config.lifecycle_capacity_factor
        self.scaffold_context = None
        self.is_gestating = False
        self.gestation_progress = 0
        self.gestation_batch = []
        self.gestation_total_loss = 0.0
        self.gestation_steps = 0
        self.sleep_progress = 0
        self.sleep_batch = []
        self.sleep_total_loss = 0.0
        self.sleep_steps = 0

        # Optimizer
        self.optimizer = torch.optim.AdamW(
            self.model.parameters(),
            lr=config.learning_rate,
            weight_decay=config.weight_decay
        )

        # Scheduler
        if config.scheduler_type == "linear":
            self.scheduler = get_linear_schedule_with_warmup(
                self.optimizer,
                num_warmup_steps=int(config.warmup_steps or config.warmup_ratio * config.total_steps),
                num_training_steps=config.total_steps
            )
        elif config.scheduler_type == "cosine":
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=config.total_steps - int(config.warmup_steps or config.warmup_ratio * config.total_steps),
                eta_min=config.cosine_min_lr
            )
        else:
            self.scheduler = None

        # Apply dropout if specified
        if config.dropout_rate > 0:
            for module in self.model.modules():
                if isinstance(module, torch.nn.Dropout):
                    module.p = config.dropout_rate

    def get_life_curve_weight(self):
        """Calculate lifecycle weight based on data exposure."""
        x = self.data_exposure / self.lora_capacity if self.lora_capacity > 0 else 0
        if self.config.lifecycle_curve == "sigmoid_linear":
            weight = 1 / (1 + math.exp(-self.config.sigmoid_scale * (x - self.config.sigmoid_shift)))
        else:
            weight = 1 - math.exp(-x)
        return min(1.0, weight)

    def update_exposure(self, prompts: List[str], temperament_score: float):
        """Update data exposure based on prompts and temperament."""
        exposure_gain = (
            self.config.exposure_gain_eager
            if temperament_score > 0.5
            else self.config.exposure_gain_default
        )
        for prompt in prompts:
            if self.state and prompt not in self.state.seen_prompts:
                self.state.add_seen_prompt(prompt)
                self.data_exposure += exposure_gain

    def has_repetition(self, output_ids: torch.Tensor, special_ids: set) -> bool:
        """Check for repeated sequences in output_ids."""
        ids = output_ids.tolist()
        filtered = [i for i in ids if i not in special_ids]
        n = self.config.repetition_n
        for i in range(len(filtered) - 2 * n):
            if filtered[i:i + n] == filtered[i + n:i + 2 * n]:
                return True
        return False

    def get_loss_weight(self, batch: dict) -> float:
        """Calculate loss weight based on log entries."""
        log_entries = self.logger.read() if hasattr(self.logger, "read") else []
        prompts = batch['prompt']
        for prompt in prompts:
            if any(e["prompt"] == prompt and e.get("is_system_question", False) and e["response"] for e in log_entries):
                return 1.2
        return 1.0

    def train_step(
        self,
        batch: dict,
        scaffold_context: Optional[torch.Tensor] = None,
        grad_clip: bool = True,
        loss_weight_fn: Optional[Callable] = None,
        dry_run: bool = False,
        memory_check: Optional[Callable] = None
    ) -> tuple[Optional[float], Optional[float]]:
        """Execute a single training step."""
        if memory_check:
            memory_check()

        self.scaffold_context = scaffold_context

        if dry_run:
            self.model.eval()
            with torch.no_grad():
                with torch.autocast(device_type=self.device.type, dtype=torch.float16 if self.config.use_amp else torch.bfloat16):
                    outputs = self.model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
                    loss = self.loss_fn(outputs.logits, batch["labels"])
            return loss.item(), None

        self.model.train()
        with torch.autocast(device_type=self.device.type, dtype=torch.float16 if self.config.use_amp else torch.bfloat16):
            outputs = self.model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
            loss = self.loss_fn(outputs.logits, batch["labels"])
            weight = (loss_weight_fn(batch) if loss_weight_fn else self.get_loss_weight(batch))
            loss *= weight
            if self.config.enable_lifecycle_weighting:
                lifecycle_weight = self.get_life_curve_weight()
                loss *= lifecycle_weight

        if torch.isnan(loss) or torch.isinf(loss):
            self.logger({"event": "invalid_loss", "loss": str(loss.item()), "timestamp": time.time()})
            return None, None

        scaled_loss = loss / self.config.grad_accum_steps
        scaled_loss.backward()

        if (self.global_step + 1) % self.config.grad_accum_steps == 0:
            if grad_clip:
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.max_grad_norm)
            self.optimizer.step()
            if self.scheduler:
                self.scheduler.step()
            self.optimizer.zero_grad()

        self.global_step += 1

        confidence = None
        if self.config.metrics_to_track and ("accuracy" in self.config.metrics_to_track or "confidence" in self.config.metrics_to_track):
            with torch.no_grad():
                probs = torch.softmax(outputs.logits, dim=-1)
                confidence = torch.max(probs, dim=-1).values.mean().item()

        if self.config.checkpoint_interval and self.global_step % self.config.checkpoint_interval == 0:
            self.save_checkpoint(self.global_step)

        return loss.item(), confidence

    def validate(self, data: Union[List[dict], 'DataLoader'], scaffold_provider: Optional[Callable] = None) -> tuple[float, dict]:
        """Validate model on provided data, returning loss and metrics."""
        self.model.eval()
        total_loss = 0.0
        total_correct = 0
        total_tokens = 0
        batches = 0
        metrics = {metric: 0.0 for metric in self.config.metrics_to_track}

        if isinstance(data, (list, tuple)):
            data_iter = [data[i:i + self.config.batch_size] for i in range(0, len(data), self.config.batch_size)]
        else:
            data_iter = data

        with torch.no_grad():
            for batch in data_iter:
                if isinstance(batch, (list, tuple)):
                    batch = collate_batch(batch, self.tokenizer.pad_token_id, self.config.max_seq_length, self.tokenizer)
                batch = {k: v.to(self.device) if isinstance(v, torch.Tensor) else v for k, v in batch.items()}

                scaffold_context = scaffold_provider(batch) if scaffold_provider else self.scaffold_context
                if scaffold_context is not None:
                    scaffold_context = scaffold_context.to(self.device)

                with torch.autocast(device_type=self.device.type, dtype=torch.float16 if self.config.use_amp else torch.bfloat16):
                    outputs = self.model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
                    loss = self.loss_fn(outputs.logits, batch["labels"])

                total_loss += loss.item()
                batches += 1

                if "accuracy" in self.config.metrics_to_track:
                    preds = outputs.logits.argmax(dim=-1)
                    mask = batch["labels"] != -100
                    correct = (preds[mask] == batch["labels"][mask]).sum().item()
                    total_correct += correct
                    total_tokens += mask.sum().item()
                    metrics["accuracy"] = total_correct / total_tokens if total_tokens > 0 else 0.0
                if "perplexity" in self.config.metrics_to_track:
                    perplexity = torch.exp(loss).item()
                    metrics["perplexity"] = perplexity

        avg_loss = total_loss / batches if batches > 0 else 0.0
        metrics["loss"] = avg_loss
        self.logger({
            "event": "validation",
            "loss": avg_loss,
            "metrics": metrics,
            "timestamp": time.time()
        })
        return avg_loss, metrics

    def save_checkpoint(self, step: int, suffix: Optional[str] = None):
        """Save trainer state to checkpoint."""
        checkpoint_dir = self.config.checkpoint_path
        os.makedirs(checkpoint_dir, exist_ok=True)
        checkpoint_name = f"checkpoint_{step}{f'_{suffix}' if suffix else ''}.pth"
        checkpoint_path = os.path.join(checkpoint_dir, checkpoint_name)

        state_dict = {
            "model_state": self.model.state_dict(),
            "optimizer_state": self.optimizer.state_dict(),
            "scheduler_state": self.scheduler.state_dict() if self.scheduler else None,
            "global_step": self.global_step,
            "best_valid_loss": self.best_valid_loss,
            "patience": self.patience,
            "data_exposure": self.data_exposure
        }
        torch.save(state_dict, checkpoint_path)
        self.logger({
            "event": "checkpoint_saved",
            "path": checkpoint_path,
            "step": step,
            "timestamp": time.time()
        })
        print(f"Checkpoint saved: {checkpoint_path}")

    def load_checkpoint(self, checkpoint_path: str):
        """Load trainer state from checkpoint."""
        if not os.path.exists(checkpoint_path):
            self.logger({
                "event": "checkpoint_load_failed",
                "path": checkpoint_path,
                "error": "File not found",
                "timestamp": time.time()
            })
            raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")

        state_dict = torch.load(checkpoint_path, map_location=self.device)
        self.model.load_state_dict(state_dict["model_state"])
        self.optimizer.load_state_dict(state_dict["optimizer_state"])
        if state_dict["scheduler_state"] and self.scheduler:
            self.scheduler.load_state_dict(state_dict["scheduler_state"])
        self.global_step = state_dict["global_step"]
        self.best_valid_loss = state_dict["best_valid_loss"]
        self.patience = state_dict["patience"]
        self.data_exposure = state_dict.get("data_exposure", 0)
        self.logger({
            "event": "checkpoint_loaded",
            "path": checkpoint_path,
            "step": self.global_step,
            "timestamp": time.time()
        })
        print(f"Checkpoint loaded: {checkpoint_path} at step {self.global_step}")

    def should_stop(self) -> bool:
        """Check if training should stop based on early stopping criteria."""
        return self.patience >= self.config.max_patience

    def gestate(self, log_entries: List[dict], resume: bool = False) -> bool:
        """Perform gestation training on log entries."""
        if not self.config.enable_gestation:
            return False

        if not log_entries:
            print("No log data to gestate.")
            self._reset_gestation_state()
            return False

        if not resume and not self._should_gestate(log_entries):
            return False

        if not resume:
            self.is_gestating = True
            self.gestation_progress = 0
            self.gestation_batch = [
                {"prompt": entry["prompt"], "completion": entry["response"]}
                for entry in log_entries if "prompt" in entry and "response" in entry
            ]
            self.gestation_total_loss = 0.0
            self.gestation_steps = 0
            print("\nTrainer Gestating...")
            if self.config.enable_dreaming and self._should_dream():
                self._dream()

            self.data_exposure += sum(
                len(entry["prompt"]) + len(entry["response"])
                for entry in log_entries
                if "prompt" in entry and "response" in entry
            )

        if self.gestation_progress < len(self.gestation_batch):
            batch = [self.gestation_batch[self.gestation_progress]]
            formatted_batch = collate_batch(
                batch,
                self.tokenizer.pad_token_id,
                self.config.max_seq_length,
                self.tokenizer
            )
            loss, confidence = self.train_step(
                batch=formatted_batch,
                scaffold_context=self.scaffold_context,
                grad_clip=True,
                loss_weight_fn=self.loss_weight_fn,
                dry_run=False,
                memory_check=self.memory_check
            )

            self.gestation_total_loss += loss if loss is not None else 0.0
            self.gestation_steps += 1 if loss is not None else 0
            self.gestation_progress += 1
            if self.gestation_steps % 5 == 0 and self.gestation_steps > 0:
                print(f"Gestation progress: {self.gestation_progress}/{len(self.gestation_batch)}, loss: {self.gestation_total_loss / self.gestation_steps:.4f}")
            return True

        avg_loss = self.gestation_total_loss / self.gestation_steps if self.gestation_steps > 0 else 0
        print(f"\nGestation complete: {len(self.gestation_batch)}/{len(self.gestation_batch)}, loss: {avg_loss:.4f}")
        self._reset_gestation_state()
        return False

    def _should_gestate(self, log_entries: List[dict]) -> bool:
        """Determine if gestation should proceed."""
        if len(log_entries) < self.config.sleep_log_min:
            print(f"Gestation check: Log size {len(log_entries)} < {self.config.sleep_log_min}. No gestation.")
            return False
        avg_confidence = self.state.sleep_confidence_sum / self.state.sleep_confidence_count if self.state.sleep_confidence_count > 0 else 0.5
        should_gestate = (len(log_entries) >= self.config.sleep_log_min) and (self.state.sleep_confidence_count == 0 or avg_confidence > self.config.sleep_conf_threshold)
        print(f"Gestation check: Confidence {avg_confidence:.2f} > {self.config.sleep_conf_threshold}, Log {len(log_entries)} >= {self.config.sleep_log_min}, Gestate: {should_gestate}")
        return should_gestate

    def _reset_gestation_state(self):
        """Reset gestation-related state."""
        self.is_gestating = False
        self.gestation_progress = 0
        self.gestation_batch = []
        self.gestation_total_loss = 0.0
        self.gestation_steps = 0

    def _should_dream(self):
        """Determine if dreaming should occur."""
        if not self.state or not self.config.dream_temperament_on:
            return False
        swing_dream = (
            len(self.state.confidence_history) >= self.config.confidence_history_maxlen and
            torch.var(torch.tensor(list(self.state.confidence_history))).item() > self.config.dream_swing_var
        )
        lifecycle_dream = (
            abs(self.state.temperament_score - self.state.last_temperament_score) > self.config.dream_lifecycle_delta
        )
        history_dream = False
        if len(self.state.temperament_history) >= self.config.temperament_history_maxlen:
            trend = torch.tensor(list(self.state.temperament_history)).mean().item() - self.state.temperament_history[0]
            history_dream = abs(trend) > 0.3
        should_dream = swing_dream or lifecycle_dream or history_dream
        self.logger({
            "event": "dream_check",
            "swing_dream": swing_dream,
            "lifecycle_dream": lifecycle_dream,
            "history_dream": history_dream,
            "should_dream": should_dream,
            "timestamp": time.time()
        })
        return should_dream

    def _dream(self):
        """Generate dream prompts and update dream memory."""
        print("--- Trainer Dreaming ---")
        log_entries = self.logger.read() if hasattr(self.logger, "read") else []
        if not log_entries:
            print("No memories to dream on.")
            self.logger({
                "event": "dream_failed",
                "reason": "No log entries",
                "timestamp": time.time()
            })
            return

        # Select base prompt
        last_prompt = (
            self.state.history.messages[-1]["prompt"]
            if self.state and hasattr(self.state, "history") and self.state.history.messages
            else random.choice(log_entries)["prompt"]
        )
        prompt_inputs = self.tokenizer(
            last_prompt,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.config.max_seq_length
        ).to(self.device)
        with torch.no_grad():
            prompt_hidden = self.model(**prompt_inputs, output_hidden_states=True).hidden_states[-1].mean(dim=1)

        with self.memory_lock:
            if hasattr(self.state, "last_prompt_embedding"):
                self.state.last_prompt_embedding = prompt_hidden.detach().cpu()

        # Choose dream entry
        if self.config.enable_prompt_driven_dreams:
            weights = []
            for i, entry in enumerate(log_entries):
                if "prompt" not in entry:
                    continue
                log_inputs = self.tokenizer(
                    entry["prompt"],
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=self.config.max_seq_length
                ).to(self.device)
                log_hidden = self.model(**log_inputs, output_hidden_states=True).hidden_states[-1].mean(dim=1)
                similarity = F.cosine_similarity(prompt_hidden, log_hidden).item()
                recency = (i + 1) / len(log_entries)
                weight = self.config.dream_prompt_weight * similarity + (1 - self.config.dream_prompt_weight) * recency
                weights.append(weight)
            dream_entry = random.choices(log_entries, weights=weights, k=1)[0] if weights else random.choice(log_entries)
        else:
            dream_entry = random.choice(log_entries)
        dream_prompt = dream_entry["prompt"]

        # Apply noise based on temperament and novelty
        is_novel = dream_prompt not in getattr(self.state, "seen_prompts", set())
        noise_scale = (
            self.config.dream_noise_scale +
            (self.config.temp_melancholy_noise if getattr(self.state, "temperament_score", 0) <= -0.5 else 0) +
            (self.config.dream_novelty_boost if is_novel else 0)
        )
        noise_scale = min(noise_scale, 0.1)

        with torch.no_grad():
            inputs = self.tokenizer(
                dream_prompt,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self.config.max_seq_length
            ).to(self.device)
            hidden_states = self.model(**inputs, output_hidden_states=True).hidden_states[-1]
            noise = torch.randn_like(hidden_states) * noise_scale
            dream_layer = (hidden_states.mean(dim=1) + noise).detach().cpu()

        # Update dream memory
        with self.memory_lock:
            dream_memory = getattr(self.state, "dream_memory", deque(maxlen=100))
            for i in range(len(dream_memory)):
                tensor, weight = dream_memory[i]
                dream_memory[i] = (tensor, weight * self.config.dream_memory_decay)

            dream_memory = deque(
                [(t, w) for t, w in dream_memory if w >= self.config.dream_prune_threshold],
                maxlen=getattr(self.state, "dream_memory_maxlen", 100)
            )
            weight = self.config.dream_memory_weight
            if is_novel:
                weight += self.config.dream_novelty_boost
            dream_memory.append((dream_layer, min(weight, 1.5)))
            self.state.dream_memory = dream_memory

            print(f"Added dream memory (weight: {weight:.2f}), Total memories: {len(dream_memory)}")

        self.logger({
            "event": "dream_completed",
            "prompt": dream_prompt,
            "novelty": is_novel,
            "memory_count": len(dream_memory),
            "timestamp": time.time()
        })
        print(f"Dreaming from prompt similarity: {max(weights) if weights else 0:.2f}, novelty boost: {self.config.dream_novelty_boost if is_novel else 0:.3f}, dream count: {len(dream_memory)}")
        print("--- Dream Concluded ---")

    def sleep_train(self, log_entries: List[dict]):
        """Perform sleep training on log_entries."""
        if not self.config.enable_sleep_training or not self._should_gestate(log_entries):
            return
        print("\n--- Sleep Training Initiated ---")
        batch = [
            {"prompt": entry["prompt"], "completion": entry["response"]}
            for entry in log_entries if "prompt" in entry and "response" in entry
        ]
        if not batch:
            print("No valid training data in logs.")
            return

        if self.config.enable_dreaming and self._should_dream():
            self._dream()

        original_epochs = self.config.max_epochs
        self.config.max_epochs = 1
        self.train(
            train_data=batch,
            valid_data=None,
            scaffold_provider=None
        )
        self.config.max_epochs = original_epochs

        self.data_exposure += sum(
            len(entry["prompt"]) + len(entry["response"])
            for entry in batch
        )
        self._reset_sleep_state()
        print("Sleep Training Complete")

    def _reset_sleep_state(self):
        """Reset sleep-related state."""
        self.sleep_progress = 0
        self.sleep_batch = []
        self.sleep_total_loss = 0.0
        self.sleep_steps = 0

    def cleanup(self):
        """Clean up trainer resources."""
        try:
            self._reset_gestation_state()
            self._reset_sleep_state()
            self.optimizer.zero_grad(set_to_none=True)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            self.logger({
                "event": "trainer_cleanup",
                "timestamp": time.time(),
                "details": "Trainer resources cleared"
            })
            print("Trainer cleanup completed")
        except Exception as e:
            self.logger({
                "event": "trainer_cleanup_failed",
                "error": str(e),
                "timestamp": time.time()
            })
            print(f"Trainer cleanup failed: {e}")

    def train(
        self,
        train_data: Union[List[dict], 'DataLoader'],
        valid_data: Optional[Union[List[dict], 'DataLoader']] = None,
        scaffold_provider: Optional[Callable] = None,
        resume_checkpoint: Optional[str] = None
    ):
        """Run training loop over epochs."""
        if resume_checkpoint:
            self.load_checkpoint(resume_checkpoint)

        if isinstance(train_data, (list, tuple)):
            train_iter = [train_data[i:i + self.config.batch_size] for i in range(0, len(train_data), self.config.batch_size)]
        else:
            train_iter = train_data

        if valid_data and isinstance(valid_data, (list, tuple)):
            valid_iter = [valid_data[i:i + self.config.batch_size] for i in range(0, len(valid_data), self.config.batch_size)]
        else:
            valid_iter = valid_data

        for epoch in range(self.config.max_epochs):
            self.model.train()
            epoch_loss = 0.0
            steps_in_epoch = 0

            for batch in train_iter:
                if isinstance(batch, (list, tuple)):
                    batch = collate_batch(batch, self.tokenizer.pad_token_id, self.config.max_seq_length, self.tokenizer)
                batch = {k: v.to(self.device) if isinstance(v, torch.Tensor) else v for k, v in batch.items()}

                scaffold_context = scaffold_provider(batch) if scaffold_provider else self.scaffold_context
                if scaffold_context is not None:
                    scaffold_context = scaffold_context.to(self.device)

                loss, confidence = self.train_step(
                    batch,
                    scaffold_context=scaffold_context,
                    grad_clip=True,
                    loss_weight_fn=self.loss_weight_fn,
                    dry_run=False,
                    memory_check=self.memory_check
                )

                if loss is not None:
                    epoch_loss += loss
                    steps_in_epoch += 1
                    self.update_exposure(batch["prompt"], self.state.temperament_score if self.state else 0.0)
                    self.logger({
                        "event": "train_step",
                        "epoch": epoch + 1,
                        "step": self.global_step,
                        "loss": loss,
                        "confidence": confidence,
                        "data_exposure": self.data_exposure,
                        "timestamp": time.time()
                    })

                if valid_iter and self.config.validate_every_n_steps and self.global_step % self.config.validate_every_n_steps == 0:
                    valid_loss, metrics = self.validate(valid_iter, scaffold_provider)
                    self.logger({
                        "event": "validation",
                        "epoch": epoch + 1,
                        "step": self.global_step,
                        "loss": valid_loss,
                        "metrics": metrics,
                        "timestamp": time.time()
                    })
                    if valid_loss < self.best_valid_loss:
                        self.best_valid_loss = valid_loss
                        self.patience = 0
                        self.save_checkpoint(self.global_step, suffix="best")
                    else:
                        self.patience += 1
                    if self.should_stop():
                        self.logger({
                            "event": "early_stopping",
                            "epoch": epoch + 1,
                            "step": self.global_step,
                            "valid_loss": valid_loss,
                            "timestamp": time.time()
                        })
                        print(f"Early stopping at epoch {epoch + 1}, step {self.global_step}")
                        return

            avg_epoch_loss = epoch_loss / steps_in_epoch if steps_in_epoch > 0 else 0.0
            self.logger({
                "event": "epoch_end",
                "epoch": epoch + 1,
                "avg_loss": avg_epoch_loss,
                "data_exposure": self.data_exposure,
                "timestamp": time.time()
            })
            print(f"Epoch {epoch + 1}/{self.config.max_epochs}: Avg Loss = {avg_epoch_loss:.4f}")
