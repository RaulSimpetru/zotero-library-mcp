"""Tools for notes, annotations, and file attachments."""

import json
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path

import fitz
from fuzzysearch import find_near_matches
from mcp.server.fastmcp import Context

from ._helpers import (
    MAX_ATTACHMENT_BYTES,
    _attach_file_local,
    _download_pdf,
    _download_file_from_url,
    _get_zot,
    _use_webdav,
    _attach_file_webdav,
    _validate_limit,
    _zot_call,
)
from .file_resources import register_temp_resource
from .responses import resource_result, tool_error
from .runtime import OpenAIFile, validate_server_path
from .tool_annotations import DESTRUCTIVE, READ_ONLY, WRITE


HIGHLIGHT_COLORS = ["#ffd400", "#ff6666", "#5fb236", "#2ea8e5", "#a28ae5"]
DEFAULT_HIGHLIGHT_COLOR = "#ffd400"


def _validate_hex_color(color: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("color must be a six-digit hex value such as #FFD400")
    return color.lower()


def _color_tuple(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _normalize_text(t: str) -> str:
    """Normalize ligatures, quotes, and whitespace for matching."""
    t = t.replace("\ufb01", "fi").replace("\ufb02", "fl")
    t = t.replace("\ufb00", "ff").replace("\ufb03", "ffi").replace("\ufb04", "ffl")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", t.strip())


def _fuzzy_find_in_page(words, word_texts, search_norm, max_l_dist=None):
    """Find the best fuzzy match for search_norm in a page's word list.

    Uses fuzzysearch (Levenshtein-based) on the joined word text, then maps
    the character-level match back to word bounding boxes. Returns
    (rects, matched_text, dist) or (None, None, None) if no match found.
    """
    if not words or not search_norm:
        return None, None, None

    full_text = " ".join(w.lower() for w in word_texts)
    # Default: allow up to 20% of search length as edit distance
    max_dist = max_l_dist if max_l_dist is not None else max(1, len(search_norm) // 5)

    matches = find_near_matches(search_norm, full_text, max_l_dist=max_dist)
    if not matches:
        return None, None, None

    # Pick the match with the lowest edit distance
    best = min(matches, key=lambda m: m.dist)

    # Map character positions back to word indices
    char_count = 0
    match_rects = []
    matched_words = []
    for i, w in enumerate(words):
        word_start = char_count
        word_end = char_count + len(word_texts[i])
        if word_end > best.start and word_start < best.end:
            match_rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
            matched_words.append(w[4])
        char_count = word_end + 1  # +1 for the space

    if not match_rects:
        return None, None, None

    return match_rects, " ".join(matched_words), best.dist


def register(mcp):
    @mcp.tool(annotations=WRITE)
    async def add_note(item_key: str, note: str) -> str:
        """Add a note to a Zotero item.

        The note is created as a child of the specified item.
        Supports HTML formatting (e.g. <b>bold</b>, <i>italic</i>, <ul><li>lists</li></ul>).

        Args:
            item_key: The parent Zotero item key to attach the note to
            note: The note content (plain text or HTML)
        """
        if not note.strip():
            return tool_error("note must not be empty")
        zot = _get_zot()

        try:
            await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        template = await _zot_call(zot.item_template, "note")
        template["note"] = note

        try:
            result = await _zot_call(zot.create_items, [template], parentid=item_key)
        except Exception as e:
            return tool_error(f"Failed to create note: {e}")

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            note_key = created.get("key", "unknown")
            return f"Added note [{note_key}] to item {item_key}"
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE)
    async def create_annotation(
        item_key: str,
        quoted_text: str,
        comment: str = "",
        color: str = DEFAULT_HIGHLIGHT_COLOR,
        max_l_dist: int | None = None,
        attachment_key: str | None = None,
        page_number: int | None = None,
        occurrence: int = 1,
    ) -> str:
        """Highlight a text passage in a PDF attached to a Zotero item.

        Searches the PDF for the quoted text and creates a visible highlight
        annotation in Zotero's PDF reader. Uses three strategies in order:
        exact match, normalized word match, and fuzzy match (for OCR errors,
        hyphenation differences, or minor transcription mismatches).

        Smart overlap handling:
        - If the same text is already highlighted, appends the new comment
          to the existing annotation instead of creating a duplicate.
        - If the new text is a sub-passage of an existing highlight (or vice
          versa), the new highlight is created in a contrasting color so both
          are visually distinct.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
            quoted_text: The text passage to highlight in the PDF (fuzzy matching
                         handles minor differences from the actual PDF text)
            comment: Optional comment to attach to the highlight
            color: Highlight color as hex (default "#ffd400" yellow)
            max_l_dist: Maximum Levenshtein distance for fuzzy matching. Default
                        is ~20% of the search text length. Increase if the PDF
                        has many OCR errors; decrease for stricter matching.
            attachment_key: Optional PDF attachment key when the item has multiple PDFs
            page_number: Optional one-based page number to search
            occurrence: One-based occurrence to highlight when text repeats
        """
        try:
            quoted_text = quoted_text.strip()
            if len(quoted_text) < 3:
                raise ValueError("quoted_text must contain at least 3 characters")
            if len(quoted_text) > 10000:
                raise ValueError("quoted_text must not exceed 10,000 characters")
            color = _validate_hex_color(color)
            if max_l_dist is not None and not 0 <= max_l_dist <= len(quoted_text):
                raise ValueError("max_l_dist must be between 0 and the quoted text length")
            if page_number is not None and page_number < 1:
                raise ValueError("page_number must be at least 1")
            if occurrence < 1:
                raise ValueError("occurrence must be at least 1")
            zot = _get_zot()
        except Exception as exc:
            return tool_error(f"Invalid annotation request: {exc}")
        tmp_path = None
        doc = None

        try:
            tmp_path, att_key = await _download_pdf(zot, item_key, attachment_key)
        except Exception as e:
            return tool_error(f"Could not download PDF: {e}")

        try:
            # --- Overlap detection against existing highlights ---
            existing_anns = []
            try:
                att_children = await _zot_call(zot.children, att_key)
                for ann in att_children:
                    d = ann.get("data", {})
                    if d.get("itemType") == "annotation" and d.get("annotationType") == "highlight":
                        existing_anns.append(d)
            except Exception:
                pass

            normalized_new = _normalize_text(quoted_text).lower()
            for ann in existing_anns:
                existing_text = _normalize_text(ann.get("annotationText", "")).lower()

                if normalized_new == existing_text:
                    ann_key = ann.get("key")
                    ann_version = ann.get("version")
                    old_comment = ann.get("annotationComment", "")
                    separator = "\n---\n" if old_comment else ""
                    new_full_comment = old_comment + separator + comment
                    if comment:
                        await _zot_call(
                            zot.update_item,
                            {
                                "key": ann_key,
                                "version": ann_version,
                                "annotationComment": new_full_comment,
                            },
                        )
                        return f"Updated existing highlight [{ann_key}]: appended comment"
                    return f"Highlight [{ann_key}] already exists; no duplicate created"

                elif normalized_new in existing_text:
                    if color == DEFAULT_HIGHLIGHT_COLOR:
                        existing_color = ann.get("annotationColor", DEFAULT_HIGHLIGHT_COLOR)
                        color = next((c for c in HIGHLIGHT_COLORS if c != existing_color), "#ff6666")
                    break

                elif existing_text in normalized_new:
                    if color == DEFAULT_HIGHLIGHT_COLOR:
                        existing_color = ann.get("annotationColor", DEFAULT_HIGHLIGHT_COLOR)
                        color = next(
                            (candidate for candidate in HIGHLIGHT_COLORS if candidate != existing_color),
                            "#ff6666",
                        )
                    break

            doc = fitz.open(tmp_path)
            if page_number is not None and page_number > doc.page_count:
                return tool_error(
                    f"page_number {page_number} exceeds the PDF's {doc.page_count} pages"
                )
            found_rects = []
            found_page = None

            search_norm = _normalize_text(quoted_text).lower()

            # Strategy 1: PyMuPDF's built-in search
            pages = [doc[page_number - 1]] if page_number is not None else list(doc)
            remaining_occurrence = occurrence
            for page in pages:
                rects = page.search_for(quoted_text)
                if rects:
                    if remaining_occurrence > len(rects):
                        remaining_occurrence -= len(rects)
                        continue
                    found_rects = [rects[remaining_occurrence - 1]]
                    found_page = page
                    break

            # Strategy 2: word-based search with normalization
            if not found_rects:
                for page in pages:
                    words = page.get_text("words")
                    if not words:
                        continue
                    word_texts = [_normalize_text(w[4]) for w in words]
                    full_text = " ".join(word_texts).lower()
                    pos = -1
                    start = 0
                    for _ in range(occurrence):
                        pos = full_text.find(search_norm, start)
                        if pos < 0:
                            break
                        start = pos + max(1, len(search_norm))
                    if pos < 0:
                        continue
                    char_count = 0
                    match_rects = []
                    for i, w in enumerate(words):
                        word_start = char_count
                        word_end = char_count + len(word_texts[i])
                        if word_end > pos and word_start < pos + len(search_norm):
                            match_rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
                        char_count = word_end + 1
                    if match_rects:
                        found_rects = match_rects
                        found_page = page
                        break

            # Strategy 3: fuzzy matching fallback
            fuzzy_matched_text = None
            if not found_rects:
                best_page = None
                best_rects = None
                best_text = None
                best_dist = None
                for page in pages:
                    words = page.get_text("words")
                    if not words:
                        continue
                    word_texts = [_normalize_text(w[4]) for w in words]
                    rects, matched, dist = _fuzzy_find_in_page(
                        words, word_texts, search_norm, max_l_dist
                    )
                    if rects and (best_dist is None or dist < best_dist):
                        best_dist = dist
                        best_page = page
                        best_rects = rects
                        best_text = matched
                if best_rects:
                    found_rects = best_rects
                    found_page = best_page
                    fuzzy_matched_text = best_text

            if not found_rects or found_page is None:
                return tool_error(f"Text not found in PDF: \"{quoted_text[:80]}...\"")

            page_index = found_page.number
            page_label = str(page_index + 1)
            itm = ~found_page.transformation_matrix
            rects_list = [
                [
                    itm.a * r.x0 + itm.e,
                    itm.d * r.y1 + itm.f,
                    itm.a * r.x1 + itm.e,
                    itm.d * r.y0 + itm.f,
                ]
                for r in found_rects
            ]

            y_pos = int(found_rects[0].y0)
            sort_index = f"{page_index:05d}|000000|{y_pos:05d}"

            doc.close()
            doc = None

            # Use the actual matched text from the PDF when fuzzy-matched
            annotation_text = fuzzy_matched_text if fuzzy_matched_text else quoted_text

            annotation = {
                "itemType": "annotation",
                "parentItem": att_key,
                "annotationType": "highlight",
                "annotationText": annotation_text,
                "annotationComment": comment,
                "annotationColor": color,
                "annotationPageLabel": page_label,
                "annotationSortIndex": sort_index,
                "annotationPosition": json.dumps({
                    "pageIndex": page_index,
                    "rects": rects_list,
                }),
                "tags": [],
            }

            result = await _zot_call(zot.create_items, [annotation], parentid=att_key)

            if result.get("successful"):
                created = list(result["successful"].values())[0]
                ann_key = created.get("key", "unknown")

                preview_path = ""
                try:
                    with fitz.open(tmp_path) as preview_doc:
                        preview_page = preview_doc[page_index]
                        for r in found_rects:
                            highlight = preview_page.add_highlight_annot(r)
                            highlight.set_colors(stroke=_color_tuple(color))
                            highlight.update()
                        pix = preview_page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    with tempfile.NamedTemporaryFile(
                        suffix=".png", prefix="zotero_annot_", delete=False
                    ) as preview_file:
                        preview_path = preview_file.name
                    pix.save(preview_path)
                except Exception:
                    if preview_path:
                        Path(preview_path).unlink(missing_ok=True)
                    preview_path = ""

                if fuzzy_matched_text:
                    msg = f"Created highlight [{ann_key}] on page {page_label} (fuzzy match): \"{fuzzy_matched_text[:60]}...\""
                else:
                    msg = f"Created highlight [{ann_key}] on page {page_label}: \"{quoted_text[:60]}...\""
                if preview_path:
                    try:
                        uri = register_temp_resource(
                            preview_path,
                            name=f"annotation-{ann_key}.png",
                            mime_type="image/png",
                        )
                    except Exception as exc:
                        Path(preview_path).unlink(missing_ok=True)
                        return f"{msg}\nPreview unavailable: {exc}"
                    return resource_result(
                        msg,
                        uri=uri,
                        name=f"annotation-{ann_key}.png",
                        mime_type="image/png",
                        size=os.path.getsize(preview_path),
                        annotation_key=ann_key,
                        page=page_label,
                    )
                return msg
            elif result.get("failed"):
                return tool_error(f"Rejected: {list(result['failed'].values())}")
            else:
                return tool_error(f"Unexpected response from Zotero: {result}")

        except Exception as e:
            return tool_error(f"Failed to create annotation: {e}")
        finally:
            if doc is not None:
                doc.close()
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @mcp.tool(annotations=READ_ONLY)
    async def get_annotations(item_key: str, limit: int = 100) -> str:
        """List all highlights and annotations on a paper's PDF.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
            limit: Maximum number of annotations to return (default 100)
        """
        try:
            limit = _validate_limit(limit, maximum=500)
            zot = _get_zot()
            children = await _zot_call(zot.children, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        annotations = []
        for child in children:
            cd = child.get("data", {})
            if cd.get("itemType") == "attachment" and cd.get("contentType") == "application/pdf":
                att_key = cd.get("key", "")
                if not att_key:
                    continue
                try:
                    att_children = await _zot_call(zot.children, att_key)
                except Exception:
                    continue
                for ann in att_children:
                    d = ann.get("data", {})
                    if d.get("itemType") != "annotation":
                        continue
                    ann_type = d.get("annotationType", "?")
                    text = d.get("annotationText", "")
                    comment = d.get("annotationComment", "")
                    color = d.get("annotationColor", "")
                    page = d.get("annotationPageLabel", "?")
                    key = d.get("key", "?")

                    line = f"[{key}] p.{page} ({ann_type}, {color})"
                    if text:
                        line += f": \"{text[:100]}\""
                    if comment:
                        line += f" — {comment[:500]}"
                    annotations.append(line)
                    if len(annotations) >= limit:
                        break
            if len(annotations) >= limit:
                break

        if not annotations:
            return "No annotations found for this item."

        return f"Annotations (showing {len(annotations)}):\n" + "\n".join(annotations)

    @mcp.tool(annotations=WRITE, meta={"openai/fileParams": ["file"]})
    async def attach_file(
        item_key: str,
        file: OpenAIFile | None = None,
        file_path: str | None = None,
    ) -> str:
        """Attach a ChatGPT file input or an authorized local file to an item.

        Args:
            item_key: The Zotero item key to attach the file to
            file: File object supplied by ChatGPT through openai/fileParams
            file_path: Local server path; stdio only unless HTTP roots are explicitly enabled
        """
        if (file is None) == (file_path is None):
            return tool_error("Provide exactly one of file or file_path")

        tmp_path = None
        temp_dir = None
        try:
            if file is not None:
                extension = mimetypes.guess_extension(file.mime_type or "") or ""
                safe_name = os.path.basename(file.file_name or f"attachment{extension}")
                suffix = Path(safe_name).suffix[:16]
                tmp_path, _ = await _download_file_from_url(
                    file.download_url,
                    suffix=suffix,
                    max_bytes=MAX_ATTACHMENT_BYTES,
                )
                temp_dir = tempfile.mkdtemp(prefix="zotero_upload_")
                source_path = str(Path(temp_dir) / safe_name)
                os.replace(tmp_path, source_path)
                tmp_path = None
                filename = safe_name
            else:
                source = validate_server_path(file_path or "")
                if not source.is_file():
                    return tool_error(f"File not found: {source}")
                if source.stat().st_size > MAX_ATTACHMENT_BYTES:
                    return tool_error("Attachment exceeds the 100 MB size limit")
                source_path = str(source)
                filename = source.name
        except Exception as exc:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            elif tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            return tool_error(f"Could not read attachment input: {exc}")

        zot = _get_zot()

        try:
            await _zot_call(zot.item, item_key)
        except Exception as e:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return tool_error(f"Could not find item {item_key}: {e}")

        try:
            if _use_webdav():
                result = await _attach_file_webdav(zot, item_key, source_path)
                if result:
                    return f"Attached '{filename}' to item {item_key} (via WebDAV)"
                return tool_error("Failed to attach file via WebDAV")
            result = await _attach_file_local(zot, item_key, source_path)
            if not result:
                return tool_error("Failed to attach file to Zotero storage")
            return f"Attached '{filename}' to item {item_key}"
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            elif tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @mcp.tool(annotations=READ_ONLY)
    async def download_pdf(
        item_key: str,
        ctx: Context,
        attachment_key: str | None = None,
    ) -> str:
        """Return a PDF as a remote-safe MCP resource link.

        Useful when Zotero's fulltext index is incomplete (e.g. for books)
        and you need to read the PDF directly with other tools.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
            attachment_key: Optional PDF attachment key when the item has multiple PDFs
        """
        zot = _get_zot()

        async def report_progress(value: float, message: str) -> None:
            await ctx.report_progress(value, total=100, message=message)

        try:
            tmp_path, att_key = await _download_pdf(
                zot,
                item_key,
                attachment_key,
                progress=report_progress,
            )
        except Exception as e:
            return tool_error(f"Could not download PDF: {e}")

        try:
            attachment = await _zot_call(zot.item, att_key)
            attachment_data = attachment.get("data", {})
            filename = os.path.basename(
                attachment_data.get("filename") or attachment_data.get("title") or f"{item_key}.pdf"
            )
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
        except Exception:
            filename = f"{item_key}.pdf"

        try:
            size = os.path.getsize(tmp_path)
            uri = register_temp_resource(
                tmp_path,
                name=filename,
                mime_type="application/pdf",
            )
            await report_progress(100, "PDF resource is ready")
            return resource_result(
                f"PDF ready: {filename} ({size / (1024 * 1024):.1f} MB)",
                uri=uri,
                name=filename,
                mime_type="application/pdf",
                size=size,
                item_key=item_key,
                attachment_key=att_key,
            )
        except Exception as exc:
            Path(tmp_path).unlink(missing_ok=True)
            return tool_error(f"Could not expose PDF resource: {exc}")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def save_pdf(
        item_key: str,
        save_path: str,
        attachment_key: str | None = None,
    ) -> str:
        """Save a Zotero PDF to an authorized local server path."""

        zot = _get_zot()
        try:
            tmp_path, _ = await _download_pdf(zot, item_key, attachment_key)
        except Exception as exc:
            return tool_error(f"Could not download PDF: {exc}")

        staging: Path | None = None
        try:
            destination = validate_server_path(save_path, for_write=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
            shutil.copy2(tmp_path, staging)
            os.replace(staging, destination)
            staging = None
            size_mb = destination.stat().st_size / (1024 * 1024)
            return f"Saved PDF to {destination} ({size_mb:.1f} MB)"
        except Exception as e:
            return tool_error(f"Failed to save PDF: {e}")
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            if staging is not None:
                staging.unlink(missing_ok=True)

    @mcp.tool(annotations=READ_ONLY)
    async def list_notes(item_key: str, limit: int = 50) -> dict[str, object]:
        """List child notes for an item with bounded note content."""

        try:
            limit = _validate_limit(limit, maximum=200)
            zot = _get_zot()
            children = await _zot_call(zot.children, item_key)
        except Exception as exc:
            return tool_error(f"Could not list notes for {item_key}: {exc}")

        notes = []
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            content = data.get("note", "")
            notes.append(
                {
                    "key": data.get("key", ""),
                    "note": content[:2000],
                    "truncated": len(content) > 2000,
                    "date_modified": data.get("dateModified", ""),
                }
            )
            if len(notes) >= limit:
                break
        return {"item_key": item_key, "count": len(notes), "notes": notes}

    @mcp.tool(annotations=WRITE)
    async def update_note(note_key: str, note: str) -> str:
        """Replace the content of an existing Zotero note."""

        if not note.strip():
            return tool_error("note must not be empty")
        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, note_key)
            data = item.get("data", {})
            if data.get("itemType") != "note":
                return tool_error(f"Item {note_key} is not a note")
            data["note"] = note
            await _zot_call(zot.update_item, data)
            return f"Updated note [{note_key}]"
        except Exception as exc:
            return tool_error(f"Failed to update note: {exc}")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_note(note_key: str) -> str:
        """Permanently delete a Zotero note."""

        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, note_key)
            if item.get("data", {}).get("itemType") != "note":
                return tool_error(f"Item {note_key} is not a note")
            await _zot_call(zot.delete_item, item)
            return f"Deleted note [{note_key}]"
        except Exception as exc:
            return tool_error(f"Failed to delete note: {exc}")

    @mcp.tool(annotations=WRITE)
    async def update_annotation(
        annotation_key: str,
        comment: str | None = None,
        color: str | None = None,
    ) -> str:
        """Update the comment and/or highlight color of an annotation."""

        if comment is None and color is None:
            return tool_error("Provide comment and/or color")
        try:
            if color is not None:
                color = _validate_hex_color(color)
            zot = _get_zot()
            item = await _zot_call(zot.item, annotation_key)
            data = item.get("data", {})
            if data.get("itemType") != "annotation":
                return tool_error(f"Item {annotation_key} is not an annotation")
            if comment is not None:
                data["annotationComment"] = comment
            if color is not None:
                data["annotationColor"] = color
            await _zot_call(zot.update_item, data)
            return f"Updated annotation [{annotation_key}]"
        except Exception as exc:
            return tool_error(f"Failed to update annotation: {exc}")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_annotation(annotation_key: str) -> str:
        """Permanently delete a Zotero annotation."""

        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, annotation_key)
            if item.get("data", {}).get("itemType") != "annotation":
                return tool_error(f"Item {annotation_key} is not an annotation")
            await _zot_call(zot.delete_item, item)
            return f"Deleted annotation [{annotation_key}]"
        except Exception as exc:
            return tool_error(f"Failed to delete annotation: {exc}")
