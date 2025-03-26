#!/usr/bin/env python3
import os
import re
import sys
import tempfile
import subprocess
import datetime
import time
import requests  # used for local API calls

# If using the OpenAI API mode, import openai.
try:
    import openai
except ImportError:
    openai = None

# ----------------------------------------------------------------------
# Configurable Settings
# ----------------------------------------------------------------------
# Set the API mode: "openai" for the official OpenAI API, or "local" for your locally hosted API.
API_MODE = "local"
OPENAI_API_KEY = "sk-ABCD"  # or set via environment variable
MODEL_NAME = "mistral-small-24b-instruct-2501"
TEMPERATURE = 0.5
MAX_TOKENS = 32768
TIMEOUT = 60                          # time limit for running generated code
MAX_CODE_HISTORY = 3                  # keep the last n "rounds" of code attempts
MAX_FAIL_WO_FDBK = 10                  # if we get this many FAILs in a row, ask the user for feedback

# Strings used by the assistant to indicate pass/fail
PASS_STR = "Project output fully satisfies project description, that is the final version and we can stop iterating"
FAIL_STR = "Project output does not fully satisfy project description, submitting revised script to re-evaluate output"

# API endpoint URLs (adjust as necessary)
LOCAL_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# ----------------------------------------------------------------------
# Name & Logging Setup
# ----------------------------------------------------------------------
script_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# If using the OpenAI API, configure the client.
if API_MODE == "openai":
    if openai is None:
        print("OpenAI library not found. Please install the openai package or switch API_MODE to 'local'.")
        sys.exit(1)
    openai.api_key = OPENAI_API_KEY

# Create a logfile for all console output.
log_filename = f"{script_name}_{timestamp}.txt"
log_file = open(log_filename, "w", encoding="utf-8")


def log_print(*args, **kwargs):
    """
    Print to console and also write to the log file simultaneously.
    """
    message = " ".join(str(arg) for arg in args)
    print(message, **kwargs)
    log_file.write(message + "\n")
    log_file.flush()


def openai_chat_completion(messages):
    """
    Call the GPT model with the given 'messages' conversation.
    Returns the text from GPT or an error message if something fails.
    
    This function supports both the official OpenAI API and a locally hosted API.
    """
    if API_MODE == "openai":
        try:
            completion = openai.ChatCompletion.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS
            )
            return completion.choices[0].message.content
        except Exception as e:
            log_print("Error calling OpenAI API:", e)
            return "No response due to an error."
    elif API_MODE == "local":
        try:
            headers = {"Content-Type": "application/json"}
            # If your local API requires an authorization header, you can include it here.
            if OPENAI_API_KEY:
                headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
            data = {
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": TEMPERATURE,
                "max_tokens": MAX_TOKENS
            }
            response = requests.post(LOCAL_API_URL, json=data, headers=headers)
            response.raise_for_status()
            completion = response.json()
            # Assuming the response follows the same structure as OpenAI's API:
            return completion["choices"][0]["message"]["content"]
        except Exception as e:
            log_print("Error calling Local API:", e)
            return "No response due to an error."
    else:
        log_print("Invalid API_MODE selected!")
        return "No response due to error in API_MODE."


def extract_code_blocks(response: str) -> str:
    """
    Extract Python code from triple backticks in GPT's response.
    If multiple code blocks exist, returns the last one found.
    """
    code_match = re.findall(r"```(?:python)?\s*(.*?)\s*```", response, flags=re.DOTALL)
    if code_match:
        return code_match[-1]
    return ""


def run_code(code_string: str) -> str:
    """
    Write 'code_string' to a temporary file, run it, capture stdout/stderr.
    Return the combined output plus a brief note on how long execution took.
    """
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".py", delete=False) as temp:
        temp_name = temp.name
        temp.write(code_string)

    try:
        start_time = time.time()
        result = subprocess.run(
            [sys.executable, temp_name],
            capture_output=True,
            text=True,
            timeout=TIMEOUT
        )
        end_time = time.time()
        duration = end_time - start_time

        output = result.stdout + "\n" + result.stderr
        output += f"\n[Code execution took {duration:.2f} seconds.]\n"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Code timed out."
    except Exception as e:
        return f"Error: {str(e)}"
    finally:
        try:
            os.remove(temp_name)
        except OSError:
            pass

def save_code_permanently(code_string: str, feedback_iter: int, code_iter: int) -> str:
    """
    Save 'code_string' to a .py file.
    Return the absolute path to the saved file.
    """
    filename = f"{script_name}_{timestamp}_fdbk{feedback_iter}_iter{code_iter}.py"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(code_string)
    return os.path.abspath(filename)


def save_output_permanently(output_string: str, feedback_iter: int, code_iter: int) -> str:
    """
    Save 'output_string' to a .txt file.
    Return the absolute path to the saved file.
    """
    filename = f"{script_name}_{timestamp}_fdbk{feedback_iter}_iter{code_iter}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(output_string)
    return os.path.abspath(filename)


def prune_history(conversation_history, max_code_rounds=5):
    """
    Keep:
      1) conversation_history[0] => system prompt
      2) conversation_history[1] => original project description
      3) any user messages with "User Feedback:"
      4) last 'max_code_rounds' "rounds" of code attempts (user -> assistant).
    """
    if len(conversation_history) <= 2:
        return conversation_history

    pinned = conversation_history[:2]
    remainder = conversation_history[2:]

    kept_reversed = []
    code_round_count = 0

    for msg in reversed(remainder):
        if msg["role"] == "user" and "User Feedback:" in msg["content"]:
            kept_reversed.append(msg)
        elif code_round_count < max_code_rounds:
            if msg["role"] == "assistant":
                kept_reversed.append(msg)
            else:
                if (
                    "We are in the Code Generation Step." in msg["content"] # wtf is this code, consolidate the strings
                    or "We are in the Code Judgment Step." in msg["content"]
                ):
                    kept_reversed.append(msg)
                    code_round_count += 1
        else:
            pass

    kept_reversed.reverse()
    pruned = pinned[:] + kept_reversed
    return pruned


def request_user_feedback():
    """
    Prompt the user for feedback, return it if given, otherwise return None.
    """
    log_print("User, please review and provide feedback or press Enter to skip.")
    user_feedback = input("<<User feedback:>> ")
    log_print("<<User feedback:>> " + user_feedback)
    if user_feedback.strip():
        return user_feedback
    else:
        return None


def handle_user_feedback(user_feedback, conversation_history, feedback_round):
    """
    If user_feedback is given, append it to conversation_history,
    increment feedback_round, and return updated feedback_round plus code_iter reset to 0.
    """
    log_print("User provided feedback => requesting further code updates.")
    feedback_round += 1
    code_iter = 0

    feedback_msg = (
        "User Feedback:\n" + user_feedback + "\n"
        "Please revise or improve the code accordingly, returning only the code in triple backticks, no pass/fail statements."
    )
    conversation_history.append({"role": "user", "content": feedback_msg})
    return feedback_round, code_iter


def main():
    # ------------------ INTRO ------------------
    system_prompt = (
        "You are a helpful AI assistant that writes Python code to satisfy a project description.\n\n"
        "We will do this in **two** main steps each iteration, plus a user feedback review:\n\n"
        "1) Initial Code Generation Step:\n"
        "   - We provide a project description.\n"
        "   - You return valid Python code in a single code block, with no pass/fail statements.\n\n"
        "2) Code Judgment Step:\n"
        
        "   - We provide the project description, your code, and the code's output.\n"
        f"   - If the output satisfies the project, reply exactly:\n\n'{PASS_STR}'\n\n"
        f"   - If not, start your reply with exactly:\n\n'{FAIL_STR}'\n\n"
        "   - No extra commentary outside these instructions please.\n\n"
    )

    conversation_history = [{"role": "system", "content": system_prompt}]

    log_print("==============================================================")
    log_print(f"Welcome to {script_name}!")
    log_print("We do a multi-step iteration with code generation, code judgment, and optional user feedback.")
    log_print(f"Keeping only the last {MAX_CODE_HISTORY} code attempts in context, plus user feedback.\n")
    log_print("==============================================================\n")

    # Hardcoded project description for demonstration.
    project_description = """
    You, an LLM, are being called by a python script on my, the user's, computer.
    You are running locally thus I am not paying per token, and I can run you indefinitely for only electricity cost.
    The API to access you is located at http://127.0.0.1:1234/v1/chat/completions
    Please try to write a python script that demonstrates connecting to the API and getting a test response in a chat message format.
    The output format should resemble an IRC log, as [timestamp] <user>:
    """
    
    
    
    log_print(f"<<User project description:>>\n{project_description}\n")
    conversation_history.append({"role": "user", "content": project_description})

    # Tracking feedback & code iteration
    feedback_round = 0
    code_iter = 0

    project_complete = False
    last_run_output = ""
    current_code = ""

    # Additional stats
    total_passes = 0
    total_fails = 0

    # This will hold GPT-provided code from a FAIL response to skip the next code generation step.
    pending_fail_code = None

    # Track how many FAILs have occurred since last user feedback.
    fails_since_feedback = 0

    while not project_complete:
        iteration_start = time.time()

        # Sub-step timers
        gen_start = None
        gen_end = None
        run_start = None
        run_end = None
        judge_start = None
        judge_end = None
        feedback_start = None
        feedback_end = None

        gpt_pass_fail_status = "INCOMPLETE"  # GPT’s perspective.
        user_feedback_status = "No feedback yet"

        conversation_history = prune_history(conversation_history, MAX_CODE_HISTORY)

        # -------------- CODE GENERATION STEP --------------
        generation_skipped = False
        if pending_fail_code is not None:
            code_iter += 1
            log_print(f"\n---- Using GPT-provided code from fail response => feedback {feedback_round}, code iteration {code_iter} ----")
            new_code = pending_fail_code
            pending_fail_code = None
            generation_skipped = True
        else:
            code_iter += 1
            log_print(f"\n==== Code Generation Step => Feedback {feedback_round}, Code Iteration {code_iter} ====")
            gen_start = time.time()

            user_msg_gen = (
                "We are in the Code Generation Step.\n"
                f"Project description:\n{project_description}\n\n"
                "Please return full valid Python code (in triple backticks) with no pass/fail statements.\n"
            )
            conversation_history.append({"role": "user", "content": user_msg_gen})
            conversation_history = prune_history(conversation_history, MAX_CODE_HISTORY)

            response_gen = openai_chat_completion(conversation_history)
            gen_end = time.time()

            log_print("---- GPT Response (Code Generation) ----\n")
            log_print(response_gen)
            conversation_history.append({"role": "assistant", "content": response_gen})

            new_code = extract_code_blocks(response_gen)
            if not new_code.strip():
                log_print("*** WARNING: GPT returned no valid code. ***")
                last_run_output = "No code was returned."
                new_code = ""
            else:
                code_path = save_code_permanently(new_code, feedback_round, code_iter)
                log_print(f"==> Code saved to: {code_path}")

        # -------------- RUN CODE --------------
        if new_code.strip():
            current_code = new_code
            run_start = time.time()
            log_print(f"\n---- Running code => feedback {feedback_round}, iteration {code_iter} ----")
            last_run_output = run_code(current_code)
            run_end = time.time()

            log_print("-------- Code Output --------")
            log_print(last_run_output)
            log_print("----------------------------\n")

            out_path = save_output_permanently(last_run_output, feedback_round, code_iter)
            log_print(f"==> Output saved to: {out_path}")
        else:
            current_code = ""
            last_run_output = "No code was run."

        # -------------- CODE JUDGMENT --------------
        log_print(f"==== Code Judgment Step => feedback {feedback_round}, code iteration {code_iter} ====")
        judge_start = time.time()

        user_msg_judge = (
            "We are in the Code Judgment Step.\n"
            f"Project description:\n{project_description}\n\n"
            "Here is the code:\n"
            f"```python\n{current_code}\n```\n\n"
            f"And here is the output:\n{last_run_output}\n\n"
            f"If the output satisfies the project, reply exactly:\n{PASS_STR}\n\n"
            f"Otherwise reply exactly:\n{FAIL_STR}\n\n"
            f"Followed by a failure analysis, and then a final updated version."
        )
        conversation_history.append({"role": "user", "content": user_msg_judge})
        conversation_history = prune_history(conversation_history, MAX_CODE_HISTORY)

        response_judge = openai_chat_completion(conversation_history)
        judge_end = time.time()

        log_print("---- GPT Response (Code Judgment) ----\n")
        log_print(response_judge)
        conversation_history.append({"role": "assistant", "content": response_judge})

        lower_resp = response_judge.lower()
        if PASS_STR.lower() in lower_resp:
            gpt_pass_fail_status = "PASS"
            total_passes += 1
            fails_since_feedback = 0  # reset because it's a pass

            log_print("~~~~ GPT indicates the project is complete. Moving to user review. ~~~~\n")
            feedback_start = time.time()
            user_feedback = request_user_feedback()
            #infinite self improvement mode
            #user_feedback = "looks good! this version has been saved as a functional successful version. now, can you think of any further improvements?"
            feedback_end = time.time()

            if user_feedback:
                user_feedback_status = "Changes requested"
                feedback_round, code_iter = handle_user_feedback(user_feedback, conversation_history, feedback_round)
            else:
                user_feedback_status = "Accepted"
                log_print("No user feedback => project finalized. Goodbye!")
                project_complete = True

        elif FAIL_STR.lower() in lower_resp:
            gpt_pass_fail_status = "FAIL"
            total_fails += 1
            fails_since_feedback += 1

            log_print("~~~~ GPT indicates the project is NOT complete => checking for code block in fail message. ~~~~")
            revised_code = extract_code_blocks(response_judge)
            if revised_code.strip():
                log_print("Found new code in GPT's fail response => next iteration will skip code generation step.")
                pending_fail_code = revised_code
            else:
                log_print("No revised code found => next iteration does normal code generation.")

            # If we have failed a certain number of times, force user feedback.
            if fails_since_feedback >= MAX_FAIL_WO_FDBK:
                log_print(f"Reached {fails_since_feedback} consecutive FAIL(s) without user feedback.")
                feedback_start = time.time()
                user_feedback = request_user_feedback()
                feedback_end = time.time()

                if user_feedback:
                    user_feedback_status = "Changes requested"
                    feedback_round, code_iter = handle_user_feedback(user_feedback, conversation_history, feedback_round)
                else:
                    user_feedback_status = "No feedback provided (continue)."

                # Reset the fail count after forcing feedback attempt.
                fails_since_feedback = 0

        else:
            gpt_pass_fail_status = "INCOMPLETE"
            log_print("~~~~ GPT did not pass or fail => treat as incomplete. Checking for code block. ~~~~")
            revised_code = extract_code_blocks(response_judge)
            if revised_code.strip():
                log_print("Found new code => next iteration will skip code generation step.")
                pending_fail_code = revised_code
            else:
                log_print("No code found => next iteration does normal code generation.")

            # Do not increment fails_since_feedback because GPT didn't explicitly say FAIL.

        # -------------- ITERATION SUMMARY --------------
        iteration_end = time.time()
        iteration_duration = iteration_end - iteration_start

        code_gen_time = 0.0
        code_run_time = 0.0
        code_judge_time = 0.0
        user_fb_time = 0.0

        if not generation_skipped and gen_start and gen_end:
            code_gen_time = gen_end - gen_start
        if run_start and run_end:
            code_run_time = run_end - run_start
        if judge_start and judge_end:
            code_judge_time = judge_end - judge_start
        if feedback_start and feedback_end:
            user_fb_time = feedback_end - feedback_start

        log_print("--------------------------------------------------------------")
        log_print("Iteration Summary:")
        log_print(f" Feedback round:         {feedback_round}")
        log_print(f" Code iteration:         {code_iter}")

        log_print(f" GPT pass/fail status:   {gpt_pass_fail_status}")
        log_print(f" User feedback status:   {user_feedback_status}")

        if generation_skipped:
            log_print(" (Used GPT-provided code from previous fail response; no new code generation call.)")

        log_print(f"   Code Generation time: {code_gen_time:.2f} s")
        log_print(f"   Code Execution time:  {code_run_time:.2f} s")
        log_print(f"   Judgment Step time:   {code_judge_time:.2f} s")
        log_print(f"   User Feedback time:   {user_fb_time:.2f} s")
        log_print(f" Total iteration time:   {iteration_duration:.2f} s")
        log_print("--------------------------------------------------------------\n")

    # Final summary.
    log_print("\n===================== FINAL SUMMARY =====================")
    log_print(f"Total PASS statements: {total_passes}")
    log_print(f"Total FAIL statements: {total_fails}")
    log_print("Exiting script now. Goodbye!")
    log_file.close()


if __name__ == "__main__":
    main()
