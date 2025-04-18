import os
import re
import json
import requests
import asyncio
from typing import Any, Dict
from dotenv import load_dotenv
from pyngrok import ngrok
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Import your local modules
from fixa import Test, Agent, Scenario, TestRunner
from fixa.evaluators import LocalEvaluator

load_dotenv(override=True)

TEST_OUTPUT_FILE = "test_runs_output.jsonl"
RECORDINGS_DIR = "recordings"
os.makedirs(RECORDINGS_DIR, exist_ok=True)

async def run_scenario_with_data(data: Any) -> Dict[str, Any]:
    TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
    TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')

    scenario_prompt = (
        f"You are {data.caller}, calling from the property at {data.property}. "
        f"Your contact number is {data.contact}. "
        f"The issue you're reporting is: {data.issue}\n\n"
        f"Urgency: {data.urgency}. "
        "You are speaking to a property manager. If the manager says 'goodbye' or ends the call, "
        "call the function end_call.\n\n"
        "Start by greeting them and explaining your issue. You may also express any frustrations "
        "you feel are relevant based on the problem described."
    )

    scenario = Scenario(name="client_complaint", prompt=scenario_prompt)
    client_agent = Agent(name="TenantCaller", prompt="")
    test = Test(scenario=scenario, agent=client_agent)

    port = 8765
    public_url = os.getenv("NGROK_PUBLIC_URL")
    if not public_url:
        logger.debug("No NGROK_PUBLIC_URL set; creating tunnel...")
        try:
            tunnel = ngrok.connect(port, subdomain="daria-tech", authtoken=os.getenv("NGROK_AUTH_TOKEN"))
            public_url = tunnel.public_url
            logger.debug(f"Ngrok tunnel created: {public_url}")
        except Exception as e:
            logger.error(f"Failed to create ngrok tunnel: {e}")
            raise
    else:
        logger.debug(f"Using existing ngrok tunnel from environment: {public_url}")

    # Debug print the URL that will be used by TestRunner
    logger.debug(f"TestRunner will use ngrok_url: {public_url}")

    test_runner = TestRunner(
        port=port,
        ngrok_url=public_url,
        twilio_phone_number=os.getenv("TWILIO_PHONE_NUMBER", "+19064226939"),
        evaluator=LocalEvaluator(),
    )
    test_runner.add_test(test)

    complaint_phone = os.getenv("COMPLAINT_PHONE_NUMBER", "+1235550000")
    logger.debug(f"Complaint phone set to: {complaint_phone}")

    try:
        logger.debug("Starting outbound test via TestRunner...")
        test_results = await test_runner.run_tests(
            phone_number=complaint_phone,
            test_type=TestRunner.OUTBOUND,
        )
        logger.debug(f"Outbound test results: {test_results}")
    except Exception as e:
        logger.error(f"Error during TestRunner.run_tests: {e}")
        raise

    result = test_results[0]
    # (Further debug statements could log details of the result)
    logger.debug(f"Test result obtained: {result}")

    call_sid = getattr(result, "call_sid", "unknown_call_sid")
    transcript_data = result.transcript
    recording_name = None
    if result.stereo_recording_url:
        m = re.search(r'/Recordings/([^/?]+)', result.stereo_recording_url)
        if m:
            recording_name = m.group(1)

    entry = {
        "call_sid": call_sid,
        "scenario_name": result.test.scenario.name,
        "agent_name": result.test.agent.name,
        "transcript": transcript_data,
        "recording_name": recording_name,
        "evaluation_results": (
            [r.__dict__ for r in result.evaluation_results.evaluation_results]
            if result.evaluation_results else None
        ),
    }

    with open(TEST_OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    logger.debug(f"Entry written to {TEST_OUTPUT_FILE}: {entry}")

    if recording_name:
        recording_url_no_auth = re.sub(r'^https://[^@]*@', 'https://', result.stereo_recording_url)
        local_mp3 = os.path.join(RECORDINGS_DIR, f"{recording_name}.mp3")
        logger.debug(f"Downloading recording from {recording_url_no_auth} to {local_mp3}...")
        with requests.get(
            recording_url_no_auth,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            stream=True
        ) as r:
            r.raise_for_status()
            with open(local_mp3, 'wb') as out_file:
                for chunk in r.iter_content(chunk_size=8192):
                    out_file.write(chunk)
        logger.debug("Recording downloaded successfully.")
    return entry

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scenario_with_data.py '<json_data>'")
        sys.exit(1)
    json_data = sys.argv[1]
    data_dict = json.loads(json_data)
    from types import SimpleNamespace
    data_obj = SimpleNamespace(**data_dict)
    result = asyncio.run(run_scenario_with_data(data_obj))
    print(result)