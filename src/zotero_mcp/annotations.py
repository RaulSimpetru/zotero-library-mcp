"""Tools for notes, annotations, and file attachments."""

import json
import os
import re
import tempfile
import fitz
from fuzzysearch import find_near_matches

from ._helpers import (
    _download_pdf,
    _get_zot,
    _use_webdav,
    _attach_file_webdav,
)


HIGHLIGHT_COLORS = ["#ffd400", "#ff6666", "#5fb236", "#2ea8e5", "#a28ae5"]
DEFAULT_HIGHLIGHT_COLOR = "#ffd400"


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
    @mcp.tool()
    async def add_note(item_key: str, note: str) -> str:
        """Add a note to a Zotero item.

        The note is created as a child of the specified item.
        Supports HTML formatting (e.g. <b>bold</b>, <i>italic</i>, <ul><li>lists</li></ul>).

        Args:
            item_key: The parent Zotero item key to attach the note to
            note: The note content (plain text or HTML)
        """
        zot = _get_zot()

        try:
            zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        template = zot.item_template("note")
        template["note"] = note

        try:
            result = zot.create_items([template], parentid=item_key)
        except Exception as e:
            return f"Failed to create note: {e}"

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            note_key = created.get("key", "unknown")
            return f"Added note [{note_key}] to item {item_key}"
        elif result.get("failed"):
            return f"Rejected: {list(result['failed'].values())}"
        else:
            return f"Unexpected: {result}"

    @mcp.tool()
    async def create_annotation(
        item_key: str,
        quoted_text: str,
        comment: str = "",
        color: str = DEFAULT_HIGHLIGHT_COLOR,
        max_l_dist: int | None = None,
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
        """
        zot = _get_zot()
        tmp_path = None

        try:
            tmp_path, att_key = await _download_pdf(zot, item_key)
        except Exception as e:
            return f"Could not download PDF: {e}"

        try:
            # --- Overlap detection against existing highlights ---
            existing_anns = []
            try:
                att_children = zot.children(att_key)
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
                    zot.update_item({
                        "key": ann_key,
                        "version": ann_version,
                        "annotationComment": new_full_comment,
                    })
                    return f"Updated existing highlight [{ann_key}]: appended comment"

                elif normalized_new in existing_text:
                    if color == DEFAULT_HIGHLIGHT_COLOR:
                        existing_color = ann.get("annotationColor", DEFAULT_HIGHLIGHT_COLOR)
                        color = next((c for c in HIGHLIGHT_COLORS if c != existing_color), "#ff6666")
                    break

                elif existing_text in normalized_new:
                    ann_key = ann.get("key")
                    ann_version = ann.get("version")
                    old_comment = ann.get("annotationComment", "")
                    separator = "\n---\n" if old_comment else ""
                    new_full_comment = old_comment + separator + comment
                    zot.update_item({
                        "key": ann_key,
                        "version": ann_version,
                        "annotationComment": new_full_comment,
                    })
                    return f"Updated existing highlight [{ann_key}]: appended comment (broader passage)"

            doc = fitz.open(tmp_path)
            found_rects = []
            found_page = None

            search_norm = _normalize_text(quoted_text).lower()

            # Strategy 1: PyMuPDF's built-in search
            for page in doc:
                rects = page.search_for(quoted_text)
                if rects:
                    first_match = [rects[0]]
                    for r in rects[1:]:
                        if abs(r.y0 - first_match[-1].y0) < 20:
                            first_match.append(r)
                        else:
                            break
                    found_rects = first_match
                    found_page = page
                    break

            # Strategy 2: word-based search with normalization
            if not found_rects:
                for page in doc:
                    words = page.get_text("words")
                    if not words:
                        continue
                    word_texts = [_normalize_text(w[4]) for w in words]
                    full_text = " ".join(word_texts).lower()
                    pos = full_text.find(search_norm)
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
                for page in doc:
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
                doc.close()
                return f"Text not found in PDF: \"{quoted_text[:80]}...\""

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

            result = zot.create_items([annotation], parentid=att_key)

            if result.get("successful"):
                created = list(result["successful"].values())[0]
                ann_key = created.get("key", "unknown")

                preview_path = ""
                try:
                    preview_doc = fitz.open(tmp_path)
                    preview_page = preview_doc[page_index]
                    for r in found_rects:
                        highlight = preview_page.add_highlight_annot(r)
                        highlight.set_colors(stroke=fitz.utils.getColor("yellow"))
                        highlight.update()
                    pix = preview_page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    preview_path = tempfile.mktemp(suffix=".png", prefix="zotero_annot_")
                    pix.save(preview_path)
                    preview_doc.close()
                except Exception:
                    preview_path = ""

                if fuzzy_matched_text:
                    msg = f"Created highlight [{ann_key}] on page {page_label} (fuzzy match): \"{fuzzy_matched_text[:60]}...\""
                else:
                    msg = f"Created highlight [{ann_key}] on page {page_label}: \"{quoted_text[:60]}...\""
                if preview_path:
                    msg += f"\n\nPreview image saved to: {preview_path}"
                    msg += "\nOpen or read this image to visually verify the highlight placement."
                return msg
            elif result.get("failed"):
                return f"Rejected: {list(result['failed'].values())}"
            else:
                return f"Unexpected: {result}"

        except Exception as e:
            return f"Failed to create annotation: {e}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @mcp.tool()
    async def get_annotations(item_key: str) -> str:
        """List all highlights and annotations on a paper's PDF.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
        """
        zot = _get_zot()

        try:
            children = zot.children(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        annotations = []
        for child in children:
            cd = child.get("data", {})
            if cd.get("itemType") == "attachment" and cd.get("contentType") == "application/pdf":
                att_key = cd.get("key", "")
                if not att_key:
                    continue
                try:
                    att_children = zot.children(att_key)
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
                        line += f" — {comment}"
                    annotations.append(line)

        if not annotations:
            return "No annotations found for this item."

        return f"{len(annotations)} annotations:\n" + "\n".join(annotations)

    @mcp.tool()
    async def attach_file(item_key: str, file_path: str) -> str:
        """Attach a local file (e.g. PDF) to an existing Zotero item.

        Args:
            item_key: The Zotero item key to attach the file to
            file_path: Absolute path to the file on your local machine
        """
        if not os.path.isfile(file_path):
            return f"File not found: {file_path}"

        zot = _get_zot()

        try:
            zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        filename = os.path.basename(file_path)

        if _use_webdav():
            result = await _attach_file_webdav(zot, item_key, file_path)
            if result:
                return f"Attached '{filename}' to item {item_key} (via WebDAV)"
            return f"Failed to attach file via WebDAV"
        else:
            try:
                zot.attachment_simple([file_path], item_key)
            except Exception as e:
                return f"Failed to attach file: {e}"
            return f"Attached '{filename}' to item {item_key}"

    @mcp.tool()
    async def download_pdf(item_key: str, save_path: str) -> str:
        """Download the PDF attachment of a Zotero item to a local file.

        Useful when Zotero's fulltext index is incomplete (e.g. for books)
        and you need to read the PDF directly with other tools.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
            save_path: Local file path to save the PDF to (e.g. "/tmp/paper.pdf")
        """
        zot = _get_zot()

        try:
            tmp_path, att_key = await _download_pdf(zot, item_key)
        except Exception as e:
            return f"Could not download PDF: {e}"

        try:
            dest = os.path.expanduser(save_path)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            os.rename(tmp_path, dest)
            size_mb = os.path.getsize(dest) / (1024 * 1024)
            return f"Saved PDF to {dest} ({size_mb:.1f} MB)"
        except OSError:
            import shutil
            try:
                shutil.move(tmp_path, dest)
                size_mb = os.path.getsize(dest) / (1024 * 1024)
                return f"Saved PDF to {dest} ({size_mb:.1f} MB)"
            except Exception as e:
                return f"Failed to save PDF: {e}"
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
