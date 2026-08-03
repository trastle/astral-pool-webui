import time

from app import render_cards, render_dashboard, render_help


def test_dashboard_shows_waiting_message_with_no_data():
    html = render_dashboard(data=None, error=None, updated_at=None)
    assert "Waiting for first poll" in html


def test_dashboard_shows_error_banner():
    html = render_dashboard(data=None, error="boom", updated_at=1000.0)
    assert "boom" in html
    assert "banner alert" in html


def test_dashboard_shows_stale_warning_after_two_poll_intervals(sample_data):
    stale_updated_at = time.time() - 1000  # POLL_INTERVAL_SECONDS is 60 in tests
    html = render_dashboard(data=sample_data, error=None, updated_at=stale_updated_at)
    assert "banner warn" in html
    assert "out of date" in html


def test_dashboard_shows_key_fields_humanized(sample_data):
    html = render_dashboard(data=sample_data, error=None, updated_at=time.time())
    assert "Auto" in html
    assert "target 7.4" in html
    assert "Inhibited Indefinitely" in html


def test_cards_never_show_raw_enum_identifiers(sample_data):
    # Unlike the full dashboard (which also has a deliberate raw-field-dump
    # section), the status cards specifically should always be humanized.
    html = render_cards(sample_data)
    assert "Inhibited Indefinitely" in html
    assert "InhibitedIndefinitely" not in html
    assert "OrpProbeCleanCalibrate" not in html


def test_dashboard_shows_pump_schedule(sample_data):
    html = render_dashboard(data=sample_data, error=None, updated_at=time.time())
    assert "Pump schedule" in html
    assert "10:00" in html and "14:00" in html
    assert "4.0h/day" in html


def test_dashboard_shows_info_message_banner_when_present(sample_data):
    sample_data = dict(sample_data, info_message="OrpProbeCleanCalibrate")
    html = render_dashboard(data=sample_data, error=None, updated_at=time.time())
    assert "banner warn" in html
    assert "ORP Probe Clean Calibrate" in html


def test_dashboard_hides_banner_for_no_message(sample_data):
    html = render_dashboard(data=sample_data, error=None, updated_at=time.time())
    assert "banner warn" not in html
    assert "banner alert" not in html


def test_salt_cell_card_reflects_live_state_not_lifetime_reversal_count(sample_data):
    """Regression test: the card used to show 'reversing' just because
    cell_reversal_count > 0 (a lifetime counter, not a live state), so it
    was permanently wrong. It should reflect cell_is_operating instead."""
    html = render_dashboard(data=sample_data, error=None, updated_at=time.time())
    assert "<div class='value'>Idle</div>" in html
    assert "1365 lifetime reversals" in html

    operating_data = dict(sample_data, cell_is_operating=True)
    html = render_dashboard(data=operating_data, error=None, updated_at=time.time())
    assert "<div class='value'>Operating</div>" in html


def test_render_help_links_to_official_resources_not_a_copy():
    html = render_help()
    assert "astralpool.com.au/eq-support" in html
    assert ".pdf" in html.lower()
    assert "ORP Probe Clean Calibrate" in html


def test_acid_dosing_card_shows_countdown_when_inhibited_for_a_period(sample_data):
    data = dict(
        sample_data,
        acid_dosing_inhibit_status="InhibitedForAPeriod",
        acid_dosing_inhibit_time_remaining=45,
    )
    html = render_cards(data)
    assert "45s remaining" in html


def test_acid_dosing_card_has_no_countdown_when_not_time_limited(sample_data):
    # sample_data's status is InhibitedIndefinitely - no countdown applies.
    html = render_cards(sample_data)
    assert "remaining" not in html
