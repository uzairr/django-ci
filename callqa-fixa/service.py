import subprocess
import json
import os
import time
import sys
import threading
import tempfile
import shlex
from typing import Any, List, Dict, Set, Optional
import uvicorn
import asyncio

from pydantic import BaseModel
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

# Ensure your application can import your local modules.
sys.path.insert(0, "/home/ubuntu/fixa/fixa/src")
sys.path.insert(0, "/home/ubuntu/affable/")


# ----- Data model for incoming test data -----
class BeaumontData(BaseModel):
    caller: str
    contact: str
    property: str
    issue: str
    urgency: str


from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

app = FastAPI()

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)

# Paths for output files and recordings
TEST_OUTPUT_FILE = "test_runs_output.jsonl"
RECORDINGS_DIR = "recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# Initialize WebSocket listeners and logs storage
listeners: Set[WebSocket] = set()
logs: List[str] = []  # Store recent logs
max_logs = 1000  # Maximum number of logs to store

# Import TestRunner after ensuring paths are set
import requests
from runtestv2 import TwilioTestRunner

# Lock for thread-safe log operations
log_lock = threading.Lock()


# Broadcast function for WebSockets
async def broadcast_to_listeners(message: str):
    """Send message to all connected WebSocket clients"""
    global logs
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted_message = f"[{timestamp}] {message}"

    # Thread-safe log addition
    with log_lock:
        # Store log for history
        logs.append(formatted_message)
        # Keep only the most recent logs
        if len(logs) > max_logs:
            logs = logs[-max_logs:]

    # Broadcast to all connected clients
    disconnected = set()
    for websocket in listeners:
        try:
            await websocket.send_text(formatted_message)
        except Exception:
            # Mark for removal if sending fails
            disconnected.add(websocket)

    # Remove disconnected clients
    listeners.difference_update(disconnected)


# Custom stream reader for subprocess output
async def read_stream(stream, prefix: str):
    """Read from a stream line by line and broadcast each line"""
    while True:
        line = await stream.readline()
        if not line:
            break
        line_str = line.decode('utf-8', errors='replace').rstrip()
        if line_str:  # Skip empty lines
            await broadcast_to_listeners(f"{prefix}: {line_str}")


def fetch_transcript_from_deepgram(recording_url: str) -> list:
    """
    Fetch the transcript from Deepgram using the provided recording URL.
    """
    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
    if not DEEPGRAM_API_KEY:
        asyncio.create_task(broadcast_to_listeners("Deepgram API key not set."))
        return []

    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    endpoint = f"https://api.deepgram.com/v1/listen?url={recording_url}"

    try:
        response = requests.get(endpoint, headers=headers)
        if response.status_code == 200:
            data = response.json()
            transcript = data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript",
                                                                                                           "")
            asyncio.create_task(broadcast_to_listeners(f"Fetched transcript from Deepgram: {transcript}"))
            return [transcript] if transcript else []
        else:
            asyncio.create_task(broadcast_to_listeners(f"Deepgram API error: {response.status_code} {response.text}"))
            return []
    except Exception as e:
        asyncio.create_task(broadcast_to_listeners(f"Deepgram exception: {str(e)}"))
        return []


async def log_debug(message: str):
    """Debug logger that also broadcasts to WebSockets"""
    print(f"DEBUG: {message}")
    await broadcast_to_listeners(f"DEBUG: {message}")


# ----- Helper functions for running tests -----
async def run_scenario_with_data(data: BeaumontData) -> dict:
    """
    Calls scenario_with_data.py with provided data using async subprocess.
    """
    # Dump the data to JSON
    json_data = json.dumps(data.model_dump())

    # Create a temporary file to store the JSON data
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tf:
        tf.write(json_data)
        temp_file = tf.name

    try:
        await log_debug(f"Starting scenario with data (using temp file {temp_file})")

        # Prepare command with properly escaped arguments
        cmd = ["python", "scenario_with_data.py", temp_file]

        # Prepare environment variables
        env = os.environ.copy()
        additional_paths = "/home/ubuntu/fixa/fixa/src:/home/ubuntu/fixa/fixa/src/test_runner"
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = additional_paths + ":" + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = additional_paths

        await log_debug(f"Environment PYTHONPATH: {env.get('PYTHONPATH')}")
        await log_debug(f"Running command: {' '.join(cmd)}")

        # Run the process with async output capture
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        # Set up tasks to read stdout and stderr concurrently
        stdout_task = asyncio.create_task(read_stream(process.stdout, "STDOUT"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "STDERR"))

        # Wait for the process to complete
        exit_code = await process.wait()

        # Ensure all output is processed
        await stdout_task
        await stderr_task

        await log_debug(f"Process exited with code: {exit_code}")

        if exit_code != 0:
            raise RuntimeError(f"Error running scenario_with_data.py, exit code: {exit_code}")

        # Get the results
        result = get_last_test_entry()
        await log_debug(f"Test completed successfully")
        return result

    except Exception as e:
        await log_debug(f"ERROR: {str(e)}")
        raise
    finally:
        # Clean up the temp file
        try:
            os.unlink(temp_file)
        except Exception as e:
            await log_debug(f"Failed to delete temp file: {str(e)}")


async def run_runtest(line_index: int) -> None:
    """
    Runs runtestv2.py with the integrated WebSocket logging.
    """
    await broadcast_to_listeners(f"Starting test run for case index: {line_index}")

    try:
        # Create TwilioTestRunner with our broadcast function
        runner = TwilioTestRunner(broadcast_callback=broadcast_to_listeners)

        # Run the test directly
        await broadcast_to_listeners(f"Executing test for line index {line_index}")
        results = await runner.run_test(line_index=line_index)

        # Log results summary
        result_summary = f"Test completed with {len(results)} results"
        await broadcast_to_listeners(result_summary)

        # Log details of each result
        for i, result in enumerate(results):
            call_sid = getattr(result, 'call_sid', 'unknown')
            transcript_length = len(getattr(result, 'transcript', []))
            await broadcast_to_listeners(
                f"Result {i + 1}: Call SID {call_sid}, Transcript entries: {transcript_length}")

    except Exception as e:
        error_msg = f"Error running test: {str(e)}"
        await broadcast_to_listeners(error_msg)
        raise RuntimeError(error_msg)


async def run_subprocess_with_logging(cmd, env=None):
    """Generic function to run a subprocess with output logging to WebSocket"""
    await log_debug(f"Running command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )

    # Read stdout and stderr concurrently
    stdout_task = asyncio.create_task(read_stream(process.stdout, "STDOUT"))
    stderr_task = asyncio.create_task(read_stream(process.stderr, "STDERR"))

    # Wait for the process to complete
    exit_code = await process.wait()

    # Ensure all output is processed
    await stdout_task
    await stderr_task

    await log_debug(f"Process exited with code: {exit_code}")

    if exit_code != 0:
        raise RuntimeError(f"Process failed with exit code: {exit_code}")


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


# ----- API endpoints -----
@app.get("/run_test/{line_index}")
async def run_test(line_index: int, background_tasks: BackgroundTasks):
    """Endpoint to run a test case by line index"""
    try:
        # Run test in background task to not block the response
        background_tasks.add_task(run_runtest, line_index)
        return {
            "status": "Test started",
            "message": "Check WebSocket or status endpoint for updates",
            "line_index": line_index
        }
    except Exception as e:
        await log_debug(f"Error in /run_test: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/recordings/{recording_name}")
async def get_recording(recording_name: str):
    """Endpoint to retrieve a recording file"""
    filepath = os.path.join(RECORDINGS_DIR, recording_name)
    if not os.path.isfile(filepath):
        await log_debug(f"Recording not found: {recording_name}")
        raise HTTPException(status_code=404, detail="Recording not found")

    await log_debug(f"Serving recording: {recording_name}")
    return FileResponse(filepath, media_type="audio/mpeg")


@app.post("/run_case")
async def run_case(data: BeaumontData, background_tasks: BackgroundTasks):
    """
    Endpoint to run a test scenario based on incoming Beaumont data.
    """
    try:
        # Log the received data
        await log_debug(f"Received case data: caller={data.caller}, property={data.property}, issue={data.issue}")

        # Run in background task
        background_tasks.add_task(run_scenario_with_data, data)
        return {
            "status": "Test case started",
            "message": "Check WebSocket or status endpoint for updates",
            "data": data.model_dump()
        }
    except Exception as e:
        await log_debug(f"Error in /run_case: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clear_logs")
async def clear_logs_endpoint():
    """Endpoint to clear the log history"""
    global logs
    with log_lock:
        logs = []
    await broadcast_to_listeners("Logs cleared")
    return {"status": "success", "message": "Logs cleared"}


@app.get("/welcome")
async def greeting():
    """Simple welcome endpoint"""
    await log_debug("Welcome endpoint accessed")
    return "Hey! welcome to the call-qa"


@app.get("/status")
async def status():
    """Endpoint to check service status and recent logs"""
    global logs
    with log_lock:
        current_logs = logs[-50:] if logs else []
    return {
        "status": "running",
        "logs_count": len(logs),
        "logs": current_logs  # This will include both INFO and DEBUG logs
    }


@app.websocket("/ws/listen")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time log streaming"""
    await websocket.accept()
    client_id = id(websocket)
    await log_debug(f"New WebSocket client connected: {client_id}")

    listeners.add(websocket)

    # Send recent logs to the new client (including both INFO and DEBUG)
    with log_lock:
        recent_logs = logs[-100:] if logs else []

    try:
        # Send welcome message and log history
        await websocket.send_text(f"Connected to call-qa log stream. {len(recent_logs)} recent logs available.")

        for log in recent_logs:
            await websocket.send_text(log)

        # Keep connection open and handle client messages
        while True:
            # Wait for any message from client (can be used for ping-pong)
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # Process client messages if needed
                if data == "ping":
                    await websocket.send_text("pong")
                else:
                    await websocket.send_text(f"Received: {data}")
            except asyncio.TimeoutError:
                # Send keepalive message every 30 seconds
                await websocket.send_text("keepalive")
    except WebSocketDisconnect:
        await log_debug(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        await log_debug(f"WebSocket error with client {client_id}: {str(e)}")
    finally:
        if websocket in listeners:
            listeners.remove(websocket)


# Startup event to initialize the service
@app.on_event("startup")
async def startup_event():
    """Initialize service on startup"""
    await broadcast_to_listeners("Service started")


# ----- Main entry point -----
if __name__ == "__main__":
    # Start the FastAPI application with Uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
