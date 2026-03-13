from groq import AsyncGroq, APIError, APIConnectionError


class GroqSTTService:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncGroq(api_key=api_key)
        self._model = model

    async def transcribe(self, audio: bytes, filename: str) -> str:
        try:
            response = await self._client.audio.transcriptions.create(
                file=(filename, audio),
                model=self._model,
                response_format='text',
            )
        except APIConnectionError as exc:
            raise RuntimeError(
                f'Groq connection failed: {exc}'
            ) from exc
        except APIError as exc:
            raise RuntimeError(
                f'Groq transcription failed ({exc.status_code}): {exc.message}'
            ) from exc
        except Exception as exc:
            raise RuntimeError(f'Groq transcription failed: {exc}') from exc

        return str(response).strip()

    async def close(self) -> None:
        await self._client.close()
