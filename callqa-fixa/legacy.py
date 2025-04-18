import subprocess
import json
import os

from typing import Any
import uvicorn
import asyncio

from pydantic import BaseModel
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

# Ensure your application can import your local modules.
sys.path.insert(0, "/home/ubuntu/fixa/fixa/src")


# ----- Data model for incoming test data -----
class BeaumontData(BaseModel):
    caller: str
    contact: str
    property: str
    issue: str
    urgency: str


from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

app = FastAPI()

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers
)

# Paths for output files and recordings
TEST_OUTPUT_FILE = "test_runs_output.jsonl"
RECORDINGS_DIR = "recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
listeners = set()

import requests


def fetch_transcript_from_deepgram(recording_url: str) -> list:
    """
    Fetch the transcript from Deepgram using the provided recording URL.
    Adjust the endpoint and parsing as needed per Deepgram's current API.
    """
    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
    if not DEEPGRAM_API_KEY:
        print("Deepgram API key not set.")
        return []
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    # Example endpoint – adjust based on Deepgram documentation.
    endpoint = f"https://api.deepgram.com/v1/listen?url={recording_url}"
    response = requests.get(endpoint, headers=headers)
    if response.status_code == 200:
        data = response.json()
        transcript = data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
        print(f"Fetched transcript from Deepgram: {transcript}")
        return [transcript] if transcript else []
    else:
        print(f"Deepgram API error: {response.status_code} {response.text}")
        return []


def log_debug(message: str):
    # Simple debug printer; in production, consider using the logging module.
    print(f"DEBUG: {message}")


# ----- Helper functions for running tests -----
def run_scenario_with_data(data: BeaumontData) -> dict:
    """
    Calls scenario_with_data.py (which performs the full test/call flow)
    by passing the provided Beaumont data as JSON on the command line.
    Returns the most recent entry from test_runs_output.jsonl.
    """
    # Dump the data to JSON using the new Pydantic v2 method
    json_data = json.dumps(data.model_dump())
    cmd = ["python", "scenario_with_data.py", json_data]
    log_debug(f"Command: {cmd}")

    # Prepare environment variables for the subprocess.
    env = os.environ.copy()
    additional_paths = "/home/ubuntu/fixa/fixa/src:/home/ubuntu/fixa/fixa/src/test_runner"
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = additional_paths + ":" + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = additional_paths
    log_debug(f"Environment PYTHONPATH: {env.get('PYTHONPATH')}")

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    log_debug(f"Subprocess return code: {proc.returncode}")
    if proc.stdout:
        log_debug(f"Subprocess stdout: {proc.stdout}")
    if proc.stderr:
        log_debug(f"Subprocess stderr: {proc.stderr}")
    if proc.returncode != 0:
        err_details = proc.stderr.strip() or "Unknown error"
        raise RuntimeError(f"Error running scenario_with_data.py: {err_details}")
    return get_last_test_entry()


def run_runtest(line_index: int) -> None:
    """
    Runs runtest.py for the given case index.
    """
    cmd = ["python", "runtestv2.py", str(line_index)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err_details = proc.stderr.strip() or "Unknown error"
        raise RuntimeError(f"Error running runtest.py: {err_details}")


def get_last_test_entry() -> dict:
    """
    Reads and returns the last JSON entry from test_runs_output.jsonl.
    """
    if not os.path.exists(TEST_OUTPUT_FILE):
        raise HTTPException(status_code=404, detail="No test_runs_output.jsonl found")
    with open(TEST_OUTPUT_FILE, "r", encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    if not lines:
        raise HTTPException(status_code=404, detail="test_runs_output.jsonl is empty")
    return json.loads(lines[-1])


# ----- Existing endpoints -----
@app.get("/run_test/{line_index}")
def run_test(line_index: int):
    try:
        run_runtest(line_index)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    try:
        result = get_last_test_entry()
    except HTTPException as e:
        raise e
    return result


@app.get("/recordings/{recording_name}")
def get_recording(recording_name: str):
    filepath = os.path.join(RECORDINGS_DIR, recording_name)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(filepath, media_type="audio/mpeg")


@app.post("/run_case")
async def run_case(data: BeaumontData):
    """
    Endpoint to run a test scenario based on incoming Beaumont data.
    This spawns a separate thread to call scenario_with_data.py.
    """
    try:
        result = await asyncio.to_thread(run_scenario_with_data, data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


# ----- New endpoints for real-time streaming via Twilio -----

def clear_logs():
    global logs
    logs = []


@app.get("/welcome")
async def greeting():
    return "Hey! welcome to the call-qa"


@app.get("/status")
async def status(background_tasks: BackgroundTasks):
    current_logs = logs[-20:]
    return {"status": "running", "logs": current_logs}


@app.websocket("/ws/listen")
async def local_listen(websocket: WebSocket):
    # This is your local script connecting
    await websocket.accept()
    listeners.add(websocket)
    try:
        while True:
            # Just keep connection open
            await asyncio.sleep(99999)
    except WebSocketDisconnect:
        listeners.remove(websocket)


# ----- Main entry point -----
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
