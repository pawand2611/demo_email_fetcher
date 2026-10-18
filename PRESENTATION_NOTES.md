# Presentation flow and speaker notes

Monthly Review 1 · Mailbox Viewer (Group 08) · about 15–20 minutes.
Deck: the 15-slide project update (also printable from `Monthly_Review_1_Mailbox_Viewer.html`).

**The story in one line:** *"We built an AI assistant that finds payment
documents in a mailbox, tracks every payment conversation until it's paid,
and we've already found and fixed its biggest flaws."*

| Part | Slides | Time |
|---|---|---|
| Opening and status | 1–4 | 4 min |
| What we built | 5–8 | 4 min |
| How we decided | 9–10 | 2 min |
| The AI models | 11–12 | 3 min |
| Honest look and what's next | 13–15 | 4 min |
| Live demo (optional) and questions | — | 5+ min |

---

## Part 1: Opening and status

### Slide 1: Title (20 s)
> "This is our Super25 project, the Mailbox Viewer. Roctotpal and I built it,
> with Venkateshwar as our mentor. I'll show what we built, where we are
> against the plan, and what we learned."

### Slide 2: Project Summary (1 min)
> "The problem: finance teams receive invoices, receipts and statements mixed
> in with everything else, and someone has to read every email to find them.
> Our assistant does that automatically. It reads the mailbox, finds the
> payment documents, and keeps an aging view of every payment conversation.
> We'll call it successful when every payment email is caught with an
> explainable decision, and when we can show the accuracy on real mail."

### Slide 3: Milestones (1.5 min)
> "Month 1 is complete: the data model and mailbox sync, the first attachment
> model, and the backend with the viewer. In Month 2 the retrained model is
> done and aging tickets are on track. Next come measuring accuracy on real
> mail, then login and the production frontend, and then the pilot in Month 3."

*Tip:* be ready to explain M4. Only the attachment model (LayoutLMv3) was
retrained. Laya is used as published, and fine-tuning it is the next step.

### Slide 4: Demo divider (10 s)
> "Now the work itself."

---

## Part 2: What we built

### Slide 5: My work so far (45 s)
> "I started with two warm-up projects, a Spring Boot REST app tested with
> Postman and a FastAPI CRUD app, to learn API building. The same FastAPI now
> runs this project's backend. Then the main project, step by step: data
> model, sync, the backend and frontend split, the AI models, and the
> decision rules."

### Slide 6: Where we are (1.5 min). This is the key slide, so slow down.
> "The whole pipeline works end to end. We **fetch** Gmail, read-only. We
> **cache** it in PostgreSQL on AWS and **sync** only new mail. To
> **classify**, two separate AI pipelines run on our own machine: LayoutLMv3
> reads attachments, including scanned ones, and Laya reads the email text.
> Every payment conversation becomes an aging **ticket** that opens, moves to
> in progress when we reply, and closes when payment is confirmed. And every
> decision is **logged with its reason**."

*The line to land:* "Zero emails go to an outside AI service; everything runs
on our machine."

### Slide 7: Architecture (1 min)
> "Two services. The backend is the only part that touches Gmail, the models
> and the database. The viewer only talks to the backend. That means another
> frontend, or another business process, could reuse the same backend
> unchanged."

### Slide 8: ERD (45 s)
> "Six tables for the mail. A thread holds many emails, and each email has
> its participants and attachments. The decision log records every email we
> judged, even the ones we didn't keep, so nothing is decided silently. Two
> more tables hold the tickets."

---

## Part 3: How we decided

### Slide 9: Decisions: platform (1 min)
> "At every step we compared options. Two examples. We moved from SQLite to
> PostgreSQL on AWS for a shared database. And we tried Azure storage through
> an emulator, but its telemetry raised a security alert, so we removed it and
> store files locally. Each row on this slide is a deliberate choice with a
> reason."

### Slide 10: Decisions: classification (1 min)
> "We replaced keyword rules with trained models, because models understand
> meaning and layout. We use both the attachment and the email text,
> attachment first. And we tuned the thresholds on our real mailbox, for
> example sending only uncertain *payment* answers to review rather than every
> uncertain email."

---

## Part 4: The AI models

### Slide 11: LayoutLMv3 (v2) (1.5 min)
> "The attachment model answers one question: is this an invoice? Roctotpal
> retrained it as v2 on 3,800 varied documents, updating every layer instead
> of just the last one. On his test sets it went from 77–98% to 100%. More
> importantly, on our own real documents, the invoice the old model missed is
> now detected, and false alarms like a business letter are gone."

*Caveat to say yourself, before anyone asks:* "Five documents is a sanity
check, not an accuracy figure. Measuring on real mail is our next milestone."

### Slide 12: Laya (1 min)
> "Laya reads the email text and tells us what kind of document it's about.
> We tested six ways of asking it. Asking the document type from the body
> alone was best, at 100% on generated test emails, and that's exactly the
> setup we use. On real mail it's weaker, for example short 'Re: invoice'
> replies, which is why we plan to fine-tune it on our own emails."

---

## Part 5: An honest look, and what's next

### Slide 13: Flaws (1.5 min). Present this one confidently: it shows testing works.
> "Testing on a real mailbox found two real flaws, and we fixed both. One:
> invoice photos sent from a phone were ignored, because Gmail embeds them
> like a signature logo. Two: the first model missed a clearly English
> invoice. That's fixed by the v2 retrain. Still open: we haven't measured
> accuracy on real mail, short replies are hard for Laya, and speed on a
> laptop CPU is about 6 seconds per email."

### Slide 14: Aging tickets and roadmap (1.5 min)
> "Every payment conversation becomes a ticket: open with the first payment
> email, in progress when we reply, closed automatically when an email
> confirms the payment, or by hand. A new invoice can never close a ticket by
> mistake. Next: measure accuracy on real mail, fine-tune Laya, then login,
> the production frontend, and a pilot."

### Slide 15: Future improvements (1 min)
> "To make it fast at scale: the right concurrency tool for each kind of
> work. For example, asyncpg would let the database queries for a page run at
> the same time instead of one after another. For the models: we already load
> them once at start-up, and micro-batching, half precision and ONNX
> quantisation would cut the 6 seconds per email."

### Close (15 s)
> "In short: it works end to end, we found and fixed its biggest flaws, and
> the next step is proving accuracy on real mail. Happy to take questions, or
> show it live."

---

## Live demo (optional, 3–4 min)

**Do it only if the backend connected before the meeting.** Otherwise, skip
it and say the slides show the working system.

1. **Overview:** the numbers, ticket counts and decision chart.
2. **Inbox:** open a payment conversation, then show an email's badges
   (decision, document type, which check decided) and its **Decision** tab
   with the reason.
3. **Tickets:** a ticket's age and history; close one by hand with a reason,
   then reopen it.
4. **Decision log:** filter to "not kept" to show that even dropped emails are
   recorded.

---

## Likely questions

| Question | Answer |
|---|---|
| How accurate is it on real mail? | Not measured yet; all figures are from test data. That's milestone M6: a small labelled set of real, anonymised emails. |
| Is the data safe? | The models run on our machine, offline, with telemetry off. The mailbox is read-only. The only data that leaves the laptop goes to our own AWS database. |
| Why two models? | Attachments carry the document itself, and the email text covers emails without one. Attachment first; text as the second opinion. |
| What did it cost? | No paid AI service. The models are open and run locally. The cost is the AWS database and laptop compute. |
| Why is it slow sometimes? | The database is in AWS Virginia, about 0.5 s per query. We added batching and caching. Moving the backend close to the database is the real fix. |
| Can it be used elsewhere? | Yes. The backend is separate, and the thresholds are settings, so a new use case needs a new model or rule, not a rebuild. |
| What's the biggest risk? | Real-mail accuracy is unproven, and Laya fine-tuning needs approval to use real emails and access to a GPU. |

---

## Before the meeting

- **The night before:** print or export the PDF as a fallback.
- **30 minutes before:** start the backend (`cd backend`, then
  `uvicorn main:app`) and check the database connects (the network has been
  unreliable). Then start the frontend (`cd frontend`, then
  `streamlit run app.py`) and press **♻️ Reload data**.
- **Slide 3:** fix the review date on the title slide, and make sure the
  milestone dates match what you agreed with your mentor.
- **Practice slides 6 and 13 out loud:** they carry the story.
