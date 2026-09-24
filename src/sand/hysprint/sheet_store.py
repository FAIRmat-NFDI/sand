"""The hysprint experiment sheet stored in the experiment upload.

The xlsx is parsed by NOMAD into entries that the collection references as
derived_entries. Beside it, the extraction record keeps the pristine
extraction result and the xlsx hash, which detects hand-edited sheets.
"""

import hashlib
import json
import time
from datetime import datetime, timezone

import httpx
from nomad.utils import generate_entry_id

from sand.hysprint.sheet import DERIVED_SHEET_MAINFILE, EXTRACTED_JSON_MAINFILE
from sand.services.nomad_api import entry_mainfiles, processed_entry_ids
from sand.services.voice_eln import EntryHandle, VoiceElnService

XLSX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _sheet_hash_matches(current_xlsx: bytes, extraction: bytes | None) -> bool:
    """Whether the stored xlsx still matches the extraction record's hash."""
    if extraction is None:
        return True
    try:
        stored = json.loads(extraction).get('xlsx_sha256')
    except ValueError:
        return True
    return not stored or hashlib.sha256(current_xlsx).hexdigest() == stored


class SheetStore:
    def __init__(self, voice: VoiceElnService) -> None:
        self._voice = voice

    async def read(
        self, client: httpx.AsyncClient, upload_id: str, collection_entry_id: str
    ) -> bytes | None:
        """The stored sheet's bytes; None when nothing was extracted yet."""
        await self._voice.resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        return await self._voice.writer.read_raw_file(
            client, upload_id, DERIVED_SHEET_MAINFILE
        )

    async def store_extracted(  # noqa: PLR0913
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        xlsx: bytes,
        collection_entry_id: str,
        *,
        archive: dict,
        input_entry_ids: list[str],
    ) -> tuple[EntryHandle, bool]:
        """Replace the sheet with an extraction result, reparse, and update
        derived_entries; always regenerating keeps the path self-healing —
        every retry redoes the full work (issue #34).

        Returns (handle, replaced_edits): True when the stored xlsx no
        longer matched the recorded hash, i.e. this regenerate discarded
        hand edits — the caller warns, it never blocks.
        """
        writer = self._voice.writer
        mainfile = await self._voice.resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        await writer.read_archive(client, upload_id, mainfile)

        entry_id = generate_entry_id(upload_id, DERIVED_SHEET_MAINFILE)
        current = await writer.read_raw_file(client, upload_id, DERIVED_SHEET_MAINFILE)
        replaced_edits = False
        old_ids: list[str] = []
        if current is not None:
            stored_extraction = await writer.read_raw_file(
                client, upload_id, EXTRACTED_JSON_MAINFILE
            )
            replaced_edits = not _sheet_hash_matches(current, stored_extraction)
            old_ids = await processed_entry_ids(
                client, entry_id, 1, writer.retry_interval_s
            )

        extraction = {
            'archive': archive,
            'xlsx_sha256': hashlib.sha256(xlsx).hexdigest(),
            'extracted_at': datetime.now(timezone.utc).isoformat(),
            'input_entry_ids': input_entry_ids,
        }
        await self._reparse(
            client,
            upload_id,
            xlsx,
            extraction,
            old_ids=old_ids,
            collection_mainfile=mainfile,
        )
        return EntryHandle(upload_id=upload_id, entry_id=entry_id), replaced_edits

    async def store_uploaded(
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        xlsx: bytes,
        collection_entry_id: str,
    ) -> tuple[EntryHandle, bool]:
        """Store a user-edited sheet. The extraction record is left as the
        pristine machine result, so its hash diverging from the new xlsx
        marks the sheet as hand-edited.

        Returns (handle, changed): whether the sheet content differs from
        what was stored. Unchanged bytes skip the rewrite, but the derived
        state is still verified: a missing or failed previous parse falls
        through to a repair reparse (still reported as unchanged).
        """
        writer = self._voice.writer
        mainfile = await self._voice.resolve_collection_mainfile(
            client, upload_id, collection_entry_id
        )
        await writer.read_archive(client, upload_id, mainfile)

        entry_id = generate_entry_id(upload_id, DERIVED_SHEET_MAINFILE)
        current = await writer.read_raw_file(client, upload_id, DERIVED_SHEET_MAINFILE)
        handle = EntryHandle(upload_id=upload_id, entry_id=entry_id)
        changed = current != xlsx

        old_ids: list[str] = []
        if not changed:
            parsed_ids = await processed_entry_ids(
                client, entry_id, 5, writer.retry_interval_s
            )
            if parsed_ids:
                await self._voice.set_derived_entries(
                    client, upload_id, [entry_id, *parsed_ids], mainfile
                )
                return handle, False
            # same bytes but no parse output: repair with a full reparse
        elif current is not None:
            old_ids = await processed_entry_ids(
                client, entry_id, 1, writer.retry_interval_s
            )

        await self._reparse(
            client,
            upload_id,
            xlsx,
            None,
            old_ids=old_ids,
            collection_mainfile=mainfile,
        )
        return handle, changed

    async def _reparse(  # noqa: PLR0913
        self,
        client: httpx.AsyncClient,
        upload_id: str,
        xlsx: bytes,
        extraction: dict | None,
        *,
        old_ids: list[str],
        collection_mainfile: str,
    ) -> None:
        """Delete the previous parse output, write the sheet (and the
        extraction record, if given), wait out the parse, and point
        derived_entries at the result."""
        writer = self._voice.writer
        if old_ids:
            # delete the raw files behind stale parsed entries (NOMAD drops
            # the entries on reprocess); never sand's own mainfiles
            keep = {
                DERIVED_SHEET_MAINFILE,
                EXTRACTED_JSON_MAINFILE,
                collection_mainfile,
            }
            stale = await entry_mainfiles(
                client, upload_id, old_ids, step='find_stale_entries'
            )
            for target in stale:
                if target not in keep:
                    await writer.delete_raw_file(client, upload_id, target)

        await writer.upload_raw_file(
            client, upload_id, DERIVED_SHEET_MAINFILE, xlsx, XLSX_CONTENT_TYPE
        )
        if extraction is not None:
            await writer.write_json(
                client, upload_id, EXTRACTED_JSON_MAINFILE, extraction
            )

        # The parsed entries exist only once the upload finished processing
        # the sheet; the writer waits before each write, but the last PUT
        # just re-triggered processing, so wait again.
        await writer.wait_until_writable(
            client,
            upload_id,
            time.monotonic() + writer.write_timeout_s,
            DERIVED_SHEET_MAINFILE,
        )
        entry_id = generate_entry_id(upload_id, DERIVED_SHEET_MAINFILE)
        parsed_ids = await processed_entry_ids(
            client, entry_id, 5, writer.retry_interval_s
        )
        await self._voice.set_derived_entries(
            client, upload_id, [entry_id, *parsed_ids], collection_mainfile
        )
