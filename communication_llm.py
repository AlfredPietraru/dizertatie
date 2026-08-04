import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv()

import os
from openai import OpenAI

client = OpenAI(
    base_url="https://router.huggingface.co/v1",
    api_key=os.environ["HF_TOKEN"],
)

completion = client.chat.completions.create(
    model="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B:nscale",
    messages=[
        {
            "role": "user",
            "content": "What is the capital of France?"
        }
    ],
)

llm_message = completion.choices[0].message
print(llm_message.content)

