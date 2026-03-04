"""
vectorstore.py — Semantic vector memory using ChromaDB.

Indexes workspace memory files (daily logs, project notes) into a local
vector store for semantic retrieval. Enriches the system prompt with
only the memories relevant to each incoming message.

Uses ChromaDB with ONNX all-MiniLM-L6-v2 embeddings (no PyTorch needed).
Storage: {workspace}/vectordb/
"""

import os
import re
import json
import hashlib
import logging
from datetime import date, datetime

import chromadb

log = logging.getLogger("cleo.vectorstore")

# Singleton ChromaDB client and collection
_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None
_workspace: str | None = None

# Discard search results with cosine distance above this threshold
MAX_RETRIEVAL_DISTANCE = 1.2

# Daily file pattern: YYYY-MM-DD.md
_DAILY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

# Index state file for incremental indexing
_INDEX_STATE_FILE = "index_state.json"


def init_vectorstore(workspace: str) -> None:
    """
    Initialize ChromaDB PersistentClient and 'memories' collection.
    Called once at startup from gateway.main().
    """
    global _client, _collection, _workspace
    _workspace = workspace

    db_path = os.path.join(workspace, "vectordb")
    os.makedirs(db_path, exist_ok=True)

    _client = chromadb.PersistentClient(path=db_path)
    _collection = _client.get_or_create_collection(
        name="memories",
        metadata={"hnsw:space": "cosine"},
    )

    log.info("Vector store initialized at %s (%d existing chunks)",
             db_path, _collection.count())


def _chunk_id(source: str, text: str) -> str:
    """Deterministic chunk ID from source filename + text content."""
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()[:16]


def _is_daily_file(filename: str) -> bool:
    """Check if filename matches YYYY-MM-DD.md pattern."""
    return bool(_DAILY_PATTERN.match(filename))


def _date_from_filename(filename: str) -> str:
    """Extract ISO date from daily filename, or return empty string."""
    if _is_daily_file(filename):
        return filename.replace(".md", "")
    return ""


def _chunk_daily_file(content: str) -> list[tuple[str, int]]:
    """
    Split a daily memory file into chunks by section.

    Each '## ' header + its body = one chunk.
    Lines before the first header are grouped as a preamble chunk.
    Returns list of (chunk_text, line_number) tuples.
    """
    lines = content.split("\n")
    chunks = []
    current_chunk_lines = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        if line.startswith("## ") and current_chunk_lines:
            # Flush previous chunk
            text = "\n".join(current_chunk_lines).strip()
            if text and len(text) > 20:
                chunks.append((text, current_start))
            current_chunk_lines = [line]
            current_start = i
        else:
            current_chunk_lines.append(line)

    # Flush last chunk
    if current_chunk_lines:
        text = "\n".join(current_chunk_lines).strip()
        if text and len(text) > 20:
            chunks.append((text, current_start))

    return chunks


def _chunk_project_file(content: str) -> list[tuple[str, int]]:
    """
    Split a project/note file into paragraph-level chunks.

    Primary split: '---' dividers and '## ' headers.
    Sections over 1000 chars split at paragraph boundaries.
    Fragments under 100 chars merged with next chunk.
    Returns list of (chunk_text, line_number) tuples.
    """
    lines = content.split("\n")
    sections = []
    current_section = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        if (line.strip() == "---" or line.startswith("## ")) and current_section:
            text = "\n".join(current_section).strip()
            if text:
                sections.append((text, current_start))
            current_section = [] if line.strip() == "---" else [line]
            current_start = i
        else:
            current_section.append(line)

    if current_section:
        text = "\n".join(current_section).strip()
        if text:
            sections.append((text, current_start))

    # Split large sections at paragraph boundaries, merge small ones
    chunks = []
    for text, start_line in sections:
        if len(text) > 1000:
            paragraphs = text.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) > 1000 and current:
                    chunks.append((current.strip(), start_line))
                    current = para
                else:
                    current = current + "\n\n" + para if current else para
            if current.strip():
                chunks.append((current.strip(), start_line))
        elif len(text) < 100 and chunks:
            # Merge small fragment with previous chunk
            prev_text, prev_line = chunks[-1]
            chunks[-1] = (prev_text + "\n\n" + text, prev_line)
        elif len(text) >= 20:
            chunks.append((text, start_line))

    return chunks


def _load_index_state(workspace: str) -> dict:
    """Load index_state.json from vectordb dir."""
    path = os.path.join(workspace, "vectordb", _INDEX_STATE_FILE)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"files": {}}


def _save_index_state(workspace: str, state: dict) -> None:
    """Persist index_state.json."""
    path = os.path.join(workspace, "vectordb", _INDEX_STATE_FILE)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def _file_content_hash(filepath: str) -> str:
    """SHA-256 of file contents, hex-encoded."""
    with open(filepath, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def index_memory_files(workspace: str) -> int:
    """
    Scan all .md files in {workspace}/memory/ and index new/modified chunks.
    Skips today's file — it's working memory, loaded whole into every prompt.
    The dream heartbeat processes it into long-term storage overnight.
    Returns count of newly indexed chunks.
    """
    if _collection is None:
        log.warning("Vector store not initialized, skipping indexing")
        return 0

    memory_dir = os.path.join(workspace, "memory")
    summaries_dir = os.path.join(memory_dir, "summaries")
    if not os.path.isdir(memory_dir):
        return 0

    state = _load_index_state(workspace)
    total_new = 0

    # Collect files to index:
    # - Project files (non-date .md) from memory/
    # - Dream summaries (date .md) from memory/summaries/
    # Raw daily logs in memory/ are NOT indexed — they're noisy.
    files_to_index = []

    for filename in sorted(os.listdir(memory_dir)):
        if not filename.endswith(".md"):
            continue
        if _is_daily_file(filename):
            continue  # Skip raw daily logs — summaries are indexed instead
        filepath = os.path.join(memory_dir, filename)
        if os.path.isfile(filepath):
            files_to_index.append((filename, filepath))

    if os.path.isdir(summaries_dir):
        for filename in sorted(os.listdir(summaries_dir)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(summaries_dir, filename)
            if os.path.isfile(filepath):
                # Use summaries/ prefix in source key to distinguish from project files
                files_to_index.append((f"summaries/{filename}", filepath))

    for source_key, filepath in files_to_index:
        filename = os.path.basename(filepath)

        # Check if file has changed
        content_hash = _file_content_hash(filepath)
        file_state = state["files"].get(source_key, {})
        if file_state.get("content_hash") == content_hash:
            continue  # unchanged, skip

        # Read and chunk
        with open(filepath, "r") as f:
            content = f.read()

        if _is_daily_file(filename):
            chunks = _chunk_daily_file(content)
            category = "daily"
            chunk_date = _date_from_filename(filename)
        else:
            chunks = _chunk_project_file(content)
            category = "project"
            chunk_date = _date_from_filename(filename)

        if not chunks:
            continue

        # Delete old chunks for this file, then upsert new ones
        try:
            existing = _collection.get(where={"source": source_key})
            if existing["ids"]:
                _collection.delete(ids=existing["ids"])
        except Exception:
            pass  # no existing chunks, fine

        ids = []
        documents = []
        metadatas = []
        seen_ids = set()

        for text, line_start in chunks:
            cid = _chunk_id(source_key, text)
            if cid in seen_ids:
                continue  # skip duplicate text within same file
            seen_ids.add(cid)
            ids.append(cid)
            documents.append(text)
            metadatas.append({
                "source": source_key,
                "category": category,
                "date": chunk_date,
                "line_start": line_start,
            })

        _collection.add(ids=ids, documents=documents, metadatas=metadatas)

        # Update state
        state["files"][source_key] = {
            "content_hash": content_hash,
            "chunk_count": len(chunks),
            "indexed_at": datetime.now().isoformat(),
        }
        total_new += len(chunks)
        log.info("Indexed %s: %d chunks (%s)", source_key, len(chunks), category)

    _save_index_state(workspace, state)

    if total_new:
        log.info("Indexing complete: %d new chunks, %d total",
                 total_new, _collection.count())

    return total_new


def add_memory_entry(text: str, source_file: str, source_date: str) -> None:
    """
    Add a single new memory entry to the vector store.
    Called from append_daily_memory() when new entries are written.
    """
    if _collection is None:
        return

    chunk_id = _chunk_id(source_file, text)
    category = "daily" if _is_daily_file(source_file) else "project"

    _collection.upsert(
        ids=[chunk_id],
        documents=[text],
        metadatas=[{
            "source": source_file,
            "category": category,
            "date": source_date,
        }],
    )


def query_memories(query_text: str, n_results: int = 10,
                   where: dict | None = None) -> list[dict]:
    """
    Semantic search over indexed memories.
    Returns list of dicts: {text, source, date, distance}
    Filters out results above MAX_RETRIEVAL_DISTANCE.
    """
    if _collection is None or _collection.count() == 0:
        return []

    # Don't request more results than exist
    n = min(n_results, _collection.count())

    kwargs = {
        "query_texts": [query_text],
        "n_results": n,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    results = _collection.query(**kwargs)

    # Flatten and filter
    entries = []
    for i, doc in enumerate(results["documents"][0]):
        distance = results["distances"][0][i]
        if distance > MAX_RETRIEVAL_DISTANCE:
            continue
        metadata = results["metadatas"][0][i]
        entries.append({
            "text": doc,
            "source": metadata.get("source", ""),
            "date": metadata.get("date", ""),
            "distance": distance,
        })

    return entries


def format_retrieved_context(results: list[dict]) -> str:
    """
    Format retrieved chunks into a string for system prompt injection.
    Groups by source file, sorted by date (newest first).
    """
    if not results:
        return ""

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for r in results:
        source = r["source"]
        if source not in by_source:
            by_source[source] = []
        by_source[source].append(r)

    # Sort sources by date (newest first), then by source name
    sorted_sources = sorted(
        by_source.keys(),
        key=lambda s: by_source[s][0].get("date", ""),
        reverse=True,
    )

    parts = []
    for source in sorted_sources:
        entries = by_source[source]
        date_str = entries[0].get("date", "")
        header = f"### From {source}"
        if date_str:
            header += f" ({date_str})"

        texts = [e["text"] for e in entries]
        parts.append(header + "\n" + "\n\n".join(texts))

    return "\n\n".join(parts)
