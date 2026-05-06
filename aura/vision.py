"""Local vision preprocessing via Ollama.

Provides a VisionClient that calls a local Ollama-hosted vision model
(e.g. llama3.2-vision) through its OpenAI-compatible /v1 API to produce
detailed text descriptions of images.  Those descriptions are later
combined with the user's question and sent to DeepSeek as plain text.
"""

from __future__ import annotations

from openai import OpenAI

DEFAULT_VISION_PROMPT = (
    "Describe everything in this image in extreme detail, including text, layout, and objects."
)


class VisionClient:
    """Call a local Ollama vision model via its OpenAI-compatible endpoint."""

    def __init__(
        self,
        endpoint: str = "http://localhost:11434/v1",
        model: str = "llama3.2-vision",
    ) -> None:
        self._client = OpenAI(base_url=endpoint, api_key="ollama")  # no auth needed
        self._model = model

    def describe(self, image_b64: str) -> str:
        """Return a text description of *image_b64* (raw base64, no prefix)."""
        data_uri = f"data:image/png;base64,{image_b64}"
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            stream=False,
        )
        return response.choices[0].message.content or ""
