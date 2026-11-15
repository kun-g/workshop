"""来源与人工补发使用相同的业务日和周起点。"""
from datetime import timedelta
from zoneinfo import ZoneInfo


def period(config, now):
    local = now.astimezone(ZoneInfo(config['timezone']))
    day = (local if local.hour >= config['hour'] else local - timedelta(days=1)).date()
    week = day - timedelta(days=(day.weekday() - config['weekday']) % 7)
    return str(day), str(week)
