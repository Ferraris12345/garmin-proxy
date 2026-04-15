"""
Garmin Connect API Proxy for Railway — v4.0
============================================
Mini FastAPI app that serves Garmin Connect data via REST endpoints.

ARCHITECTURE:
Garmin PERMANENTLY blocks garth.login() from Railway/datacenter IPs (429).
This is not a rate-limit issue — it's IP-based blocking. Even one login/day
from Railway will fail. Therefore this proxy NEVER calls garth.login().

The correct flow is:
  1. Run scripts/generate_tokens.py LOCALLY on a Mac (residential IP)
  2. Either:
     a) POST the tokens directly to /admin/set-tokens (preferred — no Railway
        dashboard needed, callable from Cowork/curl)
     b) Paste tokens as env vars in Railway dashboard (manual fallback)
  3. garth auto-refreshes access_token (~25h lifetime) from Railway — this IS
     allowed because token refresh uses a different Garmin endpoint that does
     not enforce IP restrictions
  4. refresh_token lasts ~30 days. When it expires, re-run generate_tokens.py
     locally and POST to /admin/set-tokens again.

Env vars (Railway):
  API_KEY              - Secret key to protect all endpoints (required)
  GARMIN_OAUTH1_TOKEN  - JSON string of oauth1_token (optional, read at startup)
  GARMIN_OAUTH2_TOKEN  - JSON string of oauth2_token (optional, read at startup)
  TOKEN_DIR            - Token storage dir (auto-detected: /data if exists, else /tmp)

Endpoints:
  GET  /health                    - Health check (no auth)
  GET  /admin/session-status      - Auth status + token expiry (API key)
  POST /admin/set-tokens          - Inject OAuth tokens without Railway dashboard (API key)
  GET  /activities/latest         - Most recent activity
  GET  /activities?start=&end=    - Activities in date range
  GET  /daily-metrics?date=       - HRV, sleep, RHR, body battery for a date
"""

import os
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import garth
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────

API_KEY = os.environ.get("API_KEY", "change-me")

# Prefer /data (Railway persistent volume) over /tmp (ephemeral).
# Can be overridden with TOKEN_DIR env var.
_default_token_dir = (
    "/data/garmin_tokens"
    if Path("/data").exists() and os.access("/data", os.W_OK)
    else "/tmp/garmin_tokens"
)
TOKEN_DIR = os.environ.get("TOKEN_DIR", _default_token_dir)

# Tokens from env vars (read at startup, optional — can also be set via /admin/set-tokens)
_ENV_OAUTH1 = os.environ.get("GARMIN_OAUTH1_TOKEN")
_ENV_OAUTH2 = os.environ.get("GARMIN_OAUTH2_TOKEN")

app = FastAPI(title="Garmin Proxy", version="4.0")
logger = logging.getLogger("garmin-proxy")
logging.basicConfig(level=logging.INFO)

_session_initialized = False


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid or missing API key")


def _write_tokens_to_disk(oauth1_json: str, oauth2_json: str) -> None:
    """Validate and write OAuth JSON strings to TOKEN_DIR on disk."""
    try:
        json.loads(oauth1_json)
        json.loads(oauth2_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Token is not valid JSON: {e}")

    Path(TOKEN_DIR).mkdir(parents=True, exist_ok=True)
    (Path(TOKEN_DIR) / "oauth1_token.json").write_text(oauth1_json)
    (Path(TOKEN_DIR) / "oauth2_token.json").write_text(oauth2_json)
    logger.info(f"Wrote OAuth tokens to {TOKEN_DIR}")


def _load_tokens_from_env() -> Optional[tuple[str, str]]:
    """Return (oauth1, oauth2) from env vars if both are set, else None."""
    if _ENV_OAUTH1 and _ENV_OAUTH2:
        return _ENV_OAUTH1, _ENV_OAUTH2
    return None


def _init_session_from_disk() -> bool:
    """Try to resume garth session from disk tokens. Returns True on success."""
    oauth1_path = Path(TOKEN_DIR) / "oauth1_token.json"
    oauth2_path = Path(TOKEN_DIR) / "oauth2_token.json"
    if not oauth1_path.exists() or not oauth2_path.exists():
        return False
    try:
        garth.resume(TOKEN_DIR)
        garth.connectapi("/userprofile-service/usersettings")
        return True
    except Exception as e:
        logger.warning(f"Resume from disk failed: {e}")
        return False


def ensure_session():
    """
    Guarantee a usable garth session. Call order:
      1. If already initialized, do a cheap health check and return.
      2. Try disk tokens (written by set-tokens or previous startup).
      3. Try env-var tokens (write to disk first, then resume).
      4. Raise 503 with a clear message if all fail.
    """
    global _session_initialized

    if _session_initialized:
        # Periodic liveness check — re-resume if session was lost
        try:
            garth.connectapi("/userprofile-service/usersettings")
            return
        except Exception as e:
            logger.warning(f"Session check failed, re-resuming: {e}")
            _session_initialized = False

    # Try disk first (tokens may already be there from a previous call)
    if _init_session_from_disk():
        _session_initialized = True
        logger.info("Garmin session resumed from disk tokens")
        return

    # Try env vars
    env_tokens = _load_tokens_from_env()
    if env_tokens:
        try:
            _write_tokens_to_disk(*env_tokens)
            garth.resume(TOKEN_DIR)
            garth.connectapi("/userprofile-service/usersettings")
            _session_initialized = True
            logger.info("Garmin session initialized from env-var tokens")
            return
        except Exception as e:
            logger.error(f"Env-var token init failed: {e}")

    raise HTTPException(
        503,
        "Garmin session unavailable. POST valid tokens to /admin/set-tokens or "
        "set GARMIN_OAUTH1_TOKEN + GARMIN_OAUTH2_TOKEN env vars in Railway. "
        "Run scripts/generate_tokens.py locally to obtain fresh tokens."
    )


def _read_token_expiry() -> Optional[dict]:
    """
    Read oauth2_token.json from disk and extract expiry info.
    Returns dict with access_token_expires_at and estimated refresh_token_expires_at,
    or None if tokens are not on disk.
    """
    oauth2_path = Path(TOKEN_DIR) / "oauth2_token.json"
    if not oauth2_path.exists():
        return None
    try:
        data = json.loads(oauth2_path.read_text())
        result = {}

        # access_token expiry (stored by garth as Unix timestamp)
        expires_at = data.get("expires_at")
        if expires_at:
            dt = datetime.fromtimestamp(float(expires_at), tz=timezone.utc)
            result["access_token_expires_at"] = dt.isoformat()
            result["access_token_expired"] = dt < datetime.now(tz=timezone.utc)

        # refresh_token: garth doesn't always store its expiry explicitly,
        # but Garmin refresh_tokens last ~30 days from issuance.
        # We store created_at when writing via set-tokens for tracking.
        created_at = data.get("_created_at")
        if created_at:
            created_dt = datetime.fromisoformat(created_at)
            refresh_expires = created_dt.replace(
                day=created_dt.day
            )
            # ~30 days after creation
            from datetime import timedelta
            refresh_exp_dt = created_dt + timedelta(days=30)
            result["refresh_token_expires_approx"] = refresh_exp_dt.isoformat()
            result["refresh_token_days_left"] = max(
                0, (refresh_exp_dt - datetime.now(tz=timezone.utc)).days
            )

        return result or None
    except Exception as e:
        logger.warning(f"Could not read token expiry: {e}")
        return None


# ─── Startup: load env-var tokens to disk so they survive the first request ───

@app.on_event("startup")
async def startup():
    env_tokens = _load_tokens_from_env()
    if env_tokens:
        try:
            _write_tokens_to_disk(*env_tokens)
            logger.info("Startup: wrote env-var tokens to disk")
        except Exception as e:
            logger.warning(f"Startup: could not write env-var tokens: {e}")
    else:
        logger.info(
            f"Startup: no env-var tokens found. "
            f"POST to /admin/set-tokens to inject tokens."
        )


# ─── Public endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "garmin-proxy", "version": "4.0"}


# ─── Admin endpoints ──────────────────────────────────────────────────────────

@app.get("/admin/session-status")
def session_status(x_api_key: str = Header(None)):
    """
    Check authentication status, token presence, and expiry info.
    Use this to know when refresh_token is about to expire (~30 days).
    """
    require_api_key(x_api_key)

    has_env_oauth1 = bool(_ENV_OAUTH1)
    has_env_oauth2 = bool(_ENV_OAUTH2)
    oauth1_on_disk = (Path(TOKEN_DIR) / "oauth1_token.json").exists()
    oauth2_on_disk = (Path(TOKEN_DIR) / "oauth2_token.json").exists()

    result = {
        "version": "4.0",
        "token_dir": TOKEN_DIR,
        "env_var_oauth1_present": has_env_oauth1,
        "env_var_oauth2_present": has_env_oauth2,
        "tokens_on_disk": oauth1_on_disk and oauth2_on_disk,
    }

    # Token expiry info (from disk)
    expiry = _read_token_expiry()
    if expiry:
        result["token_expiry"] = expiry

    # Live session check
    if not (oauth1_on_disk and oauth2_on_disk) and not (has_env_oauth1 and has_env_oauth2):
        result["authenticated"] = False
        result["error"] = (
            "No tokens found on disk or in env vars. "
            "POST to /admin/set-tokens with your OAuth tokens."
        )
        return result

    try:
        garth.resume(TOKEN_DIR)
        garth.connectapi("/userprofile-service/usersettings")
        result["authenticated"] = True
        result["session_initialized"] = _session_initialized
    except Exception as e:
        result["authenticated"] = False
        result["error"] = f"{type(e).__name__}: {e}"

    return result


class SetTokensBody(BaseModel):
    oauth1_token: str
    oauth2_token: str


@app.post("/admin/set-tokens")
def set_tokens(body: SetTokensBody, x_api_key: str = Header(None)):
    """
    Inject Garmin OAuth tokens without touching the Railway dashboard.

    Generate tokens locally with scripts/generate_tokens.py, then POST here:

        curl -X POST https://<your-railway-url>/admin/set-tokens \\
          -H "X-API-Key: <your-api-key>" \\
          -H "Content-Type: application/json" \\
          -d '{
            "oauth1_token": "<paste GARMIN_OAUTH1_TOKEN value here>",
            "oauth2_token": "<paste GARMIN_OAUTH2_TOKEN value here>"
          }'

    On success the session is immediately initialized — no redeploy needed.
    Tokens are written to TOKEN_DIR on disk (persistent if /data volume is mounted).

    WHY NOT /admin/login?
    Garmin permanently 429s login attempts from Railway/datacenter IPs.
    Even one login attempt per day fails. Token refresh (what garth does
    automatically) uses a different endpoint that IS allowed from Railway.
    This is why we generate tokens from a local Mac and inject them here.
    """
    require_api_key(x_api_key)
    global _session_initialized

    # Annotate oauth2 JSON with creation timestamp for refresh_token expiry tracking
    try:
        oauth2_data = json.loads(body.oauth2_token)
        if "_created_at" not in oauth2_data:
            oauth2_data["_created_at"] = datetime.now(tz=timezone.utc).isoformat()
        oauth2_annotated = json.dumps(oauth2_data, separators=(",", ":"))
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"oauth2_token is not valid JSON: {e}")

    try:
        _write_tokens_to_disk(body.oauth1_token, oauth2_annotated)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to write tokens to disk: {e}")

    # Initialize garth session immediately
    try:
        garth.resume(TOKEN_DIR)
        garth.connectapi("/userprofile-service/usersettings")
        _session_initialized = True
        logger.info("Session initialized via /admin/set-tokens")
    except Exception as e:
        _session_initialized = False
        logger.error(f"Session init after set-tokens failed: {e}")
        raise HTTPException(
            502,
            f"Tokens written to disk but garth session failed: {type(e).__name__}: {e}. "
            "Check that the tokens are valid and not expired."
        )

    expiry = _read_token_expiry()
    return {
        "status": "ok",
        "authenticated": True,
        "token_dir": TOKEN_DIR,
        "token_expiry": expiry,
        "message": (
            "Tokens saved and session initialized. "
            "The proxy will auto-refresh the access_token (~25h). "
            "Re-inject when refresh_token expires (~30 days)."
        ),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_activity(raw: dict) -> dict:
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
        act["pace_min_km"] = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}"
    else:
        act["pace_min_km"] = None

    act["avg_hr"] = raw.get("averageHR")
    act["max_hr"] = raw.get("maxHR")
    act["calories"] = raw.get("calories")
    act["elevation_m"] = raw.get("elevationGain")
    act["avg_cadence"] = (
        raw.get("averageRunningCadenceInStepsPerMinute")
        or raw.get("averageBikingCadenceInRevPerMinute")
    )
    act["training_load"] = raw.get("activityTrainingLoad")
    act["avg_power"] = raw.get("avgPower")
    act["max_power"] = raw.get("maxPower")
    act["garmin_link"] = (
        f"https://connect.garmin.com/modern/activity/{act['id']}" if act["id"] else None
    )
    return act


# ─── Garmin data endpoints ────────────────────────────────────────────────────

@app.get("/activities/latest")
def get_latest_activity(x_api_key: str = Header(None)):
    require_api_key(x_api_key)
    ensure_session()

    try:
        activities = garth.connectapi(
            "/activitylist-service/activities/search/activities",
            params={"limit": 1, "start": 0},
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
    x_api_key: str = Header(None),
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
                    "limit": page_size,
                },
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
    x_api_key: str = Header(None),
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
            metrics["sleep_quality_1_5"] = (
                min(5, max(1, round(overall / 20))) if overall else None
            )
    except Exception:
        metrics["sleep_hours"] = None
        metrics["sleep_quality_1_5"] = None

    try:
        rhr_data = garth.connectapi(f"/userstats-service/wellness/daily/{date_str}")
        metrics["rhr_bpm"] = rhr_data.get("restingHeartRate") if rhr_data else None
    except Exception:
        metrics["rhr_bpm"] = None

    try:
        bb_data = garth.connectapi(
            f"/wellness-service/wellness/bodyBattery/dates/{date_str}/{date_str}"
        )
        if bb_data and isinstance(bb_data, list) and bb_data:
            metrics["body_battery_high"] = bb_data[0].get("bodyBatteryHighForDay")
            metrics["body_battery_low"] = bb_data[0].get("bodyBatteryLowForDay")
    except Exception:
        metrics["body_battery_high"] = None
        metrics["body_battery_low"] = None

    return metrics


@app.get("/workouts")
def list_workouts(limit: int = Query(10), x_api_key: str = Header(None)):
    """List structured workouts saved in Garmin Connect."""
    require_api_key(x_api_key)
    ensure_session()
    try:
        resp = garth.client.request(
            "GET", "connect", "/proxy/workout-service/workouts",
            api=True, params={"start": 0, "limit": limit},
        ).json()
        if isinstance(resp, list):
            return [{"workoutId": w.get("workoutId"), "workoutName": w.get("workoutName"),
                     "sportType": (w.get("sportType") or {}).get("sportTypeKey"),
                     "updatedDate": w.get("updatedDate")} for w in resp]
        return resp
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/workouts")
def create_workout(workout: dict, x_api_key: str = Header(None)):
    """
    Create a structured workout in Garmin Connect.

    POST a Garmin workout JSON directly — same format used by the Connect web app.
    On success returns the workoutId assigned by Garmin.

    Example body:
        {
          "workoutName": "5x1K Z4",
          "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
          "workoutSegments": [{"segmentOrder": 1, "sportType": {...}, "workoutSteps": [...]}]
        }
    """
    require_api_key(x_api_key)
    ensure_session()

    try:
        # connect.garmin.com/proxy/ is the correct host for workout mutations
        resp = garth.client.request(
            "POST", "connect", "/proxy/workout-service/workout",
            api=True, json=workout,
        )
        data = resp.json() if resp.text else {}
        if isinstance(data, dict):
            wid = data.get("workoutId") or data.get("id")
            return {
                "status": "created",
                "workoutId": wid,
                "workoutName": data.get("workoutName", workout.get("workoutName")),
                "raw": data,
            }
        return {"status": "unknown_response", "raw": data}
    except Exception as e:
        logger.error(f"Error creating workout: {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
