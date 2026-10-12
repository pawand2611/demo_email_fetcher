"""How it decides: the three checks, the live thresholds, and worked scenarios."""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from ui import TIERS, get_api

api = get_api()
st.title("🧠 How it decides")
st.caption("Three checks, in order. The first confident answer wins. "
           "Thresholds below are read live from the backend's settings.")

try:
    p = api.profiles()[0]
except ApiError as exc:
    st.error(str(exc))
    st.stop()

decides = p["attachment_decides_confidence"]
bar = p["body_min_confidence"]
floor = p["model_min_confidence"]
labels = " / ".join(p["payment_labels"])

a, b, c, d = st.columns(4)
a.metric("Attachment decides alone", f"> {decides:.2f}", help="LayoutLMv3 score needed to skip the body model")
b.metric("Email text counts as payment", f"≥ {bar:.2f}", help=f"Laya score on {labels}")
c.metric("Review band", f"{floor:.2f} – {bar:.2f}", help="Uncertain payment answers go to a person")
d.metric("Payment types", len(p["payment_labels"]), help=labels)

PAY, REVIEW, NONE, CHECK = "#e3f2e9", "#fff0d6", "#ecebe6", "#fff2df"
dot = f"""
digraph flow {{
  rankdir=LR; bgcolor=transparent; nodesep=0.35; ranksep=0.45;
  node [fontname="Helvetica" fontsize=11 style="rounded,filled" shape=box color="#c9c6be" fontcolor="#181c23"];
  edge [fontname="Helvetica" fontsize=10 color="#8a8d87" fontcolor="#8a8d87" arrowsize=0.7];

  email [label="New email" fillcolor="#ffffff"];
  c1 [label="Check 1\\nThread already\\npayment?" fillcolor="{CHECK}" color="#ffad28"];
  c2 [label="Check 2\\nLayoutLMv3 on attachment\\nscore > {decides:.2f}?" fillcolor="{CHECK}" color="#ffad28"];
  c3 [label="Check 3\\nLaya reads the email text" fillcolor="{CHECK}" color="#ffad28"];

  p1 [label="PAYMENT\\ninherited" fillcolor="{PAY}"];
  p2 [label="PAYMENT\\nconfident invoice" fillcolor="{PAY}"];
  n2 [label="NONE\\nconfident non-invoice" fillcolor="{NONE}"];
  p3 [label="PAYMENT\\n{labels} ≥ {bar:.2f}" fillcolor="{PAY}"];
  r3 [label="REVIEW\\n{floor:.2f} – {bar:.2f}, or a weak\\ninvoice the text doesn't back" fillcolor="{REVIEW}"];
  n3 [label="NONE\\neverything else" fillcolor="{NONE}"];
  log [label="decision_log\\nmodel · score · reason" shape=cylinder fillcolor="#ffffff"];

  email -> c1;
  c1 -> p1 [label="yes"];
  c1 -> c2 [label="no"];
  c2 -> p2 [label="invoice"];
  c2 -> n2 [label="not invoice"];
  c2 -> c3 [label="no attachment, or ≤ {decides:.2f}"];
  c3 -> p3; c3 -> r3; c3 -> n3;
  {{p1 p2 n2 p3 r3 n3}} -> log [style=dashed];
}}
"""
st.graphviz_chart(dot, width="stretch")

st.divider()
st.subheader("Why each check is there")
w1, w2, w3 = st.columns(3)
with w1, st.container(border=True):
    st.markdown("**Check 1 · the thread**")
    st.write("Replies like \"PFA\" or \"Thanks\" say nothing alone. Once a conversation is payment-related, "
             "its later replies stay with it. Their attachments are still labelled by LayoutLMv3.")
with w2, st.container(border=True):
    st.markdown("**Check 2 · the attachment**")
    st.write(f"The document itself is the strongest evidence. LayoutLMv3 reads its words and their position "
             f"on the page. Only a verdict above {decides:.2f} decides alone; anything weaker gets a second opinion.")
with w3, st.container(border=True):
    st.markdown("**Check 3 · the email text**")
    st.write(f"Laya is asked what the document is. {labels.capitalize()} at {bar:.2f} or more is payment; "
             f"{floor:.2f} to {bar:.2f} goes to review instead of being dropped silently.")

st.divider()
st.subheader("Scenarios")
SCENARIOS = [
    ("1 · An email with an invoice attached",
     f"LayoutLMv3 scores the invoice above {decides:.2f}, so check 2 decides: **payment**. Laya is not needed."),
    ("2 · A reply in a payment thread",
     "The thread is already payment-related, so check 1 keeps the reply as **inherited**. "
     "Any new attachment is still labelled."),
    ("3 · Payment mail first, invoice sent in a later reply",
     "Laya marks the first email payment from its text (check 3). The later invoice-only reply joins the same "
     "thread and is kept by check 1."),
    ("4 · A plain first email, invoice in the reply",
     "The first email alone is not payment. When the reply brings the invoice, the earlier messages of that "
     "thread are **fetched back in**, so the conversation is complete."),
]
for title, text in SCENARIOS:
    with st.expander(title):
        st.markdown(text)

st.divider()
st.subheader("When rules or models change")
m1, m2 = st.columns(2)
with m1, st.container(border=True):
    st.markdown("**⚖️ Re-apply rules**")
    st.write("Runs the rules again on the scores already stored. No models, no mailbox; about a minute. "
             "Use it after changing a threshold in the backend's settings and restarting the backend.")
with m2, st.container(border=True):
    st.markdown("**🧠 Re-run models**")
    st.write("Runs the models again over cached mail. Attachments only takes about 20 seconds; "
             "a full run takes many minutes. Use it after a model is retrained.")
st.caption(f"Tier in the log: {TIERS}. Both buttons are in the sidebar under Maintenance.")
