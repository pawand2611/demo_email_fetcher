"""The classification flow as a LangGraph graph.

    START ─┬─> classify_body ────────┬─> combine ─> END
           └─> classify_attachments ─┘

The two model steps run in parallel and write separate keys of the shared
state; ``combine`` applies the rule in :func:`classifier.combine`. Everything
runs locally: no LLM, no network, and LangSmith tracing is switched off in
``mailbox_viewer/__init__.py`` before LangGraph is imported.

The sync loop and re-classification call :meth:`EmailClassifier.classify`;
they do not need to know that a graph is involved.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .classifier import (
    AttachmentDecision,
    BodyDecision,
    BodyModel,
    Decision,
    DocumentModel,
    EmailInput,
    classify_attachments,
    classify_body,
    combine,
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
        body_min_confidence: float = 0.9,
    ) -> None:
        self.body_model = body_model
        self.document_model = document_model
        self.min_confidence = min_confidence
        self.body_min_confidence = body_min_confidence
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

    # -- graph ------------------------------------------------------------------------------

    def _build(self):
        graph = StateGraph(ClassifyState)
        graph.add_node("classify_body", self._body_node)
        graph.add_node("classify_attachments", self._attachments_node)
        graph.add_node("combine", self._combine_node)
        graph.add_edge(START, "classify_body")
        graph.add_edge(START, "classify_attachments")
        graph.add_edge(["classify_body", "classify_attachments"], "combine")
        graph.add_edge("combine", END)
        return graph.compile()

    def _body_node(self, state: ClassifyState) -> dict:
        email = state["email"]
        return {"body": self._on_model_thread(classify_body, email.subject, email.body, self.body_model)}

    def _attachments_node(self, state: ClassifyState) -> dict:
        return {"attachments": self._on_model_thread(classify_attachments, state["email"].attachments, self.document_model)}

    def _combine_node(self, state: ClassifyState) -> dict:
        return {"decision": self.combine(state["body"], state["attachments"])}

    def combine(self, body: BodyDecision, attachments) -> Decision:
        """The decision rule on its own, for re-deciding from stored predictions."""
        return combine(body, attachments, self.min_confidence, self.body_min_confidence)

    def mermaid(self) -> str:
        """The graph as a Mermaid diagram, for docs and the UI."""
        return self._graph.get_graph().draw_mermaid()
