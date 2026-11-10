"""Tool schemas: what the model sees. Only inbox_send and inbox_react act on the user's behalf,
and Hermes asks the user to approve each call first (see sending.py)."""

_NETWORK = {
    "type": "string",
    "enum": ["all", "whatsapp", "instagram", "linkedin"],
    "description": "Which app to look at. Default: all.",
}

INBOX_STATUS = {
    "name": "inbox_status",
    "description": (
        "Show the unified inbox status: whether it is connected, the WhatsApp/Instagram/LinkedIn accounts, "
        "paused accounts, how many messages were sent today versus the safety limits, and pending drafts."
    ),
    "parameters": {"type": "object", "properties": {}},
}

INBOX_LIST_CHATS = {
    "name": "inbox_list_chats",
    "description": (
        "List the user's most recent WhatsApp, Instagram and LinkedIn chats (newest first), with unread counts. "
        "Returns chat ids for inbox_read_chat and inbox_draft_reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "network": _NETWORK,
            "unread_only": {"type": "boolean", "description": "Only chats with unread messages."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
        },
    },
}

INBOX_READ_CHAT = {
    "name": "inbox_read_chat",
    "description": (
        "Read the latest messages of one chat (oldest first), with each message's id (for reply_to, "
        "inbox_react and inbox_attachment), its files (photo, voice note, document; and a voice note's "
        "text once transcribed), the message it replies to, reactions, and whether it mentions the user. "
        "For a group, also who has been writing (to @mention them in inbox_send). Does not mark anything "
        "as read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "Chat id from inbox_list_chats or inbox_triage."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
        },
        "required": ["chat_id"],
    },
}

INBOX_SEARCH = {
    "name": "inbox_search",
    "description": (
        "Search WhatsApp, Instagram and LinkedIn messages: exact words first (any language, any part of a "
        "word), then, when search by meaning is set up, related conversations that use other words (e.g. "
        "'rent' finds 'kiraya'). Searches the local copy kept by the background sync."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for (case-insensitive substring)."},
            "network": _NETWORK,
        },
        "required": ["query"],
    },
}

INBOX_TRIAGE = {
    "name": "inbox_triage",
    "description": (
        "Find chats that need the user: 'urgent' (urgent wording, questions, several unanswered messages) "
        "and 'unanswered' (their last message is older than the unanswered threshold). Use it for the "
        "daily digest or when the user asks what needs a reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "minimum": 1, "maximum": 168,
                      "description": "Look at chats active in this many hours. Default 24."},
        },
    },
}

INBOX_UPDATES = {
    "name": "inbox_updates",
    "description": (
        "New messages people sent the user since the last time this was called (the first call covers the "
        "last 24 hours), grouped by chat, newest first, plus alerts (watches that fired, reminders, scheduled "
        "messages sent or held). Instant: it reads what the background sync already collected and asks no app. "
        "Use it for 'anything new?' or 'did X reply?'."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_PERSON = {
    "chat_id": {"type": "string", "description": "Chat id (from inbox_list_chats, inbox_triage or inbox_updates)."},
    "person": {"type": "string", "description": "Or a name to look up, e.g. 'Priya'."},
}

INBOX_CONTEXT = {
    "name": "inbox_context",
    "description": (
        "A compact brief on one person or chat before replying or answering about them: who they are "
        "(remembered facts), a short summary of the conversation, what is still open (questions, promises, "
        "dates) and the latest messages. Use it instead of reading long histories."
    ),
    "parameters": {"type": "object", "properties": {**_PERSON}},
}

INBOX_LOOPS = {
    "name": "inbox_loops",
    "description": (
        "Open items across all chats: who is waiting on the user (questions they have not answered, things "
        "they promised), who the user is waiting on, and promises with due dates (overdue first). Use for "
        "'who am I keeping waiting?', 'what did I promise?', 'is anyone waiting on me?'. Can mark an item done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "show": {"type": "string", "enum": ["all", "waiting_on_you", "waiting_on_them", "promises"],
                     "description": "Default all."},
            "done": {"type": "integer", "description": "Mark this item id done (the user said it is handled)."},
        },
    },
}

INBOX_MEMORY = {
    "name": "inbox_memory",
    "description": (
        "What Hermes remembers about people from their chats, and the user's control over it. Actions: "
        "about (facts about a person), remember (save something the user tells you about a person), forget "
        "(a fact by id, or everything about a person), questions (things memory is unsure of: ask the user "
        "one at a time, at a natural moment, never in the middle of another task), answer (record the user's "
        "answer: yes, no, or a correction), dismiss (drop a question). One person across apps: person (their "
        "profile: name, every chat on every app, facts), link (the user says two chats are the same person: "
        "chat_id or person, plus other_chat_id), unlink (a chat linked by mistake), rename (the name to use "
        "for them). How the user writes: style (what was learned), forget_style."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["about", "remember", "forget", "questions", "answer", "dismiss",
                                                  "person", "link", "unlink", "rename", "style", "forget_style"]},
            **_PERSON,
            "other_chat_id": {"type": "string", "description": "link: the other chat of the same person."},
            "name": {"type": "string", "description": "rename: the name to use for this person."},
            "text": {"type": "string", "description": "remember: the fact, in plain words."},
            "fact_id": {"type": "integer", "description": "forget: the fact to delete."},
            "question_id": {"type": "integer", "description": "answer / dismiss: the question."},
            "answer": {"type": "string", "description": "answer: 'yes', 'no', or the correct information."},
        },
        "required": ["action"],
    },
}

INBOX_SCHEDULE = {
    "name": "inbox_schedule",
    "description": (
        "Scheduled messages. propose: suggest a send time for a saved draft (e.g. 'tomorrow 9am', 'kal subah 9 "
        "baje', 'in 2h'); this does NOT schedule it: tell the user to confirm with /inbox schedule <draft_id>, "
        "which shows them the exact text, person and time. list: what is scheduled. cancel: stop a scheduled "
        "message (the draft is kept). At send time every safety rule is checked again, and a message is held "
        "if the person wrote again in between."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["propose", "list", "cancel"]},
            "draft_id": {"type": "string", "description": "propose: the draft."},
            "when": {"type": "string", "description": "propose: when to send, in the user's words."},
            "schedule_id": {"type": "string", "description": "cancel: the scheduled message."},
        },
        "required": ["action"],
    },
}

INBOX_WATCH = {
    "name": "inbox_watch",
    "description": (
        "Alerts and reminders about chats. Types: reply (tell me when this person writes), keyword (when any "
        "message contains this text), topic (when someone writes about this, judged by meaning), no_reply "
        "(remind me if this person has not replied by a time; nudge=true also prepares a follow-up draft, never "
        "sent), snooze (hide this chat from triage until a time, then remind me). Alerts show up in "
        "inbox_updates. Actions: add, list, remove."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "type": {"type": "string", "enum": ["reply", "keyword", "topic", "no_reply", "snooze"]},
            **_PERSON,
            "text": {"type": "string", "description": "keyword / topic: what to watch for."},
            "network": {"type": "string", "enum": ["whatsapp", "instagram", "linkedin"],
                        "description": "keyword / topic: only this app."},
            "until": {"type": "string", "description": "no_reply / snooze: by when ('in 2 days', 'fri 9am'); "
                                                       "other types: optional end."},
            "nudge": {"type": "boolean", "description": "no_reply: also prepare a follow-up draft."},
            "watch_id": {"type": "integer", "description": "remove: the watch."},
        },
        "required": ["action"],
    },
}

INBOX_SEND = {
    "name": "inbox_send",
    "description": (
        "Send a WhatsApp, Instagram or LinkedIn message (and optionally files) to a person. Use it whenever the "
        "user asks to message, text, send or tell someone something. `to` is a name from their chats, a phone "
        "number with country code (WhatsApp, also people with no chat yet), a LinkedIn profile link, or an "
        "Instagram @username or profile link. Hermes shows the user an approval prompt with the exact person, "
        "text and files before anything is sent; just call the tool, do not ask for confirmation yourself. If "
        "the result has `which_one`, several people match: ask the user which, then call again with that "
        "chat_id. Only say a message was sent when the result says sent: true; otherwise give the reason."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Who: a name, +phone number, LinkedIn profile link or @username."},
            "text": {"type": "string", "description": "The message, exactly as the user wants it sent."},
            "network": {"type": "string", "enum": ["whatsapp", "instagram", "linkedin"],
                        "description": "Which app, if the user said or the name exists on several."},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Paths of files to attach (up to 5; 15 MB each)."},
            "chat_id": {"type": "string", "description": "A chat from inbox_list_chats or a which_one answer."},
            "reply_to": {"type": "string",
                         "description": "Optional: the id of a message in that chat to reply to (it is quoted). "
                                        "Message ids come from inbox_read_chat."},
        },
        "required": ["text"],
    },
}

INBOX_REACT = {
    "name": "inbox_react",
    "description": (
        "React to a message with one emoji (or remove your own reaction) on WhatsApp, Instagram or LinkedIn. "
        "Use it when the user asks to like, heart or react to a message. Hermes shows the user an approval "
        "prompt first; just call the tool. Message ids come from inbox_read_chat. Some apps accept only a few "
        "emojis; the result says if one was refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "The message to react to."},
            "emoji": {"type": "string", "description": "One emoji, e.g. 👍 or ❤️."},
            "remove": {"type": "boolean", "description": "Remove your own reaction instead of adding it."},
        },
        "required": ["message_id", "emoji"],
    },
}

INBOX_ATTACHMENT = {
    "name": "inbox_attachment",
    "description": (
        "Open a file someone sent (photo, video, voice note, PDF, document): action=open downloads it once "
        "into the plugin's folder and returns its local path (read it with your own file or image tools; "
        "small text files are returned as text). action=transcribe turns a voice note or audio into text "
        "with Hermes's speech-to-text, once; the text is kept with the message. Ids come from inbox_read_chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "transcribe"]},
            "message_id": {"type": "string", "description": "The message the file is in."},
            "attachment_id": {"type": "string",
                              "description": "Which file, when the message has several (from inbox_read_chat)."},
        },
        "required": ["action", "message_id"],
    },
}

INBOX_MARK_READ = {
    "name": "inbox_mark_read",
    "description": (
        "Mark a chat as read in WhatsApp, Instagram or LinkedIn. Only when the user asks for it: the other "
        "person may see read receipts. Nothing is ever marked read otherwise (unless the user turned on "
        "mark_read_after_reply)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"chat_id": {"type": "string", "description": "The chat to mark as read."}},
        "required": ["chat_id"],
    },
}

INBOX_DRAFT_REPLY = {
    "name": "inbox_draft_reply",
    "description": (
        "Save a draft reply to an existing chat. This NEVER sends anything. Only the user can send a "
        "draft, by typing /inbox send <draft_id> and confirming. Write the reply in the user's voice, "
        "personal to that person (identical texts to different people are blocked at send time). "
        "Tell the user the draft id and how to send it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "The chat to reply in."},
            "text": {"type": "string", "description": "The reply text."},
            "reply_to": {"type": "string",
                         "description": "Optional: the id of the message this answers (it is quoted when sent)."},
            "draft_id": {"type": "string",
                         "description": "Optional: change the text of this waiting draft instead of saving a new one "
                                        "(e.g. to fill in a [blank])."},
        },
        "required": ["chat_id", "text"],
    },
}

INBOX_LIST_DRAFTS = {
    "name": "inbox_list_drafts",
    "description": ("List saved drafts waiting for the user's approval (or sent/discarded ones). Drafts Hermes "
                    "prepared by itself for urgent chats are marked by_hermes; a draft with blanks (like [time]) "
                    "needs them filled in (inbox_draft_reply with draft_id) before it can be sent."),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pending", "sent", "discarded", "failed"],
                       "description": "Default pending."},
        },
    },
}

INBOX_SAFETY = {
    "name": "inbox_safety",
    "description": (
        "Scam and fraud warnings. action=list: chats whose latest messages look like a scam (asking for a "
        "one-time code, money or gift cards, fake bank/police/courier messages, prizes, too-easy jobs, look-alike "
        "links), with the reasons and what to do. action=check: check any text (e.g. a message the user pasted). "
        "action=safe / unsafe: the user says a chat is (not) safe; a safe chat gets no more warnings. Warnings "
        "also appear on messages in inbox_read_chat and inbox_context. Tell the user plainly; never help them "
        "share a code, PIN or password."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "check", "safe", "unsafe"], "description": "Default list."},
            "text": {"type": "string", "description": "check: the text to check."},
            "chat_id": {"type": "string", "description": "safe / unsafe: the chat."},
        },
    },
}

INBOX_CALENDAR = {
    "name": "inbox_calendar",
    "description": (
        "Plans found in the user's chats (\"meet Thursday 4pm\", \"kal shaam 6 baje\"), and whether the user is "
        "free then (plans they added, plus their calendar if they connected its iCal address). action=list: "
        "upcoming plans. action=add: put a plan in the calendar: returns Google Calendar and Outlook links and an "
        ".ics file for the user to open (nothing is saved to a calendar by itself; with Hermes's Google "
        "Workspace skill you may create the event yourself from start/end). action=dismiss: not a real plan. "
        "action=free: is the user free between start and end (ISO date-times)?"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "dismiss", "free"], "description": "Default list."},
            "plan_id": {"type": "integer", "description": "add / dismiss: the plan."},
            "chat_id": {"type": "string", "description": "list: only this chat's plans."},
            "days": {"type": "integer", "minimum": 1, "maximum": 60, "description": "list: how far ahead. Default 14."},
            "start": {"type": "string", "description": "free: start, ISO date-time (local)."},
            "end": {"type": "string", "description": "free: end, ISO date-time (local)."},
        },
    },
}

INBOX_TRANSLATE = {
    "name": "inbox_translate",
    "description": (
        "Translation. action=chat: the latest messages of a chat translated into the user's language (new "
        "messages in other languages are usually translated already and shown in inbox_read_chat). action=draft: "
        "translate a waiting draft into the chat's language (or to=...), in the user's style; the draft keeps "
        "what the user wrote, which the approval shows. action=text: translate a reply text for a chat without "
        "saving it. Uses Hermes's language model; codes and passwords are never sent to it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["chat", "draft", "text"]},
            "chat_id": {"type": "string", "description": "chat / text: the chat."},
            "draft_id": {"type": "string", "description": "draft: the waiting draft."},
            "text": {"type": "string", "description": "text: what the user wants to say."},
            "to": {"type": "string", "description": "draft / text: target language. Default: the chat's language."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "chat: messages. Default 20."},
        },
        "required": ["action"],
    },
}

INBOX_RADAR = {
    "name": "inbox_radar",
    "description": (
        "The people radar: who has been waiting longest for a reply, who the user is drifting apart from "
        "(people they used to talk to regularly who went quiet, across all apps), the user's reply time this "
        "week versus before (and per app), and birthdays in the next two weeks (from memory and the chats). "
        "Use for 'who should I get back to?', 'who haven't I talked to in a while?', 'any birthdays coming?'."
    ),
    "parameters": {"type": "object", "properties": {}},
}

INBOX_BRIEF = {
    "name": "inbox_brief",
    "description": (
        "A short brief of what the user missed, written to be read aloud: who is waiting and why, likely "
        "scams, drafts ready, plans and birthdays today or tomorrow. Instant (from the local copy). With "
        "spoken=true it is also turned into audio with Hermes's text-to-speech: put the returned media_tag in "
        "your reply so the user gets it as a voice message. Use for 'what did I miss?', 'brief me'."
    ),
    "parameters": {
        "type": "object",
        "properties": {"spoken": {"type": "boolean", "description": "Also make the audio. Default false."}},
    },
}

ALL = [INBOX_STATUS, INBOX_LIST_CHATS, INBOX_READ_CHAT, INBOX_SEARCH, INBOX_TRIAGE, INBOX_UPDATES,
       INBOX_CONTEXT, INBOX_LOOPS, INBOX_MEMORY, INBOX_SCHEDULE, INBOX_WATCH, INBOX_SEND, INBOX_REACT,
       INBOX_ATTACHMENT, INBOX_MARK_READ, INBOX_DRAFT_REPLY, INBOX_LIST_DRAFTS, INBOX_SAFETY, INBOX_CALENDAR,
       INBOX_TRANSLATE, INBOX_RADAR, INBOX_BRIEF]
