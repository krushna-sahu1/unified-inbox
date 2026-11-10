---
name: inbox
description: How to help with the user's WhatsApp, Instagram and LinkedIn messages through the unified-inbox tools - catching up, answering "did X reply?", briefing on a person before replying, tracking who is waiting, drafting in the user's voice, and handling memory questions.
---

# Working with the unified inbox

The user's chats from WhatsApp, Instagram and LinkedIn are synced in the background. Tools read
that local copy, so most answers are instant. Keep context small: fetch what the question needs,
not whole histories.

## Pick the right tool

| The user asks | Use |
|---|---|
| "Anything new?", "did Priya reply?" | `inbox_updates` (since you last checked) |
| "What needs a reply?", morning catch-up | `inbox_triage`, then `inbox_loops` with `show=waiting_on_you` |
| "Who am I keeping waiting?", "what did I promise?" | `inbox_loops` |
| Anything about one person, or before drafting a reply | `inbox_context` with `person` or `chat_id` |
| "Find the message about the rent / kiraya" | `inbox_search` (exact words first, then related by meaning) |
| The full recent thread | `inbox_read_chat` |
| "Message Suraj: ...", "send this file to Priya" | `inbox_send` (Hermes asks the user to approve) |
| "Reply to her last message", "answer that question" | `inbox_send` with `reply_to` (a message id from `inbox_read_chat`) |
| "Like that", "heart Maya's message" | `inbox_react` (Hermes asks the user to approve) |
| "What's in the PDF / photo Priya sent?" | `inbox_attachment` action `open`, then read the path with your own file or image tools |
| "What did Maya say in her voice note?" | `inbox_attachment` action `transcribe` |
| "Mark it as read" | `inbox_mark_read` (only when the user asks: they may see read receipts) |
| "Send this tomorrow at 9" | draft it, then `inbox_schedule` action `propose` |
| "Is this a scam?", "anything suspicious?" | `inbox_safety` (`check` for pasted text) |
| "Add that to my calendar", "am I free Thursday 4?" | `inbox_calendar` (`list`, `add`, `free`) |
| "What does this say?", "reply in Hindi" | `inbox_translate` (`chat`, `draft`, `text`) |
| "Who should I get back to?", "any birthdays?" | `inbox_radar` |
| "What did I miss?", "brief me" (out loud) | `inbox_brief` (`spoken=true` in a messaging app; put `media_tag` in your reply) |
| "Tell me when X replies", "remind me if no reply", "snooze" | `inbox_watch` |

## Safety first

- A message or chat with a `warning` / `safety_warning` looks like a scam. Say so plainly, give
  the advice, and never help the user share a one-time code, PIN, password, card or account
  number with it (the plugin blocks that anyway). If the user says it is fine, `inbox_safety`
  `safe` silences it.
- A message with a `translation` is in another language: read and quote the translation. To answer
  in their language, draft in the user's language, then `inbox_translate` `draft`.
- When a chat proposes a time, `inbox_context` lists `plans` with `free`: never accept a time the
  user is busy at; with `free` unknown, ask.

## Replying

- Get `inbox_context` first, then draft with `inbox_draft_reply`: short, natural, in the
  language the chat uses (Hindi, Hinglish or English), personal to that person. When the brief has
  `your_style`, write exactly like that: its guidelines, and the user's own recent messages as
  examples of length and tone. Never copy those examples word for word.
- Hermes may already have drafted a reply for an urgent chat (`by_hermes` in `inbox_list_drafts`).
  Offer that one instead of writing another; if it has `blanks_to_fill` (like [time]), ask the user
  for them and fill them in with `inbox_draft_reply` and its `draft_id`.
- The same person can be on several apps (`also_on` in the brief). Use what you know from every
  app; when the user says two chats are the same person (or not), use `inbox_memory` `link` /
  `unlink`.
- To send now, call `inbox_send` with the exact text. Hermes shows the user an approval prompt;
  do not ask "shall I send?" yourself first. If several people match (`which_one`), ask which and
  call again with that `chat_id`. Say "sent" only when the result has `sent: true`; otherwise
  give the reason in one line.
- To prepare something for later review, use `inbox_draft_reply`; the user sends it with
  `/inbox send <id>`.
- To answer one specific message, pass its id as `reply_to` (from `inbox_read_chat`); it is sent as
  a quoted reply. In a group, mention people as `@Name` using the names in `members_seen`.
- A group only needs the user when someone mentions them (`mentions_you`); don't treat every group
  message as waiting for a reply.
- Files people sent are not downloaded until asked. Open one only when the user wants to know
  what is in it; never send a received file onward unless the user asks.
- If a draft would be blocked (`would_be_blocked_now_because`), say why in one line.

## Scheduling and reminders

- You can propose a send time, never schedule: after `inbox_schedule` `propose`, tell the user
  to confirm with `/inbox schedule <draft_id>`. Repeat the exact time back in plain words.
- If a time is ambiguous ("at 6", "kal" in the past tense), ask before proposing.
- For "remind me if they don't reply", use a `no_reply` watch with `until`; add `nudge=true` when
  the user wants a follow-up ready.
- Alerts arrive through `inbox_updates`. Relay them in one short line each.

## Memory

- Facts about people, chat summaries and open items are kept automatically. When the user tells
  you something about a person ("Priya is my manager"), save it with `inbox_memory` action
  `remember`. When they ask you to forget something, use `forget` and confirm.
- Memory sometimes is not sure (two chats that may be one person, a changed fact, an unclear
  "kal"). `inbox_context` shows `memory_questions_waiting` when there are some. Ask them only at
  a calm moment, never in the middle of another task, one at a time, in plain words; record the
  reply with action `answer`. If the user does not want to answer, `dismiss` it.
- Never repeat codes, passwords, card, bank or ID numbers from messages, and never save them.

## Style

Lead with the answer: names, apps, how long someone has waited, and why it matters. Use short
lines, no filler. Mention problems (a paused account, rate limits) plainly in one line.
