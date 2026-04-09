"""
Garmin Connect API Proxy for Railway
"""

import os
import logging
from datetime import date

import garth
from fastapi import FastAPI, HTTPException, Header, Query

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
API_KEY = os.environ.get("API_KEY", "change-me")
TOKEN_DIR = "/tmp/garmin_tokens"

app = FastAPI(title="Garmin Proxy", version="1.0")
logger = logging.getLogger("garmin-proxy")
logging.basicConfig(level=logging.INFO)


def ensure_session():
    """Login or resume garth session."""
    try:
        garth.resume(TOKEN_DIR)
        garth.connectapi("/userprofile-service/usersettings")
        logger.info("Resumed existing Garmin session")
    except Exception:
        if not GARMIN_EMAIL or not GARMIN_PASSWORD:
            raise HTTPException(500, "GARMIN_EMAIL and GARMIN_PASSWORD must be set")
        logger.info("Logging in to Garmin Connect...")
        garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
        garth.save(TOKEN_DIR)
        logger.info("Logged in and saved tokens")


def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid or missing API key")


def parse_activity(raw: dict) -> dict:
    """Extract relevant fields from a Garmin activity object."""
    act = {}
    act["id"] = raw.get("activityId")
    act["name"] = raw.get("activityName", "")
    act["type"] = raw.get("activityType", {}).get("typeKey", "unknown")
    act["date"] = raw.get("startTimeLocal", "")[:10]
    act["start_time"] = raw.get("startTimeLocal", "")
    duration_sec = raw.get("duration")
    act["duration_min"] = round(duration_sec / 60, 2) if duration_sec else None
    distance_m = raw.get("distance")
    act["distance_km"] = round(distance_m / 1000, 2) if distance_m else None
    avg_speed = raw.get("averageSpeed")
    if avg_speed and avg_speed > 0 and act["type"] in ("running", "trail_running", "treadmill_running"):
        pace_sec = 1000 / avg_speed
        mins = int(pace_sec // 60)
        secs = int(pace_sec % 60)
        act["pace_min_km"] = str(mins) + ":" + str(secs).zfill(2)
    else:
        act["pace_min_km"] = None
    act["avg_hr"] = raw.get("averageHR")
    act["max_hr"] = raw.get("maxHR")
    act["calories"] = raw.get("calories")
    act["elevation_m"] = raw.get("elevationGain")
    act["avg_cadence"] = raw.get("averageRunningCadenceInStepsPerMinute") or raw.get("averageBikingCadenceInRevPerMinute")
    act["training_load"] = raw.get("activityTrainingLoad")
    act["avg_power"] = raw.get("avgPower")
    act["max_power"] = raw.get("maxPower")
    if act["id"]:
        act["garmin_link"] = "https://connect.garmin.com/modern/activity/" + str(act["id"])
    else:
        act["garmin_link"] = None
    return act


@app.get("/health")
def health():
    return {"status": "ok", "service": "garmin-proxy"}


@app.get("/activities/latest")
def get_latest_activity(x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    ensure_session()
    try:
        activities = garth.connectapi(
            "/activitylist-service/activities/search/activities",
            params={"limit": 1, "start": 0}
        )
        if not activities:
            raise HTTPException(404, "No activities found")
        return parse_activity(activities[0])
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching latest activity: %s", e)
        raise HTTPException(500, str(e))


@app.get("/activities")
def get_activities(
    start: str = Query(..., description="Start date YYYY-MM-DD"),
    end: str = Query(None, description="End date YYYY-MM-DD"),
    x_api_key: str = Header(None)
):
    require_api_key(x_api_key)
    ensure_session()
    if not end:
        end = date.today().isoformat()
    try:
        all_activities = []
        page_start = 0
        page_size = 20
        while True:
            activities = garth.connectapi(
                "/activitylist-service/activities/search/activities",
                params={"startDate": start, "endDate": end, "start": page_start, "limit": page_size}
            )
            if not activities:
                break
            all_activities.extend(activities)
            if len(activities) < page_size:
                break
            page_start += page_size
        return [parse_activity(a) for a in all_activities]
    except Exception as e:
        logger.error("Error fetching activities: %s", e)
        raise HTTPException(500, str(e))


@app.get("/daily-metrics")
def get_daily_metrics(
    date_str: str = Query(..., alias="date", description="Date YYYY-MM-DD"),
    x_api_key: str = Header(None)
):
    require_api_key(x_api_key)
    ensure_session()
    metrics = {}
    try:
        try:
            hrv_data = garth.connectapi("/hrv-service/hrv/" + date_str)
            if hrv_data and hrv_data.get("hrvSummary"):
                metrics["hrv_last_night"] = hrv_data["hrvSummary"].get("lastNightAvg")
                metrics["hrv_weekly_avg"] = hrv_data["hrvSummary"].get("weeklyAvg")
        except Exception:
            metrics["hrv_last_night"] = None
        try:
            sleep_data = garth.connectapi("/wellness-service/wellness/dailySleepData/" + date_str)
            if sleep_data:
                sleep_sec = sleep_data.get("sleepTimeSeconds")
                metrics["sleep_hours"] = round(sleep_sec / 3600, 2) if sleep_sec else None
                overall = sleep_data.get("overallSleepScore")
                if overall:
                    metrics["sleep_quality_1_5"] = min(5, max(1, round(overall / 20)))
                else:
                    metrics["sleep_quality_1_5"] = None
        except Exception:
            metrics["sleep_hours"] = None
            metrics["sleep_quality_1_5"] = None
        try:
            rhr_data = garth.connectapi("/userstats-service/wellness/daily/" + date_str)
            if rhr_data:
                metrics["rhr_bpm"] = rhr_data.get("restingHeartRate")
        except Exception:
            metrics["rhr_bpm"] = None
        try:
            bb_url = "/wellness-service/wellness/bodyBattery/dates/" + date_str + "/" + date_str
            bb_data = garth.connectapi(bb_url)
            if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
                day_data = bb_data[0]
                metrics["body_battery_high"] = day_data.get("bodyBatteryHighForDay")
                metrics["body_battery_low"] = day_data.get("bodyBatteryLowForDay")
        except Exception:
            metrics["body_battery_high"] = None
            metrics["body_battery_low"] = None
        metrics["date"] = date_str
        return metrics
    except Exception as e:
        logger.error("Error fetching daily metrics: %s", e)
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

