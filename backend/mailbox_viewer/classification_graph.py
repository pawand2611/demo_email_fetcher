"""The classification flow as a LangGraph graph.

    START ─┬─(new email)──> classify_attachments ─┬─(attachment decided)──────────> combine ─┐
           │                                      └─(otherwise)─> classify_body ──> combine  │
           └─(reply in a payment thread)──> inherit ─────────────────────────────────────────┤
                                                                                              │
          ┌─────────────────────(payment, intent on)──────────────────────────────────────────┤
          v                                                                                   │
    intent_tier1 ─┬─(unsure, tier 2 inline)──> intent_tier2 ──> END      (not payment) ──> END
                  └─(confident, or tier 2 left to the background job)──> END

* **Payment decision.** LayoutLMv3 runs first. When it is above
  ``ATTACHMENT_DECIDES_CONFIDENCE`` it settles the email on its own and Laya is
  skipped; otherwise Laya reads the body and :func:`classifier.combine` makes
  the final decision. A reply in a thread already classified as payment takes
  the ``inherit`` path: payment by the thread rule, LayoutLMv3 still labels its
  attachments, Laya is not run.
* **Intent.** Every payment email, by the models or inherited, then gets an
  intent (``intent.py``): Laya tier 1, and DeepSeek tier 2 when tier 1 is
  unsure. Tier 2 takes minutes on CPU, so by default the email is left
  ``pending`` and a background job finishes it; ``TIER2_INLINE`` runs it here.

Everything runs locally. LangSmith tracing is switched off in
``mailbox_viewer/__init__.py`` before LangGraph is imported; tier 2 talks only
to a llama.cpp server on localhost. The sync loop and re-classification call
:meth:`EmailClassifier.run`; they do not need to know that a graph is involved.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph

from .classifier import (
    ATTACHMENT_DECIDES,
    AttachmentDecision,
    BodyDecision,
    BodyModel,
    Decision,
    DocumentModel,
    EmailInput,
    attachment_decides,
    classify_attachments,
    classify_body,
    combine,
    inherited_decision,
)
from .intent import TIER2_RETRYABLE, IntentDecision, IntentStage, decide_after_tier1, decide_after_tier2

logger = logging.getLogger(__name__)


class ClassifyState(TypedDict, total=False):
    email: EmailInput
    inherited: bool  # the thread is already a payment thread
    with_intent: bool  # run the intent step after a payment decision
    body: BodyDecision
    attachments: tuple[AttachmentDecision, ...]
    decision: Decision
    intent: IntentDecision | None
    intent_input: dict[str, str] | None


@dataclass(frozen=True)
class Classification:
    """The flow's full answer for one email."""

    decision: Decision
    intent: IntentDecision | None = None
    # The email state tier 2 needs; kept while tier 2 is pending.
    intent_input: dict[str, str] | None = None


class EmailClassifier:
    def __init__(
        self,
        body_model: BodyModel,
        document_model: DocumentModel,
        min_confidence: float = 0.5,
        body_min_confidence: float = 0.7,
        decides_above: float = ATTACHMENT_DECIDES,
        intent: IntentStage | None = None,
    ) -> None:
        self.body_model = body_model
        self.document_model = document_model
        self.min_confidence = min_confidence
        self.body_min_confidence = body_min_confidence
        self.decides_above = decides_above
        self.intent = intent
        # Every model call runs on this one long-lived thread. LangGraph runs
        # nodes on pool threads, and PyTorch starts a new set of compute
        # threads for each new calling thread; without this, hundreds of
        # threads pile up and inference slows to a crawl. It also serialises
        # Laya, whose RLAgent is not documented as thread-safe.
        self._model_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="models")
        self._graph = self._build()

    def _on_model_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        return self._model_thread.submit(fn, *args).result()

    @property
    def name(self) -> str:
        text = f"body: {self.body_model.name}, attachments: {self.document_model.name}"
        return text + (f", intent: {self.intent.name}" if self.intent else "")

    @property
    def has_any_model(self) -> bool:
        return self.body_model.name != "none" or self.document_model.name != "none"

    @property
    def has_intent(self) -> bool:
        return self.intent is not None

    def run(self, email: EmailInput, inherited: bool = False) -> Classification:
        """The whole flow: payment decision, then intent for payment emails."""
        out = self._graph.invoke({"email": email, "inherited": inherited, "with_intent": True})
        return Classification(out["decision"], out.get("intent"), out.get("intent_input"))

    def classify(self, email: EmailInput) -> Decision:
        """Payment decision only (no intent step)."""
        return self._graph.invoke({"email": email, "inherited": False, "with_intent": False})["decision"]

    def inherit(self, email: EmailInput) -> Decision:
        """Thread rule only: payment by inheritance. Laya is skipped; LayoutLMv3
        still labels any attachments so an invoice file is recognised."""
        return self._graph.invoke({"email": email, "inherited": True, "with_intent": False})["decision"]

    def payment_confirmation(self, subject: str, body: str) -> float | None:
        """For tickets: how sure the body model is that this email confirms a
        payment. None when there is no body model or no text to read."""
        return self._on_model_thread(self.body_model.confirms_payment, subject, body)

    def combine(self, body: BodyDecision, attachments: Sequence[AttachmentDecision]) -> Decision:
        """The decision rule on its own, for re-deciding from stored predictions."""
        return combine(body, attachments, self.min_confidence, self.body_min_confidence, self.decides_above)

    # -- graph ------------------------------------------------------------------------------

    def _build(self):
        graph = StateGraph(ClassifyState)
        graph.add_node("classify_attachments", self._attachments_node)
        graph.add_node("classify_body", self._body_node)
        graph.add_node("combine", self._combine_node)
        graph.add_node("inherit", self._inherit_node)
        graph.add_node("intent_tier1", self._intent_tier1_node)
        graph.add_node("intent_tier2", self._intent_tier2_node)

        graph.add_conditional_edges(START, self._route_start, {"new email": "classify_attachments", "payment thread": "inherit"})
        graph.add_conditional_edges(
            "classify_attachments",
            self._route_after_attachments,
            {"attachment decided": "combine", "consult body": "classify_body"},
        )
        graph.add_edge("classify_body", "combine")
        for decided in ("combine", "inherit"):
            graph.add_conditional_edges(decided, self._route_to_intent, {"payment": "intent_tier1", "done": END})
        graph.add_conditional_edges("intent_tier1", self._route_after_tier1, {"tier 2 now": "intent_tier2", "done": END})
        graph.add_edge("intent_tier2", END)
        return graph.compile()

    # routes

    def _route_start(self, state: ClassifyState) -> str:
        return "payment thread" if state.get("inherited") else "new email"

    def _route_after_attachments(self, state: ClassifyState) -> str:
        if attachment_decides(state["attachments"], self.decides_above):
            return "attachment decided"
        return "consult body"

    def _route_to_intent(self, state: ClassifyState) -> str:
        wanted = state.get("with_intent") and self.intent is not None
        return "payment" if wanted and state["decision"].is_payment else "done"

    def _route_after_tier1(self, state: ClassifyState) -> str:
        decision = state.get("intent")
        if decision is not None and decision.pending and self.intent is not None and self.intent.tier2_inline:
            return "tier 2 now"
        return "done"

    # nodes

    def _attachments_node(self, state: ClassifyState) -> dict:
        return {"attachments": self._on_model_thread(classify_attachments, state["email"].attachments, self.document_model)}

    def _body_node(self, state: ClassifyState) -> dict:
        email = state["email"]
        return {"body": self._on_model_thread(classify_body, email.subject, email.body, self.body_model)}

    def _combine_node(self, state: ClassifyState) -> dict:
        body = state.get("body") or BodyDecision(None, None, "not run: attachment model decided")
        return {"decision": self.combine(body, state["attachments"])}

    def _inherit_node(self, state: ClassifyState) -> dict:
        email = state["email"]
        if not email.attachments:
            return {"decision": inherited_decision(0)}
        labels = self._on_model_thread(classify_attachments, email.attachments, self.document_model)
        return {"decision": inherited_decision(labels)}

    def _intent_tier1_node(self, state: ClassifyState) -> dict:
        stage = self.intent
        assert stage is not None
        email = state["email"]
        try:
            intent_input = stage.state_for(email.sender, email.recipients, email.subject, email.body, email.body_html)
            t1 = self._on_model_thread(stage.tier1.classify, intent_input)
        except Exception as exc:  # the intent step must never stop the sync
            logger.warning("intent tier 1 failed: %s: %s", type(exc).__name__, exc)
            return {"intent": None, "intent_input": None}
        decision = decide_after_tier1(t1, stage.min_confidence, stage.tier2_active)
        return {"intent": decision, "intent_input": intent_input if decision.pending else None}

    def _intent_tier2_node(self, state: ClassifyState) -> dict:
        stage = self.intent
        assert stage is not None and stage.tier2 is not None
        # An HTTP call, not PyTorch: it does not need the model thread.
        result = stage.tier2.classify(state["intent_input"])
        decision = decide_after_tier2(state["intent"], result)
        keep_input = decision.tier2_status in TIER2_RETRYABLE  # may be retried later
        return {"intent": decision, "intent_input": state["intent_input"] if keep_input else None}

    def mermaid(self) -> str:
        """The graph as a Mermaid diagram, for docs and the UI."""
        return self._graph.get_graph().draw_mermaid()
