import requests
from datetime import datetime

# Define the API endpoint
api_url = "http://127.0.0.1:1234/v1/chat/completions"

# Define the payload for the request
payload = {
    "model": "your-model-name",  # Replace with your model name if needed
    "messages": [
        {"role": "user", "content": "Hello, how are you?"}
    ]
}

# Make the POST request to the API
response = requests.post(api_url, json=payload)

# Check if the request was successful
if response.status_code == 200:
    # Parse the JSON response
    data = response.json()

    # Extract the assistant's message
    assistant_message = data['choices'][0]['message']['content']

    # Get the current timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Print the IRC log format
    print(f"[{timestamp}] <user>: Hello, how are you?")
    print(f"[{timestamp}] <assistant>: {assistant_message}")
else:
    print(f"Failed to get response from API. Status code: {response.status_code}")