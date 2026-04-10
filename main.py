"""
Garmin Connect API Proxy for Railway
=====================================
Mini FastAPI app that serves Garmin Connect data via REST endpoints.

CRITICAL ARCHITECTURE NOTE:
Garmin blocks login attempts from Railway IPs (permanent 429 rate limit).
Therefore, this proxy NEVER calls garth.login(). Instead:

  1. Run scripts/generate_tokens.py LOCALLY on a Mac/PC to get OAuth tokens
  2. Paste the resulting GARMIN_OAUTH1_TOKEN and GARMIN_OAUTH2_TOKEN JSON
     strings into Railway env vars
  3. On startup, the proxy writes those JSON strings to disk and resumes
     the session via garth.resume()
  4. garth auto-refreshes access_token (25h lifetime). The refresh_token
     lasts ~30 days, after which you re-run generate_tokens.py locally
     and update the env vars.

Env vars (Railway):
  API_KEY              - Secret key to protect endpoints
  GARMIN_OAUTH1_TOKEN  - JSON string of oauth1_token (from generate_tokens.py)
  GARMIN_OAUTH2_TOKEN  - JSON string of oauth2_token (from generate_tokens.py)
  TOKEN_DIR            - Where to write tokens on disk (default: /tmp/garmin_tokens)

Endpoints:
  GET  /health                    - Health check (no auth)
  GET  /admin/session-status      - Check if tokens are valid (API key)
  GET  /activities/latest         - Most recent activity with full details
  GET  /activities?start=&end=    - Activities in date range
  GET  /daily-metrics?date=       - HRV, sleep, RHR, body battery for a date
"""

import os
import json
import logging
from datetime import date
from pathlib import Path

import garth
from fastapi import FastAPI, HTTPException, Header, Query

# --- Config ---
API_KEY = os.environ.get("API_KEY", "change-me")
TOKEN_DIR = os.environ.get("TOKEN_DIR", "/tmp/garmin_tokens")
GARMIN_OAUTH1_TOKEN = os.environ.get("GARMIN_OAUTH1_TOKEN")
GARMIN_OAUTH2_TOKEN = os.environ.get("GARMIN_OAUTH2_TOKEN")

app = FastAPI(title="Garmin Proxy", version="3.0")
logger = logging.getLogger("garmin-proxy")
logging.basicConfig(level=logging.INFO)

_session_initialized = False


# --- Auth ---

def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid or missing API key")


def _write_tokens_from_env():
    """Write the OAuth tokens from env vars to TOKEN_DIR on disk."""
    if not GARMIN_OAUTH1_TOKEN or not GARMIN_OAUTH2_TOKEN:
        raise RuntimeError(
            "GARMIN_OAUTH1_TOKEN and GARMIN_OAUTH2_TOKEN env vars must be set. "
            "Generate them locally with scripts/generate_tokens.py."
        )

    # Validate the JSON so we fail fast with a clear error
    try:
        json.loads(GARMIN_OAUTH1_TOKEN)
        json.loads(GARMIN_OAUTH2_TOKEN)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OAuth token env var is not valid JSON: {e}")

    Path(TOKEN_DIR).mkdir(parents=True, exist_ok=True)
    (Path(TOKEN_DIR) / "oauth1_token.json").write_text(GARMIN_OAUTH1_TOKEN)
    (Path(TOKEN_DIR) / "oauth2_token.json").write_text(GARMIN_OAUTH2_TOKEN)
    logger.info(f"Wrote OAuth tokens from env vars to {TOKEN_DIR}")


def ensure_session():
    """
    Make sure there is a usable garth session. NEVER calls garth.login().
    On the first request after startup we write the env-var tokens to disk
    and resume the session. On subsequent requests we only re-resume if the
    last call failed.
    """
    global _session_initialized

    if not _session_initialized:
        try:
            _write_tokens_from_env()
            garth.resume(TOKEN_DIR)
            # Sanity check
            garth.connectapi("/userprofile-service/usersettings")
            _session_initialized = True
            logger.info("Garmin session initialized from env-var tokens")
            return
        except Exception as e:
            logger.error(f"Session init failed: {type(e).__name__}: {e}")
            raise HTTPException(
                503,
                f"Garmin session init failed: {type(e).__name__}: {e}. "
                "Check that GARMIN_OAUTH1_TOKEN and GARMIN_OAUTH2_TOKEN "
                "are set correctly in Railway."
            )

    # Already initialized - try a cheap call; if it fails, re-resume once.
    try:
        garth.connectapi("/userprofile-service/usersettings")
    except Exception as e:
        logger.warning(f"Session check failed, re-resuming: {e}")
        try:
            garth.resume(TOKEN_DIR)
            garth.connectapi("/userprofile-service/usersettings")
        except Exception as e2:
            logger.error(f"Re-resume failed: {e2}")
            _session_initialized = False
            raise HTTPException(
                503,
                f"Garmin session lost and could not be recovered: "
                f"{type(e2).__name__}: {e2}. The refresh_token may have "
                "expired - re-run scripts/generate_tokens.py locally and "
                "update the Railway env vars."
            )


# --- Helpers ---

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
        act["pace_min_km"] = f"{mins}:{secs:02d}"
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
    act["garmin_link"] = f"https://connect.garmin.com/modern/activity/{act['id']}" if act["id"] else None

    return act


# --- Public Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok", "service": "garmin-proxy", "version": "3.0"}


# --- Admin Endpoints ---

@app.get("/admin/session-status")
def session_status(x_api_key: str = Header(None)):
    """Check if env-var tokens are present and working."""
    require_api_key(x_api_key)

    has_oauth1 = bool(GARMIN_OAUTH1_TOKEN)
    has_oauth2 = bool(GARMIN_OAUTH2_TOKEN)

    result = {
        "version": "3.0",
        "token_dir": TOKEN_DIR,
        "env_var_oauth1_present": has_oauth1,
        "env_var_oauth2_present": has_oauth2,
    }

    if not (has_oauth1 and has_oauth2):
        result["authenticated"] = False
        result["error"] = "Missing GARMIN_OAUTH1_TOKEN or GARMIN_OAUTH2_TOKEN env var"
        return result

    try:
        _write_tokens_from_env()
        garth.resume(TOKEN_DIR)
        garth.connectapi("/userprofile-service/usersettings")
        result["authenticated"] = True
        result["token_files"] = [
            f.name for f in Path(TOKEN_DIR).iterdir() if f.is_file()
        ]
        return result
    except Exception as e:
        result["authenticated"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        if Path(TOKEN_DIR).exists():
            result["token_files"] = [
                f.name for f in Path(TOKEN_DIR).iterdir() if f.is_file()
            ]
        return result


# --- Garmin Data Endpoints ---

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
        logger.error(f"Error fetching latest activity: {e}")
        raise HTTPException(500, str(e))


@app.get("/activities")
def get_activities(
    start: str = Query(..., description="Start date YYYY-MM-DD"),
    end: str = Query(None, description="End date YYYY-MM-DD (defaults to today)"),
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
                params={
                    "startDate": start,
                    "endDate": end,
                    "start": page_start,
                    "limit": page_size
                }
            )
            if not activities:
                break
            all_activities.extend(activities)
            if len(activities) < page_size:
                break
            page_start += page_size

        return [parse_activity(a) for a in all_activities]
    except Exception as e:
        logger.error(f"Error fetching activities: {e}")
        raise HTTPException(500, str(e))


@app.get("/daily-metrics")
def get_daily_metrics(
    date_str: str = Query(..., alias="date", description="Date YYYY-MM-DD"),
    x_api_key: str = Header(None)
):
    require_api_key(x_api_key)
    ensure_session()

    metrics = {"date": date_str}

    try:
        hrv_data = garth.connectapi(f"/hrv-service/hrv/{date_str}")
        if hrv_data and hrv_data.get("hrvSummary"):
            metrics["hrv_last_night"] = hrv_data["hrvSummary"].get("lastNightAvg")
            metrics["hrv_weekly_avg"] = hrv_data["hrvSummary"].get("weeklyAvg")
    except Exception:
        metrics["hrv_last_night"] = None

    try:
        sleep_data = garth.connectapi(
            f"/wellness-service/wellness/dailySleepData/{date_str}"
        )
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
        rhr_data = garth.connectapi(
            f"/userstats-service/wellness/daily/{date_str}"
        )
        if rhr_data:
            metrics["rhr_bpm"] = rhr_data.get("restingHeartRate")
    except Exception:
        metrics["rhr_bpm"] = None

    try:
        bb_data = garth.connectapi(
            f"/wellness-service/wellness/bodyBattery/dates/{date_str}/{date_str}"
        )
        if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
            day_data = bb_data[0]
            metrics["body_battery_high"] = day_data.get("bodyBatteryHighForDay")
            metrics["body_battery_low"] = day_data.get("bodyBatteryLowForDay")
    except Exception:
        metrics["body_battery_high"] = None
        metrics["body_battery_low"] = None

    return metrics


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
