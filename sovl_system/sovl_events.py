import asyncio
import re
import time
from collections import defaultdict
from contextlib import contextmanager
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Generator, Union, cast
from sovl_logger import Logger, LoggerConfig
from sovl_config import ConfigManager
import traceback

# Type alias for callbacks - clearer name
EventHandler = Callable[..., Any]

# Event type validation regex (alphanumeric, underscores, hyphens, dots)
EVENT_TYPE_PATTERN = re.compile(r'^[a-zA-Z0-9_\-\.]+$')

class EventDispatcher:
    """
    Manages event subscriptions and notifications in a thread-safe manner.

    This class provides a robust event handling system allowing components
    to subscribe to specific event types and receive notifications when those
    events occur.

    Features:
        - Thread-safe operations using locks.
        - Prioritized event handlers (higher priority executed first).
        - Synchronous (`notify`) and asynchronous (`async_notify`) notification.
        - Channel-based pub/sub pattern support.
        - Optional event metadata (timestamp, event_id).
        - Validation of event types and handlers.
        - Duplicate subscription detection and warning.
        - Deferred unsubscription: Prevents errors if handlers unsubscribe
          during a notification cycle.
        - Cleanup methods for stale events or all subscribers.
    """

    __slots__ = (
        '_subscribers',
        '_channels',
        '_lock',
        '_logger',
        '_notification_depth',
        '_deferred_unsubscriptions',
        '_config_manager',
    )

    def __init__(self, config_manager: ConfigManager, logger: Optional[Logger] = None):
        """
        Initialize the EventDispatcher.

        Args:
            config_manager: ConfigManager instance for configuration handling
            logger: Optional Logger instance. If None, creates a new Logger instance.
        """
        self._config_manager = config_manager
        self._subscribers: Dict[str, List[Tuple[int, EventHandler]]] = defaultdict(list)
        self._channels: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._lock = Lock()
        self._logger = logger or Logger(LoggerConfig(log_file="sovl_events.log"))
        self._notification_depth: int = 0
        self._deferred_unsubscriptions: Dict[str, Set[EventHandler]] = defaultdict(set)

        # Initialize configuration
        self._initialize_config()

    def _initialize_config(self) -> None:
        """Initialize and validate configuration parameters."""
        try:
            # Validate required configuration sections
            required_sections = ["logging_config", "controls_config"]
            for section in required_sections:
                if not self._config_manager.validate_section(section):
                    raise ValueError(f"Missing required configuration section: {section}")

            # Validate specific configuration values
            self._validate_config_values()

        except Exception as e:
            self._log_error(
                Exception(f"Configuration initialization failed: {str(e)}"),
                "config_initialization",
                traceback.format_exc()
            )
            raise

    def _validate_config_values(self) -> None:
        """Validate specific configuration values."""
        try:
            # Log file validation
            log_file = self._get_config_value("logging_config.log_file", "sovl_events.log")
            if not isinstance(log_file, str) or not log_file:
                raise ValueError(f"Invalid log_file: {log_file}")

            # Max size validation
            max_size_mb = self._get_config_value("logging_config.max_size_mb", 10)
            if not isinstance(max_size_mb, int) or max_size_mb < 1:
                raise ValueError(f"Invalid max_size_mb: {max_size_mb}")

            # Compress old validation
            compress_old = self._get_config_value("logging_config.compress_old", False)
            if not isinstance(compress_old, bool):
                raise ValueError(f"Invalid compress_old: {compress_old}")

        except Exception as e:
            self._log_error(
                Exception(f"Configuration validation failed: {str(e)}"),
                "config_validation",
                traceback.format_exc()
            )
            raise

    def _get_config_value(self, key: str, default: Any) -> Any:
        """Get a configuration value with validation."""
        try:
            return self._config_manager.get(key, default)
        except Exception as e:
            self._log_error(
                Exception(f"Failed to get config value for {key}: {str(e)}"),
                "config_access",
                traceback.format_exc()
            )
            return default

    def _update_config(self, key: str, value: Any) -> bool:
        """Update a configuration value with validation."""
        try:
            return self._config_manager.update(key, value)
        except Exception as e:
            self._log_error(
                Exception(f"Failed to update config value for {key}: {str(e)}"),
                "config_update",
                traceback.format_exc()
            )
            return False

    def _log_error(self, error: Exception, context: str, stack_trace: Optional[str] = None) -> None:
        """Log an error with context and stack trace."""
        self._logger.log_error(
            error_msg=str(error),
            error_type=f"events_{context}_error",
            stack_trace=stack_trace or traceback.format_exc(),
            additional_info={
                "context": context,
                "timestamp": time.time()
            }
        )

    @contextmanager
    def _locked(self):
        """Context manager for acquiring and releasing the internal lock."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def _validate_event_type(self, event_type: Any) -> str:
        """
        Validates the event type format and returns it if valid.

        Args:
            event_type: The event type to validate.

        Returns:
            The validated event type string.

        Raises:
            ValueError: If the event type is not a non-empty string or
                        does not match the required pattern.
        """
        if not isinstance(event_type, str) or not event_type:
            self._logger.log_error(
                error_msg="Event type must be a non-empty string",
                error_type="validation_error"
            )
            raise ValueError("Event type must be a non-empty string")
        if not EVENT_TYPE_PATTERN.match(event_type):
            self._logger.log_error(
                error_msg=f"Invalid event type format: '{event_type}'. Must match pattern [a-zA-Z0-9_.-]+",
                error_type="validation_error"
            )
            raise ValueError(f"Invalid event type format: '{event_type}'. Must match pattern [a-zA-Z0-9_.-]+")
        return event_type

    def _validate_handler(self, handler: Any) -> EventHandler:
        """
        Validates that the handler is callable.

        Args:
            handler: The event handler to validate.

        Returns:
            The validated event handler.

        Raises:
            TypeError: If the handler is not callable.
        """
        if not callable(handler):
            self._logger.log_error(
                error_msg=f"Invalid event handler: {type(handler).__name__} is not callable.",
                error_type="validation_error"
            )
            raise TypeError(f"Invalid event handler: {type(handler).__name__} is not callable.")
        return cast(EventHandler, handler)

    def subscribe(self, event_type: str, handler: EventHandler, priority: int = 0) -> None:
        """
        Subscribes an event handler to an event type with optional priority.

        Args:
            event_type: The type of event to subscribe to.
            handler: The function or method to call when the event occurs.
            priority: An integer representing the handler's priority.

        Raises:
            ValueError: If the event_type is invalid.
            TypeError: If the handler is not callable.
        """
        valid_event_type = self._validate_event_type(event_type)
        valid_handler = self._validate_handler(handler)
        handler_name = getattr(valid_handler, '__qualname__', repr(valid_handler))

        with self._locked():
            sub_list = self._subscribers[valid_event_type]

            if any(h == valid_handler for _, h in sub_list):
                self._logger.record_event(
                    event_type="duplicate_subscription",
                    message=f"Handler '{handler_name}' is already subscribed to event '{valid_event_type}'",
                    level="warning"
                )
                return

            sub_list.append((priority, valid_handler))
            sub_list.sort(key=lambda item: item[0], reverse=True)
            self._logger.record_event(
                event_type="subscription",
                message=f"Subscribed handler '{handler_name}' to event '{valid_event_type}' with priority {priority}",
                level="debug"
            )

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """
        Unsubscribes an event handler from an event type.

        Args:
            event_type: The type of event to unsubscribe from.
            handler: The specific handler function/method to remove.

        Raises:
            ValueError: If the event_type is invalid.
            TypeError: If the handler is not callable.
        """
        valid_event_type = self._validate_event_type(event_type)
        valid_handler = self._validate_handler(handler)
        handler_name = getattr(valid_handler, '__qualname__', repr(valid_handler))

        with self._locked():
            if self._notification_depth > 0 and valid_event_type in self._subscribers:
                self._deferred_unsubscriptions[valid_event_type].add(valid_handler)
                self._logger.record_event(
                    event_type="deferred_unsubscription",
                    message=f"Deferred unsubscription for handler '{handler_name}' from event '{valid_event_type}'",
                    level="debug"
                )
                return

            if valid_event_type in self._subscribers:
                original_count = len(self._subscribers[valid_event_type])
                self._subscribers[valid_event_type] = [
                    (prio, h) for prio, h in self._subscribers[valid_event_type] if h != valid_handler
                ]
                new_count = len(self._subscribers[valid_event_type])

                if new_count < original_count:
                    self._logger.record_event(
                        event_type="unsubscription",
                        message=f"Unsubscribed handler '{handler_name}' from event '{valid_event_type}'",
                        level="debug"
                    )
                else:
                    self._logger.record_event(
                        event_type="unsubscription_warning",
                        message=f"Attempted to unsubscribe handler '{handler_name}' from event '{valid_event_type}', but it was not found.",
                        level="warning"
                    )

                if not self._subscribers[valid_event_type]:
                    del self._subscribers[valid_event_type]
                    self._deferred_unsubscriptions.pop(valid_event_type, None)
            else:
                self._logger.record_event(
                    event_type="unsubscription_warning",
                    message=f"Attempted to unsubscribe from non-existent or empty event type '{valid_event_type}'.",
                    level="warning"
                )

    def notify(self, event_type: str, *args: Any, include_metadata: bool = False, **kwargs: Any) -> None:
        """
        Notifies all subscribed handlers of an event synchronously.

        Args:
            event_type: The type of event being triggered.
            *args: Positional arguments to pass to each handler.
            include_metadata: If True, includes metadata in the event.
            **kwargs: Keyword arguments to pass to each handler.

        Raises:
            ValueError: If the event_type is invalid.
        """
        subscribers_copy = self._prepare_notification(event_type)
        if not subscribers_copy:
            self._finalize_notification()
            return

        metadata = {}
        if include_metadata:
            now = time.time()
            metadata = {
                "event_id": f"{event_type}-{now:.6f}",
                "timestamp": now,
            }

        for _, handler in subscribers_copy:
            handler_name = getattr(handler, '__qualname__', repr(handler))
            try:
                if asyncio.iscoroutinefunction(handler):
                    self._logger.record_event(
                        event_type="async_handler_warning",
                        message=f"Attempted to call async handler '{handler_name}' for event '{event_type}' using synchronous notify().",
                        level="warning"
                    )
                    continue

                call_kwargs = kwargs.copy()
                if include_metadata:
                    call_kwargs['metadata'] = metadata

                handler(*args, **call_kwargs)

            except Exception as e:
                self._logger.log_error(
                    error_msg=f"Error executing synchronous handler '{handler_name}' for event '{event_type}': {str(e)}",
                    error_type="handler_error",
                    stack_trace=str(e)
                )

        self._finalize_notification()

    async def async_notify(self, event_type: str, *args: Any, include_metadata: bool = False, **kwargs: Any) -> None:
        """
        Notifies all subscribed handlers of an event asynchronously.

        Args:
            event_type: The type of event being triggered.
            *args: Positional arguments to pass to each handler.
            include_metadata: If True, includes metadata in the event.
            **kwargs: Keyword arguments to pass to each handler.

        Raises:
            ValueError: If the event_type is invalid.
        """
        subscribers_copy = self._prepare_notification(event_type)
        if not subscribers_copy:
            self._finalize_notification()
            return

        metadata = {}
        if include_metadata:
            now = time.time()
            metadata = {
                "event_id": f"{event_type}-{now:.6f}",
                "timestamp": now,
            }

        for _, handler in subscribers_copy:
            handler_name = getattr(handler, '__qualname__', repr(handler))
            try:
                call_kwargs = kwargs.copy()
                if include_metadata:
                    call_kwargs['metadata'] = metadata

                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **call_kwargs)
                else:
                    handler(*args, **call_kwargs)

            except Exception as e:
                self._logger.log_error(
                    error_msg=f"Error executing handler '{handler_name}' during async notification for event '{event_type}': {str(e)}",
                    error_type="handler_error",
                    stack_trace=str(e)
                )

        self._finalize_notification()

    def cleanup(self) -> None:
        """Clean up event dispatcher resources."""
        try:
            with self._locked():
                self._subscribers.clear()
                self._deferred_unsubscriptions.clear()
                self._logger.record_event(
                    event_type="cleanup",
                    message="Event dispatcher cleaned up successfully",
                    level="info"
                )
        except Exception as e:
            self._logger.log_error(
                error_msg=f"Error during event dispatcher cleanup: {str(e)}",
                error_type="cleanup_error",
                stack_trace=str(e)
            )

    def _prepare_notification(self, event_type: str) -> List[Tuple[int, EventHandler]]:
        """Internal helper to prepare for notification."""
        valid_event_type = self._validate_event_type(event_type)
        with self._locked():
            self._notification_depth += 1
            # IMPORTANT: Create a *copy* of the subscriber list.
            # This allows releasing the lock while iterating and calling handlers,
            # preventing deadlocks if a handler tries to subscribe/unsubscribe.
            subscribers_copy = list(self._subscribers.get(valid_event_type, []))
        return subscribers_copy

    def _finalize_notification(self) -> None:
        """Internal helper to finalize notification and process deferred actions."""
        with self._locked():
            self._notification_depth -= 1
            if self._notification_depth == 0:
                # Process deferred unsubscriptions only when the outermost notification cycle ends
                if self._deferred_unsubscriptions:
                    self._process_deferred_unsubscriptions()

    def _process_deferred_unsubscriptions(self) -> None:
        """
        Processes handlers marked for deferred unsubscription.
        Must be called while holding the lock and when notification_depth is 0.
        """
        if not self._deferred_unsubscriptions:
            return

        self._logger.record_event(
            event_type="processing_deferred_unsubscriptions",
            message="Processing deferred unsubscriptions...",
            level="debug"
        )
        for event_type, handlers_to_remove in self._deferred_unsubscriptions.items():
            if event_type in self._subscribers:
                initial_len = len(self._subscribers[event_type])
                self._subscribers[event_type] = [
                    (prio, h) for prio, h in self._subscribers[event_type]
                    if h not in handlers_to_remove
                ]
                removed_count = initial_len - len(self._subscribers[event_type])
                if removed_count > 0:
                    self._logger.record_event(
                        event_type="processed_deferred_unsubscription",
                        message=f"Processed {removed_count} deferred unsubscription(s) for event '{event_type}'.",
                        level="debug"
                    )

                # Clean up if event type becomes empty
                if not self._subscribers[event_type]:
                    del self._subscribers[event_type]
                    self._logger.record_event(
                        event_type="cleaned_up_stale_event_type_entry",
                        message=f"Cleaned up stale event type entry: '{event_type}'",
                        level="debug"
                    )

        self._deferred_unsubscriptions.clear()
        self._logger.record_event(
            event_type="finished_processing_deferred_unsubscriptions",
            message="Finished processing deferred unsubscriptions.",
            level="debug"
        )

    def publish(self, channel: str, event: Any) -> None:
        """
        Publish an event to a specific channel.

        Args:
            channel: The channel to publish the event to.
            event: The event data to publish.
        """
        valid_channel = self._validate_event_type(channel)
        with self._locked():
            self._channels[valid_channel].put_nowait(event)
            self._logger.record_event(
                event_type="publish",
                message=f"Published event to channel '{valid_channel}'",
                level="debug"
            )

    async def subscribe_channel(self, channel: str) -> Generator[Any, None, None]:
        """
        Subscribe to a specific channel and yield events.

        Args:
            channel: The channel to subscribe to.

        Yields:
            Events published to the channel.
        """
        valid_channel = self._validate_event_type(channel)
        while True:
            try:
                event = await self._channels[valid_channel].get()
                yield event
                self._channels[valid_channel].task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log_error(
                    Exception(f"Error in channel subscription for '{valid_channel}': {str(e)}"),
                    "channel_subscription",
                    traceback.format_exc()
                )
                break

    def get_channel(self, channel: str) -> asyncio.Queue:
        """
        Get the queue for a specific channel.

        Args:
            channel: The channel to get the queue for.

        Returns:
            The queue for the specified channel.
        """
        valid_channel = self._validate_event_type(channel)
        return self._channels[valid_channel]

    def cleanup_channel(self, channel: str) -> None:
        """
        Clean up a specific channel.

        Args:
            channel: The channel to clean up.
        """
        valid_channel = self._validate_event_type(channel)
        with self._locked():
            if valid_channel in self._channels:
                del self._channels[valid_channel]
                self._logger.record_event(
                    event_type="channel_cleanup",
                    message=f"Cleaned up channel '{valid_channel}'",
                    level="debug"
                )
