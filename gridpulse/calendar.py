"""National and subdivision calendars, explicit provenance and uncertainty."""
import importlib.metadata

import holidays
import pandas as pd

STATES = ["BW", "BY", "BE", "BB", "HB", "HH", "HE", "MV", "NI", "NW", "RP", "SL", "SN", "ST", "SH", "TH"]


def calendar_days(start, end):
    dates = pd.date_range(start, end, freq="D")
    years = sorted(set(dates.year))
    national = holidays.Germany(years=years, language="de")
    states = [holidays.Germany(years=years, subdiv=s, language="de") for s in STATES]
    version = importlib.metadata.version("holidays")
    rows = []
    for stamp in dates:
        day = stamp.date()
        weekend = day.weekday() >= 5
        rows.append({"date_local": str(day), "weekday": day.weekday(),
                     "is_weekend": int(weekend), "is_national_holiday": int(day in national),
                     "workday_fraction": sum(not weekend and day not in cal for cal in states) / 16,
                     "holiday_name": national.get(day, ""), "calendar_known": 16,
                     "source_url": "https://holidays.readthedocs.io/",
                     "calendar_source": "python-holidays " + version})
    return pd.DataFrame(rows)


def api_calendar(days):
    rows = []
    for date, group in days.groupby("date"):
        known = group.is_workday_estimate.notna()
        rows.append({"date_local": date, "weekday": int(group.weekday.iloc[0]),
                     "is_weekend": int(group.is_weekend.any()),
                     "is_national_holiday": int(group.is_national_holiday.any()),
                     "workday_fraction": float(group.loc[known, "is_workday_estimate"].mean()) if known.any() else None,
                     "holiday_name": " · ".join(sorted(set(group.holiday_names) - {""})),
                     "calendar_known": int(known.sum()), "calendar_source": "Feiertage API",
                     "source_url": group.source_url.iloc[0], "raw_sha256": group.raw_sha256.iloc[0]})
    return pd.DataFrame(rows)
