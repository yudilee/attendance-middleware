import json
from datetime import date, datetime
from typing import Optional, List
from sqlalchemy.orm import Session

from app.cache import cache_get, cache_set, cache_delete
from app.database.models import ShiftSchedule, Branch, Holiday


def _json_serial(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def get_shift_schedule(db: Session, shift_id: int) -> Optional[ShiftSchedule]:
    """Retrieve shift schedule, caching in Redis for 10 minutes (600 seconds)."""
    cache_key = f"shift_schedule:{shift_id}"
    cached = cache_get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            valid_keys = {c.key for c in ShiftSchedule.__table__.columns}
            filtered_data = {k: v for k, v in data.items() if k in valid_keys}
            if filtered_data.get("anchor_date"):
                filtered_data["anchor_date"] = date.fromisoformat(filtered_data["anchor_date"])
            if filtered_data.get("created_at"):
                filtered_data["created_at"] = datetime.fromisoformat(filtered_data["created_at"])
            return ShiftSchedule(**filtered_data)
        except Exception:
            pass

    # Cache miss
    shift = db.query(ShiftSchedule).filter(ShiftSchedule.id == shift_id).first()
    if shift:
        try:
            data = {c.key: getattr(shift, c.key) for c in ShiftSchedule.__table__.columns}
            cache_set(cache_key, json.dumps(data, default=_json_serial), ttl=600)
        except Exception:
            pass
    return shift


def invalidate_shift_cache(shift_id: int):
    """Invalidate cache for a specific shift schedule."""
    cache_delete(f"shift_schedule:{shift_id}")


def get_branch_geofence(db: Session, branch_id: int) -> Optional[Branch]:
    """Retrieve branch configurations (including geofences), caching in Redis for 10 minutes (600 seconds)."""
    cache_key = f"branch_geofence:{branch_id}"
    cached = cache_get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            valid_keys = {c.key for c in Branch.__table__.columns}
            filtered_data = {k: v for k, v in data.items() if k in valid_keys}
            if filtered_data.get("updated_at"):
                filtered_data["updated_at"] = datetime.fromisoformat(filtered_data["updated_at"])
            return Branch(**filtered_data)
        except Exception:
            pass

    # Cache miss
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if branch:
        try:
            data = {c.key: getattr(branch, c.key) for c in Branch.__table__.columns}
            cache_set(cache_key, json.dumps(data, default=_json_serial), ttl=600)
        except Exception:
            pass
    return branch


def invalidate_branch_cache(branch_id: int):
    """Invalidate cache for a specific branch."""
    cache_delete(f"branch_geofence:{branch_id}")


def get_holidays(db: Session, year: int) -> List[Holiday]:
    """Retrieve all holidays for a specific year, caching in Redis for 1 hour (3600 seconds)."""
    cache_key = f"holidays_year:{year}"
    cached = cache_get(cache_key)
    if cached:
        try:
            items = json.loads(cached)
            result = []
            valid_keys = {c.key for c in Holiday.__table__.columns}
            for item in items:
                filtered_data = {k: v for k, v in item.items() if k in valid_keys}
                if filtered_data.get("date"):
                    filtered_data["date"] = date.fromisoformat(filtered_data["date"])
                result.append(Holiday(**filtered_data))
            return result
        except Exception:
            pass

    # Cache miss
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    holidays = db.query(Holiday).filter(Holiday.date >= start_date, Holiday.date <= end_date).all()
    try:
        items = []
        for h in holidays:
            item = {c.key: getattr(h, c.key) for c in Holiday.__table__.columns}
            items.append(item)
        cache_set(cache_key, json.dumps(items, default=_json_serial), ttl=3600)
    except Exception:
        pass
    return holidays


def invalidate_holiday_cache(year: int):
    """Invalidate holiday cache for a specific year."""
    cache_delete(f"holidays_year:{year}")
