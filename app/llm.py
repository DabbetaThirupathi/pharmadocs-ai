"""LLM client (OpenAI-compatible Chat Completions API).

Works with Groq (free tier), OpenAI, Together, or a local Ollama server.
Configure via environment variables:

    LLM_BASE_URL   default https://api.groq.com/openai/v1
    LLM_API_KEY    API key (not needed for local Ollama)
    LLM_MODEL      default llama-3.1-8b-instant

If no key/base URL is configured, the app falls back to a deterministic
extractive answerer so it runs fully offline.
"""
import json
import os
import urllib.request

SYSTEM_PROMPT = """You are PharmaDocs AI, a GMP documentation assistant for a sterile
injectable manufacturing site. Rules:
1. Answer ONLY from the CONTEXT passages. Never use outside knowledge.
2. Cite every factual statement with its source tag, e.g. [SOP-QA-003 v2 §5].
3. If the context does not contain the answer, reply exactly:
   "Not found in approved documents. Please contact QA."
4. Be concise and procedural. Use numbered steps when the SOP does.
5. Never invent limits, temperatures, times or approvals."""


class LLMClient:
    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")
        self.enabled = bool(self.api_key) or "localhost" in self.base_url or "ollama" in self.base_url

    def chat(self, question: str, context: str, timeout: int = 30) -> str:
        body = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}"},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
