# Changelog

## 1.4.1

**A full end-to-end review: fixes and hardening, no feature removed.**
- **Sending.** An approval in Hermes is now tied to the chat it named: if the same name comes to
  mean another chat while the prompt is open, nothing is sent. A scheduled message only goes out
  with the exact words you scheduled. Read-only chats (announcement channels) are refused before
  the send, and the page shows no reply box for them.
- **Cloud sync.** A second device joining a large project no longer stops after the first page of
  rows. Reply quotes, "drafted by Hermes" marks, a translated draft's original words and the
  new-conversation count now sync too (run `/inbox cloud setup` once to update the tables;
  projects that are not updated keep syncing as before).
- **Calendar.** Meetings that have repeated for years (a daily stand-up since 2019) show as busy
  again, and one unreadable repeat rule no longer hides the whole calendar.
- **Quiet hours** understand `23-7`, `11pm-7am` and `23:00 to 07:00`; a value that cannot be read
  is reported by `/inbox doctor` and `/inbox limits` instead of being silently off.
- **Background work** stops promptly when the plugin unloads (no sync, memory work or scheduled
  send after it). A model answer in the wrong shape is no longer retried for ever. A webhook
  delivery that failed is accepted when Unipile sends it again.
- **Smaller fixes.** `/inbox limits` describes the limits actually in force; "kal shaam 6 baje 12
  dec" keeps its date; "Delete everything" keeps the reaction allowance; the chat sidebar shows
  "2 open" instead of "[object Object] open"; error responses from Supabase and embedding
  providers are closed.

## 1.4.0

**Safer, more helpful: scam warnings, plans into your calendar, translation, a people radar and a
spoken brief.**
- **Scam and fraud shield.** Messages that ask for a one-time code or PIN ("send me the OTP",
  "code bata do", the "I sent you a code by mistake" trick), money or gift cards, a fee for a prize
  or a job, scanning a QR code to "receive" money, or a screen-sharing app; fake bank, KYC,
  police, courier and electricity messages; prizes, "guaranteed returns" and task jobs; and links
  that hide where they go (short links, bare IP addresses, look-alike addresses such as
  paypa1.com or sbi-kyc-update.xyz, .apk files). English, Hindi and Hinglish. Checked on this
  machine with fixed rules: no model, nothing sent anywhere. Signs add up and a stranger counts
  as one more, so a friend asking "send me 500 for dinner" stays quiet. You get an alert, a
  warning on the message and the chat, and the chat is never "urgent" in triage (it is listed
  apart) and never auto-drafted. **A reply that would share a code, password, card or account
  number with such a chat is stopped**, in every way of sending. Mark a chat safe to silence it.
  `inbox_safety`, `/inbox scams`, `scam_shield`.
- **Plans into your calendar.** "Let's meet Thursday 4pm", "call tomorrow at 10", "dinner on 12
  Dec, 8:30", "kal shaam 6 baje milte hain" become a suggested plan with one alert. Add it with a
  ready Google Calendar or Outlook link or an .ics file (any calendar app); nothing is written to
  a calendar by itself. Optional: your calendar's private iCal address (`calendar_ics_url`,
  read-only) so plans and drafts know when you are busy: repeating events, time zones, cancelled
  and moved meetings included. An auto-draft for a proposed time is told whether you are free
  (only the busy times, never what your other events are) and never accepts a clash.
  Cancelled ("can't make it"), past and far-off plans are ignored. `inbox_calendar`,
  `/inbox plans`, `plan_detection`.
- **Auto-translate.** Messages in languages you don't read are translated into `my_language`
  (by Hermes's model, batched, within memory's daily cap; codes and passwords removed first).
  Languages you write in yourself, and `also_read`, are never translated. Reply in theirs:
  translate a draft into the chat's language in your own style; the draft keeps what you wrote,
  and every approval (Hermes, `/inbox send`, the page) shows it "in your words". `inbox_translate`,
  `auto_translate`.
- **People radar.** Who has waited longest for you (people, not groups, strangers or scams), who
  you are drifting apart from (compared with how often you used to talk, across all their apps),
  your reply time this week against the four weeks before and per app, and birthdays in the next
  two weeks (from memory, a "happy birthday" you sent, or "my birthday is on 12 March"), with a
  reminder the day before and a weekly summary. Local, no model. `inbox_radar`, `/inbox radar`,
  `people_radar`; the weekly review automation uses it.
- **Talk to your inbox.** `inbox_brief` (and `/inbox brief`) writes what you missed to be heard:
  who is waiting and why, likely scams, drafts ready, today's plans and birthdays. With
  `spoken=true` it becomes audio with Hermes's own text-to-speech, so in a messaging app Hermes can
  send it as a voice note. The Inbox page can play it and take a dictated reply (Hermes's
  speech-to-text; the recording is deleted right after).
- Your data: plans and translations are in the export, removed by "forget this person" and by
  "delete everything" (with spoken briefs and calendar files). They stay on this machine.

## 1.3.0

**Drafts that sound like you, ready before you ask.**
- **Auto-drafts for urgent chats.** When an urgent message arrives (urgent words, a question,
  several messages; in a group only when someone mentions you), Hermes drafts a reply right away
  and you get an alert. It never sends: you review and send it. One draft per urgent moment;
  none when you already replied, already have a draft for that chat, or the chat is muted,
  archived or snoozed; never for old history. A detail only you know is left as a blank like
  [time], and such a draft can't be sent until you fill it in. If you answer in the app yourself,
  the draft is withdrawn. On by default when memory is on: at most `auto_draft_per_day` (20) a
  day, counted in memory's daily model-call cap. 0 turns it off.
- **Writing style learning.** Learned from messages you typed (never from what Hermes sent):
  length, emojis, greetings and sign-offs, Hinglish or Hindi, separately per app, plus a few
  guidelines from Hermes's model about once a week. Drafts Hermes writes (auto-drafts, follow-up
  nudges, and replies you ask for, through `inbox_context`) follow it, with a few of your own
  recent messages to that person as examples. See it, refresh it or forget it on the page or with
  `inbox_memory` (`style`, `forget_style`); `style_learning` turns it off.
- **One person across apps.** Much better at spotting the same person on WhatsApp, Instagram and
  LinkedIn ("Priya" and "Priya Nair (Recruiter, Northwind)", an Instagram handle like
  priya_nair92): it asks you once, never links by itself, never guesses between two people with
  the same first name, and never asks again after a no. After a yes they are one profile: one
  name (the fullest real one, or yours), facts from every chat with duplicates folded, and every
  chat's summary and open items in one brief. "Message Priya" goes to the app you last talked on.
  Link, unlink and rename by hand (`inbox_memory`: `person`, `link`, `unlink`, `rename`, or the
  People page); an unlinked pair is never suggested again.
- **Edit a draft** (`inbox_draft_reply` with `draft_id`, or the page), e.g. to fill in a blank.

**For the Inbox page:** your own reactions stay shown after a reload; draft and Today rows say
whether the chat is a group; the page can read media as JSON and save a copy of any received
file (for Hermes Desktop).

## 1.2.0

**Messaging.** Seven things you can now do with your WhatsApp, Instagram and LinkedIn messages:
- **Open files people send you.** Photos, videos, voice notes, PDFs and documents are downloaded
  when you (or Hermes) open one, never in the background. They are kept in the plugin's own
  folder (readable by you alone on macOS and Linux), deleted after 30 days, when the message is
  deleted, and by Delete everything. Files over 25 MB open in the app instead. Photos, audio and
  video show on the Inbox page; other files are offered as downloads and never run inside it.
  A file someone sent can be forwarded with `inbox_send` under its own name.
- **Voice notes as text.** On request, with Hermes's own speech-to-text (your `stt` setting: local
  Whisper by default). Done once per voice note; the text is kept with the message, shows when
  you read the chat and is found by search.
- **Reply to a specific message.** `inbox_send` and drafts take `reply_to`: the message is sent as a
  quoted reply. The approval prompt and `/inbox send` show what you are replying to. If that
  message was deleted before sending, nothing is sent.
- **Reactions.** `inbox_react` adds (or removes your own) emoji reaction, after Hermes's approval
  prompt like a send. Its own limits: 6 an hour, 30 a day; quiet hours apply.
- **Mark as read, only if you want.** Nothing is ever marked read on its own. `inbox_mark_read` or
  the page marks a chat read when you ask; the new `mark_read_after_reply` setting (off by
  default) marks a chat read after you reply in it.
- **Groups.** Mention people with `@Name` (they get notified); the approval prompt says it is a
  group; reading a group shows who has been writing. A group needs a reply only when someone
  mentions you. New groups are not created.
- **New Instagram chats.** Message an Instagram `@username` or profile link you have never chatted
  with (counts as a new conversation, like WhatsApp numbers and LinkedIn profiles).

**Also fixed:** a message that arrived in the same second as the last one synced (WhatsApp photo
albums) could wait for the next message before the background sync noticed it.

## 1.1.1

A review and hardening release. Nothing changes in how you use the plugin.

**Your data**
- The activity log (`/inbox log`, the Activity page) is now part of **Export everything** and is
  removed by **Delete everything**, together with `activity.log`. It keeps 90 days, like the send
  log; `activity.log` starts a new part past 1 MB.
- On macOS and Linux, `activity.log` and your exports are readable by you alone, like the database.

**Sending files**
- The approval prompt names each file with its folder, and relative paths are read from the chat's
  folder in both the prompt and the send, so what you approve is exactly what is sent.
- Key, password and login files (`.env`, `auth.json`, SSH and GPG keys, `.pem`, password-manager
  vaults, also through a link) and the plugin's own data folder are never attached.
- Yolo mode and approvals turned off are detected through Hermes's public check first, so a
  future Hermes still refuses to send without asking you.

**Reliability**
- When Hermes unloads or reloads the plugin, the background threads stop and the database is
  closed (Windows could not remove or replace an open database, and `hermes plugins doctor`
  failed there). The Inbox page follows a reload instead of keeping the old connection.
- Error answers from Unipile no longer hold a network connection open.
- Tests: the suite passes with warnings treated as errors and closes everything it opens; new
  tests cover the fixes above.

## 1.1.0

**Hermes can send.** Ask "message Suraj: ..." or "send this file to Priya" and Hermes sends it
on WhatsApp, Instagram or LinkedIn, after you approve it in Hermes's own approval prompt (the
person, the app, the exact text and the files). The AI cannot skip the prompt; if Hermes would
approve without asking (yolo mode, approvals off, an unattended run), the send is refused.
- Send to a name from your chats, a phone number with country code (WhatsApp, including people
  with no chat yet), a LinkedIn profile link, or an Instagram @username you already chat with.
  If a name matches several people, Hermes asks which one.
- Files: up to 5 per message, 15 MB each.
- New conversations are allowed, with their own daily limit (`new_conversations_per_day`,
  default 5, 0 = replies only); every other safety limit still applies.
- See what happened: `/inbox log` (what was sent, to whom, sent / failed / blocked / waiting),
  the Activity page, and a plain `activity.log` file next to the plugin's database.

## 1.0.0

The first complete release: one inbox for WhatsApp, Instagram and LinkedIn in Hermes, with
memory, automation, a full Inbox page and optional cloud sync. Nothing is ever sent without a
person confirming it.

**Your data**
- Export everything to a JSON file (`/inbox export`, or Setup → Your data on the Inbox page).
- Forget a person: facts, summary, open items and their search index (`/inbox forget <name>`, or
  People → Forget this person). Messages stay in the inbox.
- Delete everything (`/inbox reset`, or Setup → Delete everything). Sends from the last 7 days
  and account pauses are kept so your safety limits stay correct. With cloud sync: delete on this
  device only, or everywhere (Supabase and your other devices).
- On macOS and Linux the database is readable by you alone.

**Health check**
- `/inbox doctor` (or Setup → Run a check) checks the API key, every connected app, the
  database, search, background sync, memory, search by meaning, webhooks and cloud sync, and
  says how to fix anything that is off.

**Speed** (measured with `scripts/bench.py` on 400 chats and 100,000 messages)
- Chat list about 1 ms, one chat under 1 ms, word search 16–26 ms (any language), the person
  brief under 1 ms, background "what changed" checks about 23 ms. Tests guard that every hot
  query keeps using its index.

**Release**
- Generated page files always check out with the same line endings, so the build check passes
  on every system.

## 0.9.0 – optional Supabase sync
Local-first sync with your own Supabase project: everything mirrored (embeddings in pgvector),
one device does the background work, limits count every device, scheduled messages claimed once,
sends refused while Supabase is unreachable, locked-down schema.

## 0.8.0 – the Inbox page
One page for the Hermes web dashboard and Hermes Desktop: Today, Inbox, chat with context,
drafts with Send and Schedule, Scheduled, Activity, People, Automations and Setup. Connecting
needs only the API key; apps are connected from the page.

## 0.7.0 – automation
Scheduled messages (confirmed by you, re-checked at send time), watches (reply, keyword, topic),
follow-ups with nudge drafts, snoozes, alerts and report blueprints. Time phrases in English,
Hindi and Hinglish.

## 0.6.0 – memory
Facts about people, chat summaries and open items from Hermes's own model; questions when
unsure; search by meaning with OpenAI, Gemini, Cohere or Voyage embeddings; secrets redacted.

## 0.5.0 – background sync
New messages arrive by themselves (polling, or instant with webhooks); `inbox_updates`.

## 0.4.0 – foundation
No sample mode; a self-upgrading database with multilingual full-text search; recent reads
reused; daily cleanup.
