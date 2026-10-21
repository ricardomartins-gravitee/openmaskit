"""Shared CSS is exercised on the real synthetic dashboard, with native controls."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_shared_theme_and_analyzer_buttons_on_desktop_and_mobile(
    dashboard_page, openmaskit_server
):
    page = dashboard_page
    for width in [1024, 375]:
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(openmaskit_server + "/privacy")
        expect(
            page.get_by_role("heading", name="Analyzers", exact=True)
        ).to_be_visible()
        add = page.get_by_role("button", name="Add Analyzer", exact=True)
        expect(add).to_be_enabled()
        assert (
            add.evaluate("el => getComputedStyle(el).backgroundColor")
            == "rgb(88, 166, 255)"
        )
        assert (
            page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
            == "rgb(10, 12, 16)"
        )
        assert (
            page.locator(".muted").first.evaluate("el => getComputedStyle(el).color")
            == "rgb(135, 147, 165)"
        )
        add.click()
        dialog = page.locator(".privacy-panel .modal").filter(
            has=page.get_by_role("heading", name="Add Analyzer")
        )
        expect(dialog).to_be_visible()
        expect(dialog.locator("select:visible")).to_have_count(1)
        # Preserve real native HTML form elements and existing analyser flow.
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).not_to_be_visible()
        add.focus()
        page.keyboard.press("Enter")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot = Path(".runtime/design-browser")
        screenshot.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot / f"privacy-{width}.png"), full_page=True)
