import logging
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum, auto

from waldur_mastermind.chat.ui_registry import ui_registry

logger = logging.getLogger(__name__)


class ParserState(Enum):
    """Parser state relative to code block fences (```)."""

    OUTSIDE_BLOCK = auto()  # Scanning normal markdown text
    INSIDE_BLOCK = auto()  # Inside a code/mermaid/etc block


@dataclass
class TagExtractionResult:
    """
    Result of extracting a tag from content after an opening fence.
    """

    tag: str | None
    content: str
    needs_more_data: bool = (
        False  # True if we need the next chunk to determine the tag.
    )


class StreamParser:
    """
    Streaming markdown parser that converts raw text into UI components.

    The parser uses two special buffers to handle edge cases:
    - _trailing_backticks: Stores last 1-2 backticks (might form ``` with next chunk)
    - _incomplete_fence: Stores "```" + partial content when we can't determine tag yet

    Features:
    - Buffers output (~50 chars) to reduce UI jitter
    - Delegates to UI registry for special block types (mermaid, code, etc.)
    """

    FENCE_DELIMITER = "```"
    MIN_CHUNK_SIZE = 50  # Minimum buffered text size before flushing to UI

    def __init__(self):
        self._state = ParserState.OUTSIDE_BLOCK
        self._current_block_tag = None
        self._text_buffer = []  # Accumulates markdown text
        self._block_buffer = []  # Accumulates code block content
        self._trailing_backticks = ""  # Last 1-2 chars (potential partial fence)
        self._incomplete_fence = None  # Complete fence awaiting tag determination

    def parse(self, chunk: str) -> Generator[dict]:
        """
        Parse a chunk of streaming markdown text.
        Yields: UI component dictionaries for rendering
        """
        # Reconstruct complete text from previous partial fence
        text = self._reconstruct_from_trailing_backticks(chunk)

        # Prepend incomplete fence from previous chunk if present
        if self._incomplete_fence is not None:
            text = self._incomplete_fence + text
            self._incomplete_fence = None

        # Split on fence delimiter and process segments
        segments = text.split(self.FENCE_DELIMITER)

        for i, segment in enumerate(segments):
            is_last_segment = i == len(segments) - 1

            if i == 0:
                # First segment is always continuation of current state
                self._append_to_current_buffer(segment)
            else:
                # Subsequent segments mean we crossed a fence delimiter
                yield from self._handle_fence_crossing(segment, is_last_segment)

        # Flush text buffer if it's large enough
        yield from self._flush_text_if_threshold_met()

    def flush(self) -> Generator[dict]:
        """Force flush all buffers at end of stream."""
        # Treat incomplete fence as regular text
        if self._incomplete_fence:
            self._text_buffer.append(self._incomplete_fence)
            self._incomplete_fence = None

        # Add trailing backticks to text
        if self._trailing_backticks:
            self._text_buffer.append(self._trailing_backticks)
            self._trailing_backticks = ""

        # Flush all remaining content
        yield from self._flush_text_buffer()

    def _reconstruct_from_trailing_backticks(self, chunk: str) -> str:
        """
        Reconstruct complete text by handling potential split fence delimiters.

        This handles the edge case where ``` is split across chunks:
        - Chunk N ends with: "code ``"
        - Chunk N+1 starts with: "`python"
        - Result should be: "code ```python"

        We buffer the last 1-2 backticks to detect this pattern.
        """
        text = self._trailing_backticks + chunk
        self._trailing_backticks = ""

        # Save trailing backticks if they might form a fence with next chunk
        if text.endswith("``") and not text.endswith(self.FENCE_DELIMITER):
            self._trailing_backticks = "``"
            text = text[:-2]
        elif text.endswith("`") and not text.endswith("``"):
            self._trailing_backticks = "`"
            text = text[:-1]

        return text

    def _append_to_current_buffer(self, segment: str):
        """Add segment to appropriate buffer based on current state."""
        if not segment:
            return

        if self._state == ParserState.INSIDE_BLOCK:
            self._block_buffer.append(segment)
        else:
            self._text_buffer.append(segment)

    def _handle_fence_crossing(
        self, segment: str, is_last_segment: bool
    ) -> Generator[dict]:
        """
        Handle crossing a fence delimiter (```).

        When INSIDE_BLOCK: This fence closes the block, segment is text after
        When OUTSIDE_BLOCK: This fence opens a block, segment contains tag + content
        """
        if self._state == ParserState.INSIDE_BLOCK:
            # Closing fence
            yield from self._close_current_block()
            if segment:
                self._text_buffer.append(segment)
        else:
            # Opening fence - check if we can determine the tag
            extraction = self._extract_tag_from_fence_content(segment)

            if extraction.needs_more_data:
                # Only buffer incomplete fence if we're at chunk boundary
                # Otherwise, more segments in this chunk need processing
                if is_last_segment:
                    self._incomplete_fence = self.FENCE_DELIMITER + segment
                    return

                # Not at chunk boundary - treat as fence with default tag
                # This handles cases like "``````" (consecutive fences)
                tag = "code"
                yield from self._open_block(tag, "")
            else:
                # We have enough data to open the block
                yield from self._open_block(extraction.tag, extraction.content)

    def _extract_tag_from_fence_content(self, content: str) -> TagExtractionResult:
        """
        Extract tag and content from text following an opening fence.
        Strictly enforces that a tag ends at the first newline.
        """
        if not content:
            return TagExtractionResult(None, "", needs_more_data=True)

        if "\n" in content:
            tag, remaining_content = content.split("\n", 1)

            tag = tag.strip()

            # If tag is empty string (case: ```\n), default to "code".
            tag = tag or "code"

            return TagExtractionResult(tag, remaining_content)

        return TagExtractionResult(None, "", needs_more_data=True)

    def _open_block(self, tag: str, initial_content: str) -> Generator[dict]:
        """
        Transition to INSIDE_BLOCK state and emit loading indicator.
        """
        # Flush any pending text before entering block
        yield from self._flush_text_buffer()

        # Transition state
        self._state = ParserState.INSIDE_BLOCK
        self._current_block_tag = tag
        self._block_buffer = [initial_content]

        # Emit loading indicator if block type supports it
        yield from self._emit_loading_indicator(tag)

    def _close_current_block(self) -> Generator[dict]:
        """
        Transition to OUTSIDE_BLOCK state and emit the completed block.
        """
        full_content = "".join(self._block_buffer)
        yield from self._render_block_content(self._current_block_tag, full_content)

        # Reset state
        self._state = ParserState.OUTSIDE_BLOCK
        self._block_buffer = []
        self._current_block_tag = None

    def _flush_text_if_threshold_met(self) -> Generator[dict]:
        if self._state == ParserState.INSIDE_BLOCK:
            return

        buffered_size = sum(len(text) for text in self._text_buffer)
        if buffered_size >= self.MIN_CHUNK_SIZE:
            yield from self._flush_text_buffer()

    def _flush_text_buffer(self) -> Generator[dict]:
        """Emit buffered text as markdown and clear buffer."""
        if not self._text_buffer:
            return

        content = "".join(self._text_buffer)
        self._text_buffer = []

        ui_component = ui_registry.create_content("markdown", {"c": content})
        yield self._to_ui_dict(ui_component)

    def _render_block_content(self, tag: str, content: str) -> Generator[dict]:
        """Render a code block using UI registry with fallback."""
        try:
            ui_component = ui_registry.create_content_from_block(
                {"c": content, "t": tag}
            )
            yield self._to_ui_dict(ui_component)
        except ValueError:
            logger.warning(
                "Failed to create UI content for tag '%s', falling back to code block",
                tag,
                exc_info=True,
            )
            ui_component = ui_registry.create_content("code", {"c": content, "t": tag})
            yield self._to_ui_dict(ui_component)

    def _emit_loading_indicator(self, tag: str) -> Generator[dict]:
        """
        Emit loading indicator for block types that support it.
        """
        component_config = ui_registry.get_by_trigger(tag)
        if component_config and component_config.get("has_loading_state"):
            ui_component = ui_registry.create_content(
                "load", {"t": component_config.key}
            )
            yield self._to_ui_dict(ui_component)

    def _to_ui_dict(self, ui_component) -> dict:
        return {"k": ui_component.key, **ui_component.data}

    def parse_tool_result(self, result: dict) -> dict | None:
        """
        Parse tool execution result into UI component.

        Returns None for internal system errors (which should be hidden from users).
        Displays validation errors and successes to users.

        Result types:
        - "success": Tool executed successfully
        - "validation_error": User-facing error (e.g., "Flavor not found")
        - "error": Internal system error (hidden from users, logged server-side)
        """
        result_type = result.get("type")

        if result_type == "error":
            # Hide internal system errors from users - they're logged in tool_executor
            return None

        # Display both success and validation_error types to users
        # Extract UI component info from tool result
        ui_key = result.get("ui_component", "markdown")
        ui_data = result.get("ui_data", {"c": result.get("summary", "")})

        try:
            ui_component = ui_registry.create_content(ui_key, ui_data)
            return self._to_ui_dict(ui_component)
        except ValueError:
            logger.warning(
                "Failed to create UI content for tool result, falling back to markdown",
                exc_info=True,
            )
            # Fallback to markdown with summary
            ui_component = ui_registry.create_content(
                "markdown", {"c": result.get("summary", "")}
            )
            return self._to_ui_dict(ui_component)
