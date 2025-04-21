import logging
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


# Create a specialized handler for pipecat logs
class PipecatWebSocketHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setLevel(logging.DEBUG)

    def emit(self, record):
        log_entry = self.format(record)
        module_name = record.name
        line_number = record.lineno
        message = record.getMessage()

        # Format similar to the blue logs in screenshots
        formatted_message = f"{module_name}:{line_number} - {message}"

        # Use create_task to avoid blocking
        asyncio.create_task(broadcast_to_listeners(formatted_message, "DEBUG"))


# Create a function to set up pipecat loggers
def setup_pipecat_logging():
    """Set up detailed logging for all pipecat modules"""
    # Create a formatter for pipecat logs
    formatter = logging.Formatter('%(name)s:%(lineno)d - %(message)s')

    # Create the custom handler
    handler = PipecatWebSocketHandler()
    handler.setFormatter(formatter)

    # Get all loggers and add handler to pipecat ones
    for name in logging.root.manager.loggerDict:
        if name.startswith('pipecat'):
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            # Remove any existing handlers to avoid duplicates
            for hdlr in logger.handlers[:]:
                if isinstance(hdlr, PipecatWebSocketHandler):
                    logger.removeHandler(hdlr)
            logger.addHandler(handler)
            print(f"Added WebSocket handler to {name}")

    # Also add to root logger to catch any new pipecat loggers
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

async def broadcast_to_listeners(message: str, level: str = "INFO"):
    """Send message to all connected WebSocket clients with proper formatting"""
    global logs
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    # Format the message similar to the blue logs in the screenshots
    if level == "DEBUG" and "pipecat." in message:
        # Extract the pipecat module name and line number if present
        parts = message.split(" - ", 1)
        if len(parts) > 1 and "pipecat." in parts[0]:
            module_info = parts[0]
            message_content = parts[1]
            formatted_message = f"{timestamp} | DEBUG    | {module_info} - {message_content}"
        else:
            formatted_message = f"{timestamp} | DEBUG    | {message}"
    else:
        formatted_message = f"[{timestamp}] {level}: {message}"

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

async def log_debug(message: str):
    """Debug logger that also broadcasts to WebSockets"""
    print(f"DEBUG: {message}")
    await broadcast_to_listeners(f"DEBUG: {message}")


async def run_runtest(line_index: int) -> None:
    """
    Runs runtestv2.py with the integrated WebSocket logging.
    """
    await broadcast_to_listeners(f"Starting test run for case index: {line_index}")

    try:
        # Set up pipecat logging first
        setup_pipecat_logging()
        await broadcast_to_listeners("Configured pipecat loggers for WebSocket broadcasting")

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
    setup_pipecat_logging()
    await broadcast_to_listeners("Service started with pipecat logging enabled")


# ----- Main entry point -----
if __name__ == "__main__":
    # Start the FastAPI application with Uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
