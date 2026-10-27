"""Browser failure/retry boundaries; model quality is covered by the real Presidio suite."""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import E2E_BASE_URL, api_client
from tests.e2e.test_privacy_checks import analyzer_card, create_provider, modal, panel

pytestmark = pytest.mark.e2e
ENDPOINT = "http://127.0.0.1:15555"


def open_provider(page, name="Synthetic analyzer"):
    page.goto(E2E_BASE_URL + "/privacy")
    panel(page).get_by_role("button", name="Add Analyzer", exact=True).click()
    modal(page).get_by_role("textbox", name="Name", exact=True).fill(name)
    modal(page).get_by_label("Local service URL").fill(ENDPOINT)


def library():
    with api_client() as client:
        response = client.get("/api/privacy")
        response.raise_for_status()
        return response.json()


def test_failed_save_keeps_form_data_and_retry_creates_one_definition(dashboard_page):
    page = dashboard_page
    open_provider(page)
    page.route("**/api/privacy/providers/create", lambda route: route.fulfill(
        status=503, json={"error": "Synthetic service unavailable"}), times=1)
    modal(page).get_by_role("button", name="Save", exact=True).click()
    expect(modal(page).get_by_role("alert")).to_have_text("Synthetic service unavailable")
    expect(modal(page).get_by_role("textbox", name="Name", exact=True)).to_have_value("Synthetic analyzer")
    expect(modal(page).get_by_label("Local service URL")).to_have_value(ENDPOINT)
    expect(modal(page).get_by_role("button", name="Save", exact=True)).to_be_enabled()
    assert library()["providers"] == []
    modal(page).get_by_role("button", name="Save", exact=True).click()
    expect(modal(page)).not_to_be_visible()
    assert len(library()["providers"]) == 1
    page.reload()
    expect(analyzer_card(page, "Synthetic analyzer")).to_have_count(1)


def test_pending_save_disables_duplicate_submission(dashboard_page):
    page = dashboard_page
    pending = []
    open_provider(page)

    def hold_request(route):
        pending.append(route)
        page.evaluate("window.syntheticRequestHeld = true")

    page.route("**/api/privacy/providers/create", hold_request)
    save = modal(page).locator("button[type='submit']")
    save.click()
    page.wait_for_function("window.syntheticRequestHeld === true")
    expect(save).to_be_disabled()
    expect(modal(page).get_by_role("button", name="Close privacy check settings")).to_be_disabled()
    # Native disabled-button semantics must also hold for programmatic activation.
    save.evaluate("el => el.click()")
    page.keyboard.press("Escape")
    expect(modal(page)).to_be_visible()
    assert len(pending) == 1
    response = pending[0].fetch()
    pending[0].fulfill(response=response)
    expect(modal(page)).not_to_be_visible()
    assert len(library()["providers"]) == 1


def test_escape_cancels_without_creating_a_definition(dashboard_page):
    page = dashboard_page
    open_provider(page, "Cancelled analyzer")
    page.keyboard.press("Escape")
    expect(modal(page)).not_to_be_visible()
    assert library()["providers"] == []
    assert library()["checks"] == []


def test_failed_preview_clears_the_previous_masked_result(dashboard_page):
    page = dashboard_page
    page.goto(E2E_BASE_URL + "/privacy")
    create_provider(page, ENDPOINT, "Synthetic analyzer")
    analyzer_card(page, "Synthetic analyzer").get_by_role("button", name="Add Check", exact=True).click()
    modal(page).get_by_role("textbox", name="Name", exact=True).fill("Synthetic people")
    if not modal(page).get_by_label("Sample text").is_visible():
        modal(page).get_by_text("Preview", exact=True).click()
    replies = [{"text": "person_1 met person_2"}, {"error": "Synthetic analyzer unavailable"}]

    def preview(route):
        body = replies.pop(0)
        route.fulfill(status=503 if "error" in body else 200, json=body)

    page.route("**/api/privacy/preview", preview)
    modal(page).get_by_role("button", name="Preview Check", exact=True).click()
    expect(modal(page).locator(".privacy-preview")).to_contain_text("person_1")
    modal(page).get_by_role("button", name="Preview Check", exact=True).click()
    expect(modal(page).get_by_role("alert")).to_have_text("Synthetic analyzer unavailable")
    expect(modal(page).locator(".privacy-preview")).not_to_be_visible()
    assert library()["checks"] == []


def test_connection_failure_is_retryable_and_updates_visible_health(dashboard_page):
    page = dashboard_page
    page.goto(E2E_BASE_URL + "/privacy")
    create_provider(page, ENDPOINT, "Synthetic analyzer")
    card = analyzer_card(page, "Synthetic analyzer")
    page.route("**/api/privacy/preview", lambda route: route.fulfill(
        status=503, json={"error": "Synthetic analyzer unavailable"}), times=1)
    card.get_by_role("button", name="Check Connection", exact=True).click()
    expect(card.locator(".privacy-connection")).to_have_text("Unavailable")
    expect(card.get_by_role("button", name="Check Connection", exact=True)).to_be_enabled()
    page.route("**/api/privacy/preview", lambda route: route.fulfill(status=200, json={"text": "person_1"}))
    card.get_by_role("button", name="Check Connection", exact=True).click()
    expect(card.locator(".privacy-connection")).to_have_text("Connected")
    assert len(library()["providers"]) == 1


def test_analyzer_names_are_rendered_as_text_not_executable_markup(dashboard_page):
    page = dashboard_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(E2E_BASE_URL + "/privacy")
    name = '<img src=x onerror="window.syntheticXss=true">'
    create_provider(page, ENDPOINT, name)
    expect(analyzer_card(page, name)).to_be_visible()
    assert panel(page).locator("img[src='x']").count() == 0
    assert not page.evaluate("window.syntheticXss === true")
    assert errors == []
