"""Mailbox Viewer core: IMAP sync, threading, two-model classification, storage.

Every backend entry point imports this package first, so the environment
below is in place before Transformers, Hugging Face Hub or LangGraph load:

* Hugging Face works offline only and sends no telemetry; models load from
  local folders.
* LangSmith / LangChain tracing is off, so no run data leaves the machine.

``setdefault`` is not used on purpose: these must hold even if a shell or
``.env`` tries to turn them on.
"""

import os

for _key, _value in {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
    "LANGSMITH_API_KEY": "",
    "LANGCHAIN_API_KEY": "",
}.items():
    os.environ[_key] = _value
