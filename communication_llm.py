"""Minimal HTTP client for chat completions served by Ollama."""

import os

import requests
from dotenv import load_dotenv


load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST_PATH")
OLLAMA_MODEL = "qwen2.5-coder:7b"


def call_model(system_prompt: str, user_prompt: str) -> str:
    """Send a system and user prompt to the configured Ollama instance."""
    if not OLLAMA_HOST:
        raise RuntimeError("OLLAMA_HOST_PATH is not set in the environment or .env file")

    url = f"http://{OLLAMA_HOST}:11434/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "stream": False,
    }

    response = requests.post(url, json=payload, timeout=300)
    response.raise_for_status()

    data = response.json()
    try:
        return data["message"]["content"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Ollama returned an unexpected response") from error


if __name__ == "__main__":
    print(
        call_model(
            "You are a helpful assistant.",
            "What is the capital of France?",
        )
    )
