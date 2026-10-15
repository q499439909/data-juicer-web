import os
import json
import requests

def call_copilot_service(copilot_chat_history: list = [], session_id: str = None, user_id: str = None):
    """
    Calls the Copilot service with the chat history and streams the response.

    Args:
        copilot_chat_history (list): The chat history including user messages.

    Yields:
        str: Chunks of the assistant's response or an error message.
    """
    # Get service URL from environment variable, with local default
    service_url = os.getenv("COPILOT_SERVICE_URL", "http://localhost:8080")
    
    try:
        # Convert chat history to the new API format
        # Take the last user message from chat history
        last_user_message = ""
        for message in reversed(copilot_chat_history):
            if message.get("role") == "user":
                last_user_message = message.get("content", "")
                break
        
        # Prepare request body in the new format
        request_body = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": last_user_message.strip(),
                        },
                    ],
                },
            ],
            "session_id": session_id,
            "user_id": user_id,
        }
        
        # Make the HTTP request
        response = requests.post(
            f"{service_url}/process",
            headers={
                'Content-Type': 'application/json',
                'x-session-id': session_id,
            },
            json=request_body,
            stream=True
        )
        
        # Check if request was successful
        if response.status_code != 200:
            error_msg = f"API Error: {response.status_code} - {response.text}"
            yield error_msg
            return
        
        # Process streaming response
        buffer = ''
        for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
            if chunk:
                buffer += chunk
                lines = buffer.split('\n')
                buffer = lines.pop() or ''
                
                for line in lines:
                    if not line.strip() or not line.startswith('data:'):
                        continue
                    
                    json_string = line[5:].strip()  # Remove 'data:' prefix
                    if not json_string:
                        continue
                    
                    try:
                        data = json.loads(json_string)
                        
                        # Process different types of response data
                        if (data.get("object") == "content" and 
                            data.get("type") == "text" and 
                            data.get("delta") == True):
                            # Incremental text content
                            text_chunk = data.get("text", "")
                            if text_chunk:
                                yield text_chunk
                                
                    except json.JSONDecodeError:
                        # Skip malformed JSON
                        continue

    except requests.exceptions.RequestException as e:
        # Handle connection errors
        error_msg = f"Failed to connect to Copilot service at {service_url}. Make sure it's running. Error: {str(e)}"
        yield error_msg
    except Exception as e:
        # Handle other exceptions
        error_msg = f"Unexpected error: {str(e)}"
        yield error_msg

def clear_copilot_chat_history(session_id: str = None, user_id: str = None):
    """
    Clears the Copilot chat history.
    """
    service_url = os.getenv("COPILOT_SERVICE_URL", "http://localhost:8080")
    request_body = {
        "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "",
                        },
                    ],
                },
            ],
        "session_id": session_id,
        "user_id": user_id,
    }
    
    response = requests.post(
            f"{service_url}/clear",
            headers={
                'Content-Type': 'application/json',
            },
            json=request_body,
        )
    
    if response.status_code != 200:
        error_msg = f"API Error: {response.status_code} - {response.text}"
        return error_msg

    if response.json().get("status") == "ok":
        return "ok"
    else:
        return "error"

