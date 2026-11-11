"""Web dashboard over the ad catalog.

Run: `uvicorn --factory --app-dir src dashboard.app:create_app`.
"""

import datetime
import json
import uuid
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy import Engine

from dashboard import auth, fields, queries
from processing import plates
from processing.processors.claude_condition import ClaudeConditionProcessor
from processing.registry import enabled_processors
from storage.background_status import read_status
from storage.db import data_dir, default_database_url, get_engine, utcnow
from storage.photos import LocalPhotoStore

_TEMPLATES = Path(__file__).parent / "templates"
_THUMB_WIDTH = 480
PAGE_SIZE = 60


def _shekels(value) -> str:
    return f"₪{value:,}" if value is not None else "—"


def _number(value) -> str:
    return f"{value:,}" if isinstance(value, int) else ("—" if value is None else str(value))


def _when(value: datetime.datetime | None) -> str:
    if value is None:
        return "—"
    delta = utcnow() - value
    if delta < datetime.timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 0)} min ago"
    if delta < datetime.timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} h ago"
    return f"{delta.days} d ago"


_LOCAL_TIME = ZoneInfo("Asia/Jerusalem")


def _local(value: datetime.datetime | None) -> str:
    """Naive UTC -> Israel time; just the clock for today."""
    if value is None:
        return "—"
    local = value.replace(tzinfo=datetime.UTC).astimezone(_LOCAL_TIME)
    today = datetime.datetime.now(_LOCAL_TIME).date()
    return local.strftime("%H:%M:%S" if local.date() == today else "%d/%m %H:%M")


def _until(value: datetime.datetime | None) -> str:
    if value is None:
        return "—"
    seconds = int((value - utcnow()).total_seconds())
    if seconds <= 0:
        return "due now"
    if seconds < 90:
        return f"in {seconds} s"
    if seconds < 5400:
        return f"in {round(seconds / 60)} min"
    return f"in {seconds / 3600:.1f} h"


def _duration(start: datetime.datetime | None, end: datetime.datetime | None = None) -> str:
    if start is None:
        return "—"
    seconds = max(int(((end or utcnow()) - start).total_seconds()), 0)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"


def _param(value, key: str = "") -> str:
    """Format an ad attribute for display."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int) and key not in ("ad_number",):
        return f"{value:,}"
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.time() == datetime.time():
            return parsed.strftime("%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d %H:%M")
    if value == "private":
        return "Private seller"
    if value == "commercial":
        return "Dealer"
    return str(value)


def _nice_ticks(low: int, high: int, count: int = 5) -> list[int]:
    """Round axis ticks covering [low, high]."""
    if high <= low:
        high = low + 1000
    raw_step = (high - low) / (count - 1)
    magnitude = 10 ** max(len(str(int(raw_step))) - 1, 0)
    step = next(
        factor * magnitude for factor in (1, 2, 2.5, 5, 10) if factor * magnitude >= raw_step
    )
    start = int(low // step * step)
    ticks = []
    value = start
    while value < high + step:
        ticks.append(int(value))
        value += step
    return ticks


def _change_summary(changes: dict | None) -> str:
    if not changes:
        return "First seen"
    parts = []
    for field, (old, new) in changes.items():
        if field == "removed":
            parts.append("Removed from Yad2")
        elif field == "relisted":
            parts.append("Back on Yad2")
        elif field == "price":
            direction = "▼" if new < old else "▲"
            parts.append(f"Price {direction} {_shekels(old)} → {_shekels(new)}")
        elif field == "km":
            parts.append(f"Mileage {_number(old)} → {_number(new)}")
        elif field == "description":
            parts.append("Description edited")
        else:
            parts.append(f"{field.replace('_', ' ').capitalize()}: {old} → {new}")
    return "; ".join(parts)


def create_app(engine: Engine | None = None, store=None, authenticator=None) -> FastAPI:
    engine = engine or get_engine(default_database_url())
    store = store or LocalPhotoStore()
    authenticator = authenticator or auth.from_env()
    thumbs_dir = data_dir() / "thumbs"

    # Same .env as the bot, so this matches what the bot runs.
    processors = enabled_processors()
    claude = ClaudeConditionProcessor()
    claude.setup(engine)

    def require_login(request: Request) -> str:
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get(
            "CF_Authorization"
        )
        try:
            return authenticator.verify(token)
        except auth.AccessDenied as error:
            raise HTTPException(403, str(error)) from error

    app = FastAPI(title="Yad2 Car Finder", docs_url=None, redoc_url=None, openapi_url=None)
    pages = FastAPI(dependencies=[Depends(require_login)], docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=_TEMPLATES)
    templates.env.filters.update(
        shekels=_shekels, number=_number, when=_when, change_summary=_change_summary,
        param=_param, local=_local, until=_until, plate=plates.format_plate,
    )
    templates.env.globals["duration"] = _duration
    templates.env.globals["days_listed"] = queries.days_listed

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @pages.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "stats": queries.overview(engine),
                "events": queries.recent_events(engine),
                "searches": queries.search_statuses(engine),
                "nav": "overview",
            },
        )

    @pages.get("/scans", response_class=HTMLResponse)
    def scan_page(request: Request, chat: str = ""):
        chat_id = int(chat) if chat.lstrip("-").isdigit() else None
        return templates.TemplateResponse(
            request,
            "scans.html",
            {
                "searches": queries.search_statuses(engine),
                "history": queries.scan_history(engine, chat_id=chat_id),
                "counts": queries.scan_counts(engine, utcnow() - datetime.timedelta(days=1)),
                "chat": chat_id,
                "nav": "scans",
            },
        )

    @pages.get("/background", response_class=HTMLResponse)
    def background_page(request: Request):
        status = read_status(engine)
        now = utcnow()
        stale = None
        if status is None:
            stale = "No status yet. It appears after the bot's first background pass."
        elif status.state == "idle" and status.next_run_at and now - status.next_run_at > datetime.timedelta(minutes=5):
            stale = f"Expected to run {_when(status.next_run_at)}. Is the bot running?"
        elif status.state != "idle" and now - status.updated_at > datetime.timedelta(minutes=10):
            stale = f"No update {_when(status.updated_at)}. The bot may have stopped."
        return templates.TemplateResponse(
            request,
            "background.html",
            {
                "status": status,
                "stale": stale,
                "processors": queries.processor_statuses(engine, processors),
                "photos": queries.photo_counts(engine),
                "sales": queries.sale_counts(engine),
                "spent": claude.spent_this_month(),
                "budget": claude.budget,
                "claude_model": claude.model,
                "recent": queries.recent_results(engine),
                "errors": queries.recent_results(engine, 10, errors_only=True),
                "nav": "background",
            },
        )

    @pages.get("/ads", response_class=HTMLResponse)
    def ad_list(
        request: Request,
        status: str = "active",
        manufacturer: str = "",
        model: str = "",
        sub_model: str = "",
        q: str = "",
        chat: str = "",
        sort: str = "seen",
        page: int = 1,
    ):
        page = max(page, 1)
        filters = {
            "status": status, "manufacturer": manufacturer, "model": model,
            "sub_model": sub_model, "q": q, "chat": chat, "sort": sort,
        }
        chat_id = int(chat) if chat.lstrip("-").isdigit() else None
        rows, total = queries.list_ads(
            engine, status, manufacturer or None, q or None, model or None, sub_model or None,
            chat_id, sort, PAGE_SIZE, (page - 1) * PAGE_SIZE,
        )
        return templates.TemplateResponse(
            request,
            "ads.html",
            {
                "rows": rows,
                "total": total,
                "price_floor": queries.ignored_price_floor(engine),
                "conditions": queries.condition_summaries(engine, [row.token for row in rows]),
                "page": page,
                "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
                "filters": filters,
                "query": urlencode({key: value for key, value in filters.items() if value}),
                "manufacturers": queries.manufacturers(engine),
                "models": queries.models(engine, manufacturer or None),
                "sub_models": queries.sub_models(engine, manufacturer or None, model or None)
                if model
                else [],
                "chats": queries.chat_ids(engine),
                "sorts": {
                    "seen": "Newest on the market",
                    "model": "By model and subtype",
                    "listed": "Longest listed",
                    "price": "Cheapest",
                    "price_desc": "Most expensive",
                    "km": "Lowest mileage",
                    "year": "Newest year",
                    "drop": "Biggest price drop",
                    "drops": "Most price drops",
                },
                "nav": "ads",
            },
        )

    @pages.get("/ads/{token}", response_class=HTMLResponse)
    def ad_page(request: Request, token: str):
        detail = queries.ad_detail(engine, token)
        if detail is None:
            raise HTTPException(404, "Ad not found")
        ad, history, ad_photos, results, chats, same_car = detail
        price_points = [
            {"t": row.observed_at.isoformat(), "price": row.price}
            for row in history
            if row.price is not None
        ]
        if ad.price is not None and ad.removed_at is None:
            price_points.append({"t": utcnow().isoformat(), "price": ad.price})
        return templates.TemplateResponse(
            request,
            "ad.html",
            {
                "ad": ad,
                "history": list(reversed(history)),
                "photos": ad_photos,
                "results": results,
                "chats": chats,
                "same_car": same_car,
                "ownership": ad.ownership or [],
                "ownership_summary": plates.ownership_summary(ad.ownership) if ad.ownership else None,
                "owner_labels": plates.OWNER_LABELS,
                "plate": queries.latest_result(results, "plate_registry"),
                "condition": queries.latest_result(results, "claude_condition"),
                "registry_fields": plates.REGISTRY_FIELDS,
                "photo_numbers": {
                    number: photo.sha256
                    for number, photo in enumerate(
                        [photo for photo in ad_photos if photo.sha256], 1
                    )
                },
                "detail_groups": fields.detail_groups(ad.attributes),
                "equipment": fields.equipment(ad.attributes),
                "price_points": json.dumps(price_points),
                "nav": "ads",
            },
        )

    @pages.get("/market", response_class=HTMLResponse)
    def market(
        request: Request,
        manufacturer: str = "",
        model: str = "",
        sub_model: str = "",
        x: str = "km",
    ):
        x = x if x in ("km", "year") else "km"
        rows = queries.market_points(engine, manufacturer or None, model or None)
        # One row per subtype once a model is chosen, otherwise one row per model.
        groups = queries.group_points(rows, by_sub_model=bool(model))
        scatter_rows = [row for row in rows if not sub_model or row.sub_model == sub_model]
        points = [
            {
                "token": row.token,
                "title": row.title,
                "sub_model": row.sub_model,
                "x": getattr(row, x),
                "price": row.price,
                "removed": row.removed_at is not None,
                "days": queries.days_listed(row),
                "year": row.year,
                "km": row.km,
            }
            for row in scatter_rows
            if getattr(row, x) is not None
        ]
        removed = sorted(point["days"] for point in points if point["removed"])
        prices = [row.price for row in rows]
        ticks = _nice_ticks(min(prices), max(prices)) if prices else []
        low, high = (ticks[0], ticks[-1]) if ticks else (0, 1)

        def position(price: int) -> float:
            return round((price - low) / ((high - low) or 1) * 100, 2)

        return templates.TemplateResponse(
            request,
            "market.html",
            {
                "points": json.dumps(points),
                "count": len(points),
                "removed_count": len(removed),
                "median_days": removed[len(removed) // 2] if removed else None,
                "filters": {"manufacturer": manufacturer, "model": model, "sub_model": sub_model, "x": x},
                "manufacturers": queries.manufacturers(engine),
                "models": queries.models(engine, manufacturer or None),
                "sub_models": queries.sub_models(engine, manufacturer or None, model or None)
                if model
                else [],
                "groups": groups,
                "by_sub_model": bool(model),
                "ticks": ticks,
                "position": position,
                "nav": "market",
            },
        )

    def _photo_file(sha256: str) -> Path:
        if len(sha256) != 64 or not all(char in "0123456789abcdef" for char in sha256):
            raise HTTPException(404)
        key = queries.photo_key(engine, sha256)
        if key is None:
            raise HTTPException(404)
        path = store.path(key)
        if not path.exists():
            raise HTTPException(404)
        return path

    @pages.get("/photos/{sha256}")
    def photo(sha256: str):
        return FileResponse(_photo_file(sha256), headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @pages.get("/thumbs/{sha256}")
    def thumbnail(sha256: str):
        source = _photo_file(sha256)
        target = thumbs_dir / sha256[:2] / f"{sha256}.webp"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            with Image.open(source) as image:
                image.thumbnail((_THUMB_WIDTH, _THUMB_WIDTH))
                image.convert("RGB").save(temporary, "WEBP", quality=80)
            temporary.replace(target)
        return FileResponse(
            target,
            media_type="image/webp",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    app.mount("/", pages)
    return app

