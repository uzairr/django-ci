import logging
import json
import os
import asyncio
import sys
import uuid
import re
import requests
from dotenv import load_dotenv
from pyngrok import ngrok
from fixa import Test, Agent, Scenario, TestRunner
from fixa.evaluators import LocalEvaluator

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
OUTPUT_JSONL = "runs_output_v2.jsonl"
RECORDINGS_DIR = "recordings_v2"


class TwilioTestRunner:
    def __init__(self):
        # Load environment variables
        load_dotenv(override=True)

        # Configure Twilio and ngrok
        self.twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        self.twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        self.twilio_phone_number = "+17865057288"  # Twilio phone number to initiate calls from
        self.phone_number_to_call = "+923216800799"  # Phone number of your agent

        # Set up ngrok
        ngrok_token = "2syv02Nm9vzw4a5GZbI9hP8UOib_4T7pFRBgEUKe7sj6hVMh"
        if not ngrok_token:
            raise ValueError("NGROK_AUTH_TOKEN not found in environment variables")
        ngrok.set_auth_token(ngrok_token)

    async def setup_ngrok(self, port=8765):
        """Set up ngrok forwarding for the specified port"""
        listener = await ngrok.connect(port, authtoken="2syv02Nm9vzw4a5GZbI9hP8UOib_4T7pFRBgEUKe7sj6hVMh")
        return listener, port

    def load_test_case(self, case_file, line_index=0):
        """Load a specific test case from a JSONL file"""
        case_file_path = os.path.join(os.path.dirname(__file__), case_file)

        with open(case_file_path, "r") as infile:
            all_lines = infile.readlines()
            if not all_lines:
                raise Exception(f"No data found in {case_file_path}")

        try:
            selected_line = all_lines[line_index].strip()
            return json.loads(selected_line)
        except IndexError:
            raise Exception(f"Could not find line index {line_index} in {case_file_path}")

    def extract_call_details(self, case_line):
        """Extract call details from a case line"""
        call_details_str = case_line.get("response", "{}")
        call_details = json.loads(call_details_str)

        return {
            "caller": call_details.get("caller", "Unknown Caller"),
            "contact": call_details.get("contact", ""),
            "property_info": call_details.get("property", "Unknown property"),
            "issue_text": call_details.get("issue", "No issue provided"),
            "urgency": call_details.get("urgency", "Normal")
        }

    def create_scenario_prompt(self, call_details, unique_call_id):
        """Create a scenario prompt from call details"""
        return (
            f"You are {call_details['caller']}, calling from the property at {call_details['property_info']}. "
            f"Your contact number is {call_details['contact']}. "
            f"The issue you're reporting is: {call_details['issue_text']}\n\n"
            f"Urgency: {call_details['urgency']}. "
            f"This is a unique testID: {unique_call_id}. "
            "You are speaking to a property manager. If the manager says 'goodbye' or ends the call, "
            "call the function end_call.\n\n"
            "Start by greeting them and explaining your issue. If the issue that you are calling about is a maintenance issue, "
            "you are a somewhat angry customer. You may give some random wrong information and then correct yourself. "
            "You may also express any frustrations you feel based on the problem described.\n\n"
            "Note: If a 'flow' field is provided in the test case you are encouraged to use it as a guideline for the "
            "conversation's progression. However, you should not be forced to strictly adhere to it—use your best judgment. "
            "For service or maintenance calls, feel free to adopt a slightly rude, sarcastic, or frustrated tone if it suits the situation."
        )

    async def download_recording(self, call_sid, recording_name):
        """Download a call recording"""
        if not recording_name:
            logger.warning(f"No recording name provided for call {call_sid}")
            return

        if recording_name.endswith(".mp3"):
            local_mp3_filename = recording_name
        else:
            local_mp3_filename = f"{recording_name}.mp3"

        local_mp3 = os.path.join(RECORDINGS_DIR, local_mp3_filename)
        recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_account_sid}/Recordings/{recording_name}"

        logger.info(f"Downloading recording {local_mp3_filename} to {local_mp3} ...")
        try:
            with requests.get(
                    recording_url,
                    auth=(self.twilio_account_sid, self.twilio_auth_token),
                    stream=True
            ) as r:
                r.raise_for_status()
                with open(local_mp3, 'wb') as out_file:
                    for chunk in r.iter_content(chunk_size=8192):
                        out_file.write(chunk)
            logger.info(f"Successfully downloaded recording to {local_mp3}")
        except Exception as e:
            logger.error(f"Failed to download recording: {e}")

    def save_test_result(self, call_sid, result):
        """Save test result to JSONL file"""
        recording_name = None
        if result.stereo_recording_url:
            m = re.search(r'/Recordings/([^/?]+)', result.stereo_recording_url)
            if m:
                recording_name = m.group(1)

        entry = {
            "call_sid": call_sid,
            "scenario_name": result.test.scenario.name,
            "agent_name": result.test.agent.name,
            "transcript": result.transcript,
            "recording_name": recording_name,
            "evaluation_results": (
                [r.__dict__ for r in result.evaluation_results.evaluation_results]
                if result.evaluation_results else None
            ),
        }

        with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry))
            f.write("\n")

        return recording_name

    async def run_test(self, case_file="test_cases/base_cases.jsonl", line_index=0):
        """Run a single test"""
        # Create recordings directory if it doesn't exist
        os.makedirs(RECORDINGS_DIR, exist_ok=True)

        # Generate a unique ID for this call
        unique_call_id = str(uuid.uuid4())

        # Load test case and extract details
        case_line = self.load_test_case(case_file, line_index)
        call_details = self.extract_call_details(case_line)

        # Log call details
        logger.info(f"Test details:")
        for key, value in call_details.items():
            logger.info(f"{key}: {value}")

        # Create scenario prompt
        scenario_prompt = self.create_scenario_prompt(call_details, unique_call_id)

        # Set up agent and scenario
        agent = Agent(
            name="jessica",
            prompt="you are a young woman named jessica who says 'like' a lot",
        )

        scenario = Scenario(
            name="client_complaint",
            prompt=scenario_prompt,
        )

        # Set up ngrok and test runner
        # listener, port = await self.setup_ngrok()
        domain = "https://callqa-twilio.affable.com"
        breakpoint()
        test_runner = TestRunner(
            port=8765,
            ngrok_url=domain,
            twilio_phone_number=self.twilio_phone_number,
            evaluator=LocalEvaluator(),
        )

        # Add test to runner
        test = Test(scenario=scenario, agent=agent)
        test_runner.add_test(test)

        # Run the test
        logger.info(f"Starting test run to {self.phone_number_to_call}")
        test_results = await test_runner.run_tests(
            phone_number=self.phone_number_to_call,
            type=TestRunner.OUTBOUND,
        )
        logger.info("Test completed")

        # Process and save results
        for result in test_results:
            call_sid = getattr(result, "call_sid", "unknown_call_sid")
            recording_name = self.save_test_result(call_sid, result)
            if recording_name:
                await self.download_recording(call_sid, recording_name)

        return test_results


async def main():
    # Get line index from command line arguments if provided
    line_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    # Create and run the test runner
    runner = TwilioTestRunner()
    results = await runner.run_test(line_index=line_index)

    logger.info(f"Test results: {results}")


if __name__ == "__main__":
    asyncio.run(main())
