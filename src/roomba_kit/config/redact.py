"""Log redaction: mask every known secret value and secret-shaped patterns."""

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

SecretProvider = Callable[[], Iterable[str]]
MASK = "***"

_KEY = r"""[\w-]*(?:password|passwd|secret|token|authorization)["']?\s*[=:]\s*"""
# Each pattern keeps group 1 (the label) and masks the rest of the match.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"""(?i)\b({_KEY})(["'])(?:\\.|(?!\2).)*\2"""), rf"\1\2{MASK}\2"),
    (re.compile(rf"""(?i)\b({_KEY}(?:bearer\s+)?)[^\s"',;&}}]+"""), rf"\1{MASK}"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"), rf"\1{MASK}"),
    (re.compile(r"()\brk_[A-Za-z0-9_-]{16,}"), rf"\1{MASK}"),
)


class RedactFilter(logging.Filter):
    """Masks secrets in a record's formatted message and exception text.

    `provider` is called per record so newly saved secrets are picked up. If it fails (say,
    `credentials.toml` has a bad mode), the last good set is used.
    """

    def __init__(self, provider: SecretProvider | None = None) -> None:
        super().__init__()
        self._provider = provider
        self._known: tuple[str, ...] = ()
        self._known_re: re.Pattern[str] | None = None

    def _secrets_re(self) -> re.Pattern[str] | None:
        if self._provider is None:
            return None
        try:
            values = tuple(sorted({v for v in self._provider() if v}, key=len, reverse=True))
        except Exception:
            values = self._known
        if values != self._known:
            self._known = values
            self._known_re = re.compile("|".join(map(re.escape, values))) if values else None
        return self._known_re

    def redact(self, text: str) -> str:
        if (known := self._secrets_re()) is not None:
            text = known.sub(MASK, text)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            original, failed = record.getMessage(), False
        except Exception:
            original, failed = f"{record.msg} {record.args}", True
        redacted = self.redact(original)
        # Untouched records keep their args, which some formatters (uvicorn access) unpack.
        if failed or redacted != original:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def install_redaction(provider: SecretProvider) -> RedactFilter:
    """Redact every record at creation via the log record factory.

    This covers all loggers and handlers, including ones configured later and loggers that
    don't propagate (uvicorn's), which a filter on the root logger would miss.
    """
    redactor = RedactFilter(provider)
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        redactor.filter(record)
        return record

    logging.setLogRecordFactory(factory)
    return redactor
