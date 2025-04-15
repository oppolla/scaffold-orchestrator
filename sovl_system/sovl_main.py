from typing import Optional, Any, List, Dict, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
from peft import LoraConfig, get_peft_model, TaskType
import time
import random
import bitsandbytes as bnb
import json
import contextlib
from collections import deque
import traceback
import os
from threading import Lock
from sovl_curiosity import CuriosityManager, CuriosityState
from sovl_logger import Logger
from sovl_io import load_training_data, validate_quantization_mode, InsufficientDataError
from sovl_state import SOVLState, ConversationHistory
from sovl_trainer import TrainingConfig, SOVLTrainer
from sovl_config import ConfigManager
from sovl_scaffold import CrossAttentionInjector, ScaffoldManager, CrossAttentionLayer, ScaffoldTokenMapper
from sovl_processor import LogitsProcessor
from sovl_utils import (
    calculate_confidence,
    detect_repetitions,
)
from sovl_temperament import TemperamentConfig, TemperamentSystem
from sovl_memory import MemoryManager
from sovl_manager import ModelManager
from sovl_generation import GenerationManager
from sovl_tuner import SOVLTuner
from sovl_error import ErrorHandler
from sovl_state_manager import StateManager
from sovl_logging import LoggingManager
import logging
from sovl_training_cycle import TrainingCycleManager
from sovl_plugin import PluginManager
import sys

# Remove sovl_conductor import and use TYPE_CHECKING for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sovl_conductor import SOVLOrchestrator

def calculate_confidence_score(logits, generated_ids) -> float:
    """Calculate confidence score for generated tokens."""
    try:
        processor = LogitsProcessor(logits)
        return processor.calculate_confidence(generated_ids)
    except Exception as e:
        print(f"Confidence score error: {str(e)} - Using default 0.5")
        return 0.5

class SystemContext:
    """Holds shared resources like logger, device, and config manager."""
    
    def __init__(self, config_path: str):
        """
        Initialize system context with configuration and core components.
        
        Initialization order:
        1. Initialize device
        2. Create temporary logger for ConfigManager initialization
        3. Initialize ConfigManager with temporary logger
        4. Initialize LoggingManager with ConfigManager
        5. Update ConfigManager's logger with LoggingManager's logger
        6. Initialize remaining components
        
        Args:
            config_path: Path to the configuration file
            
        Raises:
            SystemInitializationError: If any critical component fails to initialize
        """
        # Initialize device first
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        try:
            # Create temporary logger with basic configuration
            temp_logger = Logger(LoggerConfig(
                log_file="sovl_init.log",
                max_size_mb=1,
                compress_old=False,
                max_in_memory_logs=100
            ))
            
            # Initialize ConfigManager with temporary logger
            self.config_manager = ConfigManager(config_path, temp_logger)
            
            # Get logging configuration from ConfigManager
            log_dir = self.config_manager.get("logging_config.log_dir", "logs")
            system_log_file = self.config_manager.get("logging_config.log_file", "sovl_logs.jsonl")
            debug_log_file = self.config_manager.get("logging_config.debug_log_file", "sovl_debug.log")
            
            # Initialize LoggingManager with ConfigManager and aligned configuration
            self.logging_manager = LoggingManager(
                config_manager=self.config_manager,
                log_dir=log_dir,
                system_log_file=system_log_file,
                debug_log_file=debug_log_file
            )
            
            # Get both system and debug logger instances
            self.logger = self.logging_manager.get_logger("system")
            self.debug_logger = self.logging_manager.get_logger("debug")
            
            # Update ConfigManager's logger with the proper logger
            self.config_manager.logger = self.logger
            
            # Log successful initialization
            self.logger.record_event(
                event_type="system_initialization",
                message="System context initialized successfully",
                level="info",
                additional_info={
                    "device": str(self.device),
                    "config_path": config_path,
                    "cuda_available": torch.cuda.is_available(),
                    "cuda_device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
                    "log_dir": log_dir,
                    "system_log_file": system_log_file,
                    "debug_log_file": debug_log_file
                }
            )
            
            # Log debug information
            self.debug_logger.record_event(
                event_type="system_initialization_debug",
                message="System context initialized with debug logging enabled",
                level="debug",
                additional_info={
                    "device": str(self.device),
                    "config_path": config_path,
                    "cuda_available": torch.cuda.is_available(),
                    "cuda_device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
                    "config_hash": self.config_manager._last_config_hash,
                    "log_dir": log_dir,
                    "system_log_file": system_log_file,
                    "debug_log_file": debug_log_file
                }
            )
            
        except Exception as e:
            # Log initialization failure to both temporary and stderr
            error_msg = f"Failed to initialize system context: {str(e)}"
            stack_trace = traceback.format_exc()
            
            # Try to log to temporary logger if it exists
            if 'temp_logger' in locals():
                temp_logger.record_event(
                    event_type="system_initialization_error",
                    message=error_msg,
                    level="error",
                    additional_info={
                        "stack_trace": stack_trace,
                        "config_path": config_path
                    }
                )
            
            # Always print to stderr
            print(f"CRITICAL: {error_msg}", file=sys.stderr)
            print(f"Stack trace:\n{stack_trace}", file=sys.stderr)
            
            # Raise a custom exception with full context
            raise SystemInitializationError(
                message=error_msg,
                config_path=config_path,
                stack_trace=stack_trace
            ) from e

class SystemInitializationError(Exception):
    """Custom exception for system initialization failures."""
    
    def __init__(self, message: str, config_path: str, stack_trace: str):
        self.message = message
        self.config_path = config_path
        self.stack_trace = stack_trace
        super().__init__(f"{message}\nConfig path: {config_path}\nStack trace:\n{stack_trace}")

class ConfigHandler:
    """Manages configuration validation and access."""
    def __init__(self, context: SystemContext):
        self.context = context
        self.core_config = context.config_manager.get_section("core_config")
        self.training_config = context.config_manager.get_section("training_config")
        self.curiosity_config = context.config_manager.get_section("curiosity_config")
        self.cross_attn_config = context.config_manager.get_section("cross_attn_config")
        self.controls_config = context.config_manager.get_section("controls_config")
        self.lora_config = context.config_manager.get_section("lora_config")
        
        # Validate all configurations
        self._validate_all_configs()

    def _validate_all_configs(self):
        """Validate and synchronize all configurations."""
        self._validate_controls_configs()
        self._validate_curiosity_configs()
        self._validate_temperament_configs()
        
        # Log final configuration state
        self.context.logger.record_event(
            event_type="config_validation",
            message="All configurations validated successfully",
            level="info",
            additional_info={
                "curiosity_config": self.curiosity_config,
                "controls_config": {k: v for k, v in self.controls_config.items() 
                                 if k.startswith(("curiosity_", "temp_"))}
            }
        )
        
    def _validate_controls_configs(self):
        """Validate controls configuration section."""
        try:
            controls_config = self.context.config_manager.get_section("controls_config", {})
            
            # Define error handling configuration keys with defaults and valid ranges
            error_handling_keys = {
                "error_handling.max_history_per_error": (10, (5, 20)),
                "error_handling.critical_threshold": (5, (3, 10)),
                "error_handling.warning_threshold": (10, (5, 15)),
                "error_handling.retry_attempts": (3, (1, 5)),
                "error_handling.retry_delay": (1.0, (0.5, 5.0)),
            }
            
            # Validate error handling configuration
            for key, (default, (min_val, max_val)) in error_handling_keys.items():
                if key not in controls_config:
                    controls_config[key] = default
                    self.context.logger.record_event(
                        event_type="config_missing_key",
                        message=f"Added missing error_handling key {key} with default {default}",
                        level="warning"
                    )
                value = controls_config[key]
                if not (min_val <= value <= max_val):
                    controls_config[key] = default
                    self.context.logger.record_event(
                        event_type="config_invalid_value",
                        message=f"Invalid {key}: {value}. Reset to default {default}",
                        level="warning"
                    )
            
            # Update controls config with validated values
            self.context.config_manager.set_section("controls_config", controls_config)
            
            # Log successful validation
            self.context.logger.record_event(
                event_type="controls_config_validated",
                message="Controls configuration validated successfully",
                level="info",
                additional_info={"error_handling_config": {
                    key: controls_config[key] for key in error_handling_keys.keys()
                }}
            )
            
        except Exception as e:
            self.context.logger.record_event(
                event_type="controls_config_validation_failed",
                message=f"Failed to validate controls configuration: {str(e)}",
                level="error",
                stack_trace=traceback.format_exc()
            )
            raise

    def _validate_curiosity_configs(self):
        """Validate and synchronize curiosity-related configurations."""
        try:
            # Define required keys, default values, and validation ranges
            required_keys = {
                "queue_maxlen": (10, (1, 50)),
                "weight_ignorance": (0.7, (0.0, 1.0)),
                "weight_novelty": (0.3, (0.0, 1.0)),
                "metrics_maxlen": (1000, (100, 10000)),
                "novelty_threshold_spontaneous": (0.9, (0.5, 1.0)),
                "novelty_threshold_response": (0.8, (0.5, 1.0)),
                "pressure_threshold": (0.7, (0.5, 0.9)),
                "pressure_drop": (0.3, (0.1, 0.5)),
                "silence_threshold": (20.0, (5.0, 60.0)),
                "question_cooldown": (60.0, (30.0, 120.0)),
                "max_new_tokens": (8, (5, 12)),
                "base_temperature": (1.1, (0.5, 1.5)),
                "temperament_influence": (0.4, (0.1, 0.6)),
                "top_k": (30, (10, 50)),
                "enable_curiosity": (True, None)
            }
            
            # Check for missing keys in curiosity_config
            missing_keys = [key for key in required_keys if key not in self.curiosity_config]
            if missing_keys:
                self.context.logger.record_event(
                    event_type="config_validation",
                    message="Missing keys in curiosity_config",
                    level="warning",
                    additional_info={
                        "missing_keys": missing_keys,
                        "default_values": {k: required_keys[k][0] for k in missing_keys}
                    }
                )
                # Add missing keys with default values
                for key in missing_keys:
                    self.curiosity_config[key] = required_keys[key][0]
            
            # Validate value ranges
            invalid_values = []
            for key, (default, valid_range) in required_keys.items():
                if valid_range is None:  # Skip validation for boolean values
                    continue
                value = self.curiosity_config[key]
                if not (valid_range[0] <= value <= valid_range[1]):
                    invalid_values.append({
                        "key": key,
                        "value": value,
                        "valid_range": valid_range
                    })
                    # Reset to default value
                    self.curiosity_config[key] = default
            
            if invalid_values:
                self.context.logger.record_event(
                    event_type="config_validation",
                    message="Invalid values in curiosity_config",
                    level="warning",
                    additional_info={"invalid_values": invalid_values}
                )
            
            # Check for mismatches between curiosity_config and controls_config
            controls_mapping = {
                "curiosity_queue_maxlen": "queue_maxlen",
                "curiosity_weight_ignorance": "weight_ignorance",
                "curiosity_weight_novelty": "weight_novelty",
                "curiosity_metrics_maxlen": "metrics_maxlen",
                "curiosity_novelty_threshold_spontaneous": "novelty_threshold_spontaneous",
                "curiosity_novelty_threshold_response": "novelty_threshold_response",
                "curiosity_pressure_threshold": "pressure_threshold",
                "curiosity_pressure_drop": "pressure_drop",
                "curiosity_silence_threshold": "silence_threshold",
                "curiosity_question_cooldown": "question_cooldown",
                "curiosity_max_new_tokens": "max_new_tokens",
                "curiosity_base_temperature": "base_temperature",
                "curiosity_temperament_influence": "temperament_influence",
                "curiosity_top_k": "top_k",
                "enable_curiosity": "enable_curiosity"
            }
            
            mismatches = []
            for controls_key, curiosity_key in controls_mapping.items():
                if controls_key in self.controls_config and curiosity_key in self.curiosity_config:
                    if self.controls_config[controls_key] != self.curiosity_config[curiosity_key]:
                        mismatches.append({
                            "controls_key": controls_key,
                            "curiosity_key": curiosity_key,
                            "controls_value": self.controls_config[controls_key],
                            "curiosity_value": self.curiosity_config[curiosity_key]
                        })
                        # Align controls_config with curiosity_config
                        self.controls_config[controls_key] = self.curiosity_config[curiosity_key]
            
            if mismatches:
                self.context.logger.record_event(
                    event_type="config_validation",
                    message="Configuration mismatches between controls_config and curiosity_config",
                    level="warning",
                    additional_info={"mismatches": mismatches}
                )
            
            # Log final configuration state
            self.context.logger.record_event(
                event_type="config_validation",
                message="Curiosity configuration validation complete",
                level="info",
                additional_info={
                    "curiosity_config": self.curiosity_config,
                    "controls_config": {k: v for k, v in self.controls_config.items() 
                                     if k.startswith(("curiosity_", "enable_curiosity"))}
                }
            )
            
        except Exception as e:
            self.context.logger.record_event(
                event_type="config_validation_error",
                message="Failed to validate curiosity configurations",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def _validate_temperament_configs(self):
        """Validate and synchronize temperament-related configurations."""
        try:
            # Define required keys and their default values
            required_keys = {
                "temp_eager_threshold": 0.8,
                "temp_sluggish_threshold": 0.6,
                "temp_mood_influence": 0.0,
                "temp_curiosity_boost": 0.2,
                "temp_restless_drop": 0.1,
                "temp_melancholy_noise": 0.02,
                "temp_smoothing_factor": 0.5,
                "temperament_decay_rate": 0.9,
                "enable_temperament": True
            }
            
            # Check for missing keys in controls_config
            missing_keys = [key for key in required_keys if key not in self.controls_config]
            if missing_keys:
                self.context.logger.record_event(
                    event_type="config_validation",
                    message="Missing keys in controls_config for temperament",
                    level="warning",
                    additional_info={
                        "missing_keys": missing_keys,
                        "default_values": {k: required_keys[k] for k in missing_keys}
                    }
                )
                # Add missing keys with default values
                for key in missing_keys:
                    self.controls_config[key] = required_keys[key]
            
            # Validate value ranges
            value_ranges = {
                "temp_eager_threshold": (0.7, 0.9),
                "temp_sluggish_threshold": (0.4, 0.6),
                "temp_mood_influence": (0.0, 1.0),
                "temp_curiosity_boost": (0.0, 0.5),
                "temp_restless_drop": (0.0, 0.5),
                "temp_melancholy_noise": (0.0, 0.05),
                "temp_smoothing_factor": (0.0, 1.0),
                "temperament_decay_rate": (0.0, 1.0)
            }
            
            invalid_values = []
            for key, (min_val, max_val) in value_ranges.items():
                if key in self.controls_config:
                    value = self.controls_config[key]
                    if not (min_val <= value <= max_val):
                        invalid_values.append({
                            "key": key,
                            "value": value,
                            "range": (min_val, max_val)
                        })
            
            if invalid_values:
                self.context.logger.record_event(
                    event_type="config_validation",
                    message="Invalid values in temperament configurations",
                    level="warning",
                    additional_info={"invalid_values": invalid_values}
                )
                # Reset invalid values to defaults
                for invalid in invalid_values:
                    self.controls_config[invalid["key"]] = required_keys[invalid["key"]]
            
        except Exception as e:
            self.context.logger.record_event(
                event_type="config_validation_error",
                message="Failed to validate temperament configurations",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def validate(self, model_config: Any = None) -> bool:
        """
        Validate the configuration, propagating any validation errors.
        
        Args:
            model_config: Optional model configuration for layer validation
            
        Returns:
            bool: True if validation succeeds
            
        Raises:
            ValueError: If configuration validation fails
        """
        try:
            # First validate basic configuration without model context
            self.context.config_manager.validate_or_raise(None)
            return True
        except ValueError as e:
            # Log the error and re-raise to prevent silent failures
            self.context.logger.record_event(
                event_type="config_validation_error",
                message="Configuration validation failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def validate_with_model(self, model_config: Any) -> bool:
        """
        Validate configuration with model-specific checks.
        
        Args:
            model_config: Model configuration for layer validation
            
        Returns:
            bool: True if validation succeeds
            
        Raises:
            ValueError: If configuration validation fails
        """
        try:
            # Validate with model context for layer-specific checks
            self.context.config_manager.validate_or_raise(model_config)
            return True
        except ValueError as e:
            # Log the error and re-raise to prevent silent failures
            self.context.logger.record_event(
                event_type="config_validation_error",
                message="Model-specific configuration validation failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

class ModelLoader:
    """Handles model loading and token mapping."""
    
    def __init__(self, context: SystemContext, config_handler: ConfigHandler):
        self.context = context
        self.config_handler = config_handler
        self.base_model = None
        self.scaffold_model = None
        self.base_tokenizer = None
        self.scaffold_tokenizer = None
        self.token_mapper = None
        self._initialize_components()
        
    def _initialize_components(self) -> None:
        """Initialize model components and token mapper."""
        try:
            # Load base model and tokenizer
            self.base_model, self.base_tokenizer = self._load_base_model()
            
            # Initialize token mapper
            self.token_mapper = ScaffoldTokenMapper(
                base_tokenizer=self.base_tokenizer,
                scaffold_tokenizer=self.scaffold_tokenizer,
                logger=self.context.logger
            )
            
            # Log initialization
            self.context.logger.record_event(
                event_type="model_loader_initialized",
                message="Model loader initialized successfully",
                level="info",
                additional_info={
                    "base_model_device": str(self.base_model.device),
                    "scaffold_model_device": str(self.scaffold_model.device) if self.scaffold_model else None,
                    "base_vocab_size": len(self.base_tokenizer),
                    "scaffold_vocab_size": len(self.scaffold_tokenizer) if self.scaffold_tokenizer else None
                }
            )
        except Exception as e:
            self.context.logger.record_event(
                event_type="model_loader_init_failed",
                message="Failed to initialize model loader",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def sync_token_map(self, state: SOVLState) -> None:
        """
        Synchronize token maps between ScaffoldTokenMapper and SOVLState.
        
        Args:
            state: Current SOVLState instance
            
        Raises:
            ValueError: If token map validation fails
        """
        try:
            # Validate state token map
            self._validate_token_map(state.token_map)
            
            # Update token mapper with state's token map
            self.token_mapper.update_token_map(state.token_map)
            
            # Log synchronization
            self.context.logger.record_event(
                event_type="token_map_synced",
                message="Token maps synchronized successfully",
                level="info",
                additional_info={
                    "token_map_size": len(state.token_map),
                    "base_vocab_size": len(self.base_tokenizer),
                    "scaffold_vocab_size": len(self.scaffold_tokenizer) if self.scaffold_tokenizer else None,
                    "state_hash": state.state_hash
                }
            )
        except Exception as e:
            self.context.logger.record_event(
                event_type="token_map_sync_failed",
                message="Failed to synchronize token maps",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc(),
                    "state_hash": state.state_hash
                }
            )
            raise ValueError(f"Token map synchronization failed: {str(e)}")

    def _validate_token_map(self, token_map: Dict[int, List[int]]) -> None:
        """
        Validate token map against tokenizer vocabularies.
        
        Args:
            token_map: Token mapping dictionary to validate
            
        Raises:
            ValueError: If validation fails
        """
        try:
            base_vocab_size = len(self.base_tokenizer)
            scaffold_vocab_size = len(self.scaffold_tokenizer) if self.scaffold_tokenizer else None
            
            # Validate base token IDs
            for base_id in token_map.keys():
                if not 0 <= base_id < base_vocab_size:
                    raise ValueError(f"Invalid base token ID {base_id} exceeds vocab size {base_vocab_size}")
            
            # Validate scaffold token IDs
            if scaffold_vocab_size is not None:
                for scaffold_ids in token_map.values():
                    for scaffold_id in scaffold_ids:
                        if not 0 <= scaffold_id < scaffold_vocab_size:
                            raise ValueError(f"Invalid scaffold token ID {scaffold_id} exceeds vocab size {scaffold_vocab_size}")
            
            # Log validation
            self.context.logger.record_event(
                event_type="token_map_validated",
                message="Token map validation successful",
                level="info",
                additional_info={
                    "token_map_size": len(token_map),
                    "base_vocab_size": base_vocab_size,
                    "scaffold_vocab_size": scaffold_vocab_size
                }
            )
        except Exception as e:
            self.context.logger.record_event(
                event_type="token_map_validation_failed",
                message="Token map validation failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise ValueError(f"Token map validation failed: {str(e)}")

    def inject_cross_attention(self) -> None:
        """Inject cross-attention layers into the scaffold model."""
        try:
            # Ensure models are on correct device
            self.base_model = self.base_model.to(self.context.device)
            self.scaffold_model = self.scaffold_model.to(self.context.device)
            
            # Sync token maps before injection
            if hasattr(self.context, 'state_tracker'):
                self.sync_token_map(self.context.state_tracker.state)
            
            # Inject cross-attention layers
            self._inject_cross_attention_layers()
            
            # Log successful injection
            self.context.logger.record_event(
                event_type="cross_attention_injected",
                message="Cross-attention layers injected successfully",
                level="info",
                additional_info={
                    "base_model_device": str(self.base_model.device),
                    "scaffold_model_device": str(self.scaffold_model.device),
                    "token_map_size": len(self.token_mapper.token_map) if self.token_mapper else None
                }
            )
        except Exception as e:
            self.context.logger.record_event(
                event_type="cross_attention_injection_failed",
                message="Failed to inject cross-attention layers",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

class StateTracker:
    """Tracks and manages the system's state."""
    
    def __init__(self, context: SystemContext, config_handler: ConfigHandler):
        """Initialize the state tracker with context and configuration."""
        self.context = context
        self.config_handler = config_handler
        self.state_manager = StateManager(
            config_manager=context.config_manager,
            logger=context.logger,
            device=context.device
        )
        # Auto-initialize state
        self.state = self.state_manager.initialize_state()
        self._log_event("state_tracker_initialized", {
            "state_hash": self.state.state_hash,
            "conversation_id": self.state.history.conversation_id
        })
        
    def _log_event(self, event: str, data: Optional[Dict] = None) -> None:
        """Log an event with standardized fields."""
        self.context.logger.record_event(
            event_type=f"state_{event}",
            message=f"State event: {event}",
            level="info",
            additional_info=data
        )
        
    def _log_error(self, message: str, error: Exception) -> None:
        """Log an error with stack trace."""
        self.context.logger.log_error(
            error_msg=message,
            error_type="state_error",
            stack_trace=traceback.format_exc(),
            additional_info={"error": str(error)}
        )
        
    def load_state(self) -> None:
        """Load state from file or initialize new state if not found."""
        try:
            # Only load if state path exists, otherwise keep initialized state
            state_path = self.config_handler.config_manager.get("state_config.state_path")
            if state_path and os.path.exists(state_path):
                with self.state.lock:
                    self.state = self.state_manager.load_state(state_path)
                    self._log_event("state_loaded", {
                        "state_path": state_path,
                        "state_hash": self.state.state_hash,
                        "conversation_id": self.state.history.conversation_id
                    })
            else:
                self._log_event("state_initialized", {
                    "state_hash": self.state.state_hash,
                    "conversation_id": self.state.history.conversation_id
                })
        except Exception as e:
            self._log_error("State loading failed", e)
            raise StateError(f"Failed to load state: {str(e)}")
            
    def save_state(self) -> None:
        """Save current state to file."""
        try:
            state_path = self.config_handler.config_manager.get("state_config.state_path")
            if state_path:
                with self.state.lock:
                    self.state_manager.save_state(state_path)
                    self._log_event("state_saved", {
                        "state_path": state_path,
                        "state_hash": self.state.state_hash
                    })
        except Exception as e:
            self._log_error("State saving failed", e)
            raise StateError(f"Failed to save state: {str(e)}")
            
    def get_state(self) -> SOVLState:
        """Get the current state instance."""
        if self.state is None:
            raise StateError("State not initialized")
        return self.state

class ErrorManager:
    """Manages error handling and recovery for the SOVL system."""
    
    def __init__(self, context: SystemContext, state_tracker: StateTracker):
        """Initialize the error manager with context and state tracker."""
        self.context = context
        self.state_tracker = state_tracker
        self.error_handler = ErrorHandler(
            config=context.config_manager.get_section("error_handling"),
            logger=context.logger
        )

    def handle_generation_error(self, error: Exception, prompt: str) -> str:
        """Handle generation errors and attempt recovery."""
        state = self.state_tracker.get_state()
        try:
            # First attempt standard error handling
            self.error_handler.handle_generation_error(error, prompt, state)
            
            # If error persists, attempt recovery
            error_key = f"generation:generate:{type(error).__name__}"
            if self.error_handler.error_counts[error_key] >= self.error_handler.severity_thresholds["critical"]:
                self.error_handler._recover_generation(error_key)
                self.context.logger.record_event(
                    event_type="generation_recovery_triggered",
                    message="Triggered generation recovery after error",
                    level="info"
                )
                
            return "An error occurred during generation. The system has attempted recovery."
            
        except Exception as e:
            self.context.logger.record_event(
                event_type="generation_error_handling_failed",
                message=f"Failed to handle generation error: {str(e)}",
                level="error",
                stack_trace=traceback.format_exc()
            )
            return "A critical error occurred during generation."

    def handle_training_error(self, error: Exception, batch_size: int) -> None:
        """Handle training errors and attempt recovery."""
        state = self.state_tracker.get_state()
        try:
            # First attempt standard error handling
            self.error_handler.handle_training_error(error, batch_size, state)
            
            # If error persists, attempt recovery
            error_key = f"training:train_step:{type(error).__name__}"
            if self.error_handler.error_counts[error_key] >= self.error_handler.severity_thresholds["critical"]:
                self.error_handler._recover_training(error_key)
                self.context.logger.record_event(
                    event_type="training_recovery_triggered",
                    message="Triggered training recovery after error",
                    level="info"
                )
                
        except Exception as e:
            self.context.logger.record_event(
                event_type="training_error_handling_failed",
                message=f"Failed to handle training error: {str(e)}",
                level="error",
                stack_trace=traceback.format_exc()
            )

    def handle_curiosity_error(self, error: Exception, event_type: str) -> None:
        """Handle curiosity errors and attempt recovery."""
        state = self.state_tracker.get_state()
        try:
            # First attempt standard error handling
            self.error_handler.handle_curiosity_error(error, event_type, state)
            
            # If error persists, attempt recovery
            error_key = f"curiosity:{event_type}:{type(error).__name__}"
            if self.error_handler.error_counts[error_key] >= self.error_handler.severity_thresholds["critical"]:
                self.error_handler._recover_curiosity(error_key)
                self.context.logger.record_event(
                    event_type="curiosity_recovery_triggered",
                    message="Triggered curiosity recovery after error",
                    level="info"
                )
                
        except Exception as e:
            self.context.logger.record_event(
                event_type="curiosity_error_handling_failed",
                message=f"Failed to handle curiosity error: {str(e)}",
                level="error",
                stack_trace=traceback.format_exc()
            )

    def handle_memory_error(self, error: Exception, model_size: int) -> bool:
        """Handle memory errors and attempt recovery."""
        state = self.state_tracker.get_state()
        try:
            # First attempt standard error handling
            self.error_handler.handle_memory_error(error, model_size, state)
            
            # If error persists, attempt recovery
            error_key = f"memory:check_health:{type(error).__name__}"
            if self.error_handler.error_counts[error_key] >= self.error_handler.severity_thresholds["critical"]:
                self.error_handler._recover_memory(error_key)
                self.context.logger.record_event(
                    event_type="memory_recovery_triggered",
                    message="Triggered memory recovery after error",
                    level="info"
                )
                
            # Check if system is healthy after recovery
            return self.error_handler.error_counts[error_key] < self.error_handler.severity_thresholds["critical"]
            
        except Exception as e:
            self.context.logger.record_event(
                event_type="memory_error_handling_failed",
                message=f"Failed to handle memory error: {str(e)}",
                level="error",
                stack_trace=traceback.format_exc()
            )
            return False

class MemoryMonitor:
    """Monitors system memory health."""

    def __init__(self, context: SystemContext):
        """Initialize the memory monitor with context."""
        self.context = context
        self.memory_manager = MemoryManager(context)

    def check_memory_health(self, model_size: int, trainer: Optional[SOVLTrainer] = None) -> bool:
        """Check memory health and handle any errors."""
        try:
            is_healthy = self.memory_manager.check_memory_health(model_size, trainer)
            self.context.logger.log_memory_health(
                model_size=model_size,
                trainer=trainer,
                health_status="healthy" if is_healthy else "unhealthy",
                device=self.context.device
            )
            return is_healthy
        except Exception as e:
            # Handle memory errors through ErrorManager
            self.context.error_manager.handle_memory_error(e, model_size)
            return False

class TemperamentAdjuster:
    """Adjusts temperament based on system state and curiosity."""
    
    def __init__(self, context: SystemContext, state_tracker: StateTracker):
        self.context = context
        self.state_tracker = state_tracker
        self.temperament_system = TemperamentSystem(
            config=context.config,
            logger=context.logger,
            state=state_tracker.state
        )
        
    def update_temperament(self, curiosity_manager: Optional[CuriosityManager] = None):
        """
        Update temperament based on system state and curiosity pressure.
        
        Args:
            curiosity_manager: Optional CuriosityManager instance to get pressure from
        """
        try:
            state = self.state_tracker.state
            
            # Get curiosity pressure if manager is provided
            curiosity_pressure = None
            if curiosity_manager is not None:
                curiosity_pressure = curiosity_manager.get_pressure()
                if not (0.0 <= curiosity_pressure <= 1.0):
                    self.context.logger.record_event(
                        event_type="temperament_warning",
                        message=f"Invalid curiosity pressure: {curiosity_pressure}",
                        level="warning"
                    )
                    curiosity_pressure = None
            
            # Calculate lifecycle stage
            lifecycle_stage = state.data_exposure / state.lora_capacity if state.lora_capacity > 0 else 0.0
            
            # Update temperament with proper synchronization
            with state.lock:
                self.temperament_system.update_temperament(
                    confidence=state.confidence_history[-1] if state.confidence_history else 0.5,
                    lifecycle_stage=lifecycle_stage,
                    curiosity_pressure=curiosity_pressure
                )
                
                # Update state with new temperament
                state.temperament_score = self.temperament_system.score
                state.temperament_history.append(state.temperament_score)
                
                # Log the update
                self.context.logger.record_event(
                    event_type="temperament_updated",
                    message=f"Temperament updated: score={state.temperament_score:.3f}, mood={self.temperament_system.mood_label}",
                    level="info",
                    additional_info={
                        "score": state.temperament_score,
                        "mood": self.temperament_system.mood_label,
                        "curiosity_pressure": curiosity_pressure,
                        "lifecycle_stage": lifecycle_stage,
                        "confidence": state.confidence_history[-1] if state.confidence_history else 0.5
                    }
                )
                
        except Exception as e:
            self.context.logger.record_event(
                event_type="temperament_error",
                message=f"Failed to update temperament: {str(e)}",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

class CuriosityEngine:
    """Manages curiosity-driven question generation and exploration."""
    
    def __init__(
        self,
        context: SystemContext,
        model_loader: ModelLoader,
        state_tracker: StateTracker,
        error_manager: ErrorManager
    ):
        self.context = context
        self.model_loader = model_loader
        self.state_tracker = state_tracker
        self.error_manager = error_manager
        self.curiosity_manager = None
        self._initialize_curiosity_manager()
        
    def _initialize_curiosity_manager(self) -> None:
        """Initialize the curiosity manager with proper error handling."""
        try:
            self.curiosity_manager = CuriosityManager(
                config_manager=self.context.config_manager,
                logger=self.context.logger,
                device=self.context.device
            )
            self.context.logger.info("Curiosity manager initialized successfully")
        except Exception as e:
            self.context.logger.log_error(
                error_msg=f"Failed to initialize curiosity manager: {str(e)}",
                error_type="initialization_error",
                stack_trace=traceback.format_exc()
            )
            raise SystemInitializationError(
                message="Failed to initialize curiosity manager",
                config_path=self.context.config_path,
                stack_trace=traceback.format_exc()
            )
            
    def generate_question(self, context_str: str = "", spontaneous: bool = False) -> Optional[str]:
        """Generate a curiosity-driven question with conversation history validation."""
        try:
            # Get current state with lock
            with self.state_tracker.state.lock:
                state = self.state_tracker.get_state()
                
                # Validate conversation history
                if not state.conversation_history:
                    self.context.logger.record_event(
                        event_type="conversation_error",
                        message="No conversation history available",
                        level="error",
                        additional_info={
                            "state_hash": state.state_hash(),
                            "context": context_str
                        }
                    )
                    return None
                
                # Check conversation ID consistency
                current_id = state.conversation_history.conversation_id
                if current_id != self._last_conversation_id:
                    self.context.logger.record_event(
                        event_type="conversation_mismatch",
                        message="Conversation ID mismatch detected",
                        level="warning",
                        additional_info={
                            "current_id": current_id,
                            "last_id": self._last_conversation_id,
                            "state_hash": state.state_hash()
                        }
                    )
                    self._last_conversation_id = current_id
                
                # Generate question using validated state
                question = self.curiosity_manager.generate_question(
                    context_str=context_str,
                    spontaneous=spontaneous
                )
                
                # Log question generation
                if question:
                    self.context.logger.record_event(
                        event_type="curiosity_question",
                        message="Generated curiosity question",
                        level="info",
                        additional_info={
                            "question": question,
                            "conversation_id": current_id,
                            "spontaneous": spontaneous,
                            "state_hash": state.state_hash()
                        }
                    )
                
                return question
                
        except Exception as e:
            self.context.logger.record_event(
                event_type="curiosity_error",
                message=f"Failed to generate question: {str(e)}",
                level="error",
                additional_info={
                    "error": str(e)
                }
            )
            raise
        except Exception as e:
            self.context.logger.record_event(
                event_type="data_error",
                message="Failed to load training data",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise ValueError(f"Failed to load training data: {str(e)}")

    def run_training_cycle(self, train_data: Optional[List] = None, valid_data: Optional[List] = None, 
                          epochs: Optional[int] = None, batch_size: Optional[int] = None):
        """
        Run a training cycle with the provided or default data.

        Args:
            train_data: Optional training data to use instead of default
            valid_data: Optional validation data to use instead of default
            epochs: Optional number of epochs to train for
            batch_size: Optional batch size for training

        Raises:
            ValueError: If no training data is available
        """
        # Use provided data or fall back to initialized data
        train_data = train_data or self.train_data
        valid_data = valid_data or self.valid_data

        # Validate data presence
        if not train_data:
            self.context.logger.record_event(
                event_type="training_error",
                message="No training data available for training cycle",
                level="error",
                additional_info={
                    "using_provided_data": train_data is not self.train_data,
                    "valid_data_available": bool(valid_data)
                }
            )
            raise ValueError("No training data available for training cycle")

        # Log training cycle start
        self.context.logger.record_event(
            event_type="training_cycle_start",
            message="Starting training cycle",
            level="info",
            additional_info={
                "train_samples": len(train_data),
                "valid_samples": len(valid_data),
                "epochs": epochs,
                "batch_size": batch_size
            }
        )

        # Execute pre-training hooks with full context
        self.plugin_manager.execute_hook("on_training_step", {
            "batch_size": len(train_data),
            "dry_run": False,
            "train_samples": len(train_data),
            "valid_samples": len(valid_data),
            "epochs": epochs,
            "batch_size": batch_size,
            "state": self.state_tracker.state.to_dict()
        })
        
        try:
            # Run the training cycle
            self.cycle_trainer.run_training_cycle(
                train_data=train_data,
                valid_data=valid_data,
                epochs=epochs,
                batch_size=batch_size
            )
            
            # Execute post-training hooks with full context
            self.plugin_manager.execute_hook("on_training_step_complete", {
                "batch_size": len(train_data),
                "result": "success",
                "train_samples": len(train_data),
                "valid_samples": len(valid_data),
                "epochs": epochs,
                "batch_size": batch_size,
                "state": self.state_tracker.state.to_dict()
            })
            
        except Exception as e:
            # Execute error hook if training fails
            self.plugin_manager.execute_hook("on_training_error", {
                "error": str(e),
                "stack_trace": traceback.format_exc(),
                "train_samples": len(train_data),
                "valid_samples": len(valid_data),
                "epochs": epochs,
                "batch_size": batch_size,
                "state": self.state_tracker.state.to_dict()
            })
            
            # Log training error
            self.context.logger.record_event(
                event_type="training_error",
                message="Training cycle failed",
                level="error",
                additional_info={
                    "error": str(e),
                    "stack_trace": traceback.format_exc()
                }
            )
            raise

    def handle_training_complete(self, epoch: int, avg_loss: float, data_exposure: float):
        """Handle completion of a training cycle."""
        self.state_tracker.update_data_exposure(data_exposure)
        
        # Execute training complete hook
        self.plugin_manager.execute_hook("on_training_complete", {
            "epoch": epoch,
            "avg_loss": avg_loss,
            "data_exposure": data_exposure,
            "state": self.state_tracker.state.to_dict()
        })
        
        self.context.logger.record_event(
            event_type="training_complete",
            message="Training cycle completed",
            level="info",
            additional_info={
                "epoch": epoch,
                "avg_loss": avg_loss,
                "data_exposure": data_exposure
            }
        )
