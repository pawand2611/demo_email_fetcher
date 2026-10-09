"""The classification flow as a LangGraph graph.

    START ─> classify_attachments ─┬─(attachment decided)───────────> combine ─> END
                                   └─(otherwise)─> classify_body ────> combine

LayoutLMv3 runs first. When it is above ``ATTACHMENT_DECIDES_CONFIDENCE`` it
settles the email on its own and Laya is skipped; otherwise Laya reads the
body and :func:`classifier.combine` makes the final decision. The thread rule
(later emails of a payment thread inherit it) is applied by the sync before
this graph is called.

Everything runs locally: no LLM, no network, and LangSmith tracing is switched
off in ``mailbox_viewer/__init__.py`` before LangGraph is imported. The sync
loop and re-classification call :meth:`EmailClassifier.classify`; they do not
need to know that a graph is involved.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


class ClassifyState(TypedDict, total=False):
    email: EmailInput
    body: BodyDecision
    attachments: tuple[AttachmentDecision, ...]
    decision: Decision


class EmailClassifier:
    def __init__(
        self,
        body_model: BodyModel,
        document_model: DocumentModel,
        min_confidence: float = 0.5,
        body_min_confidence: float = 0.7,
        decides_above: float = ATTACHMENT_DECIDES,
    ) -> None:
        self.body_model = body_model
        self.document_model = document_model
        self.min_confidence = min_confidence
        self.body_min_confidence = body_min_confidence
        self.decides_above = decides_above
        # Every model call runs on this one long-lived thread. LangGraph runs
        # nodes on pool threads, and PyTorch starts a new set of compute
        # threads for each new calling thread; without this, hundreds of
        # threads pile up and inference slows to a crawl.
        self._model_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="models")
        self._graph = self._build()

    def _on_model_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        return self._model_thread.submit(fn, *args).result()

    @property
    def name(self) -> str:
        return f"body: {self.body_model.name}, attachments: {self.document_model.name}"

    @property
    def has_any_model(self) -> bool:
        return self.body_model.name != "none" or self.document_model.name != "none"

    def classify(self, email: EmailInput) -> Decision:
        return self._graph.invoke({"email": email})["decision"]

    def inherit(self, email: EmailInput) -> Decision:
        """Thread rule: payment by inheritance. Laya is skipped; LayoutLMv3
        still labels any attachments so an invoice file is recognised."""
        if not email.attachments:
            return inherited_decision(0)
        labels = self._on_model_thread(classify_attachments, email.attachments, self.document_model)
        return inherited_decision(labels)

    def combine(self, body: BodyDecision, attachments: Sequence[AttachmentDecision]) -> Decision:
        """The decision rule on its own, for re-deciding from stored predictions."""
        return combine(body, attachments, self.min_confidence, self.body_min_confidence, self.decides_above)

    # -- graph ------------------------------------------------------------------------------

    def _build(self):
        graph = StateGraph(ClassifyState)
        graph.add_node("classify_attachments", self._attachments_node)
        graph.add_node("classify_body", self._body_node)
        graph.add_node("combine", self._combine_node)
        graph.add_edge(START, "classify_attachments")
        graph.add_conditional_edges(
            "classify_attachments",
            self._route_after_attachments,
            {"attachment decided": "combine", "consult body": "classify_body"},
        )
        graph.add_edge("classify_body", "combine")
        graph.add_edge("combine", END)
        return graph.compile()

    def _route_after_attachments(self, state: ClassifyState) -> str:
        if attachment_decides(state["attachments"], self.decides_above):
            return "attachment decided"
        return "consult body"

    def _attachments_node(self, state: ClassifyState) -> dict:
        return {"attachments": self._on_model_thread(classify_attachments, state["email"].attachments, self.document_model)}

    def _body_node(self, state: ClassifyState) -> dict:
        email = state["email"]
        return {"body": self._on_model_thread(classify_body, email.subject, email.body, self.body_model)}

    def _combine_node(self, state: ClassifyState) -> dict:
        body = state.get("body") or BodyDecision(None, None, "not run: attachment model decided")
        return {"decision": self.combine(body, state["attachments"])}

    def mermaid(self) -> str:
        """The graph as a Mermaid diagram, for docs and the UI."""
        return self._graph.get_graph().draw_mermaid()
