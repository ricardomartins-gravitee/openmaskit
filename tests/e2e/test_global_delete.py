"""Global rule/mapper buttons start enabled and report failed deletions honestly."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import E2E_BASE_URL, api_client, install_stub_via_api

pytestmark = pytest.mark.e2e
TARGET = "delete-buttons"


def prepare(page, kind):
    install_stub_via_api(TARGET)
    if kind == "rules":
        payload = {"tool_name": "*", "field_path": "messages.author", "alias_prefix": "author"}
        label, singular = "Delete rule", "Rule"
    else:
        payload = {"tool_name": "*", "pattern": "SyntheticExample", "alias_prefix": "author"}
        label, singular = "Delete mapper", "Mapper"
    base = f"/api/targets/{TARGET}/{kind}"
    with api_client() as client:
        selected = client.post(base + "/create", json=payload)
        selected.raise_for_status()
        retained = client.post(base + "/create", json={**payload, "alias_prefix": "header"})
        retained.raise_for_status()
    page.goto(f"{E2E_BASE_URL}/targets/{TARGET}/tools")
    row = page.get_by_role("row").filter(has=page.get_by_role("cell", name="author", exact=True))
    button = row.get_by_role("button", name=label, exact=True)
    return base, selected.json()["id"], retained.json()["id"], row, button, singular


@pytest.mark.parametrize("kind", ["rules", "mappers"])
def test_global_delete_cancellation_exact_row_and_persistence(dashboard_page, kind):
    page = dashboard_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    base, selected, retained, row, button, singular = prepare(page, kind)
    expect(button).to_be_enabled()
    dismiss = lambda dialog: dialog.dismiss()
    page.on("dialog", dismiss)
    button.click()
    expect(row).to_be_visible()
    expect(button).to_be_enabled()
    with api_client() as client:
        assert {r["id"] for r in client.get(base).json()[kind]} == {selected, retained}
    page.remove_listener("dialog", dismiss)
    page.on("dialog", lambda dialog: dialog.accept())
    button.click()
    expect(row).to_have_count(0)
    expect(page.locator(".toast-success")).to_have_text(singular + " deleted")
    with api_client() as client:
        assert [r["id"] for r in client.get(base).json()[kind]] == [retained]
    page.reload()
    expect(page.get_by_role("row").filter(has=page.get_by_role("cell", name="header", exact=True))).to_be_visible()
    expect(page.get_by_role("button", name="Delete rule" if kind == "rules" else "Delete mapper", exact=True)).to_be_enabled()
    expect(row).to_have_count(0)
    assert errors == []


@pytest.mark.parametrize("kind", ["rules", "mappers"])
@pytest.mark.parametrize("failure", ["json", "html", "network"])
def test_global_delete_failure_keeps_row_enabled_for_retry(dashboard_page, kind, failure):
    page = dashboard_page
    base, selected, retained, row, button, singular = prepare(page, kind)
    expect(button).to_be_enabled()
    page.on("dialog", lambda dialog: dialog.accept())
    url = E2E_BASE_URL + base + f"/{selected}/delete"

    def fail(route):
        if failure == "network":
            route.abort()
        elif failure == "json":
            route.fulfill(status=500, json={"error": "Synthetic delete failure"})
        else:
            route.fulfill(status=500, content_type="text/html", body="Unavailable")

    page.route(url, fail)
    button.click()
    expect(page.locator(".toast-error")).to_be_visible()
    if failure == "json":
        expect(page.locator(".toast-error")).to_have_text("Synthetic delete failure")
    expect(page.locator(".toast-success")).to_have_count(0)
    expect(row).to_be_visible()
    expect(button).to_be_enabled()
    with api_client() as client:
        assert {r["id"] for r in client.get(base).json()[kind]} == {selected, retained}
    page.unroute(url, fail)
    button.click()
    expect(row).to_have_count(0)
    with api_client() as client:
        assert [r["id"] for r in client.get(base).json()[kind]] == [retained]
