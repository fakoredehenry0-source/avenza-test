"""Bridge between the backend contracts and the vendored ``avenza_ai`` package.

The AI layer may classify a request, parse a number, render a read-back or
transcribe audio, but it can never approve a transfer. The deterministic backend
parser remains the amount authority and WebAuthn approval remains mandatory.
"""

from .workflow import AIWorkflow, build_ai, build_intent

__all__ = ["AIWorkflow", "build_ai", "build_intent"]
