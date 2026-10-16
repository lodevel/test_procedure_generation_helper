"""LLM worker thread for asynchronous LLM requests."""

import logging

from PySide6.QtCore import QThread, Signal

from .backend_base import LLMBackend, LLMRequest

log = logging.getLogger(__name__)


class LLMWorker(QThread):
    """Worker thread for LLM requests.

    Supports streaming mode: when the backend supports it, emits
    thinking_chunk and text_chunk signals progressively as SSE events
    arrive from the LLM, before the final finished signal.
    """

    finished = Signal(object)  # LLMResponse
    error = Signal(str)
    thinking_chunk = Signal(str)  # Progressive thinking/reasoning text delta
    text_chunk = Signal(str)  # Progressive response text delta

    def __init__(self, backend: LLMBackend, request: LLMRequest, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._request = request
        self._cancelled = False
        # Accumulate all emitted streaming text so it can be restored
        # when the user switches away from this tab and comes back.
        self.accumulated_thinking = ""
        self.accumulated_response = ""

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True
        log.info("LLMWorker: Cancellation requested")
        # Also cancel on the backend (supports mid-flight abort for streaming)
        self._backend.cancel()

    def _on_thinking_chunk(self, text: str):
        """Callback from streaming backend for thinking/reasoning chunks."""
        if not self._cancelled:
            self.accumulated_thinking += text
            self.thinking_chunk.emit(text)

    def _on_text_chunk(self, text: str):
        """Callback from streaming backend for text response chunks."""
        if not self._cancelled:
            self.accumulated_response += text
            self.text_chunk.emit(text)

    def run(self):
        try:
            log.debug(
                f"LLMWorker.run() starting - backend={self._backend.__class__.__name__}, "
                f"task={self._request.task}"
            )

            if hasattr(self._backend, 'send_request_streaming'):
                log.debug("LLMWorker: Using streaming send_request")
                response = self._backend.send_request_streaming(
                    self._request,
                    thinking_callback=self._on_thinking_chunk,
                    text_callback=self._on_text_chunk,
                )
            else:
                response = self._backend.send_request(self._request)

            if self._cancelled:
                log.debug("LLMWorker: Request was cancelled")
                self.error.emit("Request cancelled by user")
                return

            log.debug(
                f"LLMWorker.run() got response - "
                f"raw_response length={len(response.raw_response)}, success={response.success}"
            )
            if not response.success:
                log.warning(f"LLMWorker.run() response failed - error: {response.error_message}")
            self.finished.emit(response)
        except Exception as e:
            if not self._cancelled:
                log.error(f"LLMWorker.run() exception: {e}", exc_info=True)
                self.error.emit(str(e))
