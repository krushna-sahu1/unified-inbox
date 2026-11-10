from datetime import datetime

from unified_inbox.pacing import (
    DAY, HOUR, AccountGate, SendRecord, Settings, backoff_seconds, check_send, in_quiet_hours,
    is_near_duplicate, load_settings, send_gap_seconds,
)

NOW = 1_800_000_000.0
NOON = datetime(2026, 11, 9, 12, 0)


def _check(settings=None, **over):
    kw = dict(now=NOW, local_now=NOON, chat_id="c1", network="whatsapp", text="Sounds good, see you at 6!",
              recent_sends=[], account=AccountGate(), last_inbound_at=NOW - HOUR, next_send_allowed_at=0.0)
    kw.update(over)
    return check_send(settings or Settings(), **kw)


def test_defaults_allow_a_normal_reply():
    assert _check() == []


def test_settings_can_only_get_stricter():
    s = load_settings(lambda k, d: {"daily_send_limit": 500, "min_send_gap_seconds": 1,
                                    "daily_limit_linkedin": 50, "hourly_send_limit": "3"}.get(k, d))
    assert s.daily_send_limit == 40
    assert s.min_send_gap_seconds == 20
    assert s.daily_limit_linkedin == 20
    assert s.hourly_send_limit == 3


def test_bad_setting_values_fall_back_to_defaults():
    s = load_settings(lambda k, d: {"daily_send_limit": "lots"}.get(k, d))
    assert s.daily_send_limit == 20


REPLIES_ONLY = Settings(new_conversations_per_day=0)


def test_replies_only_when_new_conversations_are_off():
    assert any("never messaged you" in r for r in _check(REPLIES_ONLY, last_inbound_at=None))
    assert any("older than 7 days" in r for r in _check(REPLIES_ONLY, last_inbound_at=NOW - 8 * DAY))


def test_new_conversations_have_their_own_daily_limit():
    assert _check(last_inbound_at=None) == []  # default: up to 5 a day
    starts = [SendRecord(f"n{i}", "whatsapp", f"hi {i}", NOW - 3 * HOUR, started=True) for i in range(5)]
    assert any("New-conversation limit" in r for r in _check(last_inbound_at=None, recent_sends=starts))
    assert any("New-conversation limit" in r for r in _check(last_inbound_at=NOW - 8 * DAY, recent_sends=starts))
    assert _check(recent_sends=starts) == []  # replies are not limited by it


def test_daily_and_network_limits():
    sends = [SendRecord(f"c{i}", "instagram", f"msg {i}", NOW - 2 * HOUR - i) for i in range(10)]
    reasons = _check(network="instagram", chat_id="new", recent_sends=sends)
    assert any("Instagram daily limit" in r for r in reasons)
    sends = [SendRecord(f"c{i}", "whatsapp" if i % 2 else "instagram", f"m{i}", NOW - 2 * HOUR - i)
             for i in range(20)]
    assert any("Daily limit reached (20" in r for r in _check(chat_id="new", recent_sends=sends))


def test_hourly_and_per_chat_limits():
    sends = [SendRecord(f"c{i}", "whatsapp", f"m{i}", NOW - 60 * i) for i in range(1, 6)]
    assert any("Hourly limit" in r for r in _check(chat_id="x", recent_sends=sends))
    sends = [SendRecord("c1", "whatsapp", "a", NOW - 300), SendRecord("c1", "whatsapp", "b", NOW - 200)]
    assert any("to this chat" in r for r in _check(recent_sends=sends))


def test_send_gap():
    assert any("Too soon" in r for r in _check(next_send_allowed_at=NOW + 30))


def test_duplicate_to_other_person_blocked_but_short_texts_ok():
    text = "Hey! Thanks so much for the invite, I'll be there on Friday."
    sends = [SendRecord("other", "whatsapp", text.upper() + "!!", NOW - 3 * HOUR)]
    assert any("very similar" in r for r in _check(text=text, recent_sends=sends))
    sends = [SendRecord("other", "whatsapp", "Thanks!", NOW - 3 * HOUR)]
    assert _check(text="thanks", recent_sends=sends) == []


def test_paused_accounts():
    assert any("paused" in r for r in _check(account=AccountGate(paused_until=NOW + 600, pause_reason="x")))


def test_linkedin_daily_limit():
    s = Settings()
    assert s.daily_limit_linkedin == 10
    sends = [SendRecord(f"c{i}", "linkedin", f"msg {i}", NOW - 2 * HOUR - i) for i in range(10)]
    assert any("LinkedIn daily limit reached (10" in r
               for r in _check(network="linkedin", chat_id="new", recent_sends=sends))
    assert _check(network="linkedin", chat_id="new", recent_sends=sends[:9]) == []


def test_quiet_hours_cross_midnight():
    assert in_quiet_hours("23:00-07:00", datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours("23:00-07:00", datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours("23:00-07:00", datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours("", datetime(2026, 1, 1, 23, 30))
    assert not in_quiet_hours("nonsense", datetime(2026, 1, 1, 23, 30))
    s = Settings(quiet_hours="22:00-08:00")
    assert any("Quiet hours" in r for r in _check(s, local_now=datetime(2026, 1, 1, 23, 0)))


def test_send_gap_is_randomised_between_base_and_double():
    s = Settings()
    assert send_gap_seconds(s, lambda: 0.0) == 45
    assert send_gap_seconds(s, lambda: 1.0) == 90


def test_backoff_grows_and_caps():
    r = lambda: 0.5  # noqa: E731  (no jitter)
    assert backoff_seconds(1, None, r) == 60
    assert backoff_seconds(2, None, r) == 120
    assert backoff_seconds(1, 300, r) == 300
    assert backoff_seconds(3, None, r) == HOUR


def test_near_duplicate():
    assert is_near_duplicate("See you at 6!", "see you at 6")
    assert not is_near_duplicate("See you at 6", "Can't make it today, sorry")
