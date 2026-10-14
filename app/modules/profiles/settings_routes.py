"""The Settings page (/settings): the member's own settings sections -- job
search, matching, resume writing, addresses, career boards to watch, name.
Each user's values are stored in their own rows (app/core/config/store.py);
platform settings are on the admin pages (app/modules/admin/routes.py).
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.loader import ConfigError
from app.core.config.schema import AppConfig
from app.core.config.store import USER_SECTIONS, save_section
from app.modules.alerts.views import HIDDEN_FIELDS, watchlist_response
from app.modules.auth.deps import member_session
from app.modules.jobs.catalog.store import remember_user_terms
from app.modules.jobs.search import queries_per_scan, scan_specs
from app.web.forms import parse_form, section_form
from app.web.responses import load_config_or_500, redirect
from app.web.templating import templates

router = APIRouter()

# Settings page sections, in display order, with a one-line description each.
# `watchlist` has its own page.
SETTINGS_SECTIONS: list[tuple[str, str]] = [
    ("search", "Your default job search -- prefilled on Find jobs and used for your automatic scans"),
    ("matching", "How jobs are scored against your resume"),
    ("resume", "How tailored resumes are written"),
    ("address_profiles", "Addresses shown on tailored resumes"),
    ("search_profiles", "Company career boards to watch, and the address used on resumes for them"),
    ("candidate", "Your name on tailored resumes"),
]

def _settings_response(request: Request, session: Session, config: AppConfig, *, error: str | None = None,
                       error_section: str | None = None, message: str | None = None, status_code: int = 200):
    dumped = config.model_dump(mode="json")
    sections = [
        {"key": key, "description": description,
         "form": section_form(AppConfig.model_fields[key].annotation, key, dumped[key],
                              skip=HIDDEN_FIELDS.get(key, ()))}
        for key, description in SETTINGS_SECTIONS
    ]
    warnings = []
    pairs = len(scan_specs(config))
    if pairs:
        per_scan = queries_per_scan(pairs, config.scheduler.interval_minutes, config.search.adzuna_daily_limit)
        if per_scan < pairs:
            hours = config.scheduler.interval_minutes * -(-pairs // per_scan) / 60
            warnings.append(
                f"Your automatic scans have {pairs} searches (each title × location in your job search, plus "
                f"your watches'), more than fit in one scan, so they take turns -- each is checked about every "
                f"{hours:g} hours. Fewer titles, locations or watches checks each more often."
            )
    return templates.TemplateResponse(
        request, "settings.html",
        {"sections": sections, "error": error, "error_section": error_section, "message": message,
         "warnings": warnings},
        status_code=status_code,
    )



@router.get("/settings")
def settings_page(request: Request, msg: str | None = None, session: Session = Depends(member_session)):
    return _settings_response(request, session, load_config_or_500(session), message=msg)


@router.post("/settings/{section}")
async def save_settings_section(request: Request, section: str, session: Session = Depends(member_session)):
    if section not in USER_SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    form = await request.form()
    parsed = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str))
    value = parsed.get(section)
    current = load_config_or_500(session).model_dump(mode="json")[section]
    if isinstance(current, dict) and isinstance(value or {}, dict):  # keep fields the form doesn't show
        kept = {k: current[k] for k in HIDDEN_FIELDS.get(section, ()) if k in current and k != "watches"}
        value = {**kept, **(value or {})}
    try:
        config = save_section(session, section, value)
    except ConfigError as exc:
        error = "; ".join(exc.errors)
        if section == "watchlist":
            return watchlist_response(request, session, error=error, status_code=400)
        return _settings_response(request, session, load_config_or_500(session), error=error, error_section=section, status_code=400)

    if section == "search":
        remember_user_terms(session, title=config.search.titles, location=config.search.locations,
                            keyword=config.search.keywords)
    if section == "watchlist":
        return redirect("/watchlist", msg="Saved.")
    return RedirectResponse(url=f"/settings?{urlencode({'msg': 'Saved.'})}#{section}", status_code=303)
