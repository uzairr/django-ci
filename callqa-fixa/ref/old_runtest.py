import subprocess
import json
import os
import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import FileResponse, Response
import uvicorn

from pydantic import BaseModel
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

# Ensure your application can import your local modules.
sys.path.insert(0, "/home/ubuntu/fixa/fixa/src")
sys.path.insert(0, "/home/ubuntu/fixa/fixa/src/test_runner")


# ----- Data model for incoming test data -----
class BeaumontData(BaseModel):
    caller: str
    contact: str
    property: str
    issue: str
    urgency: str


from fastapi.middleware.cors import CORSMiddleware

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
    cmd = ["python", "runtest.py", str(line_index)]
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
# In your FastAPI code:
listeners = set()


@app.websocket("/ws/stream")
async def twilio_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        async for message in websocket.iter_text():
            # This is from Twilio
            # parse JSON
            data = json.loads(message)
            # forward to all local listeners
            for listener in list(listeners):
                await listener.send_text(json.dumps(data))
    except WebSocketDisconnect:
        pass


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


@app.post("/twiml")
async def twiml_endpoint():
    '''
    ngrok_subdomain = os.getenv("NGROK_SUBDOMAIN", "daria-tech.ngrok.io")
    complaint_phone = os.getenv("COMPLAINT_PHONE_NUMBER", "+923028226466")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="wss://{ngrok_subdomain}/ws/stream" track="both_tracks" />
    </Start>
    <Say>Hello! I need some help. .</Say>
    <Dial timeout="20">{complaint_phone}</Dial>
</Response>
"""
    log_debug(f"Returning TwiML: {twiml_response}")
    return Response(content=twiml_response, media_type="application/xml")
    '''
    server_ip = os.getenv("SERVER_IP", "35.212.37.243")
    complaint_phone = os.getenv("COMPLAINT_PHONE_NUMBER", "+923028226466")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="ws://{server_ip}/ws/stream" track="both_tracks" />
    </Start>
    <Say>Hello! I need some help.</Say>
    <Dial timeout="20">{complaint_phone}</Dial>
</Response>
"""


@app.get("/")
async def root():
    return {"status": "ok"}


logs = []


def log_event(message: str):
    from datetime import datetime
    timestamp = datetime.utcnow().isoformat()
    logs.append(f"{timestamp}: {message}")
    if len(logs) > 100:
        del logs[0]


call_data = {}


def clear_logs():
    global logs
    logs = []


@app.get("/status")
async def status(background_tasks: BackgroundTasks):
    current_logs = logs[-20:]
    background_tasks.add_task(clear_logs)
    return {"status": "running", "logs": current_logs}


@app.get("/simulate_event")
async def simulate_event():
    log_event("Simulated call event occurred.")
    return {"message": "Event logged."}


@app.get("/call_status")
async def call_status():
    # For debugging, return a dummy call status that matches what TestRunner expects.
    # In production, this should return actual call statuses.
    global call_data
    return call_data


from fastapi import Request, Form


@app.post("/twilio_call_update")
async def twilio_call_update(request: Request):
    """
    Webhook Twilio calls when call status changes.
    Twilio sends form-encoded data, so parse it accordingly.
    """
    global call_data
    form_data = await request.form()

    # Twilio typically sends these (case-sensitive)
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    recording_url = form_data.get("RecordingUrl")

    # Quick sanity check
    if not call_sid:
        return "No CallSid provided", 400

    # If we haven't seen this call before, create a placeholder
    if call_sid not in call_data:
        call_data[call_sid] = {
            "transcript": None,
            "stereo_recording_url": None,
            "status": "in_progress",
            "error": None,
        }

    # Update status from Twilio
    call_data[call_sid]["status"] = call_status or "unknown"

    # If Twilio includes a RecordingUrl for a completed call, store it
    if recording_url and call_status == "completed":
        # Twilio recordings typically end in .mp3, or .wav, etc.
        call_data[call_sid]["stereo_recording_url"] = recording_url + ".mp3"

    print(f"Updated call_data for {call_sid} with status={call_status}, RecordingUrl={recording_url}")
    return {"message": "OK"}


# ----- Main entry point -----
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
