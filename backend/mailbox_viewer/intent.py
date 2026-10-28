"""Two-tier intent classification for billing emails: Laya, then DeepSeek.

Runs on every email whose final decision is ``payment`` (decided by the
models, or inherited from a payment thread). It answers "what is this billing
email for?" with one of :data:`INTENTS`.

* **Tier 1, Laya typed-decisions** (local, ~1.6 s on CPU) answers one
  ``choice`` question and returns per-intent probabilities and a
  ``confidence`` (1 - normalised entropy). At or above
  ``TIER1_MIN_CONFIDENCE`` its answer is final.
* **Tier 2, DeepSeek-R1-Distill-Llama-8B** behind a local llama.cpp server
  (~100 s on CPU) decides the emails tier 1 is unsure about. On CPU it runs
  asynchronously: the email is stored with ``tier2_status = "pending"`` and a
  background job resolves it later (``intent_jobs.py``). With a GPU it can run
  inline in the LangGraph flow (``TIER2_INLINE=true``).
* If tier 2 is unavailable, times out or gives no valid label, tier 1's answer
  stands with ``needs_review = True``. A final ``other`` always needs review.

This module holds the labels, prompt, parser, router and the llama.cpp
client. It imports no model code; the Laya loader lives in
``document_model.py``. Nothing here sends email content anywhere but the
configured localhost llama.cpp server.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

# -- labels (exact keys and descriptions from the specification) -----------------------------

INTENTS: dict[str, str] = {
    "new_invoice": "a first bill for a charge, sent for payment",
    "payment_reminder": "a reminder about an upcoming or overdue payment due date",
    "payment_confirmation": "confirms a payment was received, or sends a receipt",
    "credit_note": "a refund, credit memo or billing correction",
    "dispute_query": "a question about, or a discrepancy flagged on, a bill",
    "partial_payment": "acknowledges or announces that only part of the amount was paid",
    "cancellation": "an invoice was voided, withdrawn or cancelled",
    "other": "anything else, for human review",
}
INTENT_KEYS: tuple[str, ...] = tuple(INTENTS)
INTENT_OTHER = "other"

# Tier 1: the Laya question.
QUESTION: dict[str, Any] = {
    "intent": {
        "type": "choice",
        "instructions": "What is the purpose of this billing email (use `subject` and `body`)?",
        "criteria": INTENTS,
    }
}

# Who made the final call.
DECIDED_TIER1 = "tier1"
DECIDED_TIER2 = "tier2"
DECIDED_FALLBACK = "tier1_fallback"
DECIDED_BY = (DECIDED_TIER1, DECIDED_TIER2, DECIDED_FALLBACK)

# Tier-2 status. ``pending`` = queued for the background job; None = not called.
TIER2_PENDING = "pending"
TIER2_OK = "ok"
TIER2_UNPARSED = "unparsed"
TIER2_TIMEOUT = "timeout"
TIER2_UNAVAILABLE = "unavailable"
TIER2_STATUSES = (TIER2_PENDING, TIER2_OK, TIER2_UNPARSED, TIER2_TIMEOUT, TIER2_UNAVAILABLE)
# Failures worth retrying later (the stored input is kept for these).
TIER2_RETRYABLE = (TIER2_TIMEOUT, TIER2_UNAVAILABLE)

# -- tier 2: prompt and request (verbatim from the specification) -----------------------------

DECISION_RULES = """Rules:
- partial_payment: the email says only part of the amount was paid (a short payment, an instalment, a remaining or open balance after a payment). This wins over payment_reminder and payment_confirmation.
- payment_confirmation: the full payment was received or a receipt is sent; nothing is still owed.
- payment_reminder: nothing has been paid yet and the sender asks for payment of an existing bill.
- dispute_query: the recipient questions or disputes a bill (wrong amount, missing PO number, double charge, item not delivered).
- new_invoice: a new bill is being sent for payment.
- other: not about a specific bill's payment status, e.g. bank detail changes, tax forms, vendor registration, statement requests, contact changes, holiday notices, meetings."""

# DeepSeek-R1 guidance: no system prompt, temperature 0.6, top_p 0.95. The
# temperature and token budget are settings; these two are fixed by the spec.
TIER2_TOP_P = 0.95
TIER2_SEED = 42
# The health probe must answer fast; a slow /health means the server is busy or down.
TIER2_HEALTH_TIMEOUT_S = 5.0

_TIER2_ANSWER_RE = re.compile(r'\{\s*"intent"\s*:\s*"([a-z_]+)"\s*\}')


def build_prompt(state: Mapping[str, str]) -> str:
    labels = "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
    email = "\n".join(f"{k.capitalize()}: {v}" for k, v in state.items() if v)
    return (f"Classify the intent of this billing email into exactly one label.\n\nLabels:\n{labels}\n\n"
            f"{DECISION_RULES}\n\nEmail:\n{email}\n\nThink briefly, then end your answer with one line of JSON "
            'exactly like {"intent": "<label>"} using one of the labels above.')


def parse_tier2(content: str | None, reasoning: str | None) -> str | None:
    """The last ``{"intent": "<label>"}`` whose label is one of the 8 keys,
    searched in the answer and then the reasoning. None when there is none:
    free text is never interpreted."""
    text = (content or "") + "\n" + (reasoning or "")
    valid = [label for label in _TIER2_ANSWER_RE.findall(text) if label in INTENTS]
    return valid[-1] if valid else None


# -- the email "state" shared by both tiers ------------------------------------------------------


def build_state(
    headers: Mapping[str, str],
    body: str,
    clean: Callable[[str], str] | None = None,
    max_chars: int | None = None,
) -> dict[str, str]:
    """``{"from", "to", "subject", "body"}`` for both tiers. ``clean`` is Laya's
    ``clean_email_body`` (strips quoted replies, signatures, disclaimers); it is
    passed in because it ships with the Laya model files. Attachment names and
    other metadata are deliberately not part of the state."""
    if clean is not None:
        body = clean(body)
    if max_chars is not None:
        body = body[:max_chars]
    return {
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "body": body,
    }


def headers_for(sender: str, recipients: str, subject: str) -> dict[str, str]:
    return {"From": sender, "To": recipients, "Subject": subject}


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Plain text of an HTML body, for HTML-only emails. Standard library only."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep what was read
        pass
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


# -- results and the router ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tier1Result:
    intent: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class Tier2Result:
    intent: str | None
    status: str  # ok | unparsed | timeout | unavailable
    seconds: float | None = None


@dataclass(frozen=True)
class IntentDecision:
    intent: str  # final label (one of the 8)
    decided_by: str  # tier1 | tier2 | tier1_fallback
    needs_review: bool
    tier1_intent: str
    tier1_confidence: float
    tier1_probabilities: dict[str, float] = field(default_factory=dict)
    tier2_intent: str | None = None
    tier2_status: str | None = None  # pending | ok | unparsed | timeout | unavailable | None (not called)
    tier2_seconds: float | None = None

    @property
    def pending(self) -> bool:
        return self.tier2_status == TIER2_PENDING


class IntentError(RuntimeError):
    """A model answered with something outside the 8 intents, or could not be loaded."""


def checked_tier1(intent: str, probabilities: Mapping[str, Any], confidence: Any) -> Tier1Result:
    """Validate Laya's raw answer: the label must be one of the 8 keys."""
    if intent not in INTENTS:
        raise IntentError(f"tier 1 answered {intent!r}, not one of the {len(INTENTS)} intents")
    probs = {k: float(probabilities.get(k, 0.0)) for k in INTENT_KEYS}
    return Tier1Result(intent, probs, max(0.0, min(1.0, float(confidence))))


def decide_after_tier1(t1: Tier1Result, min_confidence: float, tier2_enabled: bool) -> IntentDecision:
    """Confident tier 1 is final. Otherwise the email waits for tier 2
    (``pending``), or falls back to tier 1 for review when tier 2 is off."""
    base = dict(tier1_intent=t1.intent, tier1_confidence=t1.confidence, tier1_probabilities=dict(t1.probabilities))
    if t1.confidence >= min_confidence:
        return IntentDecision(t1.intent, DECIDED_TIER1, t1.intent == INTENT_OTHER, **base)
    if not tier2_enabled:
        return IntentDecision(t1.intent, DECIDED_FALLBACK, True, **base)
    # Until tier 2 answers, tier 1's low-confidence guess is shown and flagged for review.
    return IntentDecision(t1.intent, DECIDED_TIER1, True, tier2_status=TIER2_PENDING, **base)


def decide_after_tier2(previous: IntentDecision, t2: Tier2Result) -> IntentDecision:
    """Apply tier 2's answer to a pending (or failed, being retried) decision."""
    if t2.intent in INTENTS and t2.status == TIER2_OK:
        return replace(previous, intent=t2.intent, decided_by=DECIDED_TIER2, needs_review=t2.intent == INTENT_OTHER,
                       tier2_intent=t2.intent, tier2_status=TIER2_OK, tier2_seconds=t2.seconds)
    return replace(previous, intent=previous.tier1_intent, decided_by=DECIDED_FALLBACK, needs_review=True,
                   tier2_intent=None, tier2_status=t2.status, tier2_seconds=t2.seconds)


# -- model interfaces ---------------------------------------------------------------------------------


class IntentTier1Model(Protocol):
    name: str

    def classify(self, state: Mapping[str, str]) -> Tier1Result: ...


class IntentTier2Model(Protocol):
    name: str

    def healthy(self) -> bool: ...

    def classify(self, state: Mapping[str, str]) -> Tier2Result:
        """Never raises: every failure is a status."""
        ...


def classify_intent(state: Mapping[str, str], tier1: IntentTier1Model, tier2: IntentTier2Model | None,
                    min_confidence: float, tier2_enabled: bool) -> IntentDecision:
    """The whole router in one synchronous call (used by the inline path and tests)."""
    decision = decide_after_tier1(tier1.classify(state), min_confidence, tier2_enabled and tier2 is not None)
    if not decision.pending:
        return decision
    assert tier2 is not None
    return decide_after_tier2(decision, tier2.classify(state))


@dataclass
class IntentStage:
    """Everything the LangGraph intent nodes need, built once at start-up."""

    tier1: IntentTier1Model
    tier2: IntentTier2Model | None
    min_confidence: float = 0.4
    tier2_enabled: bool = True
    tier2_inline: bool = False
    clean: Callable[[str], str] | None = None
    max_chars: int | None = 3000

    @property
    def tier2_active(self) -> bool:
        return self.tier2_enabled and self.tier2 is not None

    @property
    def name(self) -> str:
        if not self.tier2_active:
            return f"{self.tier1.name} (tier 2 off)"
        return f"{self.tier1.name} -> {self.tier2.name} ({'inline' if self.tier2_inline else 'background job'})"

    def state_for(self, sender: str, recipients: str, subject: str, body: str, body_html: str = "") -> dict[str, str]:
        text = body if body.strip() else html_to_text(body_html)
        return build_state(headers_for(sender, recipients, subject), text, self.clean, self.max_chars)


# -- tier 2: the llama.cpp server client ----------------------------------------------------------------


class LlamaServerTier2:
    """DeepSeek-R1-Distill behind llama.cpp's ``llama-server`` on localhost.
    Standard library HTTP; no new dependency."""

    def __init__(self, url: str, timeout_s: float, max_tokens: int, temperature: float) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.name = "deepseek-r1-distill (llama.cpp)"

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=TIER2_HEALTH_TIMEOUT_S) as resp:
                return resp.status == 200
        except (OSError, ValueError):
            return False

    def classify(self, state: Mapping[str, str]) -> Tier2Result:
        started = time.monotonic()
        if not self.healthy():
            return Tier2Result(None, TIER2_UNAVAILABLE, None)
        payload = {
            "messages": [{"role": "user", "content": build_prompt(state)}],
            "temperature": self.temperature,
            "top_p": TIER2_TOP_P,
            "max_tokens": self.max_tokens,
            "seed": TIER2_SEED,
        }
        request = urllib.request.Request(
            f"{self.url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # Non-streaming: the server sends nothing until the answer is complete,
            # so the read timeout bounds the whole request.
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (TimeoutError, socket.timeout):
            return Tier2Result(None, TIER2_TIMEOUT, _since(started))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return Tier2Result(None, TIER2_TIMEOUT, _since(started))
            logger.warning("tier 2 request failed: %s", exc)
            return Tier2Result(None, TIER2_UNAVAILABLE, _since(started))
        except (OSError, ValueError) as exc:  # connection reset, bad JSON
            logger.warning("tier 2 request failed: %s: %s", type(exc).__name__, exc)
            return Tier2Result(None, TIER2_UNAVAILABLE, _since(started))

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return Tier2Result(None, TIER2_UNPARSED, _since(started))
        label = parse_tier2(message.get("content"), message.get("reasoning_content"))
        return Tier2Result(label, TIER2_OK if label else TIER2_UNPARSED, _since(started))


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)


def log_decision(message_id: str, decision: IntentDecision) -> None:
    """One line per email: never the body."""
    logger.info(
        "intent %s: %s by %s (tier1 %s %.2f, tier2 %s/%s, %ss)%s",
        message_id, decision.intent, decision.decided_by, decision.tier1_intent, decision.tier1_confidence,
        decision.tier2_intent or "-", decision.tier2_status or "-",
        decision.tier2_seconds if decision.tier2_seconds is not None else "-",
        " needs review" if decision.needs_review else "",
    )
