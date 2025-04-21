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


def setup_pipecat_logging():
    """Set up logging interception for pipecat modules"""

    # Create custom handler that forwards logs to WebSockets
    class PipecatHandler(logging.Handler):
        def emit(self, record):
            if hasattr(record, 'name') and record.name.startswith('pipecat'):
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                level = record.levelname
                module = record.name
                message = record.getMessage()
                line_number = record.lineno if hasattr(record, 'lineno') else 0

                # Format exactly like the blue logs in screenshots
                formatted_message = f"{timestamp} | {level:<8} | {module}:{line_number} - {message}"

                # Use asyncio.create_task to run the broadcast async
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(broadcast_to_listeners(formatted_message))
                else:
                    print(f"Log bypassed (no event loop): {formatted_message}")

    # Configure and add handler to all pipecat loggers
    pipecat_handler = PipecatHandler()
    pipecat_handler.setLevel(logging.DEBUG)

    # Get all loggers and configure pipecat ones
    for name in logging.root.manager.loggerDict:
        if name.startswith('pipecat'):
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            logger.addHandler(pipecat_handler)
            # Make sure propagation is enabled
            logger.propagate = True
            print(f"Added WebSocket handler to {name}")

    # Also add to root logger to catch any new loggers
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(pipecat_handler)

async def broadcast_to_listeners(message: str):
    """Send message to all connected WebSocket clients"""
    global logs

    # Thread-safe log addition
    with log_lock:
        # Store log for history
        logs.append(message)
        # Keep only the most recent logs
        if len(logs) > max_logs:
            logs = logs[-max_logs:]

    # Broadcast to all connected clients
    disconnected = set()
    for websocket in listeners:
        try:
            await websocket.send_text(message)
        except Exception:
            # Mark for removal if sending fails
            disconnected.add(websocket)

    # Remove disconnected clients
    listeners.difference_update(disconnected)

async def log_debug(message: str):
    """Debug logger that also broadcasts to WebSockets"""
    print(f"DEBUG: {message}")
    await broadcast_to_listeners(f"DEBUG: {message}")


# Modify the run_runtest function to properly set up pipecat logging
async def run_runtest(line_index: int) -> None:
    """
    Runs runtestv2.py with the integrated WebSocket logging.
    """
    await broadcast_to_listeners(f"Starting test run for case index: {line_index}")

    try:
        # Make sure logging is set up before creating the runner
        setup_pipecat_logging()

        # Define direct interceptor for pipecat messages
        async def direct_broadcast(message, level="INFO"):
            # If this is a pipecat message, format it correctly
            if isinstance(message, str) and "pipecat." in message:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                formatted = f"{timestamp} | DEBUG    | {message}"
                await broadcast_to_listeners(formatted)
            else:
                # Otherwise just broadcast it normally
                await broadcast_to_listeners(f"[TEST] {message}")

        # Create TwilioTestRunner with direct broadcast function
        runner = TwilioTestRunner(broadcast_callback=direct_broadcast)

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
    print(f"New WebSocket client connected: {client_id}")

    # Add to active listeners set
    listeners.add(websocket)

    # Send recent logs to the new client
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
        print(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        print(f"WebSocket error with client {client_id}: {str(e)}")
    finally:
        if websocket in listeners:
            listeners.remove(websocket)


# Update startup event
@app.on_event("startup")
async def startup_event():
    """Initialize service on startup"""
    # Set up pipecat logging on startup
    setup_pipecat_logging()
    await broadcast_to_listeners("Service started with pipecat logging enabled")


# ----- Main entry point -----
if __name__ == "__main__":
    # Start the FastAPI application with Uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
