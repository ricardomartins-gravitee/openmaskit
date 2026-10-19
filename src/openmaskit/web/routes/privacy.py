"""Dashboard-only administration of shared and server-scoped privacy checks."""
from pathlib import Path

from pydantic import ValidationError
from starlette.responses import HTMLResponse, JSONResponse

from openmaskit.privacy.adapters import CheckFailure


def page_with_privacy_checks(filename):
    static = Path(__file__).parent.parent / "static"
    return HTMLResponse((static / filename).read_text().replace(
        "<!-- PRIVACY_CHECKS_PANEL -->", (static / "privacy_panel.html").read_text()))


async def privacy_page(request):
    return page_with_privacy_checks("privacy.html")


def context(request):
    state = request.app.state.proxy_state
    registry = getattr(state, "privacy_checks", None)
    if registry is None:
        raise ValueError("Privacy check registry is not available")
    scope = request.path_params.get("target_name")
    if scope is not None and state.get_target(scope) is None:
        raise ValueError("Server not found")
    return registry, scope


def error_response(exc):
    if isinstance(exc, ValidationError):
        details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"error": "Invalid check settings", "details": details}, status_code=400)
    if isinstance(exc, (CheckFailure, TimeoutError)):
        return JSONResponse({"error": "Analyzer could not complete this check. Check its connection and settings."}, status_code=422)
    return JSONResponse({"error": str(exc)}, status_code=400)


async def privacy_list(request):
    try:
        registry, scope = context(request)
        checks = registry.visible("checks", scope)
        for check in checks:
            check["used_by"] = len({a["target_name"] for a in registry.assignments.values()
                                    if a["check_id"] == check["id"]})
        return JSONResponse({"providers": registry.visible("providers", scope), "checks": checks,
                             "assignments": registry.assigned(scope) if scope else []})
    except ValueError as exc:
        return error_response(exc)


async def privacy_save(request):
    try:
        registry, scope = context(request)
        kind = request.path_params["kind"]
        if kind not in {"providers", "checks"}:
            raise ValueError("Unknown privacy definition type")
        row = await registry.save(kind, scope, await request.json(), request.path_params.get("identifier"))
        return JSONResponse(row, status_code=200 if "identifier" in request.path_params else 201)
    except (ValueError, TypeError) as exc:
        return error_response(exc)


async def privacy_delete(request):
    try:
        registry, scope = context(request)
        kind = request.path_params["kind"]
        if kind not in {"providers", "checks"}:
            raise ValueError("Unknown privacy definition type")
        await registry.delete(kind, scope, request.path_params["identifier"])
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return error_response(exc)


async def privacy_assign(request):
    try:
        registry, scope = context(request)
        if not scope:
            raise ValueError("Choose a server for this assignment")
        row = await registry.assign(scope, await request.json(), request.path_params.get("identifier"))
        return JSONResponse(row)
    except (ValueError, TypeError) as exc:
        return error_response(exc)


async def privacy_unassign(request):
    try:
        registry, scope = context(request)
        await registry.unassign(scope, request.path_params["identifier"])
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return error_response(exc)


async def privacy_preview(request):
    try:
        registry, scope = context(request)
        return JSONResponse(await registry.preview(scope, await request.json()))
    except (ValueError, TypeError, KeyError, CheckFailure, TimeoutError) as exc:
        return error_response(exc)
